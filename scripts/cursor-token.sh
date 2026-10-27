#!/bin/bash
# 配置 Cursor token 到 macOS Keychain。
#
# 为什么需要这一步：Cursor 的额度接口要求订阅登录态（Bearer token），
# 而这个 token 只有 Cursor 客户端自己知道。本程序不会去读取或解密 Cursor
# 的内部存储，所以需要你手动取一次、交给它保管在自己的 Keychain 条目里。
#
# 用法：
#   bash scripts/cursor-token.sh          # 配置 / 更新
#   bash scripts/cursor-token.sh --check  # 只检查当前配置是否可用
#
# token 有效期约 2 个月；在 Cursor 里退出登录/重新登录会让它提前失效。

set -u

SERVICE="cursor-access-token"
BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="/usr/bin/python3"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "本脚本只支持 macOS（依赖 security 命令读写 Keychain）。" >&2
    exit 1
fi

# ---------------------------------------------------------------- 检查模式
check_only=0
[ "${1:-}" = "--check" ] && check_only=1

verify() {
    "$PY" - "$SERVICE" "$BASE_DIR" <<'PY'
import base64, json, os, subprocess, sys, time, urllib.error, urllib.request

service, base_dir = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(base_dir, "lib"))
try:
    import report  # 复用同一套读取与请求逻辑，避免两处实现不一致
except Exception as exc:
    print("  无法加载 lib/report.py：%s" % exc)
    sys.exit(2)

try:
    token = report.read_cursor_token()
except Exception as exc:
    print("  读取失败：%s" % exc)
    sys.exit(2)

expiry = report.token_expiry(token)
if expiry:
    left = expiry - time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(expiry))
    print("  有效期至 %s（还剩 %d 天）" % (day, max(int(left // 86400), 0)))
else:
    print("  无法解析有效期（token 可能不是 JWT，仍会尝试请求）")

try:
    data = report.http_json(
        report.CURSOR_USAGE_URL, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Connect-Protocol-Version": "1"},
        payload={})
except Exception as exc:
    print("  接口验证失败：%s" % exc)
    sys.exit(3)

plan = data.get("planUsage") or {}
print("  ✅ 验证通过")
print("     Cursor Models  %.1f%%" % float(plan.get("autoPercentUsed") or 0.0))
print("     Others Models  %.1f%%" % float(plan.get("apiPercentUsed") or 0.0))
if plan.get("totalPercentUsed") is not None:
    print("     合计           %.1f%%" % float(plan["totalPercentUsed"]))
PY
}

if [ "$check_only" -eq 1 ]; then
    echo "检查 $SERVICE ..."
    verify
    exit $?
fi

# ---------------------------------------------------------------- 取 token 指引
cat <<'TEXT'
配置 Cursor token
=================

1) 在 Cursor 里按 ⌘⇧P 打开命令面板，输入 Toggle Developer Tools，回车
2) 切到 Network 面板，过滤框输入 cursor.sh
3) 触发一次请求：点右上角齿轮打开 Cursor Settings（或点左下角账户头像），
   直到列表里出现 api2.cursor.sh
4) 点开它 → Headers → Request Headers → 复制这一整行：

       authorization: Bearer eyJhbGciOi...

   只复制 token 本身也可以（以 eyJ 开头）；带 "Bearer " 前缀同样能识别。
5) 看不到请求时：命令面板执行 Reload Window 后重试

注意：token 等同于你的 Cursor 登录凭证，只应保存在本机 Keychain，
不要贴到聊天、issue 或任何公开位置。

TEXT

printf "粘贴 token（不回显）后回车："
IFS= read -rs TOKEN
echo
if [ -z "${TOKEN}" ]; then
    echo "没有读到内容。" >&2
    exit 1
fi

# 先校验、通过才写入。失败不碰 Keychain，避免覆盖掉当前仍可用的 token。
echo "正在验证（尚未写入 Keychain）..."
verify_out="$(printf '%s' "$TOKEN" | "$PY" "$BASE_DIR/lib/report.py" --verify-cursor-token)" || true
if ! printf '%s' "$verify_out" | "$PY" -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.stderr.write("验证脚本没有返回结果\n")
    sys.exit(1)
if not data.get("ok"):
    sys.stderr.write((data.get("error") or "验证失败") + "\n")
    sys.exit(1)
if data.get("summary"):
    print(data["summary"])
'; then
    echo "验证未通过，未写入 Keychain。" >&2
    unset TOKEN
    exit 1
fi

echo "验证通过，正在写入 Keychain ..."
if ! printf '%s' "$TOKEN" | "$PY" "$BASE_DIR/lib/report.py" --store-keychain "$SERVICE"; then
    echo "写入 Keychain 失败。原有条目未改动。" >&2
    unset TOKEN
    exit 1
fi
unset TOKEN

echo
echo "完成。菜单栏会在下一次刷新时显示 Cursor 额度。"
