// ai-quota-bar — macOS 菜单栏原生组件
//
// 定位：**纯渲染层**。所有数据来自同项目的 Python 取数脚本
//       (`quota.1m.sh --json`)。
// 取数逻辑、阈值规则、降级策略仍集中在 Python 侧，
// 因此调整这些内容不需要重新编译本程序。
//
// 菜单栏标题使用 QuotaBarView 自绘的两行布局（标签行 + 数值行）。
//
// 配色约定：菜单栏一律系统文字色（深色菜单栏下呈白色），
// 预警配色只出现在下拉菜单里。

import Cocoa

// MARK: - 配置

/// 定位 quota.1m.sh：bundle 在 <项目>/native/AI Quota Bar.app，脚本在项目根。
/// 优先 Bundle.main 推导（任何人 clone 到任意目录都可用）；
/// 推导失败（如直接跑裸二进制调试）回退到环境变量 AI_QUOTA_BAR_HOME 或内置默认路径。
private func locateScriptPath() -> String {
    // .../native/AI Quota Bar.app/Contents/MacOS/xxx → 上跳 5 级 = 项目根
    // （首次删除去掉的是文件名，之后依次是 MacOS/、Contents/、.app/、native/）
    if let execURL = Bundle.main.executableURL {
        let projectRoot = execURL
            .deletingLastPathComponent()      // MacOS/（同时去掉可执行文件名）
            .deletingLastPathComponent()      // Contents/
            .deletingLastPathComponent()      // AI Quota Bar.app/
            .deletingLastPathComponent()      // native/
            .deletingLastPathComponent()      // 项目根
        let candidate = projectRoot
            .appendingPathComponent("quota.1m.sh").path
        if FileManager.default.fileExists(atPath: candidate) {
            return candidate
        }
    }
    if let env = ProcessInfo.processInfo.environment["AI_QUOTA_BAR_HOME"] {
        let candidate = (env as NSString).appendingPathComponent("quota.1m.sh")
        if FileManager.default.fileExists(atPath: candidate) {
            return candidate
        }
    }
    return "quota.1m.sh" // 兜底：PATH 查找
}
private let scriptPath = locateScriptPath()
// App 统一 60s 调一次脚本；真正按 provider 分频在 Python 侧完成：
// 网络类（Cursor/DeepSeek）靠 300s 缓存挡住不重复发请求，
// 本地类（ChatGPT 扫 Codex 会话文件）20s TTL 必然过期、每次重读，成本≈0。
private let refreshSeconds: TimeInterval = 60

// MARK: - 数据结构（对应 Python 端 build_payload）

private struct ColumnPayload: Decodable {
    let label: String
    let value: String
    // color 字段刻意不解析：菜单栏统一用系统文字色，预警色只在下拉菜单出现
}

private struct SegmentEntry: Decodable {
    let text: String
    let color: String?
}

private struct MenuEntry: Decodable {
    let kind: String
    let title: String?
    let color: String?
    let mono: Bool?
    let size: Int?
    let href: String?
    /// 应用内动作名（如 configure-cursor-token）。由 buildMenu 映射到选择器，
    /// 与 href（打开链接）/ refresh（重取数据）并列，见下。
    let action: String?
    /// 动作的附加参数：目前用于 DeepSeek 的 Keychain 服务名
    /// （对应 config.json 的 deepseek.keychainService，改配置无需重编译）
    let service: String?
    let refresh: Bool?
    /// tab 停靠位（pt）：title 里的 \t 会吸附到这些位置，用于菜单多列对齐。
    /// 空格补齐对 CJK 无效（苹方回退宽度 ≠ 2×等宽 ASCII），tab 是系统级对齐。
    let tabs: [Double]?
    /// 右对齐 tab 停靠位（pt）：\t 后的文字以此位置为右缘（如「倒计时」列）。
    let tabsRight: [Double]?
    /// 分段着色：一行内不同片段用不同颜色（如「Cursor Pro  91.7%」）。
    /// 存在时优先于 title/color。
    let segments: [SegmentEntry]?
}

private struct Payload: Decodable {
    let stale: Bool
    let columns: [ColumnPayload]
    let menu: [MenuEntry]
}

// MARK: - 颜色工具

private func parseHex(_ hex: String?) -> NSColor? {
    guard let hex, hex.hasPrefix("#"), hex.count == 7 else { return nil }
    var value: UInt64 = 0
    guard Scanner(string: String(hex.dropFirst())).scanHexInt64(&value) else { return nil }
    return NSColor(
        srgbRed: CGFloat((value >> 16) & 0xFF) / 255.0,
        green: CGFloat((value >> 8) & 0xFF) / 255.0,
        blue: CGFloat(value & 0xFF) / 255.0,
        alpha: 1.0)
}

private var isDarkAppearance: Bool {
    NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
}

/// 深色菜单栏上把颜色提亮，保证对比度
private func adapt(_ color: NSColor) -> NSColor {
    guard isDarkAppearance else { return color }
    return color.blended(withFraction: 0.32, of: .white) ?? color
}

// MARK: - 菜单信息行自绘视图

/// 禁用菜单项会把接近 labelColor 的文字色替换成 disabled 灰（四轮实测：
/// 动态 labelColor / 烘焙色 / 纯白 / 96% 白均被替换），而 attributedTitle
/// 里的显式颜色则忠实渲染。含默认色的行改用本视图自绘，绕开该规则。
private final class MenuRowView: NSView {
    private let content: NSAttributedString
    /// 与标准菜单项的文字起点一致（像素实测：标准项文字原点比自绘视图
    /// 原点靠左 3.5pt，20-3.5=16.5；tab 停靠位是按这个原点调校的）
    private static let leadingInset: CGFloat = 16.5

    init(content: NSAttributedString) {
        self.content = content
        super.init(frame: .zero)
        let s = content.size()
        frame = NSRect(x: 0, y: 0,
                       width: ceil(s.width) + Self.leadingInset + 4,
                       height: ceil(s.height) + 6)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let s = content.size()
        content.draw(at: NSPoint(x: Self.leadingInset,
                                 y: (bounds.height - s.height) / 2))
    }
}

// MARK: - 应用主体

final class QuotaBarApp: NSObject, NSApplicationDelegate {

    private var statusItem: NSStatusItem!
    private let barView = QuotaBarView()
    private var timer: Timer?
    private var payload: Payload?
    private var isFetching = false
    /// 配置弹窗里的输入框（「从剪贴板填入」按钮需要引用它）
    private weak var tokenField: NSSecureTextField?

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            // 初始铺满按钮；入窗后 QuotaBarView.syncFrameToWindow 会把视图
            // 扩到整个状态栏项窗口（窗口是新菜单栏高度，按钮仅 22pt）。
            // 点击由视图转发（视图盖住按钮后按钮收不到点击）
            barView.frame = button.bounds
            button.addSubview(barView)
            barView.onClick = { [weak self] in self?.handleClick() }
        }

        setContent(.placeholder("额度 --"))
        refresh()

        let timer = Timer(timeInterval: refreshSeconds, repeats: true) { [weak self] _ in
            self?.refresh()
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer

        // 系统外观切换时重绘（否则深色模式下颜色不会跟着变）
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(appearanceChanged),
            name: NSNotification.Name("AppleInterfaceThemeChangedNotification"),
            object: nil)
    }

    @objc private func appearanceChanged() {
        if let payload { apply(payload) }
    }

    // MARK: 数据刷新

    @objc func refresh() {
        guard !isFetching else { return }
        isFetching = true

        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = Self.runScript()
            DispatchQueue.main.async {
                guard let self else { return }
                self.isFetching = false
                if let result {
                    self.payload = result
                    self.apply(result)
                } else {
                    self.setError()
                }
            }
        }
    }

    private static func runScript() -> Payload? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = ["--json"]
        process.standardInput = FileHandle.nullDevice

        let out = Pipe()
        process.standardOutput = out
        process.standardError = Pipe()

        do {
            try process.run()
        } catch {
            return nil
        }

        // 先读满再等待退出，避免大输出把管道缓冲写满造成死锁
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        guard process.terminationStatus == 0 else { return nil }
        return try? JSONDecoder().decode(Payload.self, from: data)
    }

    // MARK: 菜单栏两行标题（自绘）

    private func apply(_ payload: Payload) {
        setContent(.columns(payload.columns.map { QuotaColumn(label: $0.label, value: $0.value) }))
    }

    /// 更新视图内容并同步状态项宽度（自绘视图需要显式给定长度）
    private func setContent(_ content: QuotaBarContent) {
        barView.content = content
        statusItem.length = barView.intrinsicContentSize.width
    }

    private func setError() {
        setContent(.error("额度 !"))
    }

    // MARK: 下拉菜单

    @objc private func handleClick() {
        guard let payload else {
            refresh()
            return
        }
        statusItem.menu = Self.buildMenu(payload, target: self)
        statusItem.button?.performClick(nil)
        statusItem.menu = nil
    }

    private static func buildMenu(_ payload: Payload, target: QuotaBarApp) -> NSMenu {
        let menu = NSMenu()
        menu.autoenablesItems = false

        // 默认文字色：禁用项会把"接近 labelColor 的值"替换成 disabled 灰
        // （实测动态 labelColor、其烘焙形态、纯白均被替换，渲染亮度仅 ~80；
        // 而显式非默认色全部忠实渲染）。96%/4% 灰白视觉等同黑/白，
        // 但不等于 labelColor 的值，可逃过替换
        let defaultColor = NSColor(white: isDarkAppearance ? 0.96 : 0.04, alpha: 1)

        for entry in payload.menu {
            if entry.kind == "sep" {
                menu.addItem(.separator())
                continue
            }

            let item = NSMenuItem()
            let size = CGFloat(entry.size ?? 13)
            let font = entry.mono == true
                ? NSFont.monospacedSystemFont(ofSize: size, weight: .regular)
                : NSFont.systemFont(ofSize: size)

            var paragraphStyle: NSMutableParagraphStyle?
            if entry.tabs != nil || entry.tabsRight != nil {
                let style = NSMutableParagraphStyle()
                // 左对齐列（名称/百分比/条形图）+ 右对齐列（如「倒计时」），
                // 同一套 NSTextTab 机制，只是对齐参数不同
                style.tabStops = (entry.tabs ?? []).map {
                    NSTextTab(textAlignment: .left, location: CGFloat($0))
                } + (entry.tabsRight ?? []).map {
                    NSTextTab(textAlignment: .right, location: CGFloat($0))
                }
                paragraphStyle = style
            }

            if entry.segments == nil && entry.color == nil, let title = entry.title {
                // 只给字体、不给颜色：文字颜色完全交给系统，hover 时自动
                // 反白，与系统菜单高亮样式一致（attributedTitle 一旦指定
                // 前景色，高亮时系统就不会再替换它，会蓝底灰字）
                var attributes: [NSAttributedString.Key: Any] = [.font: font]
                if let paragraphStyle { attributes[.paragraphStyle] = paragraphStyle }
                item.attributedTitle = NSAttributedString(string: title, attributes: attributes)
            } else if let segments = entry.segments {
                // 分段着色：逐段设置颜色，整行共享字体与 tab 停靠位
                let text = NSMutableAttributedString()
                var usesDefaultColor = false
                let isActionable = entry.href != nil || entry.refresh == true
                for segment in segments {
                    var attrs: [NSAttributedString.Key: Any] = [.font: font]
                    if let paragraphStyle { attrs[.paragraphStyle] = paragraphStyle }
                    if let explicit = parseHex(segment.color) {
                        attrs[.foregroundColor] = adapt(explicit)
                    } else if isActionable {
                        // 可点项的无色段不设颜色属性：系统默认色渲染，
                        // hover 时自动反白
                    } else {
                        usesDefaultColor = true
                        attrs[.foregroundColor] = defaultColor
                    }
                    text.append(NSAttributedString(string: segment.text, attributes: attrs))
                }
                if usesDefaultColor {
                    // 含默认色的信息行用自绘视图，绕开禁用项的颜色替换规则
                    item.view = MenuRowView(content: text)
                } else {
                    item.attributedTitle = text
                }
            } else {
                var attributes: [NSAttributedString.Key: Any] = [
                    .font: font,
                    .foregroundColor: parseHex(entry.color).map(adapt) ?? defaultColor,
                ]
                if let paragraphStyle { attributes[.paragraphStyle] = paragraphStyle }
                item.attributedTitle = NSAttributedString(
                    string: entry.title ?? "",
                    attributes: attributes)
            }

            if let href = entry.href, let url = URL(string: href) {
                item.target = target
                item.action = #selector(openLink(_:))
                item.representedObject = url
            } else if entry.refresh == true {
                item.target = target
                item.action = #selector(refresh)
            } else if let action = entry.action {
                item.target = target
                switch action {
                case "configure-cursor-token":
                    item.action = #selector(configureCursorToken)
                case "configure-deepseek-key":
                    item.action = #selector(configureDeepSeekKey(_:))
                    if let service = entry.service {
                        item.representedObject = service
                    }
                default:
                    break
                }
            }

            // 只有带动作的项才可点击：无动作的信息行禁用后不参与 hover
            // 高亮，与系统菜单"可点项才高亮"的交互一致；文字颜色由
            // attributedTitle 显式指定，禁用不会变灰
            item.isEnabled = item.action != nil

            if entry.kind == "sub" {
                item.indentationLevel = 1
            }
            menu.addItem(item)
        }

        return menu
    }

    @objc private func openLink(_ sender: NSMenuItem) {
        if let url = sender.representedObject as? URL {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - 菜单内配置凭证（Cursor token / DeepSeek 密钥共用）

    private struct VerifyResult: Decodable {
        let ok: Bool
        let error: String?
        /// 成功摘要由 Python 侧拼好：两个数据源的字段不同（百分比 vs 余额），
        /// 格式化集中在一处，Swift 只负责显示
        let summary: String?
    }

    /// 校验结果：成功带数据，失败带可直接展示的中文原因
    private enum VerifyOutcome {
        case ok(VerifyResult)
        case failed(String)
    }

    /// 一种可配置凭证的静态描述：两个数据源只差文案、Keychain 服务名与校验参数
    private struct CredentialSpec {
        let title: String
        let hint: String
        let placeholder: String
        let service: String
        let verifyFlag: String
        /// 出现在提示语里的名词（"token" / "密钥"）
        let noun: String
    }

    private static let cursorSpec = CredentialSpec(
        title: "配置 Cursor token",
        hint: """
        ① 在 Cursor 里按 ⌘⇧P 打开命令面板，输入 Toggle Developer Tools，回车
        ② 切到 Network 面板，过滤框输入 cursor.sh
        ③ 触发一次请求：点右上角齿轮打开 Cursor Settings（或点左下角账户头像），
           直到列表里出现 api2.cursor.sh
        ④ 点开它 → Headers → Request Headers → 复制
           authorization: Bearer eyJ... 整行，粘贴到下面

        看不到请求时：命令面板执行 Reload Window 后重试。

        token 等同登录凭证，只保存在本机 Keychain，约 2 个月后需更新。
        """,
        placeholder: "eyJ...（纯 token 或整行 authorization 都可以）",
        service: "cursor-access-token",
        verifyFlag: "--verify-cursor-token",
        noun: "token")

    private static let deepseekSpec = CredentialSpec(
        title: "配置 DeepSeek 密钥",
        hint: """
        在 https://platform.deepseek.com/api_keys 创建一个 API Key
        （以 sk- 开头），粘贴到下面。

        余额是账户级的，同一账号下任意一个 Key 查询结果都相同；
        建议用专用 Key，便于将来单独撤销。密钥不会过期。
        """,
        placeholder: "sk-...",
        service: "deepseek-api-key",
        verifyFlag: "--verify-deepseek-key",
        noun: "密钥")

    @objc private func configureCursorToken() {
        configureCredential(Self.cursorSpec, service: Self.cursorSpec.service)
    }

    /// DeepSeek 的服务名以菜单项带来的为准（对应 config.json 的
    /// deepseek.keychainService），这样改配置不需要重编译
    @objc private func configureDeepSeekKey(_ sender: NSMenuItem) {
        let service = (sender.representedObject as? String) ?? Self.deepseekSpec.service
        configureCredential(Self.deepseekSpec, service: service)
    }

    /// 弹窗粘贴 → 校验 → 写 Keychain → 刷新菜单。
    /// 校验放在写入之前，失败不会覆盖掉当前仍可用的凭证。
    private func configureCredential(_ spec: CredentialSpec, service: String) {
        let alert = NSAlert()
        alert.messageText = spec.title
        alert.informativeText = spec.hint
        alert.addButton(withTitle: "保存")
        alert.addButton(withTitle: "取消")

        let container = NSView(frame: NSRect(x: 0, y: 0, width: 420, height: 56))
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 28, width: 420, height: 24))
        field.placeholderString = spec.placeholder
        container.addSubview(field)
        tokenField = field

        let pasteButton = NSButton(title: "从剪贴板填入",
                                   target: self,
                                   action: #selector(fillTokenFromClipboard))
        pasteButton.bezelStyle = .rounded
        pasteButton.frame = NSRect(x: 0, y: 0, width: 130, height: 24)
        container.addSubview(pasteButton)

        alert.accessoryView = container
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }

        let secret = Self.normalizeToken(field.stringValue)
        guard !secret.isEmpty else {
            showMessage("没有输入内容", "请粘贴\(spec.noun)后重试。", style: .warning)
            return
        }

        // 1) 先校验：不写入、不改缓存
        switch verify(secret, flag: spec.verifyFlag) {
        case .failed(let message):
            showMessage("\(spec.noun)校验失败", message, style: .warning)
        case .ok(let result):
            // 2) 校验通过才落库
            if let writeError = writeToKeychain(secret, service: service) {
                showMessage("写入 Keychain 失败", writeError, style: .critical)
                return
            }
            showMessage("配置成功", result.summary ?? "已保存。")
            refresh()
        }
    }

    @objc private func fillTokenFromClipboard() {
        guard let text = NSPasteboard.general.string(forType: .string) else { return }
        tokenField?.stringValue = text
    }

    private func verify(_ secret: String, flag: String) -> VerifyOutcome {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = [flag]
        let input = Pipe()
        let out = Pipe()
        process.standardInput = input
        process.standardOutput = out
        process.standardError = Pipe()
        do {
            try process.run()
        } catch {
            return .failed("无法执行取数脚本：\(error.localizedDescription)")
        }
        // 先写满 stdin 再关，避免对方阻塞在读 stdin 上
        input.fileHandleForWriting.write(Data(secret.utf8))
        try? input.fileHandleForWriting.close()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard let result = try? JSONDecoder().decode(VerifyResult.self, from: data) else {
            return .failed("脚本返回了无法解析的结果")
        }
        return result.ok ? .ok(result) : .failed(result.error ?? "未知错误")
    }

    private static func normalizeToken(_ raw: String) -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if let range = text.range(of: "bearer ", options: [.caseInsensitive, .backwards]) {
            text = String(text[range.upperBound...])
        }
        return text.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: .whitespacesAndNewlines).first ?? ""
    }

    /// 写 Keychain。密码从 stdin 交给脚本，由脚本用 security 的交互提示写入，
    /// 不把明文放进 argv（ps 看不到）。条目仍由 security 创建，之后 Python
    /// 再用 security 读取时不会弹出授权框。
    private func writeToKeychain(_ secret: String, service: String) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = ["--store-keychain", service]
        let input = Pipe()
        let err = Pipe()
        process.standardInput = input
        // 丢掉 stdout。挂上管道却不读，输出一多会把子进程堵死。
        process.standardOutput = FileHandle.nullDevice
        process.standardError = err
        do {
            try process.run()
        } catch {
            return "无法执行取数脚本：\(error.localizedDescription)"
        }
        input.fileHandleForWriting.write(Data(secret.utf8))
        try? input.fileHandleForWriting.close()
        let data = err.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus != 0 else { return nil }
        let text = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return text.isEmpty ? "写入 Keychain 失败（退出码 \(process.terminationStatus)）" : text
    }

    private func showMessage(_ title: String, _ body: String,
                             style: NSAlert.Style = .informational) {
        let alert = NSAlert()
        alert.alertStyle = style
        alert.messageText = title
        alert.informativeText = body
        alert.addButton(withTitle: "好")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}

// MARK: - 入口

let application = NSApplication.shared
let appDelegate = QuotaBarApp()
application.delegate = appDelegate
application.setActivationPolicy(.accessory)
application.run()
