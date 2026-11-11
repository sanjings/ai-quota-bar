#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cursor / DeepSeek / Claude 额度采集与输出。

由 quota.1m.sh 调用，向 stdout 输出结构化 JSON（供原生菜单栏 App 消费）。

设计要点
--------
1. 取数（fetch_*）与输出（build_payload）分离，阈值与容错规则集中在取数侧。
2. Cursor 令牌只读用户自己写入的 Keychain 条目（cursor-access-token），
   不读取、不解密 Cursor 的内部存储，也不主动刷新 token。
3. 接口字段做容错，取不到时降级显示而不是崩溃。
4. 全部使用标准库，不依赖任何第三方包。
5. Claude 只读用户自己粘贴进 Keychain 的 claude.ai sessionKey。
   不读 Claude Code 的凭据文件或钥匙串，也不刷新登录态。
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

# Claude 订阅用量：claude.ai 用量页背后的接口，不是已公开的 Admin API。
# Admin 的 usage/cost 只给历史消耗，且没有剩余余额；这里要的是 5 小时 / 周已用百分比。
# 凭证是用户从浏览器复制的 sessionKey，不是 sk-ant-api / sk-ant-admin。
CLAUDE_ORGS_URL = "https://claude.ai/api/organizations"
CLAUDE_USAGE_URL = "https://claude.ai/api/organizations/%s/usage"
CLAUDE_USAGE_PAGE = "https://claude.ai/settings/usage"
CLAUDE_KEYCHAIN_SERVICE = "claude-session-key"
CLAUDE_SESSION_PREFIX = "sk-ant-sid"

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
    "claude": {
        "enabled": True,
        "keychainService": CLAUDE_KEYCHAIN_SERVICE,
    },
    "display": {
        # 缓存 TTL 按获取方式分两档：fetch=走网络的 provider（Cursor/DeepSeek/Claude），
        # local=纯本地读取的 provider（ChatGPT，扫 Codex 会话文件，成本≈0）。
        # App 每 60s 调用一次：local 档必然过期 → 重读；fetch 档命中 → 不发请求。
        # 新增 provider 只需决定归哪档，App 侧无需感知。
        "cacheSeconds": {"fetch": 300, "local": 20},
        "showBars": True,
        "barWidth": 8,
    },
    "thresholds": {"warn": 50.0, "danger": 80.0},
    # 缺省打开。设置面板可以关掉。判断仍用上面的 thresholds 和低余额阈值。
    "notifications": {"enabled": True},
}

# 预警用正橙，和危险红分开；深色菜单由 Swift adapt() 再提亮。
COLOR = {
    "ok": "#0969DA",
    "warn": "#FFA500",
    "danger": "#FF1111",
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


def read_keychain_secret(service):
    """读取用户自己写入的 Keychain 通用密码。未配置和空值都按凭证错误处理。"""
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


def read_deepseek_key(service):
    return read_keychain_secret(service)


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
# Claude（claude.ai 订阅用量）
# --------------------------------------------------------------------------

_CLAUDE_SESSION_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def normalize_claude_session(raw):
    """取出 sessionKey 本身。兼容带引号、Cookie 头、sessionKey= 前缀和后面的其它 cookie。"""
    text = (raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    lower = text.lower()
    if lower.startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
        lower = text.lower()
    marker = lower.rfind("sessionkey=")
    if marker >= 0:
        text = text[marker + len("sessionkey="):]
    if ";" in text:
        text = text.split(";", 1)[0]
    text = text.strip().strip('"').strip("'").strip()
    parts = text.split()
    return parts[0] if parts else ""


def claude_session_format_error(secret):
    if not secret:
        return "没有读到内容，请粘贴 sessionKey"
    if not secret.startswith(CLAUDE_SESSION_PREFIX):
        return "sessionKey 应以 sk-ant-sid 开头"
    if any(ch not in _CLAUDE_SESSION_CHARS for ch in secret):
        return "sessionKey 应以 sk-ant-sid 开头"
    return None


def read_claude_key(service):
    secret = normalize_claude_session(read_keychain_secret(service))
    error = claude_session_format_error(secret)
    if error:
        if error.startswith("没有读到"):
            raise CredentialError("密钥为空")
        raise CredentialError(error)
    return secret


def claude_org_uuid(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 80:
        return None
    for ch in text:
        if ch not in "0123456789abcdefABCDEF-":
            return None
    return text


def iter_claude_orgs(payload):
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = None
        for key in ("organizations", "data"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        if items is None:
            return []
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def select_claude_org(orgs):
    """挑订阅聊天组织。API 控制台组织没有 chat，打 /usage 会被拒绝。"""
    chat = []
    for org in orgs or []:
        if not isinstance(org, dict):
            continue
        uuid = claude_org_uuid(org.get("uuid") or org.get("id"))
        caps = org.get("capabilities") or []
        if not uuid or not isinstance(caps, list) or "chat" not in caps:
            continue
        chat.append((org, uuid))
    if not chat:
        return None
    for org, uuid in chat:
        if "subscription" in str(org.get("billing_type") or ""):
            return uuid
    return chat[0][1]


def _window_from_pct(pct, resets_at):
    try:
        number = float(pct)
    except (TypeError, ValueError):
        return None
    if number != number or number < 0 or number in (float("inf"), float("-inf")):
        return None
    return {"pct": number, "resetsAt": parse_time(resets_at)}


def claude_bucket(value):
    """已用百分比。utilization 和 limits[].percent 是同一单位（0–100），缺失不当成 0。"""
    if not isinstance(value, dict):
        return None
    raw = value.get("utilization")
    if raw is None:
        raw = value.get("percent")
    resets = value.get("resets_at")
    if resets is None:
        resets = value.get("resetsAt")
    return _window_from_pct(raw, resets)


def _clean_label(text, fallback):
    raw = " ".join(str(text or "").replace("\t", " ").split())
    if not raw:
        return fallback
    if len(raw) > 32:
        return raw[:31] + "…"
    return raw


def _model_alert_key(label):
    digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:12]
    slug = "".join(ch.lower() if ch.isascii() and ch.isalnum() else "-" for ch in label)
    slug = "-".join(part for part in slug.split("-") if part)[:24]
    if slug:
        return "claude.model.%s.%s" % (slug, digest)
    return "claude.model.%s" % digest


def parse_claude_usage(data):
    """把用量页 JSON 收成 session / week / models / extra。没有百分比则抛错，不写成 0%。"""
    if not isinstance(data, dict):
        raise RuntimeError("当前账号没有订阅额度数据")
    session = claude_bucket(data.get("five_hour"))
    week = claude_bucket(data.get("seven_day"))
    scoped = []
    limits = data.get("limits") if isinstance(data.get("limits"), list) else []
    for entry in limits:
        if not isinstance(entry, dict):
            continue
        window = claude_bucket(entry)
        if window is None:
            continue
        kind = entry.get("kind") or entry.get("type")
        if kind in ("session", "five_hour") and session is None:
            session = window
        elif kind in ("weekly_all", "seven_day", "weekly") and week is None:
            week = window
        elif kind == "weekly_scoped":
            scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name") or model.get("name") or entry.get("display_name")
            label = _clean_label(name, "模型")
            scoped.append({
                "label": label,
                "pct": window["pct"],
                "resetsAt": window["resetsAt"],
                "key": _model_alert_key(label),
            })
    if scoped:
        models = scoped
    else:
        models = []
        for key, label in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
            window = claude_bucket(data.get(key))
            if window is None:
                continue
            models.append({
                "label": label,
                "pct": window["pct"],
                "resetsAt": window["resetsAt"],
                "key": _model_alert_key(label),
            })
    extra = None
    raw_extra = data.get("extra_usage")
    if isinstance(raw_extra, dict) and raw_extra.get("is_enabled") is True:
        extra = claude_bucket(raw_extra)
    if session is None and week is None and not models and extra is None:
        raise RuntimeError("当前账号没有订阅额度数据")
    return {"session": session, "week": week, "models": models, "extra": extra}


def claude_interpret(status, data):
    """把 HTTP 结果收成 JSON，或抛出凭证错误 / 普通错误。权限错误先于 401/403。"""
    err_type = None
    err_message = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            err_type = err.get("type")
            err_message = err.get("message") or ""
        elif data.get("type") == "error":
            err_type = "error"
    if err_type == "permission_error":
        raise CredentialError("没有订阅用量权限（%s）" % (err_message or "该组织不能查询用量"))
    if err_type in ("authentication_error", "unauthorized") or status in (401, 403):
        raise CredentialError("Claude 密钥已失效（登录已过期或已退出）")
    if err_type:
        raise RuntimeError(err_message or "Claude 接口返回错误")
    if status >= 400:
        raise RuntimeError("HTTP %s" % status)
    if data is None:
        raise CredentialError("Claude 密钥已失效（接口没有返回额度数据）")
    return data


def claude_request(url, session_key):
    headers = {
        "Accept": "application/json",
        "Cookie": "sessionKey=" + session_key,
        "User-Agent": "AI-Quota-Bar",
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise RuntimeError("网络不可达（%s）" % exc.reason)
    try:
        data = json.loads(raw) if raw.strip() else None
    except ValueError:
        data = None
    return claude_interpret(status, data)


def fetch_claude_usage(session_key, request=None):
    request = request or claude_request
    orgs = request(CLAUDE_ORGS_URL, session_key)
    uuid = select_claude_org(iter_claude_orgs(orgs))
    if not uuid:
        raise CredentialError(
            "未找到订阅组织（需要 claude.ai 的 chat 组织，不是 API 控制台）")
    usage = request(CLAUDE_USAGE_URL % uuid, session_key)
    return parse_claude_usage(usage)


def claude_summary(parsed):
    parts = []
    if parsed.get("session"):
        parts.append("5小时 %.1f%%" % parsed["session"]["pct"])
    if parsed.get("week"):
        parts.append("周额度 %.1f%%" % parsed["week"]["pct"])
    for model in parsed.get("models") or []:
        parts.append("%s %.1f%%" % (model["label"], model["pct"]))
    if parsed.get("extra"):
        parts.append("额外用量 %.1f%%" % parsed["extra"]["pct"])
    return " · ".join(parts) if parts else "已连通，但没有百分比"


def verify_claude_key(raw):
    """校验待配置的 sessionKey。只验证，不写 Keychain。"""
    secret = normalize_claude_session(raw)
    error = claude_session_format_error(secret)
    if error:
        return {"ok": False, "error": error}
    try:
        parsed = fetch_claude_usage(secret)
    except CredentialError as exc:
        return {"ok": False, "error": str(exc)}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "summary": claude_summary(parsed)}


def fetch_claude(service):
    key = read_claude_key(service)
    parsed = fetch_claude_usage(key)
    parsed["ts"] = time.time()
    parsed["error"] = None
    parsed["credId"] = cred_id(key)
    return parsed


def claude_enabled(cfg):
    section = cfg.get("claude") if isinstance(cfg, dict) else None
    if not isinstance(section, dict) or "enabled" not in section:
        return True
    return bool(section.get("enabled"))


def claude_service_name(cfg):
    section = cfg.get("claude") if isinstance(cfg, dict) else None
    name = section.get("keychainService") if isinstance(section, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return CLAUDE_KEYCHAIN_SERVICE


def _window_pct(window):
    if not isinstance(window, dict) or window.get("pct") is None:
        return None
    try:
        pct = float(window.get("pct"))
    except (TypeError, ValueError):
        return None
    if pct != pct or pct < 0 or pct in (float("inf"), float("-inf")):
        return None
    return pct


def claude_primary_pcts(claude):
    """菜单栏上展示的百分比。优先 5 小时和周额度；都没有时才退到模型或额外用量。"""
    if not isinstance(claude, dict) or claude.get("error"):
        return []
    pcts = []
    for key in ("session", "week"):
        pct = _window_pct(claude.get(key))
        if pct is not None:
            pcts.append(pct)
    if pcts:
        return pcts
    for model in claude.get("models") or []:
        pct = _window_pct(model)
        if pct is not None:
            pcts.append(pct)
        if len(pcts) == 2:
            break
    if pcts:
        return pcts
    extra = _window_pct(claude.get("extra"))
    return [] if extra is None else [extra]


def claude_column_value(pcts):
    if len(pcts) >= 2:
        return "%d%%\u200a/\u200a%d%%" % (int(round(pcts[0])), int(round(pcts[1])))
    if pcts:
        return "%d%%" % int(round(pcts[0]))
    return "--"


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


def deepseek_balance_state(deepseek, low_balance):
    """DeepSeek 要不要标红。通知和菜单栏共用，避免两套比较。

    返回 (low, unavailable)。余额等于阈值算低。
    total 为空或解析不了时不算低；available 不是 false 时不算不可用。
    """
    unavailable = bool(deepseek) and deepseek.get("available") is False
    low = False
    if deepseek and deepseek.get("total"):
        try:
            low = float(deepseek["total"]) <= float(low_balance)
        except (TypeError, ValueError):
            low = False
    return low, unavailable


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
    text = text.replace("Cursor ", "").replace("DeepSeek ", "").replace("Claude ", "")
    for sep in ("（", "(", "："):
        if sep in text:
            text = text.split(sep)[0]
    return text.strip() or "不可用"


def build_payload(cfg, cursor, cursor_err, cursor_stale,
                  deepseek, deepseek_err, deepseek_stale,
                  codex, codex_err, codex_stale,
                  claude=None, claude_err=None, claude_stale=False):
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
            "value": "%d%%\u200a/\u200a%d%%" % (int(round(primary_pct)),
                                                 int(round(secondary_pct))),
            "color": COLOR[level(max(primary_pct, secondary_pct), warn, danger)],
        })
    claude_on = claude_enabled(cfg)
    claude_pcts = claude_primary_pcts(claude) if claude_on else []
    if claude_on and claude_pcts:
        columns.append({
            "label": "Claude",
            "value": claude_column_value(claude_pcts),
            "color": COLOR[level(max(claude_pcts), warn, danger)],
        })
    elif claude_on and (claude_err or (claude or {}).get("error")):
        columns.append({
            "label": "Claude",
            "value": "--",
            "color": COLOR["danger"],
        })
    if cursor:
        models_pct = cursor.get("modelsPct", 0.0)
        other_pct = cursor.get("otherPct", 0.0)
        grok_pct = (cursor.get("grok") or {}).get("pct")
        has_grok = grok_pct is not None
        value = "%d%%\u200a/\u200a%d%%" % (int(round(models_pct)), int(round(other_pct)))
        level_pct = max(models_pct, other_pct)
        if has_grok:
            value += "\u200a/\u200a%d%%" % int(round(float(grok_pct)))
            level_pct = max(level_pct, float(grok_pct))
        columns.append({
            "label": "Cursor / Grok Bot" if has_grok else "Cursor",
            "value": value,
            "color": COLOR[level(level_pct, warn, danger)],
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
    ds_low, ds_unavailable = deepseek_balance_state(deepseek, low_balance)
    if deepseek and deepseek.get("total"):
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
            # 动作的附加参数（DeepSeek / Claude 的 Keychain 服务名可配置）
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

    if claude_on:
        claude_service = claude_service_name(cfg)
        claude_rows = []
        if isinstance(claude, dict) and not claude.get("error"):
            if claude.get("session"):
                claude_rows.append(("5小时额度", claude["session"]))
            if claude.get("week"):
                claude_rows.append(("周额度", claude["week"]))
            for model in claude.get("models") or []:
                if isinstance(model, dict) and model.get("pct") is not None:
                    claude_rows.append((model.get("label") or "模型", model))
            if claude.get("extra"):
                claude_rows.append(("额外用量", claude["extra"]))
        if claude_rows:
            menu.append(separator())
            tabs = [15, 125, 178]
            menu.append({
                "kind": "item", "mono": True, "tabs": tabs,
                "segments": [{"text": "Claude", "color": None}],
            })
            for label, window in claude_rows:
                pct = _window_pct(window)
                if pct is None:
                    continue
                row = "\t%s\t%5.1f%%" % (_clean_label(label, "额度"), pct)
                if show_bars:
                    row += "\t" + bar(pct, width)
                menu.append(item(row, color=COLOR[level(pct, warn, danger)],
                                 mono=True, tabs=tabs))
                resets_at = window.get("resetsAt") or 0
                if resets_at:
                    menu.append(item(
                        "\t重置时间  %s · %s" % (fmt_clock(resets_at),
                                                 countdown(resets_at * 1000,
                                                           with_minutes=False)),
                        color=COLOR["muted"], size=12, tabs=[15]))
            menu.append(item("\t更换密钥", action="configure-claude-key",
                             service=claude_service, tabs=[15]))
        else:
            shown_err = claude_err or (claude or {}).get("error")
            if shown_err or isinstance(claude, dict):
                menu.append(separator())
                menu.append({
                    "kind": "item",
                    "segments": [
                        {"text": "Claude 不可用", "color": COLOR["danger"]},
                        {"text": " · " + short_reason(shown_err or "读取失败"),
                         "color": COLOR["muted"]},
                    ],
                })
                menu.append(item("配置 Claude 密钥", action="configure-claude-key",
                                 service=claude_service))

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
    if cursor_stale or deepseek_stale or codex_stale or claude_stale:
        menu.append(item("数据为缓存值（刷新失败）", color=COLOR["warn"], size=12))
    menu.append(item("前往 ChatGPT 用量面板", href=CODEX_USAGE_URL))
    menu.append(item("前往 Claude 用量页", href=CLAUDE_USAGE_PAGE))
    menu.append(item("前往 Cursor 用量面板",
                     href="https://cursor.com/dashboard?tab=spending"))
    menu.append(item("前往 DeepSeek 用量面板", href="https://platform.deepseek.com/usage"))
    # 刷新与上次更新时间合并为页脚一行：整行可点击触发刷新。
    # 字号与「前往 xxx」行一致（不指定 size，用系统默认）。
    # 时间用 muted 灰；「· 手动刷新」不给颜色——可点项的无色段由系统渲染，
    # hover 自动反白（见 main.swift buildMenu 的 segments 分支）
    data_ts = latest_ts(cursor, deepseek, codex, claude if claude_on else None)
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

    # 通知是附加数据。模块缺失、收集抛错，都退回「没有待发通知」，菜单照常输出。
    notify_on = True
    alert_list = []
    try:
        import notify
        notify_on = notify.notifications_enabled(cfg)
        alert_list = notify.collect_alerts(
            cfg, cursor, deepseek, codex, level,
            claude if claude_on else None)
    except Exception:
        alert_list = []

    return {
        "updatedAtEpoch": time.time(),
        "stale": bool(cursor_stale or deepseek_stale or codex_stale or claude_stale),
        "columns": columns,
        "menu": menu,
        "notificationsEnabled": notify_on,
        "alerts": alert_list,
    }


def _menu_blob(payload):
    parts = []
    for entry in payload.get("menu") or []:
        if entry.get("title"):
            parts.append(entry["title"])
        for segment in entry.get("segments") or []:
            parts.append(segment.get("text") or "")
        if entry.get("action"):
            parts.append(entry["action"])
        if entry.get("href"):
            parts.append(entry["href"])
    return "\n".join(parts)


def self_test():
    """不联网、不读 Keychain。覆盖未配置、关闭数据源，以及单源失败不影响其它列。"""
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    cfg = deep_merge(DEFAULT_CONFIG, {})
    expect(claude_enabled(cfg) is True, "缺省应显示 Claude")
    expect(claude_service_name(cfg) == CLAUDE_KEYCHAIN_SERVICE, "缺省钥匙串服务名不对")
    expect(claude_enabled({"claude": {"enabled": False}}) is False, "enabled false 应关闭")
    expect(claude_enabled({}) is True, "没有 claude 段时按打开处理")
    expect(claude_service_name({"claude": {"keychainService": "  custom  "}}) == "custom",
           "钥匙串服务名应去掉空白")

    raw = "Cookie: sessionKey=sk-ant-sid01-abc_DEF-123; other=nope"
    expect(normalize_claude_session(raw) == "sk-ant-sid01-abc_DEF-123",
           "应从 Cookie 头里取出 sessionKey")
    expect(normalize_claude_session("  'sk-ant-sid01-plain'  ") == "sk-ant-sid01-plain",
           "应去掉引号")
    bad = verify_claude_key("sessionKey=sk-ant-api01-SECRETVALUE")
    expect(bad.get("ok") is False and "sk-ant-sid" in (bad.get("error") or ""),
           "API Key 应在发请求前被拒绝：%r" % (bad,))
    expect("SECRETVALUE" not in json.dumps(bad, ensure_ascii=False), "格式错误不应回显密钥")
    empty = verify_claude_key("   ")
    expect(empty.get("ok") is False and "没有读到" in (empty.get("error") or ""),
           "空白应提示没有内容")

    expect(claude_bucket({"utilization": None, "resets_at": "2026-01-01T00:00:00Z"}) is None,
           "缺失的百分比不能当成 0")
    expect(claude_bucket({"percent": 0, "resets_at": None})["pct"] == 0, "真实的 0% 要保留")
    expect(select_claude_org([
        {"uuid": "11111111-1111-1111-1111-111111111111", "capabilities": ["api"]},
        {"uuid": "../not-a-uuid", "capabilities": ["chat"]},
        {"uuid": "22222222-2222-2222-2222-222222222222", "capabilities": ["chat"],
         "billing_type": "stripe_subscription"},
        {"uuid": "33333333-3333-3333-3333-333333333333", "capabilities": ["chat"]},
    ]) == "22222222-2222-2222-2222-222222222222", "应选带 chat 的订阅组织，跳过 API 组织和非法 id")
    expect(select_claude_org([{"uuid": "11111111-1111-1111-1111-111111111111",
                               "capabilities": ["api"]}]) is None, "只有 API 组织时不硬选")
    wrapped = iter_claude_orgs({"data": [{"uuid": "abc"}]})
    expect(len(wrapped) == 1 and wrapped[0]["uuid"] == "abc", "组织列表可以包在 data 里")

    try:
        parse_claude_usage({"five_hour": None, "seven_day": None,
                            "extra_usage": {"is_enabled": False, "utilization": 80}})
        expect(False, "没有窗口时应失败，而不是显示 0%")
    except RuntimeError as exc:
        expect("没有订阅额度数据" in str(exc), "空用量的文案不对：%s" % exc)

    parsed = parse_claude_usage({
        "five_hour": None,
        "seven_day": {"utilization": 1, "resets_at": None},
        "seven_day_opus": {"utilization": 9, "resets_at": "2026-11-12T00:00:00Z"},
        "limits": [
            {"kind": "session", "percent": 15, "resets_at": "2026-11-10T01:00:00Z"},
            {"kind": "weekly_scoped", "percent": 7, "resets_at": "2026-11-16T00:00:00Z",
             "scope": {"model": {"display_name": "Fable"}}},
        ],
        "extra_usage": {"is_enabled": False, "utilization": 99},
    })
    expect(parsed["session"]["pct"] == 15, "five_hour 缺失时用 limits.session")
    expect(parsed["week"]["pct"] == 1, "已有的 seven_day 不被 limits 覆盖")
    expect([item["label"] for item in parsed["models"]] == ["Fable"],
           "有 weekly_scoped 时不再重复旧的 Opus 字段")
    expect(parsed["extra"] is None, "未开启的额外用量不展示")
    expect(parsed["session"]["resetsAt"] > 0, "session 重置时间应能解析")

    legacy = parse_claude_usage({
        "five_hour": {"utilization": 4, "resets_at": None},
        "seven_day": {"utilization": 5, "resets_at": None},
        "seven_day_sonnet": {"utilization": 0, "resets_at": None},
        "seven_day_opus": None,
    })
    expect(legacy["models"][0]["label"] == "Sonnet" and legacy["models"][0]["pct"] == 0,
           "没有 limits 时保留旧的模型周额度，0% 也要")

    seen = []

    def fake_request(url, key):
        seen.append((url, key))
        if url == CLAUDE_ORGS_URL:
            return [
                {"uuid": "11111111-1111-1111-1111-111111111111", "capabilities": ["api"]},
                {"uuid": "22222222-2222-2222-2222-222222222222", "capabilities": ["chat"],
                 "billing_type": "stripe_subscription"},
            ]
        return {
            "five_hour": {"utilization": 12, "resets_at": "2026-11-10T00:00:00Z"},
            "seven_day": {"utilization": 34, "resets_at": "2026-11-16T00:00:00Z"},
        }

    usage = fetch_claude_usage("sk-ant-sid01-secret", request=fake_request)
    expect(len(seen) == 2 and seen[1][0].endswith(
        "/22222222-2222-2222-2222-222222222222/usage"), "应请求选中的组织用量")
    expect(all("secret" not in url for url, _key in seen), "请求地址不能带上密钥")
    expect("secret" not in json.dumps(usage), "解析结果不能带上密钥")
    expect(usage["session"]["pct"] == 12 and usage["week"]["pct"] == 34, "用量百分比不对")

    def interpret_error(status, data):
        try:
            claude_interpret(status, data)
        except Exception as exc:
            return type(exc), str(exc)
        return None, ""

    kind, text = interpret_error(401, None)
    expect(kind is CredentialError and "密钥已失效" in text, "401 应是密钥失效")
    kind, text = interpret_error(403, {
        "type": "error",
        "error": {"type": "permission_error", "message": "Invalid authorization"},
    })
    expect(kind is CredentialError and text.startswith("没有订阅用量权限"),
           "权限错误不能写成密钥失效：%s" % text)
    kind, text = interpret_error(429, {"error": "slow down"})
    expect(kind is RuntimeError and text == "HTTP 429", "429 应留给缓存降级：%s" % text)
    expect(claude_interpret(200, [{"uuid": "ok"}]) == [{"uuid": "ok"}], "200 的 JSON 应原样返回")

    def raise_auth():
        raise CredentialError("未配置密钥")

    def raise_down():
        raise RuntimeError("HTTP 429")

    missing, missing_err, missing_stale, missing_drop = resolve(
        True, raise_auth, {"ts": 1, "session": {"pct": 8}}, False)
    expect(missing is None and missing_drop and not missing_stale and "未配置" in missing_err,
           "未配置不能继续显示旧百分比")
    cached, down_err, down_stale, down_drop = resolve(
        True, raise_down,
        {"ts": 1, "session": {"pct": 8}, "week": {"pct": 9}, "models": [], "extra": None},
        False)
    expect(down_stale and not down_drop and cached["session"]["pct"] == 8,
           "网络失败应降级到这份缓存")
    sibling, sibling_err, _, _ = resolve(
        True, lambda: {"total": "8.5", "currency": "CNY", "available": True}, None, False)
    expect(sibling_err is None and sibling["total"] == "8.5", "前一个源失败后，下一个源仍要取数")
    state = {"cursor": {"keep": True}, "claude": {"session": {"pct": 1}}}
    if missing_drop:
        state.pop("claude", None)
    expect("cursor" in state and "claude" not in state, "丢掉 Claude 缓存时不能动其它源")

    codex = {"primaryPct": 4, "secondaryPct": 5, "primaryResetsAt": 0, "secondaryResetsAt": 0}
    deepseek = {"total": "30", "currency": "CNY", "available": True, "error": None, "ts": 3}
    unconfigured = build_payload(
        cfg, None, None, False, deepseek, None, False, codex, None, False,
        None, "未配置密钥", False)
    labels = [column["label"] for column in unconfigured["columns"]]
    claude_col = [column for column in unconfigured["columns"] if column["label"] == "Claude"]
    expect(labels[:2] == ["ChatGPT", "Claude"] and "DeepSeek" in labels,
           "未配置时 Claude 列仍占位，其它列还在：%r" % (labels,))
    expect(len(claude_col) == 1 and claude_col[0]["value"] == "--"
           and claude_col[0]["color"] == COLOR["danger"], "未配置不能显示成百分比")
    blob = _menu_blob(unconfigured)
    expect("Claude 不可用" in blob and "未配置密钥" in blob and "configure-claude-key" in blob,
           "未配置提示或入口不对：%s" % blob)
    expect("ChatGPT Plus" in blob and "余额" in blob, "未配置 Claude 时其它区块应还在")
    expect(CLAUDE_USAGE_PAGE in blob, "下拉应有 Claude 用量页")

    off = deep_merge(cfg, {"claude": {"enabled": False}})
    hidden = build_payload(
        off, None, None, False, deepseek, None, False, codex, None, False,
        {"session": {"pct": 90, "resetsAt": 1}, "week": {"pct": 91, "resetsAt": 2},
         "models": [], "extra": None},
        "未配置密钥", False)
    hidden_labels = [column["label"] for column in hidden["columns"]]
    expect("Claude" not in hidden_labels, "关掉之后菜单栏不能再出现 Claude：%r" % (hidden_labels,))
    hidden_blob = _menu_blob(hidden)
    expect("configure-claude-key" not in hidden_blob and "Claude 不可用" not in hidden_blob,
           "关掉之后下拉不能再出现 Claude 区块")
    expect(not any(segment.get("text") == "Claude"
                   for entry in hidden["menu"]
                   for segment in (entry.get("segments") or [])),
           "关掉之后不能再出现 Claude 标题")
    expect("ChatGPT Plus" in hidden_blob and CLAUDE_USAGE_PAGE in hidden_blob,
           "关掉 Claude 不应拿掉其它区块和用量页链接")
    expect(all(item["key"].startswith("codex.") for item in hidden["alerts"]),
           "关掉之后不能再为 Claude 产生通知：%r" % (hidden["alerts"],))

    high = {
        "session": {"pct": 60, "resetsAt": 111},
        "week": {"pct": 91, "resetsAt": 222},
        "models": [{"label": "Fable", "pct": 80, "resetsAt": 333, "key": "claude.model.fable.test"}],
        "extra": {"pct": 10, "resetsAt": 0},
        "error": None,
        "ts": 50,
    }
    shown = build_payload(
        cfg, None, "未配置 Cursor token", False, deepseek, None, False,
        codex, None, False, high, None, True)
    shown_labels = [(column["label"], column["value"]) for column in shown["columns"]]
    expect(shown_labels[0][0] == "ChatGPT" and shown_labels[1] == (
        "Claude", "60%\u200a/\u200a91%"), "菜单栏应是 5 小时 / 周额度：%r" % (shown_labels,))
    expect(shown["columns"][1]["color"] == COLOR["danger"], "较高的一格达到危险阈值")
    expect(any(column["label"] == "Cursor" and column["value"] == "--"
               for column in shown["columns"]), "Cursor 失败时仍占位")
    expect(any(column["label"] == "DeepSeek" for column in shown["columns"]),
           "DeepSeek 不应被 Cursor 或 Claude 的状态影响")
    shown_blob = _menu_blob(shown)
    expect("5小时额度" in shown_blob and "周额度" in shown_blob and "Fable" in shown_blob
           and "额外用量" in shown_blob and "\u2588" in shown_blob and "重置时间" in shown_blob,
           "下拉进度或模型行缺失：%s" % shown_blob)
    expect("Cursor 不可用" in shown_blob and "数据为缓存值" in shown_blob,
           "其它源的失败和缓存提示应还在")
    alert_keys = [item["key"] for item in shown["alerts"]]
    expect("claude.session" in alert_keys and "claude.week" in alert_keys
           and "claude.model.fable.test" in alert_keys and "claude.extra" not in alert_keys,
           "只提醒越线的 Claude 窗口：%r" % (alert_keys,))
    by_key = {item["key"]: item for item in shown["alerts"]}
    expect(by_key["claude.session"]["level"] == "warn" and by_key["claude.session"]["cycle"] == "111",
           "5 小时 60%% 走原来的预警阈值")
    expect(by_key["claude.week"]["level"] == "danger" and by_key["claude.week"]["source"] == "Claude",
           "周额度 91%% 走原来的危险阈值")

    only_week = build_payload(
        cfg, None, None, False, None, None, False, None, None, False,
        {"week": {"pct": 12.2, "resetsAt": 0}, "session": None, "models": [], "extra": None},
        None, False)
    week_col = [column for column in only_week["columns"] if column["label"] == "Claude"]
    only_blob = _menu_blob(only_week)
    expect(len(week_col) == 1 and week_col[0]["value"] == "12%",
           "只有周额度时不补一格 0%%：%r" % (week_col,))
    expect("周额度" in only_blob and "5小时额度" not in only_blob,
           "没有的 5 小时额度不应画出来")

    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    sys.stdout.write("report self-test ok\n")
    return 0


def main():
    # 校验模式：凭证从 stdin 读入，输出一行 JSON。
    # App 的配置弹窗与 scripts/*.sh 都走这里，避免出现两套判断。
    argv = sys.argv[1:]
    if argv == ["--self-test"]:
        sys.exit(self_test())
    for flag, verifier in (("--verify-cursor-token", verify_cursor_token),
                           ("--verify-deepseek-key", verify_deepseek_key),
                           ("--verify-claude-key", verify_claude_key)):
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
            elif key == "claude":
                current = current_cred_id(
                    lambda: read_claude_key(claude_service_name(cfg)))
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
    claude, claude_err, claude_stale, drop_claude = resolve(
        claude_enabled(cfg),
        lambda: fetch_claude(claude_service_name(cfg)),
        state.get("claude"), is_fresh("claude"))
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
            ("codex", codex, drop_codex),
            ("claude", claude, drop_claude)):
        if drop:
            state.pop(key, None)
        elif data is not None and not data.get("error"):
            state[key] = data
    state.pop("ts", None)
    save_state(state)

    payload = build_payload(cfg, cursor, cursor_err, cursor_stale,
                            deepseek, deepseek_err, deepseek_stale,
                            codex, codex_err, codex_stale,
                            claude, claude_err, claude_stale)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
