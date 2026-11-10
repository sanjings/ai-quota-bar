#!/bin/bash
# 取数入口（被 native/AI Quota Bar.app 调用，参数为 --json）。
#
# 找到可用的 python3，再把参数透传给 lib/report.py。
# --check-release / --stage-release / --apply-update 例外：转给 lib/release_check.py。
# 问失败或还不能安装时它打印一行 JSON，不走额度输出。
# 之所以需要这一层而非直接调用 python3：GUI 程序由 launchd 之类的方式拉起时
# PATH 与终端不同，不能依赖 `env python3`，必须显式探测解释器位置。
#
# 注：文件名与早期的 SwiftBar 方案一致（"1m" 曾表示刷新间隔），当前
# 刷新间隔由 App 的 refreshSeconds 控制，与文件名无关。

set -u

# 解析真实路径——若本文件通过软链被调用，需要还原到项目目录，
# 否则无法定位同级的 lib/report.py。
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
    LINK_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    case "$SOURCE" in
        /*) ;;
        *) SOURCE="$LINK_DIR/$SOURCE" ;;
    esac
done
BASE_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

PYTHON=""
for candidate in \
    /usr/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3
do
    if [ -x "$candidate" ]; then
        PYTHON="$candidate"
        break
    fi
done

# 版本检查和额度取数分开。没有 python 时也只回一行 JSON，避免菜单把
# 额度错误文案当成版本结果。
case "${1:-}" in
    --check-release|--stage-release|--apply-update)
    if [ -z "$PYTHON" ]; then
        printf '%s\n' '{"ok": false}'
        # 替换要靠退出码告诉菜单「别退出」。检查和下载失败则靠 JSON。
        if [ "$1" = "--apply-update" ]; then
            exit 1
        fi
        exit 0
    fi
    exec "$PYTHON" "$BASE_DIR/lib/release_check.py" "$@"
    # exec 失败才会走到这里（例如脚本被删）。不要落回额度取数。
    printf '%s\n' '{"ok": false}'
    if [ "$1" = "--apply-update" ]; then
        exit 1
    fi
    exit 0
    ;;
esac

if [ -z "$PYTHON" ]; then
    printf '额度 -- | color=#A32D2D\n'
    printf '%s\n' '---'
    printf '未找到可用的 python3 | color=#A32D2D\n'
    printf '请安装 Xcode 命令行工具或 Homebrew python | size=12 color=#888780\n'
    exit 0
fi

exec "$PYTHON" "$BASE_DIR/lib/report.py" "$@"
