# 安全说明

这个程序要在你的菜单栏里长期运行，并且需要访问凭证。下面把边界写清楚，
便于你（或审计者）判断是否可以信任它。

## 原则

1. **额度凭证只读你自己配置的 Keychain 条目。** 程序不解密 Cursor 的 token，
   不读取 Keychain 里的 `Cursor Safe Storage`。唯一例外是菜单标题：只读
   `state.vscdb` 中的套餐代码 `stripeMembershipType`（不是 token）。读不到时显示「Cursor」。
2. **不落盘。** token / API Key 只在内存中用于单次请求，不写入 `cache/`、
   不写日志、不写临时文件。
3. **不打印。** 任何输出（菜单栏文本、JSON、错误信息）都不包含凭证内容。
4. **无遥测。** 没有统计、没有崩溃上报、没有第三方 SDK。

## 数据流向

| 数据源 | 目标 | 携带内容 | 频次 |
|---|---|---|---|
| Cursor | `https://api2.cursor.sh/...GetCurrentPeriodUsage` | 你自己配置的 Bearer token | 每 5 分钟（有缓存） |
| DeepSeek | `https://api.deepseek.com/user/balance` | 你自己配置的 API Key | 每 5 分钟（有缓存） |
| ChatGPT | **不联网** | — | 每 60 秒读一次本地文件 |

除此之外没有任何出站请求。

## 凭证存放位置

| 凭证 | Keychain service | 查看 | 删除 |
|---|---|---|---|
| Cursor token | `cursor-access-token` | `security find-generic-password -s cursor-access-token -w` | `security delete-generic-password -s cursor-access-token` |
| DeepSeek key | `deepseek-api-key` | `security find-generic-password -s deepseek-api-key -w` | `security delete-generic-password -s deepseek-api-key` |

Cursor token 与你的登录态等价：拿到它就能读取你的账号用量。它有效期约两个月，
在 Cursor 里退出登录或重新登录会使其提前失效。

## 已知边界与风险

- **未公开接口**：Cursor 与 ChatGPT 的额度数据来自未公开端点或本地文件格式，
  可能随时变更导致失效。这类用法是否被服务条款允许，请自行判断——
  本项目不对此作任何保证，也不鼓励高频轮询。
- **构建产物未签名**：`native/build.sh` 使用 ad-hoc 签名，仅供本机运行。
  若你把 `.app` 分发给他人，对方需自行在系统设置中放行。
- **权限最小化**：程序不需要「完全磁盘访问权限」，仅需能读写自己的配置与
  Keychain 中由你创建的条目。

## 报告问题

发现安全问题请开 issue 或私下联系维护者；请不要在公开 issue 中附带任何真实 token。
