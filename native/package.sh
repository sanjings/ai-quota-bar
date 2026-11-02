#!/bin/bash
# 打出可分发的 macOS zip：ad-hoc 签名的 .app，以及同层的取数脚本。
#
# 用法（仓库任意子目录都可以）：
#   bash native/package.sh
#
# 环境变量：
#   APP_VERSION   zip 文件名里的版本，如 0.2.0、v0.2.0、v0.2.0-rc.1。
#                 未设置时用 native/Info.plist 的 CFBundleShortVersionString。
#                 写入 .app 的营销版本会去掉 v 前缀和连字符后缀。
#
# 产物：
#   dist/AI-Quota-Bar-<版本>-macos.zip
#   dist/AI-Quota-Bar-<版本>-macos.zip.sha256
#
# .app 写到临时目录，不覆盖 native/AI Quota Bar.app（那个路径可能正挂在菜单栏上）。
# 签名是 ad-hoc，没有自动更新。Gatekeeper 说明见 zip 里的 INSTALL.txt 和 README。

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NATIVE="$BASE_DIR/native"
APP_NAME="AI Quota Bar"

if [[ -n "${APP_VERSION:-}" ]]; then
    ZIP_VERSION="${APP_VERSION#v}"
else
    ZIP_VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$NATIVE/Info.plist")"
fi
if [[ ! "$ZIP_VERSION" =~ ^[0-9A-Za-z._+-]+$ ]]; then
    echo "APP_VERSION 含无法用于文件名的字符: $ZIP_VERSION" >&2
    exit 1
fi

tmp=""
stage=""
cleanup() {
    if [[ -n "$tmp" ]]; then
        rm -rf "$tmp"
    fi
    if [[ -n "$stage" ]]; then
        rm -rf "$stage"
    fi
}
trap cleanup EXIT

echo "==> 生成 AppIcon.icns"
tmp="$(mktemp -d)"
swiftc -O "$NATIVE/make_icon.swift" -o "$tmp/make_icon"
"$tmp/make_icon"
iconutil -c icns "$NATIVE/AppIcon.iconset" -o "$NATIVE/AppIcon.icns"

echo "==> 组装 $ZIP_VERSION"
stage="$(mktemp -d)"
ROOT_NAME="AI-Quota-Bar-${ZIP_VERSION}-macos"
DEST="$stage/$ROOT_NAME"
mkdir -p "$DEST/lib"

APP_OUTPUT="$DEST/$APP_NAME.app" APP_VERSION="$ZIP_VERSION" bash "$NATIVE/build.sh"

cp "$BASE_DIR/quota.1m.sh" "$DEST/quota.1m.sh"
cp "$BASE_DIR/lib/report.py" "$DEST/lib/report.py"
cp "$BASE_DIR/config.json" "$DEST/config.json"
cp "$BASE_DIR/LICENSE" "$DEST/LICENSE"
cp "$BASE_DIR/SECURITY.md" "$DEST/SECURITY.md"
chmod +x "$DEST/quota.1m.sh"

cat > "$DEST/INSTALL.txt" <<EOF
AI Quota Bar ${ZIP_VERSION}（macOS）

这个目录要整份保留。.app 只负责菜单栏显示，额度数据来自同目录的
quota.1m.sh 和 lib/report.py，配置在同目录的 config.json。
整份文件夹可以放到任意位置，包括「应用程序」下面的子文件夹。
不要只把 AI Quota Bar.app 拖进「应用程序」：单独拷走会找不到脚本，菜单没有数据。

系统要求：macOS 12 或更新，以及本机的 /usr/bin/python3（安装 Xcode 命令行工具后就有）。
GitHub Actions 打出的是 arm64。Intel Mac 请在本机按 README 从源码编译。

打开：
  先做下面的 Gatekeeper 步骤，再双击「AI Quota Bar.app」。

Gatekeeper：
  这个 .app 是 ad-hoc 签名（codesign --sign -），没有 Apple Developer ID，也没有公证。
  从浏览器下载后，系统会加上隔离属性，常见提示是「无法验证开发者」或「已损坏」。
  在本目录打开终端，执行：

    xattr -dr com.apple.quarantine .

  然后双击。也可以在系统设置 → 隐私与安全性里点「仍要打开」。
  右键 → 打开，有时也能过；仍提示已损坏时，用上面的 xattr。

配置与缓存：
  同目录的 config.json 可以改，下一轮刷新生效。
  cache/ 会在第一次取数后出现。
  Cursor / DeepSeek 的凭证写在本机钥匙串里，说明见 SECURITY.md。
  菜单里的配置步骤和源码安装相同，见：
  https://github.com/sanjings/ai-quota-bar/blob/main/README.md

开机自启：
  把本目录里的 AI Quota Bar.app 加进登录项，不要加另一份拷贝。
  macOS 13 及以后：系统设置 → 通用 → 登录项（较新版本里叫「登录项与扩展」）。
  macOS 12：系统偏好设置 → 用户与群组 → 登录项。

更新：
  没有自动更新。下载新的 zip 后，先退出旧进程，再打开新目录里的 .app：

    pkill -f 'AI Quota Bar.app/Contents/MacOS/ai-quota-bar'
EOF

OUT_DIR="$BASE_DIR/dist"
mkdir -p "$OUT_DIR"
ZIP="$OUT_DIR/${ROOT_NAME}.zip"
SHA="$ZIP.sha256"
rm -f "$ZIP" "$SHA"
# 不带 AppleDouble（__MACOSX）。签名在 Mach-O 和 _CodeSignature 里，解压后 codesign --verify 仍然通过。
ditto -c -k --norsrc --keepParent "$DEST" "$ZIP"
(
    cd "$OUT_DIR"
    shasum -a 256 "${ROOT_NAME}.zip" > "${ROOT_NAME}.zip.sha256"
)

echo
echo "==> 完成"
echo "    $ZIP"
cat "$SHA"
du -sh "$ZIP"
