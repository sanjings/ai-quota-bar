# 安全说明

这个程序要在你的菜单栏里长期运行，并且需要访问凭证。下面把边界写清楚，
便于你（或审计者）判断是否可以信任它。

## 原则

1. **额度凭证只读你自己配置的 Keychain 条目。** 程序不解密 Cursor 的 token，
   不读取 Keychain 里的 `Cursor Safe Storage`。也不读 Claude Code 的
   `~/.claude/.credentials.json` 或钥匙串条目 `Claude Code-credentials`，
   不刷新 Claude 的登录态。唯一例外是菜单标题：只读
   `state.vscdb` 中的套餐代码 `stripeMembershipType`（不是 token）。读不到时，下拉套餐名显示「Cursor」。菜单栏上的「Cursor / Grok Bot」来自额度接口，不另读本地存储。
2. **不落盘。** token / API Key 只在内存中用于单次请求，不写入 `cache/`、
   不写日志、不写临时文件。
3. **不打印。** 任何输出（菜单栏文本、JSON、错误信息）都不包含凭证内容。
4. **无遥测。** 没有统计、没有崩溃上报、没有第三方 SDK。版本检查和安装包下载只读取 GitHub 上公开的 Release，不上传本机信息。安装前会用同一次 Release 的 `.sha256` 校验，对不上就不替换本地文件。额度预警是本机系统通知，不联网。`cache/notify-ledger.json` 只记录已经提醒过的项目、阈值和周期，不含凭证。

## 数据流向

| 数据源 | 目标 | 携带内容 | 频次 |
|---|---|---|---|
| Cursor 双池 | `https://api2.cursor.sh/...GetCurrentPeriodUsage` | 你自己配置的 Bearer token | 每 5 分钟（有缓存） |
| Grok Bot 周额度 | `https://api2.cursor.sh/...GetSandUsageStatus` | 同一份 Cursor Bearer token | 与 Cursor 同一轮刷新（有缓存）。账号没有这笔额度时菜单不显示 |
| DeepSeek | `https://api.deepseek.com/user/balance` | 你自己配置的 API Key | 每 5 分钟（有缓存） |
| Claude 订阅用量 | `https://claude.ai/api/organizations`，然后 `https://claude.ai/api/organizations/{uuid}/usage` | 你自己配置的 `sessionKey`（Cookie，只发这一条） | 每 5 分钟（有缓存）。请求头还有 `Accept: application/json` 和 `User-Agent: AI-Quota-Bar` |
| ChatGPT | **不联网** | — | 每 60 秒读一次本地文件 |
| 版本检查 | `https://api.github.com/repos/sanjings/ai-quota-bar/releases/latest` | 不带凭证。请求头是 Accept、User-Agent（`AI-Quota-Bar`）和 GitHub API 版本 | 启动时一次，之后每 6 小时。失败则跳过，不弹窗 |
| 安装更新 | 该 Release 上的 `AI-Quota-Bar-<版本>-macos.zip` 与同名 `.sha256`（只跟随 `github.com` 和 GitHub 的发布文件域名） | 不带凭证。User-Agent 仍是 `AI-Quota-Bar` | 仅在设置里打开「允许立即安装」并点「立即更新」之后。校验失败不写安装目录 |

额度接口之外，出站请求只有上面的公开版本查询，以及你明确开始安装时的那两个文件。没有别的出站请求。Claude 这一列不去 `api.anthropic.com`，也不为了探测额度去调用 Messages API。

## 凭证存放位置

| 凭证 | Keychain service | 查看 | 删除 |
|---|---|---|---|
| Cursor token | `cursor-access-token` | `security find-generic-password -s cursor-access-token -w` | `security delete-generic-password -s cursor-access-token` |
| DeepSeek key | `deepseek-api-key` | `security find-generic-password -s deepseek-api-key -w` | `security delete-generic-password -s deepseek-api-key` |
| Claude sessionKey | `claude-session-key` | `security find-generic-password -s claude-session-key -w` | `security delete-generic-password -s claude-session-key` |

Cursor token 与你的登录态等价：拿到它就能读取你的账号用量。它有效期约两个月，
在 Cursor 里退出登录或重新登录会使其提前失效。

Claude 的 `sessionKey` 与 claude.ai 的网页登录态等价：拿到它的人可以在登录过期前使用该会话。
它不是只读的 API Key。退出 claude.ai 或会话过期后失效。程序只在请求里带上这一条 Cookie，不把它写入日志、缓存或命令行参数。

## 已知边界与风险

- **未公开接口**：Cursor、ChatGPT 与 Claude 订阅用量的数据来自未公开端点或本地文件格式，
  可能随时变更导致失效。Claude 这一列读的是用量页的已用百分比，不是官方 Admin API，也没有剩余余额。
  这类用法是否被服务条款允许，请自行判断——
  本项目不对此作任何保证，也不鼓励高频轮询。Claude 与其它网络源一样，默认 5 分钟最多请求一轮。
- **构建产物没有 Developer ID**：`native/build.sh` 与 Release 里的 zip 都是 ad-hoc 签名（`codesign --sign -`），没有公证。自己在本机编译的 `.app` 可以直接打开。从浏览器下载的 zip 带隔离属性，Gatekeeper 会拦截，放行步骤见 README「从 Release 安装」。
- **权限最小化**：程序不需要「完全磁盘访问权限」，仅需能读写自己的配置与
  Keychain 中由你创建的条目。通知权限是可选的：设置里关掉「超过阈值时发出系统通知」后不再申请，也不再发送。

## 报告问题

发现安全问题请开 issue 或私下联系维护者；请不要在公开 issue 中附带任何真实 token。
