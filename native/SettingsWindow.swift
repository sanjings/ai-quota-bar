// 菜单「设置…」打开的窗口。同时只保留一份；关掉之后再开会重新读文件。

import Cocoa

final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    private let configPath: String
    private let onSaved: () -> Void
    private let onCheckUpdate: (@escaping (String) -> Void) -> Void
    private var controls: SettingsControls?
    private static var shared: SettingsWindowController?

    static func show(
        configPath: String,
        onSaved: @escaping () -> Void,
        onCheckUpdate: @escaping (@escaping (String) -> Void) -> Void
    ) {
        if let existing = shared, existing.window?.isVisible == true {
            existing.window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let controller = SettingsWindowController(
            configPath: configPath,
            onSaved: onSaved,
            onCheckUpdate: onCheckUpdate)
        shared = controller
        controller.window?.center()
        controller.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private init(
        configPath: String,
        onSaved: @escaping () -> Void,
        onCheckUpdate: @escaping (@escaping (String) -> Void) -> Void
    ) {
        self.configPath = configPath
        self.onSaved = onSaved
        self.onCheckUpdate = onCheckUpdate
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 440, height: 360),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false)
        window.title = "设置"
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        super.init(window: window)
        window.delegate = self
        install()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    func windowWillClose(_ notification: Notification) {
        DispatchQueue.main.async { [weak self] in
            guard let self, Self.shared === self else { return }
            Self.shared = nil
        }
    }

    private func install() {
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 20, bottom: 16, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let path = wrappingLabel(configPath, size: 11, color: .secondaryLabelColor)
        path.lineBreakMode = .byTruncatingMiddle
        stack.addArrangedSubview(path)
        stack.setCustomSpacing(14, after: path)

        do {
            let root = try ConfigStore.load(from: configPath)
            let form = ConfigStore.Form.read(from: root)
            let controls = SettingsControls(
                form: form,
                target: self,
                saveAction: #selector(saveClicked),
                checkAction: #selector(checkClicked))
            self.controls = controls
            addSection(stack, title: "数据源", rows: [
                controls.cursor,
                controls.codex,
                controls.deepseek,
                controls.claude,
                labeledRow("DeepSeek 钥匙串服务名", controls.service),
                labeledRow("Claude 钥匙串服务名", controls.claudeService),
                labeledRow("低余额阈值", controls.lowBalance),
            ])
            addSection(stack, title: "显示", rows: [
                controls.showBars,
                labeledRow("进度条格数", controls.barWidth),
            ])
            addSection(stack, title: "阈值（%）", rows: [
                labeledRow("预警", controls.warn),
                labeledRow("危险", controls.danger),
            ])
            addSection(stack, title: "通知", rows: [
                controls.notifications,
                wrappingLabel(
                    "沿用上面的「预警」「危险」和 DeepSeek「低余额阈值」，不另设数字。ChatGPT 与 Claude 的 5 小时和周额度、Claude 下拉里的模型周额度和额外用量、Cursor 的合计、两个模型池和 Grok Bot、DeepSeek 余额，达到预警或危险（余额低于阈值，或官方标记为已无法请求）时各发一条系统通知。同一阈值在同一次重置周期里只发一次；用量掉下去又在 30 分钟内回来，不重复提醒。把阈值调低并保存后，下一轮刷新会按新阈值再发。关掉开关就不再发。通知失败时菜单栏照常更新。",
                    size: 12,
                    color: .secondaryLabelColor),
            ])
            addSection(stack, title: "缓存（秒）", rows: [
                labeledRow("网络数据源", controls.cacheFetch),
                labeledRow("本地数据源", controls.cacheLocal),
            ])
            addSection(stack, title: "更新", rows: [
                controls.autoUpdate,
                wrappingLabel(
                    "关闭时只在菜单提示，点一下打开 Release 页面，不下载。勾选并保存后，若这个正式版带有 macOS zip，菜单改为「立即更新」：校验通过并经确认后才替换这一份安装目录里的程序文件，然后重新打开。config.json 和 cache 会保留。校验失败不会覆盖现有安装。",
                    size: 12,
                    color: .secondaryLabelColor),
                controls.checkButton,
                controls.updateStatus,
            ])
            let note = wrappingLabel(
                "保存写入这份 config.json，下一轮刷新生效。关闭窗口不会保存。",
                size: 12,
                color: .secondaryLabelColor)
            stack.addArrangedSubview(note)
            let bottom = NSStackView(views: [controls.saveButton, controls.status])
            bottom.orientation = .horizontal
            bottom.alignment = .centerY
            bottom.spacing = 12
            stack.addArrangedSubview(bottom)
        } catch {
            stack.addArrangedSubview(wrappingLabel(error.localizedDescription, size: 13, color: .labelColor))
            stack.addArrangedSubview(wrappingLabel(
                "没有改动文件。修好 config.json 后关掉窗口再打开。",
                size: 12,
                color: .secondaryLabelColor))
        }

        let content = NSView()
        content.addSubview(stack)
        window?.contentView = content
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            stack.widthAnchor.constraint(equalToConstant: 440),
        ])
        content.layoutSubtreeIfNeeded()
        let height = max(stack.fittingSize.height, 80)
        window?.setContentSize(NSSize(width: 440, height: height))
    }

    @objc private func saveClicked() {
        guard let controls else { return }
        let service = controls.service.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        let claudeService = controls.claudeService.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !service.isEmpty, !claudeService.isEmpty else {
            showAlert("钥匙串服务名不能为空。")
            return
        }
        guard
            let low = requireNumber(controls.lowBalance, name: "低余额阈值"),
            let width = requireNumber(controls.barWidth, name: "进度条格数"),
            let warn = requireNumber(controls.warn, name: "预警"),
            let danger = requireNumber(controls.danger, name: "危险"),
            let fetch = requireNumber(controls.cacheFetch, name: "网络数据源缓存"),
            let local = requireNumber(controls.cacheLocal, name: "本地数据源缓存")
        else { return }

        let form = ConfigStore.Form(
            cursorEnabled: controls.cursor.state == .on,
            codexEnabled: controls.codex.state == .on,
            deepseekEnabled: controls.deepseek.state == .on,
            claudeEnabled: controls.claude.state == .on,
            keychainService: service,
            claudeKeychainService: claudeService,
            lowBalanceThreshold: low,
            showBars: controls.showBars.state == .on,
            barWidth: width,
            cacheFetch: fetch,
            cacheLocal: local,
            warn: warn,
            danger: danger,
            autoUpdate: controls.autoUpdate.state == .on,
            notificationsEnabled: controls.notifications.state == .on)
        do {
            let root = try ConfigStore.load(from: configPath)
            form.write(into: root)
            try ConfigStore.save(root, to: configPath)
        } catch {
            showAlert(error.localizedDescription)
            return
        }
        controls.status.stringValue = "已保存，下一轮刷新生效。"
        onSaved()
    }

    @objc private func checkClicked() {
        controls?.updateStatus.stringValue = "正在检查…"
        onCheckUpdate { [weak self] message in
            self?.controls?.updateStatus.stringValue = message
        }
    }

    private func requireNumber(_ field: NSTextField, name: String) -> NSNumber? {
        if let number = ConfigStore.parseNumber(field.stringValue) {
            return number
        }
        showAlert("「\(name)」需要填写数字。")
        return nil
    }

    private func showAlert(_ message: String) {
        guard let window else { return }
        let alert = NSAlert()
        alert.messageText = "无法保存"
        alert.informativeText = message
        alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window)
    }

    private func addSection(_ stack: NSStackView, title: String, rows: [NSView]) {
        let header = NSTextField(labelWithString: title)
        header.font = .boldSystemFont(ofSize: 13)
        stack.addArrangedSubview(header)
        stack.setCustomSpacing(4, after: header)
        for (index, row) in rows.enumerated() {
            stack.addArrangedSubview(row)
            if index == rows.count - 1 {
                stack.setCustomSpacing(14, after: row)
            }
        }
    }
}

private final class SettingsControls {
    let cursor: NSButton
    let codex: NSButton
    let deepseek: NSButton
    let claude: NSButton
    let service: NSTextField
    let claudeService: NSTextField
    let lowBalance: NSTextField
    let showBars: NSButton
    let barWidth: NSTextField
    let warn: NSTextField
    let danger: NSTextField
    let cacheFetch: NSTextField
    let cacheLocal: NSTextField
    let autoUpdate: NSButton
    let notifications: NSButton
    let checkButton: NSButton
    let updateStatus: NSTextField
    let saveButton: NSButton
    let status: NSTextField

    init(form: ConfigStore.Form, target: AnyObject, saveAction: Selector, checkAction: Selector) {
        cursor = checkbox("显示 Cursor", on: form.cursorEnabled)
        codex = checkbox("显示 ChatGPT", on: form.codexEnabled)
        deepseek = checkbox("显示 DeepSeek", on: form.deepseekEnabled)
        claude = checkbox("显示 Claude", on: form.claudeEnabled)
        service = editor(form.keychainService)
        claudeService = editor(form.claudeKeychainService)
        lowBalance = editor(ConfigStore.formatNumber(form.lowBalanceThreshold))
        showBars = checkbox("显示进度条", on: form.showBars)
        barWidth = editor(ConfigStore.formatNumber(form.barWidth))
        warn = editor(ConfigStore.formatNumber(form.warn))
        danger = editor(ConfigStore.formatNumber(form.danger))
        cacheFetch = editor(ConfigStore.formatNumber(form.cacheFetch))
        cacheLocal = editor(ConfigStore.formatNumber(form.cacheLocal))
        autoUpdate = checkbox("允许立即安装新版本", on: form.autoUpdate)
        notifications = checkbox("超过阈值时发出系统通知", on: form.notificationsEnabled)
        checkButton = NSButton(title: "检查更新", target: target, action: checkAction)
        checkButton.bezelStyle = .rounded
        updateStatus = wrappingLabel("检查更新后会显示结果。", size: 12, color: .secondaryLabelColor)
        saveButton = NSButton(title: "保存", target: target, action: saveAction)
        saveButton.bezelStyle = .rounded
        saveButton.keyEquivalent = "\r"
        status = NSTextField(labelWithString: "")
        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
    }
}

private func checkbox(_ title: String, on: Bool) -> NSButton {
    let button = NSButton(checkboxWithTitle: title, target: nil, action: nil)
    button.state = on ? .on : .off
    button.font = .systemFont(ofSize: 13)
    return button
}

private func editor(_ value: String) -> NSTextField {
    let field = NSTextField(string: value)
    field.font = .systemFont(ofSize: 13)
    field.translatesAutoresizingMaskIntoConstraints = false
    field.widthAnchor.constraint(equalToConstant: 180).isActive = true
    return field
}

private func labeledRow(_ title: String, _ field: NSTextField) -> NSStackView {
    let label = NSTextField(labelWithString: title)
    label.font = .systemFont(ofSize: 13)
    label.alignment = .right
    label.translatesAutoresizingMaskIntoConstraints = false
    label.widthAnchor.constraint(equalToConstant: 176).isActive = true
    let row = NSStackView(views: [label, field])
    row.orientation = .horizontal
    row.alignment = .centerY
    row.spacing = 8
    return row
}

private func wrappingLabel(_ text: String, size: CGFloat, color: NSColor) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .systemFont(ofSize: size)
    label.textColor = color
    label.preferredMaxLayoutWidth = 400
    return label
}
