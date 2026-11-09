#!/bin/bash
# 编译并打包 AI Quota Bar.app
#
# 用法：bash native/build.sh
# 分发 zip（.app 与取数脚本）：bash native/package.sh
#
# 产物：native/AI Quota Bar.app
# 环境变量：
#   APP_OUTPUT    .app 的输出路径。未设置时写到本脚本旁的 AI Quota Bar.app。
#   APP_VERSION   写入 Info.plist 的版本，如 0.2.0 或 v0.2.0。
#                 带连字符的预发布后缀会去掉（v0.2.0-rc.1 → 0.2.0）。
#                 未设置时沿用 native/Info.plist 里的版本号。
# 说明：ad-hoc 签名（codesign --sign -），没有 Developer ID，也没有公证。
#       本机编译出来的 .app 可以直接打开。从浏览器下载的 zip 会被 Gatekeeper
#       隔离，放行步骤见 README「从 Release 安装」。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="AI Quota Bar"
BUNDLE="${APP_OUTPUT:-$BASE_DIR/$APP_NAME.app}"
BINARY_NAME="ai-quota-bar"

echo "==> 清理旧产物"
rm -rf "$BUNDLE"

echo "==> 创建 bundle 结构"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

echo "==> 编译"
swiftc -O -o "$BUNDLE/Contents/MacOS/$BINARY_NAME" \
    "$BASE_DIR/main.swift" "$BASE_DIR/QuotaBarView.swift" \
    "$BASE_DIR/UpdateCheck.swift" "$BASE_DIR/SettingsConfig.swift" \
    "$BASE_DIR/SettingsWindow.swift" -framework Cocoa

echo "==> 写入 Info.plist"
cp "$BASE_DIR/Info.plist" "$BUNDLE/Contents/Info.plist"
if [[ -n "${APP_VERSION:-}" ]]; then
    MARKETING="${APP_VERSION#v}"
    MARKETING="${MARKETING%%-*}"
    if [[ ! "$MARKETING" =~ ^[0-9]+(\.[0-9]+){0,2}$ ]]; then
        echo "APP_VERSION 需要是数字版本（如 0.2.0 或 v0.2.0），当前: ${APP_VERSION}" >&2
        exit 1
    fi
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $MARKETING" "$BUNDLE/Contents/Info.plist"
    /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $MARKETING" "$BUNDLE/Contents/Info.plist"
    echo "    版本: $MARKETING"
fi

echo "==> 图标"
cp "$BASE_DIR/AppIcon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$BUNDLE" 2>&1 | tail -2

echo
echo "==> 完成"
echo "    产物: $BUNDLE"
du -sh "$BUNDLE"
