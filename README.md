# AI Quota Bar

macOS 菜单栏常驻小组件，同时显示 **ChatGPT（Codex）订阅额度**、**Cursor 订阅额度双池** 与 **DeepSeek 账户余额**。

```
ChatGPT Plus  Cursor  DeepSeek    ← 菜单栏常驻，标签+数值两行显示
4%/4%         92%/42% ¥2.79
```

点击菜单栏项可展开详情：额度进度条、重置倒计时、DeepSeek 充值/赠送拆分、各服务用量面板直达链接。

---

## 快速开始

```bash
git clone <本仓库>
cd ai-quota-bar
bash native/build.sh          # 编译 + 打包
open native/"AI Quota Bar.app"
```

> 要求：macOS 12+，Xcode 命令行工具（swiftc）与 python3（系统自带即可）。
> App 启动时自动定位仓库内的 `quota.1m.sh`（相对 bundle 推导），
> 也可用环境变量 `AI_QUOTA_BAR_HOME` 指定项目根目录。

**开机自启**：系统设置 → 通用 → 登录项，把 `AI Quota Bar.app` 拖入列表。

**退出**：菜单栏点开 → 任意处按 Esc 收起菜单，或

```bash
pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar"
```

---

## 数据来源

| 项目 | 获取方式 | 凭证 | 说明 |
|---|---|---|---|
| ChatGPT 额度 | **本地解析** `~/.codex/sessions` 会话记录 | 无需（订阅登录态由 Codex CLI 维护） | 无网络请求；数据随 Codex 任务结束更新 |
| Cursor 双池额度 | `api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage` | Cursor 客户端本地 token（自动只读） | 未公开接口，已做容错 |
| DeepSeek 余额 | `api.deepseek.com/user/balance`（官方公开接口） | macOS Keychain 中的 API Key | |

**Cursor 的 token 只读取自 Cursor 自身维护的数据库，本程序不会主动刷新。**
这是有意为之：refresh token 存在轮转机制，若抢先刷新而未同步回 Cursor，
可能导致 Cursor 登录态失效。若接口返回 401，在 Cursor 里激活一次即可恢复。

**ChatGPT 数据的边界**：额度数据（`rate_limits`）只在 Codex 任务结束时写入本地
会话文件，不是实时接口——不用 Codex 时数据停留在最后一次任务；窗口重置时刻
过后由本地推断归零显示。需 ChatGPT 订阅登录 + 官方直连（经代理/路由时该字段为空）。

三个数据源**互相独立**：其中一个失效时，其余照常显示，失效的那项会给出文字提示而非中断整个组件。

---

## 配置

编辑 `config.json`（改完无需重启，下一次刷新自动生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `cursor.enabled` | `true` | 是否显示 Cursor 额度 |
| `codex.enabled` | `true` | 是否显示 ChatGPT 额度 |
| `deepseek.enabled` | `true` | 是否显示 DeepSeek 余额 |
| `deepseek.keychainService` | `deepseek-api-key` | Keychain 条目的服务名 |
| `deepseek.lowBalanceThreshold` | `20.0` | 余额低于此值时预警（标红并给出充值链接） |
| `display.cacheSeconds.fetch` | `300` | 网络类数据源的缓存秒数 |
| `display.cacheSeconds.local` | `20` | 本地类数据源（ChatGPT）的缓存秒数 |
| `display.showBars` | `true` | 下拉菜单中是否显示进度条 |
| `display.barWidth` | `8` | 进度条格数 |
| `thresholds.warn` | `50.0` | 用量达到该百分比转为琥珀色 |
| `thresholds.danger` | `80.0` | 用量达到该百分比转为红色 |

**刷新节奏**：App 每 60 秒调用一次取数脚本；网络类（Cursor/DeepSeek）由 300 秒
缓存挡住不重复发请求，本地类（ChatGPT）每次重读（成本≈0）。新增数据源只需在
`lib/report.py` 中决定归 fetch 还是 local 档。

### 配置 DeepSeek 密钥

密钥存入 macOS Keychain，不落明文文件：

```bash
security add-generic-password -U -s deepseek-api-key -a "$USER" -w
```

命令会提示输入密钥（**输入时不回显**，属正常）。`-U` 表示已存在时覆盖。

未配置时，下拉菜单会显示「未在 Keychain 中配置密钥」，**不影响其余部分正常使用**。

> 提示：DeepSeek 的余额是**账户级**的，同一账号下任意一个 API Key 查到的余额都相同；
> 建议新建一个专用 Key（如命名 `menubar-quota`），便于将来单独撤销而不影响其他服务。

---

## 常见问题

**菜单栏里看不到这个组件**

按顺序排查：

```bash
PROJ="$(pwd)"   # 项目根目录

# 1. 程序是否在运行
pgrep -lf "ai-quota-bar"

# 2. 是否在正常取数（时间应为 2 分钟内）
stat -f "%Sm %N" -t "%H:%M:%S" "$PROJ/cache/state.json"

# 3. 若前两项都正常但依然看不见 → 重启一次通常即可恢复
pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar"
open "$PROJ/native/AI Quota Bar.app"
```

第 3 步能恢复的原因：菜单栏项的位置由系统分配，偶发会分配到屏幕外；
重启会重新申请位置。

**显示 `Cursor 读取失败`**

Cursor 未登录或数据库被占用。打开 Cursor 激活一次，下一轮刷新即恢复。

**显示 `ChatGPT Plus 读取失败`**

常见原因：未安装 Codex CLI / 本机会话中无额度数据。需在 Codex CLI 用
ChatGPT 订阅账号登录并直连官方（走本地代理/路由时额度字段不下发），
跑一次任务后即可显示。

**DeepSeek 显示「未在 Keychain 中配置密钥」**

按上文「配置 DeepSeek 密钥」写入即可。

**数据看起来是旧的**

上游接口请求失败时，程序会降级显示上一次成功的数据（缓存），并在下拉菜单顶部提示
「数据为缓存值（刷新失败）」。检查网络或对应服务的可用性。
ChatGPT 部分停在旧值属正常（见上文「ChatGPT 数据的边界」）。

---

## 目录结构

```
ai-quota-bar/
├── quota.1m.sh              # 取数入口：定位 python3 并调用 report.py
├── lib/report.py            # 取数 + 输出（数据层的全部逻辑）
├── config.json              # 上表的配置项
├── cache/state.json         # 运行时缓存（失败时降级显示用，git 忽略）
├── native/                  # 原生 App
│   ├── main.swift           #   程序主体（脚本定位、定时刷新、菜单构建）
│   ├── QuotaBarView.swift   #   菜单栏自绘视图（外观参数在此）
│   ├── make_icon.swift      #   App 图标生成器（代码绘制，可复现）
│   ├── Info.plist           #   LSUIElement=1（无 Dock 图标）
│   ├── build.sh             #   编译 + 打包
│   └── AI Quota Bar.app     #   应用本体（构建产物，git 忽略）
└── README.md                # 本文件
```

**取数与显示是分离的**：`lib/report.py` 负责取数和阈值判定，原生 App 只负责把结果画出来。
因此调整阈值、容错策略等只改 Python 即可（改完下一次刷新生效，无需重新编译）；
只有改动外观才需要重新编译 Swift。

---

## 改动与卸载

**修改外观**（字体、间距、配色）→ 改 `native/QuotaBarView.swift`，然后：

```bash
bash native/build.sh
```

**修改取数逻辑 / 阈值行为** → 改 `lib/report.py` 或 `config.json`，无需编译。

**重新生成图标** → 改 `native/make_icon.swift`（配色/进度参数在文件末尾），然后：

```bash
swiftc native/make_icon.swift -o /tmp/make_icon && /tmp/make_icon
iconutil -c icns native/AppIcon.iconset -o native/AppIcon.icns
bash native/build.sh
```

**卸载**：

```bash
pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar"
rm -rf <项目目录>
security delete-generic-password -s deepseek-api-key   # 若配置过密钥
# 并从 系统设置 → 通用 → 登录项 中移除 AI Quota Bar
```

---

## 说明

Cursor 的额度接口为**未公开的逆向接口**，可能随上游版本变更而失效；
ChatGPT 的额度数据依赖 Codex CLI 的本地会话记录格式，同样可能随版本变更；
DeepSeek 余额接口为官方公开接口。程序已对字段做容错处理，取不到时降级显示而非崩溃。
