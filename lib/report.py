#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor / DeepSeek 额度采集与输出。

由 quota.1m.sh 调用，向 stdout 输出结构化 JSON（供原生菜单栏 App 消费）。

设计要点
--------
1. 取数（fetch_*）与输出（build_payload）分离，阈值与容错规则集中在取数侧。
2. Cursor 令牌只读用户自己写入的 Keychain 条目（cursor-access-token），
   不读取、不解密 Cursor 的内部存储，也不主动刷新 token。
3. 接口全部为逆向/无文档接口，字段做容错，取不到时降级显示而不是崩溃。
4. 全部使用标准库，不依赖任何第三方包。
"""

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(CACHE_DIR, "state.json")

CURSOR_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
# Grok Bot 在协议里叫 Sand，和 Cursor 月度双池不是同一个额度。
CURSOR_GROK_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
)

# Cursor 额度的凭证：由用户自己提供，存在 macOS Keychain 的这条条目里
# （service = cursor-access-token，account = $USER）。
#
# 本程序不解密 Cursor 的 token，额度凭证只读用户自己的 Keychain 条目。
# 菜单上的套餐名例外：只读 state.vscdb 里的 stripeMembershipType（如 pro），
# 不读 accessToken。读不到具体套餐时显示「Cursor」。
CURSOR_TOKEN_SERVICE = "cursor-access-token"
CURSOR_DB = os.path.join(
    HOME, "Library", "Application Support", "Cursor", "User",
    "globalStorage", "state.vscdb",
)
PLAN_NAMES = {
    "free": "Cursor Free",
    "pro": "Cursor Pro",
    "pro_plus": "Cursor Pro+",
    "ultra": "Cursor Ultra",
    "team": "Cursor Team",
}
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

class CredentialError(RuntimeError):
    """凭证缺失或失效：不降级显示旧值，必须让用户看到可执行的提示。"""

def read_cursor_plan_label():
    """套餐显示名。只读 membership 代码，不读 token。读不到就返回 Cursor。"""
    if not os.path.exists(CURSOR_DB):
        return "Cursor"
    uri = "file:" + urllib.parse.quote(CURSOR_DB) + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return "Cursor"
    try:
        row = connection.execute(
            "SELECT value FROM ItemTable WHERE key = ?",
            ("cursorAuth/stripeMembershipType",),
        ).fetchone()
    except sqlite3.Error:
        return "Cursor"
    finally:
        connection.close()
    membership = ((row[0] if row else "") or "").strip().lower()
    return PLAN_NAMES.get(membership, "Cursor")


def read_cursor_token():
    """读取用户自己配置在 Keychain 里的 Cursor token。

    只读这一条 Keychain 条目，不碰 Cursor 的任何内部存储。
    未配置时给出可执行的提示，而不是静默回退到旧值。
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", CURSOR_TOKEN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("读取 Keychain 失败（%s）" % exc)
    token = (result.stdout or "").strip()
    if result.returncode != 0 or not token:
        raise CredentialError("未配置 Cursor token")
    token = normalize_token(token)
    if token.count(".") != 2:
        raise CredentialError("Cursor token 格式不对（应为三段式 JWT）")
    return token


def normalize_token(raw):
    """规范化用户粘贴的内容，兼容三种常见形态：

    - 纯 token：`eyJhbGci...`
    - 带前缀：`Bearer eyJhbGci...`
    - 整行复制：`authorization: Bearer eyJhbGci...`

    这样用户直接从 DevTools 的 Request Headers 复制整行也能用。
    """
    text = (raw or "").strip().strip('"').strip("'").strip()
    marker = text.lower().rfind("bearer ")
    if marker >= 0:
        text = text[marker + len("bearer "):]
    parts = text.split()
    return parts[0] if parts else ""


def store_keychain(service, secret, account=None):
    """把凭证写入 Keychain。密码走 security 的交互提示，不放进 argv。

    `security -w` 带上具体密码时，ps 能看到明文。省略密码并作为最后一项时，
    security 会在终端里询问（并要求再输一次）。这里用 pty 应答，调用方的
    命令行里只有 service 名。
    """
    import pty
    import select

    account = account or os.environ.get("USER") or ""
    secret = (secret or "").strip("\r\n")
    if not account:
        raise RuntimeError("无法确定当前用户")
    if not secret:
        raise RuntimeError("没有内容")

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["/usr/bin/security", "add-generic-password", "-U",
         "-s", service, "-a", account, "-w"],
        stdin=slave, stdout=slave, stderr=slave, close_fds=True,
    )
    os.close(slave)
    # 一次是密码，一次是 retype。多写无害：进程退出后多余输入被丢弃。
    blob = (secret + "\n").encode("utf-8")
    try:
        os.write(master, blob)
        os.write(master, blob)
        deadline = time.time() + 8
        while time.time() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    os.read(master, 4096)  # 丢掉回显，绝不向外打印
                except OSError:
                    break
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
            raise RuntimeError("写入 Keychain 超时")
    finally:
        os.close(master)
    if proc.returncode != 0:
        raise RuntimeError("写入 Keychain 失败（security 退出码 %s）" % proc.returncode)


def token_expiry(token):
    """从 JWT 载荷读 exp（只解码，不验签，也不需要任何密钥）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        expiry = data.get("exp")
        return int(expiry) if expiry else None
    except Exception:
        return None


def fetch_cursor():
    token = read_cursor_token()
    try:
        data = http_json(
            CURSOR_USAGE_URL,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Connect-Protocol-Version": "1",
            },
            payload={},
        )
    except RuntimeError as exc:
        # 401 通常是 token 到期或被撤销（重新登录 Cursor 会让旧 token 失效）。
        # 给可执行的提示，而不是把上一轮的旧百分比当成真值继续显示。
        if "401" in str(exc):
            raise CredentialError(
                "Cursor token 已失效（到期或在 Cursor 里重新登录过）")
        raise
    plan = data.get("planUsage") or {}
    return {
        "ts": time.time(),
        "tokenExp": token_expiry(token),
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
        "credId": cred_id(token),
        "grok": fetch_grok_bot(token),
    }


def parse_time(value):
    """接口时间：秒、毫秒，或 ISO-8601。解析不了返回 0。"""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 1e12 else number
    text = str(value).strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0


def fetch_grok_bot(token):
    """独立的一周额度。没有额度或请求失败时返回 None，不影响 Cursor 主额度。"""
    try:
        data = http_json(
            CURSOR_GROK_URL,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Connect-Protocol-Version": "1",
            },
            payload={},
        )
    except Exception:
        return None
    if data.get("includedLimitZero") is True or data.get("included_limit_zero") is True:
        return None
    if (data.get("hasNonZeroIncludedLimit") is False
            or data.get("has_non_zero_included_limit") is False):
        return None
    raw = data.get("usagePercent")
    if raw is None:
        raw = data.get("usage_percent")
    if raw is None:
        return None
    try:
        pct = float(raw)
    except (TypeError, ValueError):
        return None
    reset = data.get("nextResetTimestampUtc") or data.get("next_reset_timestamp_utc")
    return {"pct": pct, "resetsAt": parse_time(reset)}


# --------------------------------------------------------------------------
# DeepSeek
# --------------------------------------------------------------------------

def cred_id(secret):
    """凭证指纹。只放进本地缓存用来判断「还是不是这一份」，不写 token 本身。"""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def current_cred_id(reader):
    """当前 Keychain 凭证的指纹。缺失或读失败返回 None，缓存不得当成新鲜。"""
    try:
        return cred_id(reader())
    except CredentialError:
        return None


def read_deepseek_key(service):
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception as exc:
        raise CredentialError("无法调用 Keychain（%s）" % exc)
    if result.returncode != 0:
        raise CredentialError("未配置密钥")
    key = (result.stdout or "").strip()
    if not key:
        raise CredentialError("密钥为空")
    return key


def verify_cursor_token(raw):
    """校验一个「待配置」的 token（CLI 脚本与 App 配置弹窗共用同一实现）。

    只发一次请求，返回可序列化结果；不写 Keychain、不碰缓存，
    所以校验失败不会覆盖掉当前仍可用的凭证。
    """
    token = normalize_token(raw)
    if not token:
        return {"ok": False, "error": "没有读到内容，请粘贴 token"}
    if token.count(".") != 2:
        return {"ok": False, "error": "格式不对：token 应是三段式 JWT（含两个点）"}
    try:
        data = http_json(
            CURSOR_USAGE_URL,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Connect-Protocol-Version": "1",
            },
            payload={},
        )
    except RuntimeError as exc:
        if "401" in str(exc):
            return {"ok": False, "error": "token 无效或已过期（HTTP 401）"}
        return {"ok": False, "error": str(exc)}
    plan = data.get("planUsage") or {}
    models = float(plan.get("autoPercentUsed") or 0.0)
    others = float(plan.get("apiPercentUsed") or 0.0)
    expiry = token_expiry(token)
    # summary 由这里拼好，Swift 端只负责显示——避免两处格式化逻辑不一致
    summary = "Cursor Models %.1f%% · Others %.1f%%" % (models, others)
    if expiry:
        summary += "\n有效期至 %s" % time.strftime("%Y-%m-%d", time.localtime(expiry))
    return {
        "ok": True,
        "exp": expiry,
        "modelsPct": models,
        "otherPct": others,
        "summary": summary,
    }


def verify_deepseek_key(raw):
    """校验待配置的 DeepSeek Key（与 Cursor 的校验同构：只验证，不写库）。"""
    key = normalize_token(raw)  # 同一个规范化：去引号/空白，取第一段
    if not key:
        return {"ok": False, "error": "没有读到内容，请粘贴 API Key"}
    if not key.startswith("sk-"):
        return {"ok": False, "error": "格式不对：DeepSeek 的 Key 以 sk- 开头"}
    try:
        data = http_json(DEEPSEEK_BALANCE_URL,
                         headers={"Authorization": "Bearer " + key})
    except RuntimeError as exc:
        if "401" in str(exc):
            return {"ok": False, "error": "密钥无效（HTTP 401）"}
        return {"ok": False, "error": str(exc)}
    infos = data.get("balance_infos") or []
    info = infos[0] if infos else {}
    available = bool(data.get("is_available"))
    symbol = currency_symbol((info.get("currency") or "").upper())
    summary = "余额 %s%s" % (symbol, info.get("total_balance") or "0")
    if not available:
        summary += "（余额不足，已无法发起请求）"
    return {
        "ok": True,
        "total": info.get("total_balance") or "",
        "available": available,
        "summary": summary,
    }


def fetch_deepseek(service):
    key = read_deepseek_key(service)
    try:
        data = http_json(
            DEEPSEEK_BALANCE_URL,
            headers={"Authorization": "Bearer " + key},
        )
    except RuntimeError as exc:
        # 401 = 密钥被删除/重建，或账号异常。这属于凭证问题，不能拿上一次的
        # 余额继续显示（那会让人以为钱还在）。
        if "401" in str(exc):
            raise CredentialError(
                "DeepSeek 密钥已失效（可能被删除或重建）：见 README「配置 DeepSeek 密钥」")
        raise
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
        "credId": cred_id(key),
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
    """返回 (data, error, stale, drop_cache)。fresh 由调用方按该 provider 的 TTL 判断。

    凭证类错误（未配置 / 已失效）不降级，并要求丢掉旧缓存：否则 TTL 内下一轮
    仍会把上一次成功的数字当成本次结果。网络失败才保留缓存并标 stale。
    """
    if not enabled:
        return None, None, False, False
    if fresh and cached:
        return cached, None, False, False
    try:
        return fetcher(), None, False, False
    except CredentialError as exc:
        return None, str(exc), False, True
    except Exception as exc:
        if cached:
            return cached, str(exc), True, False
        return None, str(exc), False, False


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


def short_reason(text):
    """把错误文案压成一行短标签：去掉数据源名字前缀与括号里的解释。

    用于「xxx 不可用 · 未配置 token」这类标题行——保留诊断信息但不占一整行。
    """
    text = (text or "").strip()
    # 去掉数据源名字：避免「Cursor 不可用 · 未配置 Cursor token」这种重复
    text = text.replace("Cursor ", "").replace("DeepSeek ", "")
    for sep in ("（", "(", "："):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip() or "不可用"


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
            "label": "ChatGPT",
            "value": "%d%%\u2009/\u2009%d%%" % (int(round(primary_pct)),
                                                 int(round(secondary_pct))),
            "color": COLOR[level(max(primary_pct, secondary_pct), warn, danger)],
        })
    if cursor:
        models_pct = cursor.get("modelsPct", 0.0)
        other_pct = cursor.get("otherPct", 0.0)
        has_grok = (cursor.get("grok") or {}).get("pct") is not None
        columns.append({
            "label": "Cursor\u2009/\u2009Grok Bot" if has_grok else "Cursor",
            "value": "%d%%\u2009/\u2009%d%%" % (int(round(models_pct)), int(round(other_pct))),
            "color": COLOR[level(max(models_pct, other_pct), warn, danger)],
        })
    elif cfg["cursor"]["enabled"] and cursor_err:
        # 凭证缺失/失效：显示占位符而不是空着——菜单栏上少一整列会让人以为
        # 是程序坏了；但也不给出任何看着像真实额度的数字。
        columns.append({
            "label": "Cursor",
            "value": "--",
            "color": COLOR["danger"],
        })

    # 余额告警有两个来源：低于阈值（可继续用但该充值），以及官方 is_available
    # 明确为 false（余额已不足以发起请求）。后者比阈值更权威，两者都要显示成异常色。
    ds_low = False
    ds_unavailable = bool(deepseek) and deepseek.get("available") is False
    if deepseek and deepseek.get("total"):
        try:
            ds_low = float(deepseek["total"]) <= low_balance
        except (TypeError, ValueError):
            ds_low = False
        columns.append({
            "label": "DeepSeek",
            "value": "%s%s" % (currency_symbol(deepseek.get("currency")), deepseek["total"]),
            "color": COLOR["danger"] if (ds_low or ds_unavailable) else COLOR["ok"],
        })
    elif cfg["deepseek"]["enabled"] and (deepseek_err or (deepseek or {}).get("error")):
        # 同 Cursor：不可用时给占位符，避免整列消失被误判为程序故障
        columns.append({
            "label": "DeepSeek",
            "value": "--",
            "color": COLOR["danger"],
        })

    # ---------- 下拉菜单 ----------
    menu = []

    def item(title, color=None, mono=False, size=None, href=None, refresh=False,
             tabs=None, tabsRight=None, action=None, service=None):
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
        if action:
            # 应用内动作（如配置凭证）：Swift 端把 action 映射到选择器，
            # 与 href（打开链接）/ refresh（重取数据）并列
            entry["action"] = action
        if service:
            # 动作的附加参数（只有 DeepSeek 需要：Keychain 服务名可配置）
            entry["service"] = service
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
            {"text": read_cursor_plan_label(), "color": None},
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
        grok = cursor.get("grok") or None
        if grok and grok.get("pct") is not None:
            # 独立一周额度，放在 Cursor 月度倒计时之后，不并进上面的双池。
            pct = float(grok["pct"])
            row = "\tGrok Bot\t%5.1f%%" % pct
            if show_bars:
                row += "\t" + bar(pct, width)
            menu.append(item(row, color=COLOR[level(pct, warn, danger)],
                             mono=True, tabs=tabs))
            if grok.get("resetsAt"):
                resets_at = float(grok["resetsAt"])
                menu.append(item(
                    "\t重置时间  %s · %s" % (
                        fmt_clock(resets_at),
                        countdown(resets_at * 1000, with_minutes=False)),
                    color=COLOR["muted"], size=12, tabs=[15]))
        # 有效期和更换放在同一行，整行可点。日期用状态色；
        # 「· 更换 token」不给颜色，悬停时才会反白（同页脚「手动刷新」）。
        expiry = cursor.get("tokenExp")
        if expiry:
            left = expiry - time.time()
            text = fmt_clock(expiry)
            if left <= 0:
                status = "Token 已过期  %s" % text
                status_color = COLOR["danger"]
            elif left < 7 * 86400:
                status = "Token %d 天后过期  %s" % (left // 86400, text)
                status_color = COLOR["warn"]
            else:
                status = "Token 有效至  %s" % text
                status_color = COLOR["muted"]
        else:
            status = None
            status_color = None
        segments = []
        if status:
            segments.append({"text": "\t" + status, "color": status_color})
            segments.append({"text": " · 更换 token"})
        else:
            segments.append({"text": "\t更换 token"})
        menu.append({
            "kind": "item",
            "action": "configure-cursor-token",
            "size": 12,
            "tabs": [15],
            "segments": segments,
        })
    elif cursor_err:
        menu.append(separator())
        # 两行说清：标题行带简短原因，第二行是唯一可点的动作。
        # 两行都不缩进（不用 sub），左边缘与相邻区块对齐。
        menu.append({
            "kind": "item",
            "segments": [
                {"text": "Cursor 不可用", "color": COLOR["danger"]},
                {"text": " · " + short_reason(cursor_err), "color": COLOR["muted"]},
            ],
        })
        menu.append(item("配置 Cursor token", action="configure-cursor-token"))

    if cfg["deepseek"]["enabled"]:
        menu.append(separator())
        # 两种"凭证不可用"合并展示：fetch 抛错（deepseek is None）与 fetch 返回
        # 带 error 的字典（Keychain 未配置）。两处文案与入口保持一致。
        ds_error = deepseek_err or (deepseek or {}).get("error")
        if not deepseek and not ds_error:
            ds_error = "读取失败"
        if ds_error:
            menu.append({
                "kind": "item",
                "segments": [
                    {"text": "DeepSeek 不可用", "color": COLOR["danger"]},
                    {"text": " · " + short_reason(ds_error), "color": COLOR["muted"]},
                ],
            })
            menu.append(item("配置 DeepSeek 密钥", action="configure-deepseek-key",
                             service=cfg["deepseek"]["keychainService"]))
        else:
            symbol = currency_symbol(deepseek.get("currency"))
            # 「DeepSeek」用系统默认色（同「前往」行）；「余额」与数值在低预警时一起变红
            menu.append({
                "kind": "item", "mono": True,
                "segments": [
                    {"text": "DeepSeek  ", "color": None},
                    {"text": "余额",
                     "color": COLOR["danger"] if (ds_low or ds_unavailable) else COLOR["title"]},
                    {"text": "  %s%s" % (symbol, deepseek.get("total") or "--"),
                     "color": COLOR["danger"] if (ds_low or ds_unavailable) else COLOR["ok"]},
                ],
            })
            menu.append(item("\t赠金余额 %s%s · 充值余额 %s%s" % (
                symbol, deepseek.get("granted") or "0",
                symbol, deepseek.get("toppedUp") or "0"),
                color=COLOR["muted"], mono=True, size=12, tabs=[15]))
            if ds_unavailable:
                # 官方 is_available=false：余额已不足以发起请求，比"低于阈值"更严重
                menu.append(item("\t余额不足，已无法发起请求",
                                 color=COLOR["danger"], size=12, tabs=[15]))
            menu.append(item("\t去充值",
                             href="https://platform.deepseek.com/top_up",
                             tabs=[15]))
            menu.append(item("\t更换密钥", action="configure-deepseek-key",
                             service=cfg["deepseek"]["keychainService"], tabs=[15]))

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
    # 校验模式：凭证从 stdin 读入，输出一行 JSON。
    # App 的配置弹窗与 scripts/*.sh 都走这里，避免出现两套判断。
    argv = sys.argv[1:]
    for flag, verifier in (("--verify-cursor-token", verify_cursor_token),
                           ("--verify-deepseek-key", verify_deepseek_key)):
        if flag in argv:
            result = verifier(sys.stdin.read())
            sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
            return
    if "--store-keychain" in argv:
        idx = argv.index("--store-keychain")
        try:
            service = argv[idx + 1]
        except IndexError:
            sys.stderr.write("缺少 Keychain 服务名\n")
            sys.exit(2)
        try:
            store_keychain(service, sys.stdin.read())
        except Exception as exc:
            sys.stderr.write("%s\n" % exc)
            sys.exit(1)
        return

    cfg = load_config()
    state = load_state()
    now = time.time()
    ttl = cfg["display"]["cacheSeconds"]

    # 每个 provider 独立判定新鲜度：fetch 档（网络）TTL 长，local 档（ChatGPT
    # 扫本地文件）TTL 短。App 每 60s 调用一次时 fetch 档命中缓存不发请求。
    def is_fresh(key, local=False):
        cached = state.get(key) or {}
        # 带 error 的条目一律不算新鲜：它不是"成功的取数结果"，缓存它会让
        # 「刚配置好凭证」也要等一个 TTL 才生效（实测卡了 5 分钟）。
        if cached.get("error"):
            return False
        # 网络缓存只在「还是同一份凭证」时有效。Keychain 被删、换掉或读失败
        # 时必须立刻重取，否则 TTL 内会一直显示上一份成功数字。
        # 同一份凭证被服务端 401 拒绝，要等这次缓存过期后的下一次请求才能发现。
        if not local:
            if key == "cursor":
                current = current_cred_id(read_cursor_token)
            else:
                service = cfg["deepseek"]["keychainService"]
                current = current_cred_id(lambda: read_deepseek_key(service))
            if not current or cached.get("credId") != current:
                return False
            # 旧缓存没有 Grok Bot 字段时重取一次，否则要等整个 TTL。
            if key == "cursor" and "grok" not in cached:
                return False
        limit = ttl["local"] if local else ttl["fetch"]
        return (now - float(cached.get("ts") or 0)) < limit

    cursor, cursor_err, cursor_stale, drop_cursor = resolve(
        bool(cfg["cursor"]["enabled"]), fetch_cursor, state.get("cursor"),
        is_fresh("cursor"))
    deepseek, deepseek_err, deepseek_stale, drop_deepseek = resolve(
        bool(cfg["deepseek"]["enabled"]),
        lambda: fetch_deepseek(cfg["deepseek"]["keychainService"]),
        state.get("deepseek"), is_fresh("deepseek"))
    codex, codex_err, codex_stale, drop_codex = resolve(
        bool(cfg["codex"]["enabled"]), fetch_codex, state.get("codex"),
        is_fresh("codex", local=True))
    # 缓存命中路径也要过重置推断（否则 TTL 内仍显示过期百分比）
    codex = apply_reset_inference(codex)

    # 凭证失效时丢掉旧缓存。网络失败才保留上一次成功数据，供降级显示。
    # 带 error 的结果（如未配置密钥）不写入，避免挡住刚配好的凭证。
    for key, data, drop in (
            ("cursor", cursor, drop_cursor),
            ("deepseek", deepseek, drop_deepseek),
            ("codex", codex, drop_codex)):
        if drop:
            state.pop(key, None)
        elif data is not None and not data.get("error"):
            state[key] = data
    state.pop("ts", None)
    save_state(state)

    payload = build_payload(cfg, cursor, cursor_err, cursor_stale,
                            deepseek, deepseek_err, deepseek_stale,
                            codex, codex_err, codex_stale)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
