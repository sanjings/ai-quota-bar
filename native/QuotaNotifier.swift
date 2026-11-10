// 系统通知。越线判断、去重和冷却在 lib/notify.py，这里不重写阈值。
//
// 菜单栏先更新，然后才到这里。权限、脚本、通知中心任何一步失败都吞掉，
// 不往外抛。账本在 cache/notify-ledger.json，只记发过的指纹，不含凭证。

import Cocoa
import UserNotifications

struct QuotaAlert: Codable, Equatable {
    let id: String
    let key: String
    let source: String
    let metric: String
    let level: String
    let value: Double
    let threshold: Double
    let cycle: String
    let title: String
    let body: String
}

enum QuotaNotifier {
    private static let queue = DispatchQueue(label: "dev.sanjings.quotabar.notifier")
    private static let presenter = Presenter()
    /// 用户刚在设置里点了保存。下一次处理时允许再问一次通知权限。
    private static var allowPrompt = false

    /// 菜单栏应用开着设置窗口时仍算前台，系统默认不展示横幅。
    static func install() {
        let assign = {
            UNUserNotificationCenter.current().delegate = presenter
        }
        if Thread.isMainThread {
            assign()
        } else {
            DispatchQueue.main.sync(execute: assign)
        }
    }

    static func allowPromptAgain() {
        queue.async {
            allowPrompt = true
        }
    }

    /// 立即返回。真正的发送在串行队列上，避免和下一轮刷新抢账本。
    static func deliver(alerts: [QuotaAlert], enabled: Bool, scriptPath: String) {
        queue.async {
            deliverOnQueue(alerts: alerts, enabled: enabled, scriptPath: scriptPath)
        }
    }

    private static func deliverOnQueue(alerts: [QuotaAlert], enabled: Bool, scriptPath: String) {
        guard let url = ledgerURL(scriptPath: scriptPath) else { return }
        var disk = load(url)
        if allowPrompt {
            allowPrompt = false
            disk.permission.deferred = false
        }
        // 没有待发项、也没有需要冷却清理的记录时，不必再起一次 Python。
        if alerts.isEmpty && disk.sent.isEmpty {
            if disk != load(url) {
                save(disk, to: url)
            }
            return
        }
        if !enabled && disk.sent.isEmpty {
            if disk != load(url) {
                save(disk, to: url)
            }
            return
        }

        guard let plan = runPlan(
            scriptPath: scriptPath,
            alerts: alerts,
            sent: disk.sent,
            enabled: enabled)
        else {
            // 去重脚本没跑成。权限上的「可以再问一次」仍然记下，已发记录不动。
            save(disk, to: url)
            return
        }

        let deliverKeys = plan.deliver.map { $0.key }
        var delivered = Set<String>()
        if !plan.deliver.isEmpty {
            switch authorize(&disk) {
            case .allowed:
                for alert in plan.deliver where post(alert) {
                    delivered.insert(alert.key)
                }
            case .skipped:
                break
            }
        }
        // 与 lib/notify.py 的 commit_deliveries 一致：没发出去的不记成已发。
        disk.sent = commit(
            previous: disk.sent,
            proposed: plan.ledger.sent,
            deliverKeys: deliverKeys,
            delivered: delivered)
        save(disk, to: url)
    }

    /// 只把实际发出去的写成新指纹。proposed 里已经丢掉过了冷却的恢复项。
    private static func commit(
        previous: [String: SentRecord],
        proposed: [String: SentRecord],
        deliverKeys: [String],
        delivered: Set<String>
    ) -> [String: SentRecord] {
        var sent = proposed
        for key in deliverKeys where !delivered.contains(key) {
            if let old = previous[key] {
                sent[key] = old
            } else {
                sent.removeValue(forKey: key)
            }
        }
        return sent
    }

    private enum AuthResult {
        case allowed
        case skipped
    }

    private static func authorize(_ disk: inout LedgerDisk) -> AuthResult {
        install()
        guard let status = currentStatus() else { return .skipped }
        switch status {
        case .authorized, .provisional:
            return .allowed
        case .denied:
            explainDenied(&disk)
            return .skipped
        case .notDetermined:
            if disk.permission.deferred {
                return .skipped
            }
            let proceed = confirm(
                "允许额度预警通知",
                "ChatGPT、Claude、Cursor 的用量达到设置里的预警或危险阈值，或者 DeepSeek 余额低于阈值时，会发一条系统通知。用的就是设置面板里的那些数字。同一阈值在同一次重置周期里只提醒一次。\n\n点「继续」后，系统会询问是否允许「AI Quota Bar」发送通知。",
                confirmTitle: "继续",
                cancelTitle: "以后再说")
            if !proceed {
                disk.permission.deferred = true
                return .skipped
            }
            if requestAccess() {
                return .allowed
            }
            if let status = currentStatus(), status == .denied {
                explainDenied(&disk)
            }
            return .skipped
        default:
            // 含 ephemeral。这个菜单栏应用用不到，没授权就不发。
            return .skipped
        }
    }

    private static func explainDenied(_ disk: inout LedgerDisk) {
        if disk.permission.deniedExplained { return }
        let open = confirm(
            "需要打开通知权限",
            "额度预警发不出去，是因为系统通知权限没开。请到「系统设置 → 通知 → AI Quota Bar」允许通知。菜单栏上的额度不受影响。",
            confirmTitle: "打开系统设置",
            cancelTitle: "以后再说")
        disk.permission.deniedExplained = true
        if open {
            openNotificationSettings()
        }
    }

    private static func confirm(
        _ title: String,
        _ body: String,
        confirmTitle: String,
        cancelTitle: String
    ) -> Bool {
        var allowed = false
        let present = {
            let alert = NSAlert()
            alert.alertStyle = .informational
            alert.messageText = title
            alert.informativeText = body
            alert.addButton(withTitle: confirmTitle)
            alert.addButton(withTitle: cancelTitle)
            NSApp.activate(ignoringOtherApps: true)
            allowed = alert.runModal() == .alertFirstButtonReturn
        }
        if Thread.isMainThread {
            present()
        } else {
            DispatchQueue.main.sync(execute: present)
        }
        return allowed
    }

    private static func openNotificationSettings() {
        let version = ProcessInfo.processInfo.operatingSystemVersion.majorVersion
        let target = version >= 13
            ? "x-apple.systempreferences:com.apple.Notifications-Settings.extension"
            : "x-apple.systempreferences:com.apple.preference.notifications"
        guard let url = URL(string: target) else { return }
        DispatchQueue.main.async {
            NSWorkspace.shared.open(url)
        }
    }

    private static func currentStatus() -> UNAuthorizationStatus? {
        let sem = DispatchSemaphore(value: 0)
        var status: UNAuthorizationStatus?
        UNUserNotificationCenter.current().getNotificationSettings { settings in
            status = settings.authorizationStatus
            sem.signal()
        }
        if sem.wait(timeout: .now() + 8) == .timedOut {
            return nil
        }
        return status
    }

    /// 系统对话框要等用户点。不设短超时，避免对话框还开着就把这一轮当成拒绝。
    private static func requestAccess() -> Bool {
        let sem = DispatchSemaphore(value: 0)
        var granted = false
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, _ in
            granted = ok
            sem.signal()
        }
        sem.wait()
        return granted
    }

    private static func post(_ alert: QuotaAlert) -> Bool {
        let content = UNMutableNotificationContent()
        content.title = alert.title
        content.body = alert.body
        content.sound = .default
        content.threadIdentifier = alert.key
        let identifier = alert.id.isEmpty ? alert.key : alert.id
        let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
        let sem = DispatchSemaphore(value: 0)
        var posted = false
        UNUserNotificationCenter.current().add(request) { error in
            posted = error == nil
            sem.signal()
        }
        if sem.wait(timeout: .now() + 8) == .timedOut {
            return false
        }
        return posted
    }

    private static func runPlan(
        scriptPath: String,
        alerts: [QuotaAlert],
        sent: [String: SentRecord],
        enabled: Bool
    ) -> Plan? {
        var sentJSON: [String: Any] = [:]
        for (key, record) in sent {
            sentJSON[key] = ["fingerprint": record.fingerprint, "at": record.at]
        }
        let alertsJSON: [[String: Any]] = alerts.map { alert in
            [
                "id": alert.id,
                "key": alert.key,
                "source": alert.source,
                "metric": alert.metric,
                "level": alert.level,
                "value": alert.value,
                "threshold": alert.threshold,
                "cycle": alert.cycle,
                "title": alert.title,
                "body": alert.body,
            ]
        }
        let body: [String: Any] = [
            "alerts": alertsJSON,
            "ledger": ["sent": sentJSON],
            "enabled": enabled,
            "now": Date().timeIntervalSince1970,
        ]
        guard JSONSerialization.isValidJSONObject(body),
              let data = try? JSONSerialization.data(withJSONObject: body),
              let output = runProcess(scriptPath: scriptPath, stdin: data),
              output.status == 0,
              let plan = try? JSONDecoder().decode(Plan.self, from: output.data),
              plan.ok
        else { return nil }
        return plan
    }

    private struct ProcessOutput {
        let status: Int32
        let data: Data
    }

    private static func runProcess(scriptPath: String, stdin: Data) -> ProcessOutput? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = ["--plan-notifications"]
        let input = Pipe()
        let output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        input.fileHandleForWriting.write(stdin)
        try? input.fileHandleForWriting.close()

        let limit = Date().addingTimeInterval(8)
        while process.isRunning && Date() < limit {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
            return nil
        }
        process.waitUntilExit()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        return ProcessOutput(status: process.terminationStatus, data: data)
    }

    private static func ledgerURL(scriptPath: String) -> URL? {
        guard let config = ConfigStore.configPath(besideScript: scriptPath) else { return nil }
        return URL(fileURLWithPath: config)
            .deletingLastPathComponent()
            .appendingPathComponent("cache", isDirectory: true)
            .appendingPathComponent("notify-ledger.json")
    }

    private static func load(_ url: URL) -> LedgerDisk {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return LedgerDisk() }
        var disk = LedgerDisk()
        if let sent = object["sent"] as? [String: Any] {
            for (key, value) in sent {
                guard let record = value as? [String: Any],
                      let fingerprint = record["fingerprint"] as? String
                else { continue }
                let at = (record["at"] as? NSNumber)?.doubleValue ?? 0
                disk.sent[key] = SentRecord(fingerprint: fingerprint, at: at)
            }
        }
        if let permission = object["permission"] as? [String: Any] {
            disk.permission.deferred = (permission["deferred"] as? NSNumber)?.boolValue ?? false
            disk.permission.deniedExplained = (permission["deniedExplained"] as? NSNumber)?.boolValue ?? false
        }
        return disk
    }

    private static func save(_ disk: LedgerDisk, to url: URL) {
        if load(url) == disk { return }
        var sent: [String: Any] = [:]
        for (key, record) in disk.sent {
            sent[key] = ["fingerprint": record.fingerprint, "at": record.at]
        }
        let object: [String: Any] = [
            "permission": [
                "deferred": disk.permission.deferred,
                "deniedExplained": disk.permission.deniedExplained,
            ],
            "sent": sent,
        ]
        do {
            try FileManager.default.createDirectory(
                at: url.deletingLastPathComponent(),
                withIntermediateDirectories: true)
            let data = try JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys])
            var text = String(data: data, encoding: .utf8) ?? ""
            if !text.hasSuffix("\n") {
                text += "\n"
            }
            try Data(text.utf8).write(to: url, options: .atomic)
        } catch {
            return
        }
    }
}

private struct SentRecord: Codable, Equatable {
    var fingerprint: String
    var at: Double
}

private struct PermissionState: Equatable {
    var deferred = false
    var deniedExplained = false
}

private struct LedgerDisk: Equatable {
    var sent: [String: SentRecord] = [:]
    var permission = PermissionState()
}

/// 计划脚本的返回。缺字段就解码失败，调用方不改账本。
private struct Plan: Decodable {
    let ok: Bool
    let deliver: [QuotaAlert]
    let ledger: SentBox

    struct SentBox: Decodable {
        let sent: [String: SentRecord]
    }
}

private final class Presenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        if #available(macOS 11.0, *) {
            completionHandler([.banner, .list, .sound])
        } else {
            completionHandler([.alert, .sound])
        }
    }
}
