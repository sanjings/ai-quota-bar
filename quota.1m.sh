#!/bin/bash
# 取数入口（被 native/AI Quota Bar.app 调用，参数为 --json）。
#
# 本层只做一件事：找到可用的 python3 并把参数透传给 lib/report.py。
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

if [ -z "$PYTHON" ]; then
    printf '额度 -- | color=#A32D2D\n'
    printf '%s\n' '---'
    printf '未找到可用的 python3 | color=#A32D2D\n'
    printf '请安装 Xcode 命令行工具或 Homebrew python | size=12 color=#888780\n'
    exit 0
fi

exec "$PYTHON" "$BASE_DIR/lib/report.py" "$@"
