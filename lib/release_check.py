#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比当前构建和 GitHub 上最新的正式 Release。

由 quota.1m.sh --check-release <版本> 调用。stdout 只打一行 JSON：

  {"ok": true, "update": false}
  {"ok": true, "update": true, "tag": "v0.3.0", "url": "https://github.com/..."}
  {"ok": false}

ok 为 false 表示这次没问成（网络、限流、返回异常、当前版本无法解析）。
调用方应保持沉默，不要挡住额度展示。这里不下载安装包。
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = "sanjings/ai-quota-bar"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
# 正式 Release 的 JSON 目前只有几 KB。设上限是为了读响应时能停下来。
MAX_BODY = 256 * 1024
TIMEOUT_SECONDS = 8

_TAG_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._+-"


def parse_version(raw):
    """解析 v 前缀、可选的先行版本和 +build。无法比较时返回 None。

    返回 (major, minor, patch, prerelease_tuple)。缺省的 minor/patch 视为 0。
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > 64:
        return None
    if text[0] in ("v", "V"):
        text = text[1:]
    plus = text.find("+")
    if plus != -1:
        text = text[:plus]
    if not text:
        return None
    if "-" in text:
        core, pre = text.split("-", 1)
        pre_parts = pre.split(".")
        if any(part == "" for part in pre_parts):
            return None
    else:
        core = text
        pre_parts = []
    numbers = core.split(".")
    if not 1 <= len(numbers) <= 3:
        return None
    parsed = []
    for part in numbers:
        if not part.isdigit():
            return None
        if len(part) > 1 and part.startswith("0"):
            return None
        parsed.append(int(part))
    while len(parsed) < 3:
        parsed.append(0)
    for part in pre_parts:
        if not _pre_ok(part):
            return None
    return (parsed[0], parsed[1], parsed[2], tuple(pre_parts))


def _pre_ok(part):
    if not part or len(part) > 20:
        return False
    for ch in part:
        if not (ch.isalnum() or ch == "-"):
            return False
    if part.isdigit() and len(part) > 1 and part.startswith("0"):
        return False
    return True


def is_newer(remote, current):
    """remote、current 都是 parse_version 的结果。remote 更高时为 True。"""
    if remote[:3] != current[:3]:
        return remote[:3] > current[:3]
    rpre = remote[3]
    cpre = current[3]
    if not rpre and not cpre:
        return False
    # 正式版高于同号的先行版本。
    if not rpre:
        return True
    if not cpre:
        return False
    for left, right in zip(rpre, cpre):
        if left == right:
            continue
        return _identifier_key(left) > _identifier_key(right)
    return len(rpre) > len(cpre)


def _identifier_key(part):
    # 数字标识符优先级低于非数字（SemVer）。
    if part.isdigit():
        return (0, int(part))
    return (1, part)


def safe_tag(tag):
    """菜单标题和 Release 路径只用这种 tag，避免把接口原文直接拼进 URL。"""
    if not isinstance(tag, str) or not 1 <= len(tag) <= 41:
        return False
    if ".." in tag:
        return False
    return all(ch in _TAG_ALPHABET for ch in tag)


def release_page(tag):
    quoted = urllib.parse.quote(tag, safe="")
    return "https://github.com/%s/releases/tag/%s" % (REPO, quoted)


def interpret(document, current_raw):
    """根据 /releases/latest 的 JSON 决定要不要提示。

    返回 None 表示响应或当前版本无法判断（调用方按失败处理）。
    预发布和草稿不提示。比较用 SemVer，链接固定到该 tag 的 Release 页。
    """
    if not isinstance(document, dict):
        return None
    current = parse_version(current_raw)
    if current is None:
        return None
    if document.get("draft") or document.get("prerelease"):
        return {"update": False}
    tag = document.get("tag_name")
    if not safe_tag(tag):
        return {"update": False}
    remote = parse_version(tag)
    if remote is None or not is_newer(remote, current):
        return {"update": False}
    return {"update": True, "tag": tag, "url": release_page(tag)}


def fetch_latest():
    """拉取最新正式 Release。失败返回 None，不抛给菜单。"""
    request = urllib.request.Request(
        API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "AI-Quota-Bar",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    with response:
        final = urllib.parse.urlparse(response.geturl())
        if response.status != 200:
            return None
        if final.scheme != "https" or final.hostname != "api.github.com":
            return None
        if final.port not in (None, 443):
            return None
        raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        return None
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    return document


def emit(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")


def main(argv):
    args = [arg for arg in argv if arg != "--check-release"]
    if args == ["--self-test"]:
        return self_test()
    if len(args) != 1:
        emit({"ok": False})
        return 0
    current = args[0]
    try:
        document = fetch_latest()
        decision = None if document is None else interpret(document, current)
    except Exception:
        emit({"ok": False})
        return 0
    if decision is None:
        emit({"ok": False})
        return 0
    payload = {"ok": True, "update": decision["update"]}
    if decision["update"]:
        payload["tag"] = decision["tag"]
        payload["url"] = decision["url"]
    emit(payload)
    return 0


def self_test():
    failures = []

    def expect(condition, message):
        if not condition:
            failures.append(message)

    def newer(remote, current):
        left = parse_version(remote)
        right = parse_version(current)
        if left is None or right is None:
            failures.append("fixture did not parse: %r / %r" % (remote, current))
            return False
        return is_newer(left, right)

    expect(
        parse_version("v0.2.0") == parse_version("0.2.0") == parse_version("V0.2")
        == parse_version("0.2.0+build.5"),
        "normalize equal versions",
    )
    for bad in (
        "",
        "v",
        "1.2.3.4",
        "01.0.0",
        "1.02.0",
        "a.b.c",
        "1.0.0-",
        "1.0.0-.",
        "1.0.0-01",
        "1.0.0-rc..1",
        "latest",
        None,
        1,
    ):
        expect(parse_version(bad) is None, "reject %r" % (bad,))

    expect(newer("v0.2.0", "0.1.9"), "0.2.0 > 0.1.9")
    expect(not newer("v0.2.0", "0.2.0"), "same release")
    expect(not newer("v0.2.0", "0.2"), "missing patch is zero")
    expect(newer("v0.2.1", "0.2"), "0.2.1 > 0.2.0")
    expect(newer("v0.10.0", "0.9.9"), "numeric minor")
    expect(newer("v1.0.0", "0.9.9"), "major")
    expect(newer("v0.2.0", "0.2.0-rc.1"), "release > prerelease")
    expect(not newer("v0.2.0-rc.1", "0.2.0"), "prerelease < release")
    expect(newer("v0.2.0-rc.2", "0.2.0-rc.1"), "rc.2 > rc.1")
    expect(newer("v0.2.0-rc.10", "0.2.0-rc.9"), "rc.10 > rc.9")
    expect(not newer("v0.2.0-rc.9", "0.2.0-rc.10"), "rc.9 < rc.10")
    expect(newer("v0.2.0-alpha.1", "0.2.0-alpha"), "longer prerelease")
    expect(not newer("v0.2.0-alpha", "0.2.0-alpha.1"), "shorter prerelease")
    expect(newer("v0.2.0-alpha.beta", "0.2.0-alpha.1"), "numeric id < text id")
    expect(not newer("v0.2.0-alpha.1", "0.2.0-alpha.beta"), "text id > numeric id")

    def release(tag, **extra):
        document = {"tag_name": tag, "draft": False, "prerelease": False}
        document.update(extra)
        return document

    expect(interpret(release("v0.2.0"), "0.2.0") == {"update": False}, "up to date")
    found = interpret(release("v0.2.0"), "0.1.0")
    expect(
        found == {
            "update": True,
            "tag": "v0.2.0",
            "url": "https://github.com/sanjings/ai-quota-bar/releases/tag/v0.2.0",
        },
        "older build gets the release page, got %r" % (found,),
    )
    expect(interpret(release("v0.2.0"), "0.3.0") == {"update": False}, "local is newer")
    expect(
        interpret(release("v0.3.0", prerelease=True), "0.2.0") == {"update": False},
        "skip prerelease",
    )
    expect(
        interpret(release("v0.3.0", draft=True), "0.2.0") == {"update": False},
        "skip draft",
    )
    expect(interpret(release("not a release"), "0.1.0") == {"update": False}, "unsafe tag")
    expect(interpret(release("v0.3.0\n"), "0.1.0") == {"update": False}, "tag newline")
    expect(interpret(release("v0.2.0"), "nope") is None, "bad current")
    expect(interpret([], "0.1.0") is None, "payload not an object")
    flagged = interpret(release("v0.3.0-rc.1"), "0.2.0")
    expect(
        isinstance(flagged, dict) and flagged.get("update") is True,
        "unflagged newer prerelease tag still compares, got %r" % (flagged,),
    )

    # 接口给的 html_url 不采用：页面地址只由 tag 拼出来，避免跳到仓库外。
    poisoned = release("v0.3.0")
    poisoned["html_url"] = "https://evil.example/phish"
    opened = interpret(poisoned, "0.2.0")
    expect(
        isinstance(opened, dict)
        and opened.get("url") == "https://github.com/sanjings/ai-quota-bar/releases/tag/v0.3.0",
        "ignore foreign html_url, got %r" % (opened,),
    )

    if failures:
        for item in failures:
            sys.stderr.write(item + "\n")
        return 1
    sys.stdout.write("release_check self-test ok\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
