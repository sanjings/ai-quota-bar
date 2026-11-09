// 菜单「设置…」打开的窗口。同时只保留一份；关掉之后再开会重新读文件。

import Cocoa

final class SettingsWindowController: NSWindowController, NSWindowDelegate {
    private let configPath: String
    private let onSaved: () -> Void
    private var controls: SettingsControls?
    private static var shared: SettingsWindowController?

    static func show(configPath: String, onSaved: @escaping () -> Void) {
        if let existing = shared, existing.window?.isVisible == true {
            existing.window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        let controller = SettingsWindowController(configPath: configPath, onSaved: onSaved)
        shared = controller
        controller.window?.center()
        controller.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private init(configPath: String, onSaved: @escaping () -> Void) {
        self.configPath = configPath
        self.onSaved = onSaved
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
            let controls = SettingsControls(form: form, target: self, saveAction: #selector(saveClicked))
            self.controls = controls
            addSection(stack, title: "数据源", rows: [
                controls.cursor,
                controls.codex,
                controls.deepseek,
                labeledRow("DeepSeek 钥匙串服务名", controls.service),
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
            addSection(stack, title: "缓存（秒）", rows: [
                labeledRow("网络数据源", controls.cacheFetch),
                labeledRow("本地数据源", controls.cacheLocal),
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
        guard !service.isEmpty else {
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
            keychainService: service,
            lowBalanceThreshold: low,
            showBars: controls.showBars.state == .on,
            barWidth: width,
            cacheFetch: fetch,
            cacheLocal: local,
            warn: warn,
            danger: danger)
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
    let service: NSTextField
    let lowBalance: NSTextField
    let showBars: NSButton
    let barWidth: NSTextField
    let warn: NSTextField
    let danger: NSTextField
    let cacheFetch: NSTextField
    let cacheLocal: NSTextField
    let saveButton: NSButton
    let status: NSTextField

    init(form: ConfigStore.Form, target: AnyObject, saveAction: Selector) {
        cursor = checkbox("显示 Cursor", on: form.cursorEnabled)
        codex = checkbox("显示 ChatGPT", on: form.codexEnabled)
        deepseek = checkbox("显示 DeepSeek", on: form.deepseekEnabled)
        service = editor(form.keychainService)
        lowBalance = editor(ConfigStore.formatNumber(form.lowBalanceThreshold))
        showBars = checkbox("显示进度条", on: form.showBars)
        barWidth = editor(ConfigStore.formatNumber(form.barWidth))
        warn = editor(ConfigStore.formatNumber(form.warn))
        danger = editor(ConfigStore.formatNumber(form.danger))
        cacheFetch = editor(ConfigStore.formatNumber(form.cacheFetch))
        cacheLocal = editor(ConfigStore.formatNumber(form.cacheLocal))
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
