#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对比当前构建和 GitHub 上最新的正式 Release，并在明确要求时准备安装。

由 quota.1m.sh 调用。stdout 只打一行 JSON。

  --check-release <版本>
  {"ok": true, "update": false}
  {"ok": true, "update": true, "tag": "v0.3.0", "url": "https://github.com/...",
   "zip": "https://github.com/.../AI-Quota-Bar-0.3.0-macos.zip",
   "sha256": "https://github.com/.../AI-Quota-Bar-0.3.0-macos.zip.sha256"}
  {"ok": false}

  --stage-release <zip-url> <sha-url>
  下载 zip，用同一次 Release 上的 .sha256 校验并解压到临时目录。不改安装目录。
  {"ok": true, "root": "/var/.../quota-staged-.../AI-Quota-Bar-0.3.0-macos"}
  {"ok": false, "error": "校验和不匹配。没有改动当前安装。"}

  --apply-update <安装目录> <root> [--wait-pid 旧进程] [--relaunch]
  替换安装目录里的程序文件，保留 config.json 和 cache。退出码 0 表示换完。

ok 为 false 表示这次没问成（网络、限流、返回异常、当前版本无法解析）。
调用方不要挡住额度展示。预发布和草稿不提示，也不给出安装包地址。
校验或解压失败时不会调用替换。
"""

import hashlib
import hmac
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = "sanjings/ai-quota-bar"
API_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
# 正式 Release 的 JSON 目前只有几 KB。设上限是为了读响应时能停下来。
MAX_BODY = 256 * 1024
TIMEOUT_SECONDS = 8
DOWNLOAD_TIMEOUT = 60
# 当前正式包大约 300KB。上限只为了在读响应时能停下来。
MAX_ZIP = 64 * 1024 * 1024
MAX_SHA = 4096
APP_DIR = "AI Quota Bar.app"
# shortcut: 只跟随这些 GitHub 下载域名。换 CDN 时要补主机名，否则下载失败且不替换安装。
_DOWNLOAD_HOSTS = frozenset([
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "objects-origin.githubusercontent.com",
])
_DOWNLOAD_PREFIX = "/sanjings/ai-quota-bar/releases/download/"
_PRESERVE = {"config.json", "cache"}
_MACHO = (
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
)

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


def _safe_token(text, limit):
    if not isinstance(text, str) or not 1 <= len(text) <= limit:
        return False
    if ".." in text:
        return False
    return all(ch in _TAG_ALPHABET for ch in text)


def safe_tag(tag):
    """菜单标题和 Release 路径只用这种 tag，避免把接口原文直接拼进 URL。"""
    return _safe_token(tag, 41)


def release_page(tag):
    quoted = urllib.parse.quote(tag, safe="")
    return "https://github.com/%s/releases/tag/%s" % (REPO, quoted)


class UpdateError(Exception):
    """给用户看的一句中文。安装目录还没被改。"""


def zip_filename(tag):
    """与 native/package.sh 的文件名一致：去掉一个 v 前缀，保留其余部分。"""
    if not safe_tag(tag):
        return None
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    if not version or not safe_tag(version):
        return None
    return "AI-Quota-Bar-%s-macos.zip" % version


def parse_asset_url(url):
    """只接受本仓库 Release 下载地址。返回 (tag, 文件名)，否则 None。"""
    if not isinstance(url, str) or len(url) > 512:
        return None
    parts = urllib.parse.urlparse(url)
    if parts.scheme != "https" or parts.hostname != "github.com":
        return None
    if parts.username or parts.password or parts.port not in (None, 443):
        return None
    if parts.query or parts.fragment:
        return None
    path = urllib.parse.unquote(parts.path)
    if not path.startswith(_DOWNLOAD_PREFIX):
        return None
    rest = path[len(_DOWNLOAD_PREFIX):]
    if rest.count("/") != 1:
        return None
    tag, name = rest.split("/", 1)
    # 文件名比 tag 长（多了 AI-Quota-Bar- 和 -macos.zip.sha256）。
    if not safe_tag(tag) or not _safe_token(name, 96):
        return None
    return tag, name


def package_urls(document, tag):
    """从 Release JSON 里挑出与 tag 对应的 zip 和 .sha256。对不上就当没有安装包。"""
    expected = zip_filename(tag)
    if expected is None or not isinstance(document, dict):
        return None
    assets = document.get("assets")
    if not isinstance(assets, list):
        return None
    found = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if name == expected:
            found["zip"] = url
        elif name == expected + ".sha256":
            found["sha256"] = url
    zip_url = found.get("zip")
    sha_url = found.get("sha256")
    if not isinstance(zip_url, str) or not isinstance(sha_url, str):
        return None
    if parse_asset_url(zip_url) != (tag, expected):
        return None
    if parse_asset_url(sha_url) != (tag, expected + ".sha256"):
        return None
    return zip_url, sha_url


def _check_transport(url, allowed_hosts):
    parts = urllib.parse.urlparse(url)
    host = parts.hostname
    if host not in allowed_hosts:
        raise UpdateError("下载地址不被允许。没有改动当前安装。")
    local = host in ("127.0.0.1", "localhost")
    if parts.scheme != ("http" if local else "https"):
        raise UpdateError("下载地址不被允许。没有改动当前安装。")
    if parts.username or parts.password:
        raise UpdateError("下载地址不被允许。没有改动当前安装。")
    if local:
        if parts.port is None:
            raise UpdateError("下载地址不被允许。没有改动当前安装。")
    elif parts.port not in (None, 443):
        raise UpdateError("下载地址不被允许。没有改动当前安装。")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_transport(newurl, self.allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, dest, limit, allowed_hosts, disable_proxy=False):
    """把响应写到 dest。超过 limit、跳到名单外的主机，都留下不完整文件并抛错。"""
    _check_transport(url, allowed_hosts)
    handlers = [_GuardedRedirect(allowed_hosts)]
    if disable_proxy:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "AI-Quota-Bar", "Accept": "*/*"},
    )
    try:
        response = opener.open(request, timeout=DOWNLOAD_TIMEOUT)
    except UpdateError:
        raise
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise UpdateError("下载失败。没有改动当前安装。")
    with response:
        _check_transport(response.geturl(), allowed_hosts)
        if response.status != 200:
            raise UpdateError("下载失败（HTTP %s）。没有改动当前安装。" % response.status)
        remaining = limit
        with open(dest, "wb") as handle:
            while True:
                chunk = response.read(min(65536, remaining + 1))
                if not chunk:
                    break
                if len(chunk) > remaining:
                    raise UpdateError("安装包过大。没有改动当前安装。")
                remaining -= len(chunk)
                handle.write(chunk)


def parse_sha256(text, zip_name):
    """读取 shasum -a 256 的一行。文件名必须就是这个 zip。"""
    if not isinstance(text, str) or "\x00" in text:
        return None
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    if len(lines) != 1:
        return None
    parts = lines[0].split()
    digest = parts[0].lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return None
    if len(parts) == 1:
        return digest
    if len(parts) != 2:
        return None
    name = parts[1][1:] if parts[1].startswith("*") else parts[1]
    if name != zip_name:
        return None
    return digest


def assert_zip_members(zip_path):
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise UpdateError("解压失败。没有改动当前安装。")
    with archive:
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            if not name or name.startswith("/"):
                raise UpdateError("安装包路径异常。没有改动当前安装。")
            parts = [part for part in name.split("/") if part not in ("", ".")]
            if any(part == ".." for part in parts):
                raise UpdateError("安装包路径异常。没有改动当前安装。")
            mode = info.external_attr >> 16
            if (mode & 0o170000) == 0o120000:
                raise UpdateError("安装包含有链接，已拒绝。没有改动当前安装。")


def extract_zip(zip_path, dest):
    if os.path.exists("/usr/bin/ditto"):
        result = subprocess.run(
            ["/usr/bin/ditto", "-x", "-k", zip_path, dest],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise UpdateError("解压失败。没有改动当前安装。")
        return
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(dest)
        for info in archive.infolist():
            if info.is_dir():
                continue
            unix = (info.external_attr >> 16) & 0o7777
            if not unix:
                continue
            target = os.path.join(dest, info.filename)
            if os.path.isfile(target) and not os.path.islink(target):
                os.chmod(target, stat.S_IMODE(unix))


def assert_contained(dest):
    root = os.path.realpath(dest)
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise UpdateError("安装包含有链接，已拒绝。没有改动当前安装。")
            real = os.path.realpath(path)
            if real != root and not real.startswith(root + os.sep):
                raise UpdateError("安装包路径异常。没有改动当前安装。")


def valid_layout(root):
    binary = os.path.join(root, APP_DIR, "Contents", "MacOS", "ai-quota-bar")
    script = os.path.join(root, "quota.1m.sh")
    report = os.path.join(root, "lib", "report.py")
    return os.path.isfile(binary) and os.path.isfile(script) and os.path.isfile(report)


def locate_root(dest):
    names = [
        name for name in os.listdir(dest)
        if name != "__MACOSX" and not name.startswith(".")
    ]
    if len(names) == 1:
        only = os.path.join(dest, names[0])
        if os.path.isdir(only) and not os.path.islink(only) and valid_layout(only):
            return only
    if valid_layout(dest):
        return dest
    raise UpdateError("安装包内容不完整。没有改动当前安装。")


def verify_signature(root):
    """ad-hoc 签名只能证明包是完整的，不能证明是谁签的。信任来自 .sha256。"""
    if not os.path.exists("/usr/bin/codesign"):
        return
    binary = os.path.join(root, APP_DIR, "Contents", "MacOS", "ai-quota-bar")
    with open(binary, "rb") as handle:
        magic = handle.read(4)
    if magic not in _MACHO:
        raise UpdateError("安装包内容不完整。没有改动当前安装。")
    app = os.path.join(root, APP_DIR)
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", app],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        raise UpdateError("安装包签名校验没有通过。没有改动当前安装。")


def stage_archive(zip_path, sha_text):
    """校验并解压。返回临时目录里的发布根。不碰安装目录。"""
    zip_name = os.path.basename(zip_path)
    expected = parse_sha256(sha_text, zip_name)
    if expected is None:
        raise UpdateError("校验文件无法读取。没有改动当前安装。")
    digest = hashlib.sha256()
    with open(zip_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if not hmac.compare_digest(digest.hexdigest(), expected):
        raise UpdateError("校验和不匹配。没有改动当前安装。")
    unpack = tempfile.mkdtemp(prefix="quota-unpack-")
    parent = None
    try:
        assert_zip_members(zip_path)
        extract_zip(zip_path, unpack)
        assert_contained(unpack)
        root = locate_root(unpack)
        parent = tempfile.mkdtemp(prefix="quota-staged-")
        staged = os.path.join(parent, os.path.basename(root))
        shutil.move(root, staged)
        return staged
    except UpdateError:
        if parent is not None:
            shutil.rmtree(parent, ignore_errors=True)
        raise
    except Exception:
        if parent is not None:
            shutil.rmtree(parent, ignore_errors=True)
        raise UpdateError("解压失败。没有改动当前安装。")
    finally:
        shutil.rmtree(unpack, ignore_errors=True)


def stage_release(zip_url, sha_url):
    parsed_zip = parse_asset_url(zip_url)
    parsed_sha = parse_asset_url(sha_url)
    if parsed_zip is None or parsed_sha is None:
        raise UpdateError("下载地址不被允许。没有改动当前安装。")
    zip_tag, zip_name = parsed_zip
    sha_tag, sha_name = parsed_sha
    expected = zip_filename(zip_tag)
    if (
        expected is None
        or zip_name != expected
        or sha_tag != zip_tag
        or sha_name != expected + ".sha256"
    ):
        raise UpdateError("下载地址不被允许。没有改动当前安装。")
    work = tempfile.mkdtemp(prefix="quota-update-")
    try:
        zip_path = os.path.join(work, zip_name)
        sha_path = os.path.join(work, sha_name)
        download(zip_url, zip_path, MAX_ZIP, _DOWNLOAD_HOSTS)
        download(sha_url, sha_path, MAX_SHA, _DOWNLOAD_HOSTS)
        with open(sha_path, "r", encoding="utf-8", errors="replace") as handle:
            sha_text = handle.read(MAX_SHA + 1)
        if len(sha_text) > MAX_SHA:
            raise UpdateError("校验文件异常。没有改动当前安装。")
        staged = stage_archive(zip_path, sha_text)
        try:
            verify_signature(staged)
        except Exception:
            cleanup_stage(staged)
            raise
        return staged
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _restore(install, backup, moved):
    for name in reversed(moved):
        dst = os.path.join(install, name)
        if os.path.lexists(dst):
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            else:
                os.remove(dst)
        saved = os.path.join(backup, name)
        if os.path.lexists(saved):
            shutil.move(saved, dst)


def apply_update(install, staged):
    """用已校验的目录替换安装内容。config.json 和 cache 保持原文件。"""
    install = os.path.realpath(install)
    staged = os.path.realpath(staged)
    if not os.path.isdir(install) or not os.path.isdir(staged):
        raise UpdateError("找不到安装目录。没有改动当前安装。")
    if install == staged or staged.startswith(install + os.sep) or install.startswith(staged + os.sep):
        raise UpdateError("安装路径异常。没有改动当前安装。")
    if not valid_layout(staged):
        raise UpdateError("安装包内容不完整。没有改动当前安装。")
    current_app = os.path.isdir(os.path.join(install, APP_DIR))
    current_script = os.path.isfile(os.path.join(install, "quota.1m.sh"))
    if not current_app and not current_script:
        raise UpdateError("找不到当前安装目录。没有改动当前安装。")
    names = []
    for name in sorted(os.listdir(staged)):
        if name in _PRESERVE or name.startswith("."):
            continue
        names.append(name)
    backup = tempfile.mkdtemp(prefix="quota-update-backup-")
    moved = []
    try:
        for name in names:
            src = os.path.join(staged, name)
            if os.path.islink(src):
                raise UpdateError("安装包含有链接，已拒绝。没有改动当前安装。")
            dst = os.path.join(install, name)
            if os.path.lexists(dst):
                shutil.move(dst, os.path.join(backup, name))
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
            moved.append(name)
        shipped = os.path.join(staged, "config.json")
        if (
            not os.path.exists(os.path.join(install, "config.json"))
            and os.path.isfile(shipped)
            and not os.path.islink(shipped)
        ):
            shutil.copy2(shipped, os.path.join(install, "config.json"))
    except UpdateError:
        _restore(install, backup, moved)
        shutil.rmtree(backup, ignore_errors=True)
        raise
    except Exception:
        _restore(install, backup, moved)
        shutil.rmtree(backup, ignore_errors=True)
        raise UpdateError("替换安装文件失败，已恢复原来的文件。")
    else:
        shutil.rmtree(backup, ignore_errors=True)


def cleanup_stage(staged):
    if not staged:
        return
    staged = os.path.realpath(staged)
    parent = os.path.dirname(staged)
    temp = os.path.realpath(tempfile.gettempdir())
    if os.path.basename(parent).startswith("quota-staged-") and parent.startswith(temp + os.sep):
        shutil.rmtree(parent, ignore_errors=True)


def wait_until_dead(pid, timeout):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    deadline = time.time() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def detach():
    """离开菜单栏进程的生命周期，这样替换 .app 时父进程可以先退出。

    先等子进程调用 setsid，再让原进程退出。菜单看到退出码后才会结束自己。
    """
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    if devnull > 2:
        os.close(devnull)


def relaunch_app(install):
    app = os.path.join(install, APP_DIR)
    if not os.path.isdir(app) or not os.path.exists("/usr/bin/open"):
        return
    if os.path.exists("/usr/bin/xattr"):
        subprocess.run(
            ["/usr/bin/xattr", "-dr", "com.apple.quarantine", app],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    # -n：同一 bundle id 已有别的副本在跑时，仍打开这一份。
    subprocess.run(
        ["/usr/bin/open", "-n", app],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stage_cli(argv):
    if len(argv) != 2:
        emit({"ok": False, "error": "参数不对。没有改动当前安装。"})
        return 0
    try:
        root = stage_release(argv[0], argv[1])
    except UpdateError as exc:
        emit({"ok": False, "error": str(exc)})
        return 0
    except Exception:
        emit({"ok": False, "error": "更新准备失败。没有改动当前安装。"})
        return 0
    emit({"ok": True, "root": root})
    return 0


def apply_cli(argv):
    relaunch = False
    wait_pid = None
    positional = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--relaunch":
            relaunch = True
        elif arg == "--wait-pid":
            index += 1
            if index >= len(argv):
                return 1
            wait_pid = argv[index]
        else:
            positional.append(arg)
        index += 1
    if len(positional) != 2:
        return 1
    install, staged = positional
    if wait_pid is not None:
        detach()
        if not wait_until_dead(wait_pid, 60):
            return 1
    try:
        apply_update(install, staged)
    except UpdateError:
        if relaunch:
            relaunch_app(install)
        return 1
    if relaunch:
        relaunch_app(install)
    cleanup_stage(staged)
    return 0


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
    page = {"update": True, "tag": tag, "url": release_page(tag)}
    packaged = package_urls(document, tag)
    if packaged is not None:
        page["zip"], page["sha256"] = packaged
    return page


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
    if argv[:1] == ["--stage-release"]:
        return stage_cli(argv[1:])
    if argv[:1] == ["--apply-update"]:
        return apply_cli(argv[1:])
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
        if "zip" in decision:
            payload["zip"] = decision["zip"]
            payload["sha256"] = decision["sha256"]
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

    _test_update_flow(expect)

    if failures:
        for item in failures:
            sys.stderr.write(item + "\n")
        return 1
    sys.stdout.write("release_check self-test ok\n")
    return 0


def _asset_doc(tag, zip_url, sha_url, zip_name=None):
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    name = zip_name or ("AI-Quota-Bar-%s-macos.zip" % version)
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": name, "browser_download_url": zip_url},
            {"name": name + ".sha256", "browser_download_url": sha_url},
        ],
    }


def _raises_update(fn):
    try:
        fn()
    except UpdateError:
        return True
    return False


def _write_release_tree(root, marker):
    binary = os.path.join(root, APP_DIR, "Contents", "MacOS")
    os.makedirs(binary)
    os.makedirs(os.path.join(root, "lib"))
    with open(os.path.join(binary, "ai-quota-bar"), "wb") as handle:
        handle.write(marker)
    with open(os.path.join(root, "quota.1m.sh"), "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    with open(os.path.join(root, "lib", "report.py"), "w", encoding="utf-8") as handle:
        handle.write("# %s\n" % marker.decode("ascii", "replace"))
    with open(os.path.join(root, "config.json"), "w", encoding="utf-8") as handle:
        handle.write('{"from": "package"}\n')


def _zip_release(marker):
    import io

    folder = "AI-Quota-Bar-9.9.9-macos"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        payload = {
            folder + "/AI Quota Bar.app/Contents/MacOS/ai-quota-bar": marker,
            folder + "/quota.1m.sh": b"#!/bin/sh\n",
            folder + "/lib/report.py": b"# new\n",
            folder + "/config.json": b'{"from": "package"}\n',
            folder + "/INSTALL.txt": b"new\n",
        }
        for name, data in payload.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o755 if name.endswith(("ai-quota-bar", "quota.1m.sh")) else 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _test_update_flow(expect):
    good_zip = (
        "https://github.com/sanjings/ai-quota-bar/releases/download/v0.3.0/"
        "AI-Quota-Bar-0.3.0-macos.zip"
    )
    good_sha = good_zip + ".sha256"
    found = interpret(_asset_doc("v0.3.0", good_zip, good_sha), "0.2.0")
    expect(
        isinstance(found, dict)
        and found.get("zip") == good_zip
        and found.get("sha256") == good_sha,
        "asset urls, got %r" % (found,),
    )
    evil = _asset_doc("v0.3.0", "https://evil.example/a.zip", good_sha)
    rejected = interpret(evil, "0.2.0")
    expect(
        isinstance(rejected, dict) and "zip" not in rejected and rejected.get("update") is True,
        "foreign asset url is not installable, got %r" % (rejected,),
    )
    mismatched = _asset_doc(
        "v0.3.0",
        good_zip.replace("/v0.3.0/", "/v0.9.9/"),
        good_sha,
    )
    skipped = interpret(mismatched, "0.2.0")
    expect(
        isinstance(skipped, dict) and "zip" not in skipped,
        "asset tag must match release tag, got %r" % (skipped,),
    )
    preview = _asset_doc("v0.3.0", good_zip, good_sha)
    preview["prerelease"] = True
    expect(
        interpret(preview, "0.2.0") == {"update": False},
        "prerelease with assets still skipped",
    )
    expect(
        _raises_update(lambda: stage_release("http://127.0.0.1:9/nope.zip", good_sha)),
        "stage-release rejects non-release urls",
    )
    expect(
        _raises_update(lambda: _check_transport("http://127.0.0.1:9/x", _DOWNLOAD_HOSTS)),
        "production hosts do not include the test server",
    )
    expect(parse_sha256("abc  AI-Quota-Bar-0.2.0-macos.zip", "AI-Quota-Bar-0.2.0-macos.zip") is None, "short digest")
    digest = "a" * 64
    expect(
        parse_sha256(digest + "  AI-Quota-Bar-0.2.0-macos.zip", "AI-Quota-Bar-0.2.0-macos.zip") == digest,
        "shasum line",
    )
    expect(
        parse_sha256(digest + "  other.zip", "AI-Quota-Bar-0.2.0-macos.zip") is None,
        "sha filename must match the zip",
    )

    try:
        _test_stage_and_apply(expect)
    except Exception as exc:
        expect(False, "stage/apply raised %r" % (exc,))

    if os.path.exists("/usr/bin/codesign"):
        try:
            _test_fake_macho(expect)
        except Exception as exc:
            expect(False, "signature check raised %r" % (exc,))


def _test_stage_and_apply(expect):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    marker = b"new-binary"
    blob = _zip_release(marker)
    digest = hashlib.sha256(blob).hexdigest()
    sha_line = digest + "  AI-Quota-Bar-9.9.9-macos.zip\n"
    routes = {
        "/ok.zip": (200, blob),
        "/bounce": (302, b""),
        "/big": (200, b"x" * 80),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/bounce":
                self.send_response(302)
                self.send_header("Location", "https://evil.example/nope")
                self.end_headers()
                return
            status, body = routes.get(self.path, (404, b""))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = "http://127.0.0.1:%d" % server.server_address[1]
    local_hosts = _DOWNLOAD_HOSTS | {"127.0.0.1"}
    root = tempfile.mkdtemp(prefix="quota-update-test-")
    try:
        try:
            bounced = os.path.join(root, "bounced")
            expect(
                _raises_update(lambda: download(host + "/bounce", bounced, 100, local_hosts, disable_proxy=True)),
                "redirect to another host is refused",
            )
            expect(not os.path.exists(bounced), "refused redirect does not leave a file")
            big = os.path.join(root, "big")
            expect(
                _raises_update(lambda: download(host + "/big", big, 32, local_hosts, disable_proxy=True)),
                "oversize download stops",
            )
            fetched = os.path.join(root, "fetched.zip")
            download(host + "/ok.zip", fetched, MAX_ZIP, local_hosts, disable_proxy=True)
            with open(fetched, "rb") as handle:
                expect(handle.read() == blob, "download writes the zip bytes")

            install = os.path.join(root, "install")
            os.makedirs(install)
            _write_release_tree(install, b"old-binary")
            with open(os.path.join(install, "config.json"), "w", encoding="utf-8") as handle:
                handle.write('{"keep": true, "note": "user"}\n')
            cache = os.path.join(install, "cache")
            os.makedirs(cache)
            with open(os.path.join(cache, "state.json"), "w", encoding="utf-8") as handle:
                handle.write("{}\n")
            with open(os.path.join(install, "notes.txt"), "w", encoding="utf-8") as handle:
                handle.write("mine\n")
            before = open(os.path.join(install, APP_DIR, "Contents", "MacOS", "ai-quota-bar"), "rb").read()

            zip_path = os.path.join(root, "AI-Quota-Bar-9.9.9-macos.zip")
            with open(zip_path, "wb") as handle:
                handle.write(blob)
            expect(
                _raises_update(lambda: stage_archive(zip_path, ("0" * 64) + "  AI-Quota-Bar-9.9.9-macos.zip\n")),
                "bad checksum raises",
            )
            after = open(os.path.join(install, APP_DIR, "Contents", "MacOS", "ai-quota-bar"), "rb").read()
            expect(after == before, "bad checksum does not replace the install")

            slip = os.path.join(root, "slip.zip")
            import io
            slip_buf = io.BytesIO()
            with zipfile.ZipFile(slip_buf, "w") as archive:
                archive.writestr("../outside.txt", "nope")
            with open(slip, "wb") as handle:
                handle.write(slip_buf.getvalue())
            slip_sha = hashlib.sha256(slip_buf.getvalue()).hexdigest() + "  slip.zip\n"
            expect(_raises_update(lambda: stage_archive(slip, slip_sha)), "zip slip rejected")
            expect(not os.path.exists(os.path.join(root, "outside.txt")), "zip slip did not write outside")

            staged = stage_archive(zip_path, sha_line)
            try:
                apply_update(install, staged)
            finally:
                cleanup_stage(staged)
            replaced = open(os.path.join(install, APP_DIR, "Contents", "MacOS", "ai-quota-bar"), "rb").read()
            config = open(os.path.join(install, "config.json"), encoding="utf-8").read()
            note = open(os.path.join(install, "notes.txt"), encoding="utf-8").read()
            cache_text = open(os.path.join(install, "cache", "state.json"), encoding="utf-8").read()
            report = open(os.path.join(install, "lib", "report.py"), encoding="utf-8").read()
            expect(replaced == marker, "apply replaces the binary")
            expect(config == '{"keep": true, "note": "user"}\n', "apply keeps config.json, got %r" % (config,))
            expect(note == "mine\n" and cache_text == "{}\n", "apply keeps user files and cache")
            expect(report == "# new\n", "apply replaces lib")
            expect(os.path.isfile(os.path.join(install, "INSTALL.txt")), "apply adds files from the package")
        finally:
            server.shutdown()
            server.server_close()
        # 等 HTTP 线程停掉再 fork，避免子进程卡在别人持有的锁上。
        _test_wait_pid(expect, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _test_wait_pid(expect, root):
    import signal
    import subprocess

    install = os.path.join(root, "wait-install")
    staged = os.path.join(root, "wait-staged")
    os.makedirs(install)
    _write_release_tree(install, b"old-binary")
    _write_release_tree(staged, b"waited")
    sleeper = subprocess.Popen(["sleep", "60"])
    proc = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--apply-update",
            install,
            staged,
            "--wait-pid",
            str(sleeper.pid),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    binary = os.path.join(install, APP_DIR, "Contents", "MacOS", "ai-quota-bar")
    try:
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            expect(False, "apply-update --wait-pid did not detach")
            return
        expect(code == 0, "detach parent should exit 0, got %s" % code)
        time.sleep(0.4)
        expect(open(binary, "rb").read() == b"old-binary", "does not replace while the old pid is alive")
        os.kill(sleeper.pid, signal.SIGTERM)
        sleeper.wait(timeout=5)
        deadline = time.time() + 15
        while time.time() < deadline:
            if open(binary, "rb").read() == b"waited":
                break
            time.sleep(0.2)
        else:
            expect(False, "apply did not run after the old pid exited")
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)
        # 等脱离的子进程把这一小份目录写完，外面的清理才不会和它抢文件。
        time.sleep(0.5)


def _test_fake_macho(expect):
    root = tempfile.mkdtemp(prefix="quota-macho-")
    try:
        _write_release_tree(root, b"\xcf\xfa\xed\xfe" + b"\x00" * 32)
        expect(
            _raises_update(lambda: verify_signature(root)),
            "truncated macho is not installed",
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
