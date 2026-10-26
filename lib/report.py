#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor / DeepSeek 额度采集与输出。

由 quota.1m.sh 调用，向 stdout 输出结构化 JSON（供原生菜单栏 App 消费）。

设计要点
--------
1. 取数（fetch_*）与输出（build_payload）分离，阈值与容错规则集中在取数侧。
2. Cursor 令牌从 Cursor 自身维护的 SQLite 只读读取，不主动刷新，避免
   触发 refresh token 轮转导致 Cursor 登录态失效。
3. 接口全部为逆向/无文档接口，字段做容错，取不到时降级显示而不是崩溃。
4. 全部使用标准库，不依赖任何第三方包。
"""

import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(CACHE_DIR, "state.json")

CURSOR_DB = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User",
    "globalStorage", "state.vscdb",
)
CURSOR_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"

# Codex CLI 会话记录目录：订阅额度数据（rate_limits）写在 rollout jsonl 里，
# 每个 turn 结束刷新一次。纯本地文件解析，无网络请求。
CODEX_SESSIONS_DIR = os.path.join(HOME, ".codex", "sessions")
CODEX_USAGE_URL = "https://chatgpt.com/settings/usage"

DEFAULT_CONFIG = {
    "cursor": {"enabled": True},
    "deepseek": {
        "enabled": True,
        "keychainService": "deepseek-api-key",
        "lowBalanceThreshold": 20.0,
    },
    "codex": {"enabled": True},
    "display": {
        # 缓存 TTL 按获取方式分两档：fetch=走网络的 provider（Cursor/DeepSeek），
        # local=纯本地读取的 provider（ChatGPT，扫 Codex 会话文件，成本≈0）。
        # App 每 60s 调用一次：local 档必然过期 → 重读；fetch 档命中 → 不发请求。
        # 新增 provider 只需决定归哪档，App 侧无需感知。
        "cacheSeconds": {"fetch": 300, "local": 20},
        "showBars": True,
        "barWidth": 8,
    },
    "thresholds": {"warn": 50.0, "danger": 80.0},
}

COLOR = {
    "ok": "#185FA5",
    "warn": "#BA7517",
    "danger": "#A32D2D",
    "muted": "#888780",
    "title": "#444441",
}

PLAN_NAMES = {
    "free": "Cursor Free",
    "pro": "Cursor Pro",
    "pro_plus": "Cursor Pro+",
    "ultra": "Cursor Ultra",
    "team": "Cursor Team",
}


# --------------------------------------------------------------------------
# 配置与缓存
# --------------------------------------------------------------------------

def deep_merge(base, override):
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            user_cfg = json.load(handle)
    except Exception:
        user_cfg = {}
    return deep_merge(DEFAULT_CONFIG, user_cfg)


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return {}


def save_state(state):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------
# 网络
# --------------------------------------------------------------------------

def http_json(url, method="GET", headers=None, payload=None, timeout=12):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RuntimeError("HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise RuntimeError("网络不可达（%s）" % exc.reason)
    return json.loads(raw)


# --------------------------------------------------------------------------
# Cursor
# --------------------------------------------------------------------------

def read_cursor_auth():
    """只读打开 Cursor 数据库，取认证信息。绝不写入。"""
    if not os.path.exists(CURSOR_DB):
        raise RuntimeError("未找到 Cursor 数据库，请确认已安装并登录 Cursor")
    uri = "file:" + urllib.parse.quote(CURSOR_DB) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise RuntimeError("读取 Cursor 数据库失败（%s）" % exc)
    try:
        rows = dict(connection.execute(
            "SELECT key, value FROM ItemTable WHERE key IN "
            "('cursorAuth/accessToken',"
            " 'cursorAuth/refreshToken',"
            " 'cursorAuth/stripeMembershipType')"
        ).fetchall())
    finally:
        connection.close()
    if not rows.get("cursorAuth/accessToken"):
        raise RuntimeError("Cursor 未登录，请打开 Cursor 完成登录")
    return rows


def fetch_cursor():
    auth = read_cursor_auth()
    data = http_json(
        CURSOR_USAGE_URL,
        method="POST",
        headers={
            "Authorization": "Bearer " + auth["cursorAuth/accessToken"],
            "Connect-Protocol-Version": "1",
        },
        payload={},
    )
    plan = data.get("planUsage") or {}
    membership = (auth.get("cursorAuth/stripeMembershipType") or "").strip().lower()
    return {
        "ts": time.time(),
        "planName": PLAN_NAMES.get(membership, membership or "Cursor"),
        "modelsPct": float(plan.get("autoPercentUsed") or 0.0),
        "otherPct": float(plan.get("apiPercentUsed") or 0.0),
        # 官方未下发时保持 None：下游据此不显示合计，而不是兜底成 0.0%
        "totalPct": (float(plan["totalPercentUsed"])
                     if plan.get("totalPercentUsed") is not None else None),
        "cycleStart": int(data.get("billingCycleStart") or 0),
        "cycleEnd": int(data.get("billingCycleEnd") or 0),
        "message": data.get("displayMessage") or "",
        "includedSpend": int(plan.get("includedSpend") or 0),
        "planLimit": int(plan.get("limit") or 0),
        "bonusSpend": int(plan.get("bonusSpend") or 0),
    }


# --------------------------------------------------------------------------
# DeepSeek
# --------------------------------------------------------------------------

def read_deepseek_key(service):
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as exc:
        return None, "无法调用 Keychain（%s）" % exc
    if result.returncode != 0:
        return None, "未在 Keychain 中配置密钥"
    key = (result.stdout or "").strip()
    if not key:
        return None, "Keychain 中的密钥为空"
    return key, None


def fetch_deepseek(service):
    key, error = read_deepseek_key(service)
    if error:
        return {"ts": time.time(), "error": error}
    data = http_json(
        DEEPSEEK_BALANCE_URL,
        headers={"Authorization": "Bearer " + key},
    )
    infos = data.get("balance_infos") or []
    info = infos[0] if infos else {}
    return {
        "ts": time.time(),
        "error": None,
        "available": bool(data.get("is_available")),
        "currency": (info.get("currency") or "").upper(),
        "total": info.get("total_balance") or "",
        "granted": info.get("granted_balance") or "",
        "toppedUp": info.get("topped_up_balance") or "",
    }


# --------------------------------------------------------------------------
# ChatGPT Codex（本地会话记录解析）
# --------------------------------------------------------------------------

def apply_reset_inference(codex):
    """重置推断：数据只在 Codex turn 结束时落盘，不实时。窗口重置时刻
    已过时用量必然已清零（重置即归 0），据此修正过期百分比——
    否则窗口刚重置后会一直显示旧值，直到下次 Codex 活动写入新数据。
    fetch 与缓存两条路径都要过这道修正。"""
    if not codex:
        return codex
    now = time.time()
    out = dict(codex)
    for pct_key, reset_key in (
            ("primaryPct", "primaryResetsAt"),
            ("secondaryPct", "secondaryResetsAt")):
        try:
            resets_at = int(out.get(reset_key) or 0)
        except (TypeError, ValueError):
            resets_at = 0
        out[reset_key] = resets_at
        if resets_at and now > resets_at:
            out[pct_key] = 0.0
    return out


def fetch_codex():
    """扫描 ~/.codex/sessions 下最近修改的 rollout jsonl，
    取时间戳最新的一条有效 rate_limits（订阅登录直连时才非 null）。"""
    if not os.path.isdir(CODEX_SESSIONS_DIR):
        raise RuntimeError("未找到 ~/.codex/sessions，请确认已安装 Codex CLI")
    # 按修改时间倒序，只看最近的会话文件；数据只在 turn 结束时刷新，
    # 更老的文件不可能有更新的数据，找到即停。
    files = []
    for dirpath, _, filenames in os.walk(CODEX_SESSIONS_DIR):
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, name)
            try:
                files.append((os.path.getmtime(path), path))
            except OSError:
                continue
    if not files:
        raise RuntimeError("~/.codex/sessions 下没有会话记录")
    files.sort(reverse=True)

    best_ts = None
    best_rl = None
    for _, path in files[:30]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    payload = obj.get("payload") or {}
                    limits = payload.get("rate_limits") or {}
                    primary = limits.get("primary")
                    if not primary:
                        continue
                    ts = obj.get("timestamp") or ""
                    if best_ts is None or ts > best_ts:
                        best_ts = ts
                        best_rl = limits
        except OSError:
            continue
        if best_rl is not None:
            break
    if best_rl is None:
        raise RuntimeError("Codex 会话中没有额度数据（需 ChatGPT 订阅登录且直连）")

    primary = best_rl["primary"]
    secondary = best_rl.get("secondary") or {}
    return apply_reset_inference({
        "ts": time.time(),
        "primaryPct": float(primary.get("used_percent") or 0.0),
        "primaryResetsAt": int(primary.get("resets_at") or 0),
        "secondaryPct": float(secondary.get("used_percent") or 0.0),
        "secondaryResetsAt": int(secondary.get("resets_at") or 0),
    })


# --------------------------------------------------------------------------
# 格式化
# --------------------------------------------------------------------------

def level(pct, warn, danger):
    if pct >= danger:
        return "danger"
    if pct >= warn:
        return "warn"
    return "ok"


def bar(pct, width):
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(round(pct / 100.0 * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def currency_symbol(code):
    return "\u00a5" if (code or "").upper() == "CNY" else "$"


def fmt_clock(epoch):
    if not epoch:
        return "--"
    return datetime.fromtimestamp(epoch).strftime("%m/%d %H:%M")


def countdown(ms_end, with_minutes=True):
    """距重置的剩余时长（纯时长，不含"重置"字样，上下文由调用方补）。
    with_minutes=False 时超 1 天只到小时（ChatGPT 合并行用，控制菜单宽度）；
    默认带分钟（Cursor 独立行，宽度无压力）。"""
    if not ms_end:
        return ""
    delta = ms_end / 1000.0 - time.time()
    if delta <= 0:
        return "已重置"
    days = int(delta // 86400)
    hours = int((delta % 86400) // 3600)
    minutes = int((delta % 3600) // 60)
    if days > 0:
        if not with_minutes:
            return "%d 天 %d 小时" % (days, hours)
        return "%d 天 %d 小时 %d 分钟" % (days, hours, minutes)
    if hours > 0:
        return "%d 小时 %d 分钟" % (hours, minutes)
    return "%d 分钟" % max(1, minutes)


def text_width(s, size):
    """非等宽字号下的宽度估算：CJK = 1em（苹方方块字准确值），
    数字/字母 ≈ 0.6em（SF Pro），空格/标点 ≈ 0.3em。
    用于计算右对齐停靠位，防叠字。"""
    width = 0.0
    for ch in s:
        if ord(ch) > 0x2E80:
            width += size
        elif ch.isalnum():
            width += size * 0.6
        else:
            width += size * 0.3
    return width


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def resolve(enabled, fetcher, cached, fresh):
    """返回 (data, error, stale)。fresh 由调用方按该 provider 的 TTL 判断。"""
    if not enabled:
        return None, None, False
    if fresh and cached:
        return cached, None, False
    try:
        return fetcher(), None, False
    except Exception as exc:
        if cached:
            return cached, str(exc), True
        return None, str(exc), False


# --------------------------------------------------------------------------
# JSON 输出（供原生菜单栏 App 消费）
# --------------------------------------------------------------------------


def latest_ts(*sources):
    """最新一份成功数据的写入时间（秒）。没有成功数据时返回 None。

    DeepSeek 把「未配置密钥」也以成功结果落盘（含 ts），但那不是一次
    数据更新；只计入真正拿到余额的记录。
    """
    stamps = [
        float(item.get("ts") or 0)
        for item in sources
        if item and item.get("error") is None
    ]
    return max(stamps) if stamps else None


def build_payload(cfg, cursor, cursor_err, cursor_stale,
                  deepseek, deepseek_err, deepseek_stale,
                  codex, codex_err, codex_stale):
    """结构化输出：菜单栏多列、下拉菜单结构，均由原生 App 按本格式渲染。"""
    warn = float(cfg["thresholds"]["warn"])
    danger = float(cfg["thresholds"]["danger"])
    low_balance = float(cfg["deepseek"]["lowBalanceThreshold"])
    width = int(cfg["display"]["barWidth"])
    show_bars = bool(cfg["display"]["showBars"])

    # ---------- 菜单栏多行标题 ----------
    columns = []
    if codex:
        primary_pct = codex.get("primaryPct", 0.0)
        secondary_pct = codex.get("secondaryPct", 0.0)
        columns.append({
            "label": "ChatGPT Plus",
            "value": "%d%%/%d%%" % (int(round(primary_pct)),
                                    int(round(secondary_pct))),
            "color": COLOR[level(max(primary_pct, secondary_pct), warn, danger)],
        })
    if cursor:
        models_pct = cursor.get("modelsPct", 0.0)
        other_pct = cursor.get("otherPct", 0.0)
        columns.append({
            "label": "Cursor",
            "value": "%d%%/%d%%" % (int(round(models_pct)), int(round(other_pct))),
            "color": COLOR[level(max(models_pct, other_pct), warn, danger)],
        })

    ds_low = False
    if deepseek and deepseek.get("total"):
        try:
            ds_low = float(deepseek["total"]) <= low_balance
        except (TypeError, ValueError):
            ds_low = False
        columns.append({
            "label": "DeepSeek",
            "value": "%s%s" % (currency_symbol(deepseek.get("currency")), deepseek["total"]),
            "color": COLOR["danger"] if ds_low else COLOR["ok"],
        })

    # ---------- 下拉菜单 ----------
    menu = []

    def item(title, color=None, mono=False, size=None, href=None, refresh=False,
             tabs=None, tabsRight=None):
        entry = {"kind": "item", "title": title}
        if color:
            entry["color"] = color
        if mono:
            entry["mono"] = True
        if size:
            entry["size"] = size
        if href:
            entry["href"] = href
        if refresh:
            entry["refresh"] = True
        if tabs:
            # tab 停靠位（pt），title 里用 \t 分列；Swift 端据此设 NSTextTab。
            # 多列对齐必须用 tab：CJK 在等宽字体下回退苹方，
            # 宽度不等于 2×ASCII，空格补齐必然错位。
            entry["tabs"] = tabs
        if tabsRight:
            # 右对齐 tab 停靠位（pt）：\t 后的文字以此位置为右缘，
            # 用于「重置时间 … 倒计时」一行的左右分列
            entry["tabsRight"] = tabsRight
        return entry

    def sub(title, color=None, mono=False, size=None, href=None, tabs=None):
        entry = item(title, color, mono, size, href, tabs=tabs)
        entry["kind"] = "sub"
        return entry

    def separator():
        return {"kind": "sep"}

    if codex:
        menu.append(separator())
        primary_pct = codex.get("primaryPct", 0.0)
        secondary_pct = codex.get("secondaryPct", 0.0)
        # 列停靠位与 Cursor 区块共用一套三列布局（名称 / 百分比 / 条形图）
        tabs = [15, 125, 178]
        menu.append({
            "kind": "item", "mono": True, "tabs": tabs,
            "segments": [{"text": "ChatGPT Plus", "color": None}],
        })
        for label, pct, resets_at in (
                ("5小时额度", primary_pct, codex.get("primaryResetsAt", 0)),
                ("周额度", secondary_pct, codex.get("secondaryResetsAt", 0))):
            row = "\t%s\t%5.1f%%" % (label, pct)
            if show_bars:
                row += "\t" + bar(pct, width)
            menu.append(item(row, color=COLOR[level(pct, warn, danger)],
                             mono=True, tabs=tabs))
            if resets_at:
                # resets_at 为秒级时间戳；countdown 接口按毫秒入参。
                # 重置时间与剩余时长合并一行，用「 · 」分隔（同 DeepSeek 赠金行），
                # 前导 \t 吸附 15pt 停靠位缩进
                menu.append(item(
                    "\t重置时间  %s · %s" % (fmt_clock(resets_at),
                                             countdown(resets_at * 1000,
                                                       with_minutes=False)),
                    color=COLOR["muted"], size=12, tabs=[15]))
    elif codex_err:
        menu.append(separator())
        menu.append(item("ChatGPT Plus 读取失败", color=COLOR["danger"]))
        menu.append(sub(codex_err, color=COLOR["muted"], size=12))

    if cursor:
        menu.append(separator())
        models_pct = cursor.get("modelsPct", 0.0)
        other_pct = cursor.get("otherPct", 0.0)

        # 计划名与合计合并为一行：分段着色（计划名标题色，合计跟随阈值色），
        # 列位置与下面两个模型池行一致（\t + 停靠位）。
        # 官方未下发 totalPercentUsed 时 totalPct 为 None —— 只显示计划名，
        # 合计数字与条形图都不展示（不兜底成 0.0%）。
        total_pct = cursor.get("totalPct")
        # 列停靠位：标题行与明细行共用，保证百分比/条形图列在屏幕上对齐。
        # 缩进统一用「前导 \t 到 15pt 停靠位」实现（Models 明细行与
        # 计费周期/重置倒计时/赠金余额行都是）——不依赖 indentationLevel
        # 的实际缩进值（实测并非文档的 15pt），构造上保证左边缘严格齐平。
        # 其余停靠位按 13pt 等宽字体实测：标签 12 字符 103.3pt，
        # 15+103.3=118.3 < 125；百分比 48.2pt，125+48.2=173.2 < 178。
        tabs = [15, 125, 178]
        header = {"kind": "item", "mono": True, "tabs": tabs, "segments": [
            {"text": cursor.get("planName") or "Cursor", "color": None},
        ]}
        if total_pct is not None:
            total_color = COLOR[level(total_pct, warn, danger)]
            header["segments"].append(
                {"text": "\t%5.1f%%" % total_pct, "color": total_color})
            if show_bars:
                header["segments"].append(
                    {"text": "\t" + bar(total_pct, width), "color": total_color})
        menu.append(header)

        # 三列（名称 / 百分比 / 条形图）与官网命名对齐（Others）
        for label, pct in (("Cursor Models", models_pct), ("Others Models", other_pct)):
            row = "\t%s\t%5.1f%%" % (label, pct)
            if show_bars:
                row += "\t" + bar(pct, width)
            menu.append(item(row, color=COLOR[level(pct, warn, danger)],
                             mono=True, tabs=tabs))
        if cursor.get("cycleEnd"):
            # 中英混排行不用等宽字体：汉字回退苹方后比数字饱满，
            # 等宽下同字号也显得大小不一；非等宽下中文数字同为苹方更协调
            menu.append(item(
                "\t计费周期  %s → %s" % (fmt_clock(cursor.get("cycleStart", 0) / 1000.0),
                                        fmt_clock(cursor["cycleEnd"] / 1000.0)),
                color=COLOR["muted"], size=12, tabs=[15]))
            menu.append(item("\t重置倒计时  %s" % countdown(cursor["cycleEnd"]),
                             color=COLOR["muted"], size=12, tabs=[15]))
    elif cursor_err:
        menu.append(separator())
        menu.append(item("Cursor 读取失败", color=COLOR["danger"]))
        menu.append(sub(cursor_err, color=COLOR["muted"], size=12))
        menu.append(sub("请在 Cursor 中激活一次后重试", color=COLOR["muted"], size=12))

    if cfg["deepseek"]["enabled"]:
        menu.append(separator())
        if not deepseek:
            menu.append(item("DeepSeek 读取失败", color=COLOR["danger"]))
            if deepseek_err:
                menu.append(sub(deepseek_err, color=COLOR["muted"], size=12))
        elif deepseek.get("error"):
            menu.append(item("DeepSeek · %s" % deepseek["error"], color=COLOR["muted"]))
            menu.append(sub("见 README 配置密钥", color=COLOR["muted"], size=12))
        else:
            symbol = currency_symbol(deepseek.get("currency"))
            # 「DeepSeek」用系统默认色（同「前往」行）；「余额」与数值在低预警时一起变红
            menu.append({
                "kind": "item", "mono": True,
                "segments": [
                    {"text": "DeepSeek  ", "color": None},
                    {"text": "余额",
                     "color": COLOR["danger"] if ds_low else COLOR["title"]},
                    {"text": "  %s%s" % (symbol, deepseek.get("total") or "--"),
                     "color": COLOR["danger"] if ds_low else COLOR["ok"]},
                ],
            })
            menu.append(item("\t赠金余额 %s%s · 充值余额 %s%s" % (
                symbol, deepseek.get("granted") or "0",
                symbol, deepseek.get("toppedUp") or "0"),
                color=COLOR["muted"], mono=True, size=12, tabs=[15]))
            menu.append(item("\t去充值",
                             href="https://platform.deepseek.com/top_up",
                             tabs=[15]))

    menu.append(separator())
    if cursor_stale or deepseek_stale or codex_stale:
        menu.append(item("数据为缓存值（刷新失败）", color=COLOR["warn"], size=12))
    menu.append(item("前往 ChatGPT 用量面板", href=CODEX_USAGE_URL))
    menu.append(item("前往 Cursor 用量面板",
                     href="https://cursor.com/dashboard?tab=spending"))
    menu.append(item("前往 DeepSeek 用量面板", href="https://platform.deepseek.com/usage"))
    # 刷新与上次更新时间合并为页脚一行：整行可点击触发刷新。
    # 字号与「前往 xxx」行一致（不指定 size，用系统默认）。
    # 时间用 muted 灰；「· 手动刷新」不给颜色——可点项的无色段由系统渲染，
    # hover 自动反白（见 main.swift buildMenu 的 segments 分支）
    data_ts = latest_ts(cursor, deepseek, codex)
    if data_ts is None:
        updated_text = "暂无数据"
    else:
        updated_text = "上次更新 %s" % datetime.fromtimestamp(data_ts).strftime("%H:%M:%S")
    menu.append({
        "kind": "item", "refresh": True,
        "segments": [
            {"text": updated_text, "color": COLOR["muted"]},
            {"text": " · 手动刷新"},
        ],
    })

    return {
        "updatedAtEpoch": time.time(),
        "stale": bool(cursor_stale or deepseek_stale or codex_stale),
        "columns": columns,
        "menu": menu,
    }


def main():
    cfg = load_config()
    state = load_state()
    now = time.time()
    ttl = cfg["display"]["cacheSeconds"]

    # 每个 provider 独立判定新鲜度：fetch 档（网络）TTL 长，local 档（ChatGPT
    # 扫本地文件）TTL 短。App 每 60s 调用一次时 fetch 档命中缓存不发请求。
    def is_fresh(key, local=False):
        cached = state.get(key) or {}
        limit = ttl["local"] if local else ttl["fetch"]
        return (now - float(cached.get("ts") or 0)) < limit

    cursor, cursor_err, cursor_stale = resolve(
        bool(cfg["cursor"]["enabled"]), fetch_cursor, state.get("cursor"),
        is_fresh("cursor"))
    deepseek, deepseek_err, deepseek_stale = resolve(
        bool(cfg["deepseek"]["enabled"]),
        lambda: fetch_deepseek(cfg["deepseek"]["keychainService"]),
        state.get("deepseek"), is_fresh("deepseek"))
    codex, codex_err, codex_stale = resolve(
        bool(cfg["codex"]["enabled"]), fetch_codex, state.get("codex"),
        is_fresh("codex", local=True))
    # 缓存命中路径也要过重置推断（否则 TTL 内仍显示过期百分比）
    codex = apply_reset_inference(codex)

    # 独立落盘各 provider 的最新成功数据（含各自 ts，供下次判新鲜度）
    for key, data in (("cursor", cursor), ("deepseek", deepseek), ("codex", codex)):
        if data is not None:
            state[key] = data
    state.pop("ts", None)
    save_state(state)

    payload = build_payload(cfg, cursor, cursor_err, cursor_stale,
                            deepseek, deepseek_err, deepseek_stale,
                            codex, codex_err, codex_stale)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
