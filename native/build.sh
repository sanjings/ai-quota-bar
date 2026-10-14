#!/bin/bash
# 编译并打包 AI Quota Bar.app
#
# 用法：bash native/build.sh
#
# 产物：native/AI Quota Bar.app
# 说明：ad-hoc 签名（--sign -），仅本机运行；如需分发需换正式证书。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="AI Quota Bar"
BUNDLE="$BASE_DIR/$APP_NAME.app"
BINARY_NAME="ai-quota-bar"

echo "==> 清理旧产物"
rm -rf "$BUNDLE"

echo "==> 创建 bundle 结构"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

echo "==> 编译"
swiftc -O -o "$BUNDLE/Contents/MacOS/$BINARY_NAME" \
    "$BASE_DIR/main.swift" "$BASE_DIR/QuotaBarView.swift" -framework Cocoa

echo "==> 写入 Info.plist"
cp "$BASE_DIR/Info.plist" "$BUNDLE/Contents/Info.plist"

echo "==> 图标"
cp "$BASE_DIR/AppIcon.icns" "$BUNDLE/Contents/Resources/AppIcon.icns"

echo "==> ad-hoc 签名"
codesign --force --deep --sign - "$BUNDLE" 2>&1 | tail -2

echo
echo "==> 完成"
echo "    产物: $BUNDLE"
du -sh "$BUNDLE"
