# AI Quota Bar

macOS 菜单栏常驻小组件，同时显示 **ChatGPT（Codex）订阅额度**、**Cursor 订阅额度双池**、**Grok Bot 独立周额度** 与 **DeepSeek 账户余额**。Grok Bot 和 Cursor 月度双池是两笔额度，共用同一条 Cursor token。账号有这笔周额度时，菜单栏对应列的标签是 **Cursor / Grok Bot**；没有时标签仍是 **Cursor**。

```
ChatGPT    Cursor / Grok Bot    DeepSeek    ← 菜单栏常驻，标签+数值两行显示
4% / 4%    92% / 42%            ¥2.79
```

斜线两侧是细空格（U+2009）。没有独立 Grok Bot 周额度时，中间列的标签是 `Cursor`，两格百分比不变。

菜单栏上两列百分比（或余额）的含义：

| 标签 | 左边 | 右边 |
|---|---|---|
| ChatGPT | 5 小时额度已用 | 周额度已用 |
| Cursor / Grok Bot（没有独立周额度时标签为 Cursor） | Cursor Models 已用 | Others Models 已用 |
| DeepSeek | 账户余额（不是百分比） | |

Grok Bot 的已用百分比不进菜单栏这两格。这一列的颜色只看 Cursor 双池里较高的那一格；Grok Bot 自己的颜色只出现在下拉那一行。

下拉菜单里，有计费周期数据时，Grok Bot 排在 Cursor 月度「重置倒计时」之后；周期字段缺失时，紧接在 Others Models 后面。先是一行 `Grok Bot` 和百分比（进度条跟双池用同一个开关）。接口给了重置时间时，下一行是 `重置时间  mm/dd HH:MM · 倒计时`。

账号没有这笔独立额度，或这一次没有拿到已用百分比时，这两行不出现，菜单栏标签保持「Cursor」，Cursor 双池照常。菜单上写「Grok Bot」，不写协议名 Sand。下拉菜单里 ChatGPT 区块的标题仍是「ChatGPT Plus」。

点击菜单栏项可展开详情：额度进度条、重置倒计时、DeepSeek 充值/赠送拆分、各服务用量面板直达链接。ChatGPT、Cursor、DeepSeek 三个数据源互相独立，只配自己要用的即可。没配 Cursor token、或本机没有 Codex 会话时，对应那一列会显示「不可用」或读取失败，其余照常，这不是安装失败。

---

## 快速开始

当前发布是 [v0.1.0](https://github.com/sanjings/ai-quota-bar/releases/tag/v0.1.0)（[全部 Release](https://github.com/sanjings/ai-quota-bar/releases)）。该页不附带预编译 `.app`，说明里的「更多细节见 README」指的就是本文件。Release 页上的安装命令是摘要，没有包含生成 `AppIcon.icns`。按本文件的步骤安装。下面的命令都在仓库根目录执行。

`native/AppIcon.icns` 在 `.gitignore` 里，克隆下来没有这个文件。`native/build.sh` 只把它拷进 `.app`，文件不存在时会停在「图标」那一步（`cp: native/AppIcon.icns: No such file or directory`）。第一次构建先生成图标；`native/AppIcon.icns` 已经在时，可以跳过 `swiftc` 和 `iconutil` 两行。

```bash
git clone https://github.com/sanjings/ai-quota-bar.git
cd ai-quota-bar
swiftc native/make_icon.swift -o /tmp/make_icon && /tmp/make_icon
iconutil -c icns native/AppIcon.iconset -o native/AppIcon.icns
bash native/build.sh
open native/"AI Quota Bar.app"
```

`/tmp/make_icon` 按源文件位置把 `AppIcon.iconset` 写到 `native/`。跑完应能看到 `native/AppIcon.iconset`；没有的话，确认当前目录是仓库根。`iconutil` 再打成 `native/AppIcon.icns`。`bash native/build.sh` 用脚本自身所在目录编译，产物是 `native/AI Quota Bar.app`（ad-hoc 签名，本机运行）。

然后在菜单栏点开这一项配置。菜单里的入口会先向对应接口校验，通过才写入 Keychain。命令行里 Cursor 同样先校验；下面这条 DeepSeek 的 `security` 命令不校验，粘错了要等下一轮刷新才看得到失败。服务名、校验规则和两种入口的差别见下文「配置」。

```bash
bash scripts/cursor-token.sh                                         # Cursor，可选
security add-generic-password -U -s deepseek-api-key -a "$USER" -w   # DeepSeek，可选
```

> 要求：macOS 12+（`native/Info.plist` 的 `LSMinimumSystemVersion`）。Xcode 命令行工具提供 `swiftc`、`iconutil` 和 `/usr/bin/python3`。取数脚本 `quota.1m.sh` 按这个顺序找解释器：`/usr/bin/python3`、`/opt/homebrew/bin/python3`、`/usr/local/bin/python3`。`scripts/cursor-token.sh` 固定使用 `/usr/bin/python3`。
> **把 `AI Quota Bar.app` 留在 `native/`。** 可执行文件是 `native/AI Quota Bar.app/Contents/MacOS/ai-quota-bar`。程序从这个路径向上 5 级到仓库根，再找 `quota.1m.sh`。拷走，或改成别的层级（直接放在仓库根，或在 `native/` 下面再套一层目录），都找不到脚本，菜单没有数据。
> 若必须把 `.app` 放到别处：先退出已在运行的实例，再让新进程带上仓库根目录。已经在跑的实例不会改环境。登录项由系统拉起，读不到这个变量，开机自启请用留在 `native/` 里的那一份。在仓库根目录执行（`$(pwd)` 就是仓库根，目录里要有 `quota.1m.sh`）：
>
> ```bash
> pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar" || true
> open --env "AI_QUOTA_BAR_HOME=$(pwd)" -n "/你放置的路径/AI Quota Bar.app"
> ```
>
> 只有向上找 `quota.1m.sh` 失败时才会读 `AI_QUOTA_BAR_HOME`。
> ChatGPT 额度不用配凭证，读的是本机 Codex CLI 会话；Cursor / DeepSeek 的凭证说明见下文。Grok Bot 不另配凭证，和 Cursor 共用 `cursor-access-token`。
> 凭证只进本机 Keychain，边界见 [SECURITY.md](SECURITY.md)。

**开机自启**：把 `native/AI Quota Bar.app` 这个路径加进登录项。macOS 13 及以后：系统设置 → 通用 → 登录项（较新版本里叫「登录项与扩展」）。macOS 12：系统偏好设置 → 用户与群组 → 登录项。

**退出**：菜单栏点开 → 按 Esc 收起菜单，或

```bash
pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar"
```

---

## 数据来源

| 项目 | 获取方式 | 凭证 | 说明 |
|---|---|---|---|
| ChatGPT 额度 | **本地解析** `~/.codex/sessions` 会话记录 | 无需（订阅登录态由 Codex CLI 维护） | 无网络请求；数据随 Codex 任务结束更新 |
| Cursor 双池额度 | `api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage` | macOS Keychain，service = `cursor-access-token`（**需手动配置一次**） | 未公开接口，已做容错 |
| Grok Bot 周额度 | `api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus` | 与 Cursor 同一条 Keychain token，不另配 | 独立的一周额度。菜单显示「Grok Bot」。没有额度或请求失败时省略相关行，不影响 Cursor 双池 |
| DeepSeek 余额 | `api.deepseek.com/user/balance`（官方公开接口） | macOS Keychain 中的 API Key | |

**关于 Cursor 凭证的边界（重要）**：额度用的 token **不从 Cursor 内部存储读取，也不解密**。
你自己从客户端取出，存进自己的 Keychain 条目，程序只读这一条。
Keychain 里的 `Cursor Safe Storage` 不碰。

菜单标题上的套餐名是唯一例外：只读 `state.vscdb` 里的 `stripeMembershipType`
（`pro`、`ultra` 这种代码，不是 token）。读不到具体套餐时，下拉里的套餐名显示「Cursor」。
菜单栏那一列的标签不看套餐代码：有独立 Grok Bot 周额度时是「Cursor / Grok Bot」，否则是「Cursor」。

这样做的取舍很明确：**放弃"零配置自动读取"，换取代码里没有任何绕过第三方应用
凭据保护的部分**——可审计、可长期维护，也不会因为 Cursor 改加密方案就整体失效。
代价是 token 约 2 个月到期，需要重新配置一次（见下节）。

**Cursor 请求不会触发 token 刷新**：程序只用不换，避免 refresh token 轮转把
Cursor 登录态踢掉。若在 Cursor 里退出登录/重新登录，旧 token 会失效，需重新配置。

**ChatGPT 数据的边界**：额度数据（`rate_limits`）只在 Codex 任务结束时写入本地
会话文件，不是实时接口——不用 Codex 时数据停留在最后一次任务；窗口重置时刻
过后由本地推断归零显示。需 ChatGPT 订阅登录 + 官方直连（经代理/路由时该字段为空）。

三个数据源**互相独立**：其中一个失效时，其余照常显示，失效的那项会给出文字提示而非中断整个组件。
Grok Bot 算在 Cursor 这一次取数里：周额度请求失败，或账号没有这笔额度时，只是不显示 Grok Bot，Cursor 双池仍用这次结果。

---

## 配置

编辑 `config.json`（改完无需重启，下一次刷新自动生效）：

| 键 | 默认 | 说明 |
|---|---|---|
| `cursor.enabled` | `true` | 是否显示 Cursor 额度（含同一条 token 上的 Grok Bot 周额度） |
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

**刷新节奏**：App 每 60 秒调用一次取数脚本；网络类（Cursor 双池与 Grok Bot、DeepSeek）由 300 秒
缓存挡住不重复发请求。Grok Bot 跟 Cursor 双池放在同一次结果里，共用这一档缓存。本地类（ChatGPT）每次重读（成本≈0）。新增数据源只需在
`lib/report.py` 中决定归 fetch 还是 local 档。

### 配置 DeepSeek 密钥

密钥存入 macOS Keychain，不落明文文件。菜单和命令行写的是同一条 Keychain 记录；菜单会先校验，命令行不会。

**① 在菜单栏里配置（推荐）** —— 下拉菜单点「配置 DeepSeek 密钥」，粘贴后保存。
弹窗里有「从剪贴板填入」。保存前会先调 `https://api.deepseek.com/user/balance` 验证，**通过才写入**；
失败只提示原因，不会覆盖原有可用密钥。Key 须以 `sk-` 开头，否则提示「格式不对：DeepSeek 的 Key 以 sk- 开头」；
接口拒绝时提示「密钥无效（HTTP 401）」。配置成功后保留「更换密钥」入口。
写入的 Keychain 服务名来自 `config.json` 的 `deepseek.keychainService`（默认 `deepseek-api-key`）。

**② 命令行**：

```bash
security add-generic-password -U -s deepseek-api-key -a "$USER" -w
```

`-s` 必须和 `deepseek.keychainService` 一致，改过配置就要换成同一个名字。`-a` 用当前用户名，与程序写入时一致。
`-w` 放在最后且不跟密码，是为了不把密钥留在进程参数里。`security` 会问两次（输入、再输入一次），
**两次都不回显**。`-U` 表示已存在时覆盖。
这条路**不做校验**，也不检查 `sk-` 前缀——粘错了要等下一轮刷新才会报「密钥已失效」。

未配置时，下拉菜单会显示「DeepSeek 不可用 · 未配置密钥」并给出可点击的配置入口，**不影响其余部分正常使用**。
DeepSeek 的 Key **不会过期**，配置一次长期有效。

> 提示：DeepSeek 的余额是**账户级**的，同一账号下任意一个 API Key 查到的余额都相同；
> 建议新建一个专用 Key（如命名 `menubar-quota`），便于将来单独撤销而不影响其他服务。

### 配置 Cursor token

Cursor 的额度查询需要你的**订阅登录态**（Bearer token）。它只有 Cursor 客户端知道，
程序不会去解 Cursor 的密文，所以需要你取一次并交给自己机器的 Keychain 保管。

**先把 token 取出来（两种入口通用）**：

1. 在 Cursor 里按 `⌘⇧P` 打开命令面板，输入 `Toggle Developer Tools` 回车
   （不依赖菜单栏名字——Cursor 各版本菜单结构不同，命令面板是稳定的）
2. 切到 **Network** 面板，过滤框输入 `cursor.sh`
3. 触发一次请求：点右上角齿轮打开 Cursor Settings，或点左下角账户头像，
   直到列表里出现 `api2.cursor.sh`
4. 点开它 → Headers → Request Headers → 复制 `authorization: Bearer eyJ...` 整行
   （只复制 `eyJ...` 部分、或带 `Bearer ` 前缀都可以，程序会自动识别）
5. 列表里一直没有请求 → 命令面板执行 `Reload Window` 后再试

**然后再选一种方式配置，效果相同：**

**① 在菜单栏里配置（推荐）** —— 下拉菜单点「配置 Cursor token」，弹窗里粘贴后保存。
弹窗内还有「从剪贴板填入」，可以先复制再点它。保存前会**先验证**：
通过才写入 Keychain（服务名 `cursor-access-token`，账户名是当前用户），失败只提示原因、不覆盖原有可用 token。
配置成功后菜单里的入口变成「更换 token」，换账号时不必去终端删条目。

**② 命令行配置**：

```bash
bash scripts/cursor-token.sh
```

脚本会再印一遍上面的取 token 步骤，然后让你粘贴（**不回显**）。顺序是先请求接口验证，**通过才写入** Keychain：

- 通过 → 打印两个模型池的百分比和到期日，再写入
- 失败 → 不写入、也不覆盖原来还能用的条目

之后随时可以检查现状：

```bash
bash scripts/cursor-token.sh --check
```

**何时需要重新配置**：

| 情况 | 表现 |
|---|---|
| 还没到期，剩余不足 7 天，接口仍返回额度 | 下拉里琥珀色「Token N 天后过期」，入口是「更换 token」 |
| 接口仍返回额度，但 token 里的 `exp` 已过 | 「Token 已过期」，并提示点「更换 token」 |
| 接口返回 401（到期被拒，或在 Cursor 里退出 / 重新登录） | 「Cursor 不可用 · token 已失效」，入口是「配置 Cursor token」 |
| Keychain 里没有 `cursor-access-token` | 「Cursor 不可用 · 未配置 token」，入口同样是「配置 Cursor token」 |

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

**`build.sh` 停在「图标」，找不到 `AppIcon.icns`**

按上文「快速开始」先跑 `swiftc` / `make_icon` 和 `iconutil`。`build.sh` 不生成图标。

**菜单栏是 `Cursor`，没有 `Grok Bot`**

账号没有独立的 Grok Bot 周额度时就是这样：下拉里不出现 Grok Bot 行，标签保持「Cursor」，Cursor Models / Others Models 照常。这一次请求没有返回已用百分比时同样隐藏，下一轮刷新再判断。

**显示 `Cursor 不可用 · 未配置 token`**

还没配置。点下一行的「配置 Cursor token」在弹窗里粘贴；或用命令行
`bash scripts/cursor-token.sh`。

**显示 `Cursor 不可用 · token 已失效`**

接口返回了 401：token 到期（约 2 个月）或在 Cursor 里重新登录过。点「配置 Cursor token」更新。
保存前会先验证，通过后用 `security add-generic-password -U` 覆盖原来那条（服务名 `cursor-access-token`）。
额度数字还在、菜单里写着「Token 已过期」时，点「更换 token」。

**显示 `ChatGPT Plus 读取失败`**

常见原因：未安装 Codex CLI / 本机会话中无额度数据。需在 Codex CLI 用
ChatGPT 订阅账号登录并直连官方（走本地代理/路由时额度字段不下发），
跑一次任务后即可显示。

**显示 `DeepSeek 不可用 · 未配置密钥`**

按上文「配置 DeepSeek 密钥」写入即可。

**显示 `DeepSeek 不可用 · 密钥已失效`**

说明接口返回了 401：key 被删除/重建，或账号状态异常。重新执行配置命令即可。
注意这类错误**不会**降级显示上次的余额——余额数字看起来还正常、实际上 key 已失效，
是最容易误判的情形，所以宁可报警也不留旧值。

**DeepSeek 显示「余额不足，已无法发起请求」**

接口的 `is_available` 为 `false`，即余额已不足以发起任何请求。此时仍显示真实余额
（通常是 `¥0.00`），但会用红色标注。与「低于阈值」不同：后者只是建议充值，
仍可正常调用。

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
  ├── scripts/
  │   └── cursor-token.sh      # 交互式配置 Cursor token 到 Keychain（含验证）
  ├── cache/state.json         # 运行时缓存（失败时降级显示用，git 忽略）
  ├── native/                  # 原生 App
  │   ├── main.swift           #   程序主体（脚本定位、定时刷新、菜单构建）
  │   ├── QuotaBarView.swift   #   菜单栏自绘视图（外观参数在此）
  │   ├── make_icon.swift      #   App 图标生成器（代码绘制，可复现）
  │   ├── AppIcon.iconset/     #   图标中间产物（git 忽略，make_icon 生成）
  │   ├── AppIcon.icns         #   构建用图标（iconutil 生成，git 忽略）
  │   ├── Info.plist           #   LSUIElement=1（无 Dock 图标）；最低系统 12.0
  │   ├── build.sh             #   编译 + 打包（拷贝已有的 AppIcon.icns，不负责生成）
  │   └── AI Quota Bar.app     #   应用本体（构建产物，git 忽略）
  ├── SECURITY.md              # 凭证处理与数据流向声明
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

**重新生成图标** → 改 `native/make_icon.swift`（配色/进度参数在文件末尾），然后在仓库根目录执行与「快速开始」相同的三步。`build.sh` 不会调用 `iconutil`，只拷贝已经生成的 `native/AppIcon.icns`：

```bash
swiftc native/make_icon.swift -o /tmp/make_icon && /tmp/make_icon
iconutil -c icns native/AppIcon.iconset -o native/AppIcon.icns
bash native/build.sh
```

**卸载**：

```bash
pkill -f "AI Quota Bar.app/Contents/MacOS/ai-quota-bar"
rm -rf <项目目录>
security delete-generic-password -s cursor-access-token   # 若配置过 Cursor token
security delete-generic-password -s deepseek-api-key      # 若配置过 DeepSeek 密钥；改过 keychainService 时换成那个名字
# 并从登录项中移除 AI Quota Bar（路径见上文「开机自启」）
```

---

## 说明

Cursor 的额度接口（月度双池，以及 Grok Bot 周额度 `GetSandUsageStatus`）为**未公开的逆向接口**，可能随上游版本变更而失效；
ChatGPT 的额度数据依赖 Codex CLI 的本地会话记录格式，同样可能随版本变更；
DeepSeek 余额接口为官方公开接口。程序已对字段做容错处理，取不到时降级显示而非崩溃。
