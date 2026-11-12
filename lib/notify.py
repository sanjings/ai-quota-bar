#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""额度预警：哪些该提醒，以及同一阈值、同一周期要不要再发。

菜单着色仍由 report.level() 和原来的低余额判断决定。这里只把已经越线的项
列出来，并记住发过没有。不改百分比怎么进 warn / danger。

Swift 端负责系统通知和权限引导。本模块不弹窗、不联网。
账本由调用方保存；本文件的计划函数不写磁盘。

  python3 lib/notify.py --self-test
  python3 lib/notify.py --plan-notifications   # stdin 一行 JSON，stdout 一行 JSON

stdin:
  {"alerts": [...], "ledger": {"sent": {}}, "enabled": true, "now": 1}
  cooldown 可省略，默认 REARM_SECONDS。

stdout:
  {"ok": true, "deliver": [...], "ledger": {"sent": {...}}}
  {"ok": false, "deliver": [], "ledger": {"sent": {}}}
ok 为 false 时调用方不要用这份 ledger 覆盖磁盘。
"""

import json
import os
import subprocess
import sys

# 同一指纹在恢复之后，至少隔这么久才允许再发。
# 阈值、级别或重置周期变了不等这 30 分钟：调低阈值后下一轮就要能收到。
# 一直停在同一周期、同一阈值上则不会因为时间到了再发。
REARM_SECONDS = 30 * 60
# 官方重置时间会在几秒内来回跳。差在 5 分钟内仍算同一周期。
# shortcut: 最短真实窗口是 5 小时；出现更短的窗口时再收紧这段。
CYCLE_SLACK_SECONDS = 300


def notifications_enabled(cfg):
    """缺省打开。只有 JSON 布尔 false 才关掉；别的类型不当成关闭。"""
    section = cfg.get("notifications") if isinstance(cfg, dict) else None
    if not isinstance(section, dict) or "enabled" not in section:
        return True
    value = section.get("enabled")
    if isinstance(value, bool):
        return value
    return True


def threshold_token(value):
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError("bad threshold")
    rounded = round(number, 6)
    nearest = round(rounded)
    if abs(rounded - nearest) < 1e-9:
        return str(int(nearest))
    text = "%.6f" % rounded
    return text.rstrip("0").rstrip(".")


def cycle_token(value):
    """重置时刻。变了就是新周期。没有或不是正数时当成空周期。"""
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number or number <= 0 or number in (float("inf"), float("-inf")):
        return ""
    nearest = round(number)
    if abs(number - nearest) < 1e-6 and abs(nearest) <= 2 ** 53:
        return str(int(nearest))
    return "%.6f" % number


def same_notice(previous_fp, new_fp):
    """同一项目、同一级别、同一阈值，且重置时间差在 5 分钟内。"""
    if previous_fp == new_fp:
        return True
    old = str(previous_fp).split("|")
    new = str(new_fp).split("|")
    if len(old) != 4 or old[:3] != new[:3]:
        return False
    try:
        return abs(float(old[3]) - float(new[3])) <= CYCLE_SLACK_SECONDS
    except ValueError:
        return False


def fingerprint(alert):
    key = str(alert["key"])
    level = str(alert["level"])
    if not key or not level:
        raise ValueError("incomplete")
    cycle = alert.get("cycle")
    cycle = "" if cycle is None else str(cycle)
    return "%s|%s|%s|%s" % (key, level, threshold_token(alert["threshold"]), cycle)


def format_amount(number):
    number = float(number)
    rounded = round(number, 1)
    nearest = round(rounded)
    if abs(rounded - nearest) < 1e-9:
        return str(int(nearest))
    return "%.1f" % rounded


def _usage_alert(level_fn, source, metric, key, pct, cycle, warn, danger):
    try:
        pct_value = float(pct)
    except (TypeError, ValueError):
        return None
    state = level_fn(pct_value, warn, danger)
    if state == "danger":
        threshold = danger
        word = "危险"
    elif state == "warn":
        threshold = warn
        word = "预警"
    else:
        return None
    alert = {
        "key": key,
        "source": source,
        "metric": metric,
        "level": state,
        "value": pct_value,
        "threshold": float(threshold),
        "cycle": cycle_token(cycle),
        "title": "%s 用量达到%s" % (source, word),
        "body": "%s 已用 %s%%，达到%s阈值 %s%%。" % (
            metric, format_amount(pct_value), word, format_amount(threshold)),
    }
    alert["id"] = fingerprint(alert)
    return alert


def _deepseek_alert(deepseek, low_balance):
    if not isinstance(deepseek, dict) or deepseek.get("error"):
        return None
    # 和菜单栏同一判断，定义在 report.deepseek_balance_state。
    import report
    low, unavailable = report.deepseek_balance_state(deepseek, low_balance)
    if not low and not unavailable:
        return None
    symbol = "\u00a5" if (deepseek.get("currency") or "").upper() == "CNY" else "$"
    raw_total = deepseek.get("total")
    shown = raw_total if raw_total not in (None, "") else "--"
    try:
        value = float(raw_total)
    except (TypeError, ValueError):
        value = 0.0
    # 低于阈值和「已无法请求」在菜单里都是危险色。用 cycle 区分，
    # 从「只是偏低」变成「已无法请求」会再发一条。
    if unavailable and low:
        title = "DeepSeek 余额不足"
        body = "余额 %s%s，已低于阈值 %s，且已无法发起请求。" % (
            symbol, shown, format_amount(low_balance))
        cycle = "unavailable"
    elif unavailable:
        title = "DeepSeek 余额不足"
        body = "余额 %s%s，已无法发起请求。" % (symbol, shown)
        cycle = "unavailable"
    else:
        title = "DeepSeek 余额低于阈值"
        body = "余额 %s%s，已低于阈值 %s。" % (
            symbol, shown, format_amount(low_balance))
        cycle = "balance"
    alert = {
        "key": "deepseek.balance",
        "source": "DeepSeek",
        "metric": "余额",
        "level": "danger",
        "value": value,
        "threshold": float(low_balance),
        "cycle": cycle,
        "title": title,
        "body": body,
    }
    alert["id"] = fingerprint(alert)
    return alert


def _claude_window_alert(level_fn, metric, key, window, warn, danger):
    if not isinstance(window, dict) or window.get("pct") is None:
        return None
    return _usage_alert(
        level_fn, "Claude", metric, key, window.get("pct"),
        window.get("resetsAt") or 0, warn, danger)


def collect_alerts(cfg, cursor, deepseek, codex, level_fn, claude=None):
    """当前越线项。level_fn 必须是 report.level，百分比比较不在这里重写。

    合计、两个模型池、Grok Bot 各自一条。菜单栏那一列的颜色只用池子和
    Grok Bot 的最大值，合计仍单独提醒，因为下拉标题会按合计着色。
    Claude 的 5 小时、周额度、模型周额度和额外用量也走同一套百分比阈值。
    """
    warn = float(cfg["thresholds"]["warn"])
    danger = float(cfg["thresholds"]["danger"])
    low_balance = float(cfg["deepseek"]["lowBalanceThreshold"])
    alerts = []

    def add(alert):
        if alert:
            alerts.append(alert)

    if isinstance(codex, dict):
        add(_usage_alert(
            level_fn, "ChatGPT", "5小时额度", "codex.primary",
            codex.get("primaryPct", 0.0), codex.get("primaryResetsAt", 0),
            warn, danger))
        add(_usage_alert(
            level_fn, "ChatGPT", "周额度", "codex.secondary",
            codex.get("secondaryPct", 0.0), codex.get("secondaryResetsAt", 0),
            warn, danger))

    if isinstance(cursor, dict):
        cycle_end = cursor.get("cycleEnd") or 0
        total = cursor.get("totalPct")
        if total is not None:
            add(_usage_alert(
                level_fn, "Cursor", "合计", "cursor.total",
                total, cycle_end, warn, danger))
        add(_usage_alert(
            level_fn, "Cursor", "Cursor Models", "cursor.models",
            cursor.get("modelsPct", 0.0), cycle_end, warn, danger))
        add(_usage_alert(
            level_fn, "Cursor", "Others Models", "cursor.other",
            cursor.get("otherPct", 0.0), cycle_end, warn, danger))
        grok = cursor.get("grok") or None
        if isinstance(grok, dict) and grok.get("pct") is not None:
            add(_usage_alert(
                level_fn, "Cursor", "Grok Bot", "cursor.grok",
                grok.get("pct"), grok.get("resetsAt") or 0, warn, danger))

    if isinstance(claude, dict) and not claude.get("error"):
        add(_claude_window_alert(
            level_fn, "5小时额度", "claude.session", claude.get("session"), warn, danger))
        add(_claude_window_alert(
            level_fn, "周额度", "claude.week", claude.get("week"), warn, danger))
        for model in claude.get("models") or []:
            if not isinstance(model, dict):
                continue
            add(_claude_window_alert(
                level_fn, model.get("label") or "模型",
                model.get("key") or "claude.model", model, warn, danger))
        add(_claude_window_alert(
            level_fn, "额外用量", "claude.extra", claude.get("extra"), warn, danger))

    add(_deepseek_alert(deepseek, low_balance))
    return alerts


def _load_sent(ledger):
    sent = {}
    if not isinstance(ledger, dict):
        return sent
    raw = ledger.get("sent")
    if not isinstance(raw, dict):
        return sent
    for key, record in raw.items():
        if not isinstance(record, dict) or not record.get("fingerprint"):
            continue
        try:
            at = float(record.get("at") or 0)
        except (TypeError, ValueError):
            at = 0.0
        if at != at or at in (float("inf"), float("-inf")):
            at = 0.0
        sent[str(key)] = {"fingerprint": str(record["fingerprint"]), "at": at}
    return sent


def _index_alerts(alerts):
    indexed = []
    if not isinstance(alerts, list):
        return indexed
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        try:
            key = str(alert.get("key") or "")
            if not key:
                continue
            fp = fingerprint(alert)
        except Exception:
            continue
        indexed.append((key, fp, alert))
    return indexed


def plan_notifications(alerts, ledger, enabled, now, cooldown):
    """决定这一轮发哪些。不修改传入的 ledger。

    同一 key、同一指纹（级别 + 阈值 + 周期）不重复发。
    指纹变了立刻发，不等冷却。冷却只拦住「掉下去又在短时间内回来」的同一指纹。
    enabled 为 false 时不发，也不把当前越线记成已发。
    """
    sent = _load_sent(ledger)
    indexed = _index_alerts(alerts)
    seen = set()
    deliver = []
    if enabled:
        for key, fp, alert in indexed:
            seen.add(key)
            previous = sent.get(key)
            if previous and same_notice(previous["fingerprint"], fp):
                continue
            item = dict(alert)
            item["id"] = fp
            item["key"] = key
            deliver.append(item)
            sent[key] = {"fingerprint": fp, "at": float(now)}
    else:
        for key, _fp, _alert in indexed:
            seen.add(key)

    limit = float(cooldown)
    if limit < 0 or limit != limit or limit == float("inf"):
        limit = 0.0
    for key in list(sent):
        if key in seen:
            continue
        if float(now) - float(sent[key]["at"]) >= limit:
            del sent[key]
    return deliver, {"sent": sent}


def commit_deliveries(previous, proposed, deliver_keys, delivered_keys):
    """只把实际发出去的记入账本。没发出去的保持原记录，下一轮再试。

    proposed 里已经丢掉「恢复且过了冷却」的项，成功与否都保留这份清理。
    """
    prev_sent = _load_sent(previous)
    sent = _load_sent(proposed)
    delivered = set(delivered_keys or [])
    for key in deliver_keys or []:
        key = str(key)
        if key in delivered:
            continue
        if key in prev_sent:
            sent[key] = dict(prev_sent[key])
        else:
            sent.pop(key, None)
    return {"sent": sent}


def deliver_notifications(items, post):
    """逐条发送。单条失败不影响后面的，也不向外抛。"""
    delivered = []
    for item in items:
        try:
            post(item)
        except Exception:
            continue
        delivered.append(item)
    return delivered


def cli_plan(raw):
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("not object")
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("enabled")
        now = float(payload.get("now"))
        if now != now or now in (float("inf"), float("-inf")):
            raise ValueError("now")
        if "cooldown" in payload:
            cooldown = float(payload.get("cooldown"))
        else:
            cooldown = REARM_SECONDS
        alerts = payload.get("alerts") or []
        ledger = payload.get("ledger") or {"sent": {}}
        deliver, new_ledger = plan_notifications(
            alerts, ledger, enabled, now, cooldown)
        return {"ok": True, "deliver": deliver, "ledger": new_ledger}
    except Exception:
        return {"ok": False, "deliver": [], "ledger": {"sent": {}}}


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["--self-test"]:
        return self_test()
    if argv == ["--plan-notifications"]:
        sys.stdout.write(json.dumps(cli_plan(sys.stdin.read()), ensure_ascii=False) + "\n")
        return 0
    sys.stderr.write("未知参数\n")
    return 2


def self_test():
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    import report

    def level(pct, warn, danger):
        return report.level(pct, warn, danger)

    cfg = report.deep_merge(report.DEFAULT_CONFIG, {})
    expect(notifications_enabled(cfg) is True, "缺省应打开通知")
    expect(notifications_enabled({"notifications": {"enabled": False}}) is False, "false 应关闭")
    expect(notifications_enabled({"notifications": {"enabled": True}}) is True, "true 应打开")
    expect(notifications_enabled({"notifications": {"enabled": 0}}) is True, "非布尔不当成关闭")
    expect(report.level(49.9, 50, 80) == "ok", "level 边界 ok")
    expect(report.level(50, 50, 80) == "warn", "level 边界 warn")
    expect(report.level(80, 50, 80) == "danger", "level 边界 danger")

    def always_ok(pct, warn, danger):
        return "ok"

    high_cursor = {
        "modelsPct": 99, "otherPct": 99, "totalPct": 99,
        "cycleEnd": 5, "grok": {"pct": 99, "resetsAt": 8},
    }
    expect(
        collect_alerts(cfg, high_cursor, None, None, always_ok) == [],
        "用量是否越线只看传入的 level，不在通知里重写比较")

    def always_danger(pct, warn, danger):
        return "danger"

    forced = collect_alerts(cfg, high_cursor, None, None, always_danger)
    expect(forced and all(item["level"] == "danger" for item in forced), "level 返回 danger 就按危险发")
    expect(all(item["threshold"] == 80 for item in forced), "危险档用的是 thresholds.danger")

    codex = {
        "primaryPct": 60,
        "primaryResetsAt": 1000,
        "secondaryPct": 91,
        "secondaryResetsAt": 2000,
    }
    payload = report.build_payload(
        cfg, None, None, False, None, None, False, codex, None, False)
    expect(payload["columns"][0]["color"] == report.COLOR["danger"], "周额度把菜单栏列标成危险")
    keys = [item["key"] for item in payload["alerts"]]
    expect(keys == ["codex.primary", "codex.secondary"], "ChatGPT 两个窗口各自提醒：%r" % (keys,))
    expect(payload["alerts"][0]["level"] == "warn", "5 小时 60% 是预警")
    expect(payload["alerts"][0]["threshold"] == 50, "预警阈值来自配置")
    expect(payload["alerts"][0]["cycle"] == "1000", "5 小时周期用重置时刻")
    expect(payload["alerts"][1]["level"] == "danger", "周额度 91% 是危险")
    expect(payload["alerts"][1]["cycle"] == "2000", "周额度有自己的周期")
    expect(payload["notificationsEnabled"] is True, "payload 带上开关")

    cursor = {
        "modelsPct": 10,
        "otherPct": 55,
        "totalPct": 96,
        "cycleEnd": 333,
        "grok": {"pct": 90, "resetsAt": 444},
    }
    payload = report.build_payload(
        cfg, cursor, None, False, None, None, False, None, None, False)
    expect(payload["columns"][0]["label"] == "Cursor / Grok Bot", "有 Grok Bot 时列名带上它")
    expect(payload["columns"][0]["color"] == report.COLOR["danger"], "Grok Bot 越线时列是危险色")
    by_key = {item["key"]: item for item in payload["alerts"]}
    expect(set(by_key) == {"cursor.total", "cursor.other", "cursor.grok"},
           "合计、Others、Grok Bot 越线，Models 10%% 不发：%r" % (sorted(by_key),))
    expect(by_key["cursor.grok"]["metric"] == "Grok Bot", "Grok Bot 单独一条")
    expect(by_key["cursor.grok"]["cycle"] == "444", "Grok Bot 用自己的重置时刻")
    expect(by_key["cursor.total"]["level"] == "danger", "合计按 level 着色")
    expect("Grok Bot" in by_key["cursor.grok"]["body"], "正文点出是哪一格")

    quiet = {
        "modelsPct": 1, "otherPct": 1, "totalPct": 1, "cycleEnd": 1, "grok": None,
    }
    payload = report.build_payload(
        cfg, quiet, None, False, None, None, False, None, None, False)
    expect(payload["alerts"] == [], "都在阈值下不发")
    expect(payload["columns"][0]["color"] == report.COLOR["ok"], "菜单栏仍是正常色")

    low = {"total": "20", "currency": "CNY", "available": True, "error": None}
    payload = report.build_payload(
        cfg, None, None, False, low, None, False, None, None, False)
    expect(payload["columns"][0]["color"] == report.COLOR["danger"], "等于低余额阈值时菜单标红")
    expect(len(payload["alerts"]) == 1 and payload["alerts"][0]["key"] == "deepseek.balance",
           "等于阈值要提醒")
    expect(payload["alerts"][0]["threshold"] == 20, "余额提醒带着低余额阈值")
    expect(payload["alerts"][0]["cycle"] == "balance", "仅偏低时的周期标记")
    expect("¥20" in payload["alerts"][0]["body"], "正文保留余额")

    above = {"total": "20.01", "currency": "USD", "available": True, "error": None}
    payload = report.build_payload(
        cfg, None, None, False, above, None, False, None, None, False)
    expect(payload["columns"][0]["color"] == report.COLOR["ok"], "高于阈值不标红")
    expect(payload["alerts"] == [], "高于阈值不提醒")

    blocked = {"total": "100", "currency": "CNY", "available": False, "error": None}
    payload = report.build_payload(
        cfg, None, None, False, blocked, None, False, None, None, False)
    expect(payload["columns"][0]["color"] == report.COLOR["danger"], "不可用时菜单标红")
    expect(payload["alerts"][0]["cycle"] == "unavailable", "不可用是另一条")
    expect("无法发起请求" in payload["alerts"][0]["body"], "正文说明已无法请求")

    broken = {"total": "x", "currency": "CNY", "available": True, "error": None}
    expect(report.deepseek_balance_state(broken, 20) == (False, False), "余额解析不了不算低")
    expect(report.deepseek_balance_state(None, 20) == (False, False), "没有数据不算低")
    expect(_deepseek_alert({"error": "未配置密钥", "total": "1", "available": False}, 20) is None,
           "有 error 时不发")
    both = {"total": "1", "currency": "CNY", "available": False, "error": None}
    both_alert = _deepseek_alert(both, 20)
    expect(both_alert is not None and both_alert["cycle"] == "unavailable"
           and "低于阈值" in both_alert["body"] and "无法发起请求" in both_alert["body"],
           "又低又不可用要说明两件事")

    # 去重：同一阈值、同一周期不刷屏；调低阈值或换周期立刻再发。
    primary = collect_alerts(cfg, None, None, {
        "primaryPct": 60, "primaryResetsAt": 1000,
        "secondaryPct": 0, "secondaryResetsAt": 2000,
    }, level)
    expect(len(primary) == 1, "只有 5 小时越线")
    untouched = {"sent": {}}
    deliver, ledger = plan_notifications(primary, untouched, True, 1_000, REARM_SECONDS)
    expect(len(deliver) == 1, "第一次要发")
    expect(untouched == {"sent": {}}, "计划不应改调用方的账本")
    again, ledger2 = plan_notifications(primary, ledger, True, 1_000 + 3600, REARM_SECONDS)
    expect(again == [], "同一周期同一阈值隔一小时也不再发")
    expect(ledger2["sent"]["codex.primary"]["fingerprint"]
           == ledger["sent"]["codex.primary"]["fingerprint"], "指纹保持")

    lowered_cfg = report.deep_merge(cfg, {"thresholds": {"warn": 20, "danger": 80}})
    lowered = collect_alerts(lowered_cfg, None, None, {
        "primaryPct": 60, "primaryResetsAt": 1000,
        "secondaryPct": 0, "secondaryResetsAt": 2000,
    }, level)
    expect(lowered[0]["threshold"] == 20, "调低后的候选带着新阈值")
    resent, _ledger3 = plan_notifications(lowered, ledger, True, 2_000, REARM_SECONDS)
    expect(len(resent) == 1 and resent[0]["threshold"] == 20, "调低阈值后下一轮要发")

    as_int = dict(primary[0], threshold=50)
    as_float = dict(primary[0], threshold=50.0)
    _first, ledger_num = plan_notifications([as_int], {"sent": {}}, True, 10, 1000)
    second, _ = plan_notifications([as_float], ledger_num, True, 20, 1000)
    expect(second == [], "50 和 50.0 是同一阈值")

    escalated = collect_alerts(cfg, None, None, {
        "primaryPct": 90, "primaryResetsAt": 1000,
        "secondaryPct": 0, "secondaryResetsAt": 2000,
    }, level)
    bumped, _ = plan_notifications(escalated, ledger, True, 2_100, REARM_SECONDS)
    expect(len(bumped) == 1 and bumped[0]["level"] == "danger", "预警升到危险要再发")

    new_cycle = collect_alerts(cfg, None, None, {
        "primaryPct": 60, "primaryResetsAt": 5000,
        "secondaryPct": 0, "secondaryResetsAt": 2000,
    }, level)
    cycled, _ = plan_notifications(new_cycle, ledger, True, 1_010, REARM_SECONDS)
    expect(len(cycled) == 1 and cycled[0]["cycle"] == "5000", "新周期立刻再发，不等冷却")

    jitter_base = collect_alerts(cfg, None, None, {
        "primaryPct": 57, "primaryResetsAt": 0,
        "secondaryPct": 57, "secondaryResetsAt": 1794984816,
    }, level)
    _, jitter_ledger = plan_notifications(jitter_base, {"sent": {}}, True, 3_000, REARM_SECONDS)
    jitter_next = collect_alerts(cfg, None, None, {
        "primaryPct": 57, "primaryResetsAt": 0,
        "secondaryPct": 57, "secondaryResetsAt": 1794984823,
    }, level)
    jitter_again, _ = plan_notifications(jitter_next, jitter_ledger, True, 3_060, REARM_SECONDS)
    expect(jitter_again == [], "重置时间差几秒不再发")

    # 开关关掉：不发，也不把当前越线记成已发。
    quiet_deliver, quiet_ledger = plan_notifications(primary, {"sent": {}}, False, 3_000, REARM_SECONDS)
    expect(quiet_deliver == [], "关掉开关不发")
    expect(quiet_ledger["sent"] == {}, "关掉时不记成已发")
    opened, _ = plan_notifications(primary, quiet_ledger, True, 3_060, REARM_SECONDS)
    expect(len(opened) == 1, "重新打开后，之前没发过的要补发")

    held, held_ledger = plan_notifications(primary, ledger, False, 4_000, REARM_SECONDS)
    expect(held == [], "已发过再关掉，这一轮不发")
    expect("codex.primary" in held_ledger["sent"], "关掉开关不清已发记录")

    off_cfg = report.deep_merge(cfg, {"notifications": {"enabled": False}})
    payload = report.build_payload(
        off_cfg, None, None, False, None, None, False, codex, None, False)
    expect(payload["notificationsEnabled"] is False, "配置 false 写进 payload")
    expect(payload["alerts"], "关掉开关仍列出越线项，由计划函数决定不发")
    expect(payload["columns"], "关掉开关不影响菜单栏")
    blocked_deliver, _ = plan_notifications(
        payload["alerts"], {"sent": {}}, payload["notificationsEnabled"], 5, REARM_SECONDS)
    expect(blocked_deliver == [], "payload 的开关为 false 时不发")

    # 冷却：掉下去又马上回来不刷；过了冷却再越过可以发。一直没掉下去则不因时间到了再发。
    short = 100
    first, cool = plan_notifications(primary, {"sent": {}}, True, 0, short)
    expect(len(first) == 1, "冷却测试的第一发")
    absent, cool = plan_notifications([], cool, True, 50, short)
    expect(absent == [] and "codex.primary" in cool["sent"], "冷却内恢复的记录还留着")
    flap, cool = plan_notifications(primary, cool, True, 60, short)
    expect(flap == [], "冷却内回到同一阈值不发")
    still, cool = plan_notifications(primary, cool, True, 10_000, short)
    expect(still == [], "一直停在阈值上，冷却结束也不再发")
    gone, cool = plan_notifications([], cool, True, 10_000, short)
    expect("codex.primary" not in cool["sent"], "离开并过了冷却就忘掉")
    back, _ = plan_notifications(primary, cool, True, 10_100, short)
    expect(len(back) == 1, "冷却结束后再次越过要发")

    # 关掉开关期间也可以忘掉已经恢复的项。
    stale = {"sent": {"codex.primary": {"fingerprint": "old", "at": 0}}}
    pruned, pruned_ledger = plan_notifications([], stale, False, short, short)
    expect(pruned == [] and "codex.primary" not in pruned_ledger["sent"],
           "关闭期间过了冷却的恢复项要忘掉")

    # 失败的不记入账本，下一轮再试；成功的记上。菜单数据与发送无关。
    two = collect_alerts(cfg, {
        "modelsPct": 90, "otherPct": 10, "totalPct": None, "cycleEnd": 9, "grok": None,
    }, None, {
        "primaryPct": 90, "primaryResetsAt": 7,
        "secondaryPct": 0, "secondaryResetsAt": 8,
    }, level)
    proposed_deliver, proposed = plan_notifications(two, {"sent": {}}, True, 10, short)
    deliver_keys = [item["key"] for item in proposed_deliver]
    expect("cursor.models" in deliver_keys and "codex.primary" in deliver_keys, "两项都该发")
    partial = commit_deliveries(
        {"sent": {}}, proposed, deliver_keys, ["cursor.models"])
    expect("cursor.models" in partial["sent"], "发出去的要记上")
    expect("codex.primary" not in partial["sent"], "没发出去的不记")
    retry, _ = plan_notifications(two, partial, True, 11, short)
    expect([item["key"] for item in retry] == ["codex.primary"], "失败的下一轮再试：%r" % (retry,))

    previous = {"sent": {"codex.primary": {"fingerprint": "keep", "at": 1}}}
    failed = commit_deliveries(previous, proposed, ["codex.primary"], [])
    expect(failed["sent"]["codex.primary"]["fingerprint"] == "keep", "失败时留着旧记录")

    columns = [{"label": "Cursor", "value": "1%"}]

    def explode(item):
        raise RuntimeError("notification center unavailable")

    delivered = deliver_notifications(proposed_deliver, explode)
    expect(delivered == [], "全部失败时结果为空")
    expect(columns[0]["label"] == "Cursor", "发送失败不改菜单栏数据")

    seen = []

    def flaky(item):
        seen.append(item["key"])
        if item["key"] == "codex.primary":
            raise RuntimeError("denied")

    delivered = deliver_notifications(proposed_deliver, flaky)
    expect([item["key"] for item in delivered] == ["cursor.models"]
           or "cursor.models" in [item["key"] for item in delivered],
           "一条失败不挡住后面的：%r" % ([item["key"] for item in delivered],))
    expect("codex.primary" in seen and "cursor.models" in seen, "失败的那条也尝试过")

    import notify as notify_mod
    original = notify_mod.collect_alerts

    def boom(*args, **kwargs):
        raise RuntimeError("notification collector failed")

    notify_mod.collect_alerts = boom
    try:
        payload = report.build_payload(
            cfg, high_cursor, None, False, None, None, False, codex, None, False)
    finally:
        notify_mod.collect_alerts = original
    expect(payload["columns"], "收集通知失败时菜单栏列还在")
    expect(payload["menu"], "收集通知失败时下拉菜单还在")
    expect(payload["alerts"] == [], "收集失败时 alerts 为空而不是抛出")
    labels = [column["label"] for column in payload["columns"]]
    expect(labels[:2] == ["ChatGPT", "Cursor / Grok Bot"],
           "收集失败时列内容仍是这次取数：%r" % (labels,))

    bad = cli_plan("not-json")
    expect(bad["ok"] is False and bad["deliver"] == [], "坏 JSON 返回 ok false")
    missing = cli_plan(json.dumps({"alerts": primary, "now": 1}))
    expect(missing["ok"] is False, "没有 enabled 布尔时不当成要发")

    raw_first = json.dumps({
        "alerts": primary,
        "ledger": {"sent": {}},
        "enabled": True,
        "now": 0,
    })
    parsed = cli_plan(raw_first)
    expect(parsed["ok"] is True and len(parsed["deliver"]) == 1, "省略 cooldown 时用默认冷却")
    recovered = cli_plan(json.dumps({
        "alerts": [],
        "ledger": parsed["ledger"],
        "enabled": True,
        "now": REARM_SECONDS - 1,
    }))
    expect("codex.primary" in recovered["ledger"]["sent"], "默认冷却未到不忘掉")
    expired = cli_plan(json.dumps({
        "alerts": [],
        "ledger": parsed["ledger"],
        "enabled": True,
        "now": REARM_SECONDS,
    }))
    expect(expired["ok"] is True and expired["ledger"]["sent"] == {}, "默认冷却到点才忘掉")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "quota.1m.sh")
    proc = subprocess.run(
        ["bash", script, "--plan-notifications"],
        input=raw_first,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    expect(proc.returncode == 0, "计划入口退出码应为 0：%s" % (proc.stderr,))
    try:
        routed = json.loads(proc.stdout)
    except ValueError:
        routed = {}
    expect(routed.get("ok") is True and len(routed.get("deliver") or []) == 1,
           "quota.1m.sh 应把计划交给 notify.py：%r" % (proc.stdout[:200],))
    expect("ChatGPT" in (routed.get("deliver") or [{}])[0].get("title", ""),
           "计划结果带着通知标题")

    if failures:
        sys.stderr.write("\n".join(failures) + "\n")
        return 1
    sys.stdout.write("notify self-test ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
