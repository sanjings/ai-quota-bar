// 读写与 quota.1m.sh 同目录的 config.json。
//
// Python 每次启动都 load_config()：这里只负责把文件写回去，
// 下一轮刷新自然读到新值。面板没展示的键原样保留。

import Foundation

enum ConfigStoreError: LocalizedError {
    case notObject
    case unreadable(String)

    var errorDescription: String? {
        switch self {
        case .notObject:
            return "config.json 的顶层不是对象。"
        case .unreadable(let detail):
            return detail
        }
    }
}

enum ConfigStore {
    /// 与 quota.1m.sh 里 `cd -P` 之后的目录一致，软链也落到同一份 config.json。
    static func configPath(besideScript scriptPath: String) -> String? {
        guard scriptPath.contains("/") else { return nil }
        let resolved = (scriptPath as NSString).resolvingSymlinksInPath
        let directory = (resolved as NSString).deletingLastPathComponent
        guard !directory.isEmpty else { return nil }
        return (directory as NSString).appendingPathComponent("config.json")
    }

    /// 文件不存在时返回空对象（与 Python 读失败后走默认配置一样）。
    /// 解析失败则抛出，调用方不得覆盖原文件。
    static func load(from path: String) throws -> NSMutableDictionary {
        guard FileManager.default.fileExists(atPath: path) else {
            return NSMutableDictionary()
        }
        let data: Data
        do {
            data = try Data(contentsOf: URL(fileURLWithPath: path))
        } catch {
            throw ConfigStoreError.unreadable(error.localizedDescription)
        }
        if data.isEmpty {
            return NSMutableDictionary()
        }
        let object: Any
        do {
            object = try JSONSerialization.jsonObject(with: data, options: [.mutableContainers])
        } catch {
            throw ConfigStoreError.unreadable("config.json 无法解析：\(error.localizedDescription)")
        }
        guard let root = object as? NSMutableDictionary else {
            throw ConfigStoreError.notObject
        }
        return root
    }

    static func save(_ root: NSMutableDictionary, to path: String) throws {
        let data = try JSONSerialization.data(
            withJSONObject: root,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        var text = String(data: data, encoding: .utf8) ?? ""
        if !text.hasSuffix("\n") {
            text += "\n"
        }
        try Data(text.utf8).write(to: URL(fileURLWithPath: path), options: .atomic)
    }

    /// 复用已有子字典，这样保存不会丢掉面板没编辑的键。
    static func child(_ key: String, in parent: NSMutableDictionary) -> NSMutableDictionary {
        if let existing = parent[key] as? NSMutableDictionary {
            return existing
        }
        if let existing = parent[key] as? NSDictionary {
            let copy = NSMutableDictionary(dictionary: existing)
            parent[key] = copy
            return copy
        }
        let created = NSMutableDictionary()
        parent[key] = created
        return created
    }

    static func parseNumber(_ text: String) -> NSNumber? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if let integer = Int(trimmed) {
            return NSNumber(value: integer)
        }
        guard let value = Double(trimmed), value.isFinite else { return nil }
        return NSNumber(value: value)
    }

    static func formatNumber(_ number: NSNumber) -> String {
        if isBool(number) { return "" }
        let value = number.doubleValue
        guard value.isFinite else { return "" }
        if value.rounded() == value, abs(value) <= Double(Int.max) {
            return String(Int(value))
        }
        return String(value)
    }

    private static func isBool(_ number: NSNumber) -> Bool {
        CFGetTypeID(number) == CFBooleanGetTypeID()
    }

    struct Form {
        var cursorEnabled: Bool
        var codexEnabled: Bool
        var deepseekEnabled: Bool
        var keychainService: String
        var lowBalanceThreshold: NSNumber
        var showBars: Bool
        var barWidth: NSNumber
        var cacheFetch: NSNumber
        var cacheLocal: NSNumber
        var warn: NSNumber
        var danger: NSNumber
        /// 关闭时只提示并打开 Release 页。打开后菜单可以下载并安装。
        var autoUpdate: Bool

        static func read(from root: NSDictionary) -> Form {
            let cursor = root["cursor"] as? NSDictionary
            let codex = root["codex"] as? NSDictionary
            let deepseek = root["deepseek"] as? NSDictionary
            let display = root["display"] as? NSDictionary
            let cache = display?["cacheSeconds"] as? NSDictionary
            let thresholds = root["thresholds"] as? NSDictionary
            let update = root["update"] as? NSDictionary
            return Form(
                cursorEnabled: readBool(cursor?["enabled"], default: true),
                codexEnabled: readBool(codex?["enabled"], default: true),
                deepseekEnabled: readBool(deepseek?["enabled"], default: true),
                keychainService: (deepseek?["keychainService"] as? String) ?? "deepseek-api-key",
                lowBalanceThreshold: readNumber(deepseek?["lowBalanceThreshold"], default: 20),
                showBars: readBool(display?["showBars"], default: true),
                barWidth: readNumber(display?["barWidth"], default: 8),
                cacheFetch: readNumber(cache?["fetch"], default: 300),
                cacheLocal: readNumber(cache?["local"], default: 20),
                warn: readNumber(thresholds?["warn"], default: 50),
                danger: readNumber(thresholds?["danger"], default: 80),
                autoUpdate: readBool(update?["auto"], default: false))
        }

        func write(into root: NSMutableDictionary) {
            let cursor = ConfigStore.child("cursor", in: root)
            cursor["enabled"] = cursorEnabled
            let codex = ConfigStore.child("codex", in: root)
            codex["enabled"] = codexEnabled
            let deepseek = ConfigStore.child("deepseek", in: root)
            deepseek["enabled"] = deepseekEnabled
            deepseek["keychainService"] = keychainService
            deepseek["lowBalanceThreshold"] = lowBalanceThreshold
            let display = ConfigStore.child("display", in: root)
            display["showBars"] = showBars
            display["barWidth"] = barWidth
            let cache = ConfigStore.child("cacheSeconds", in: display)
            cache["fetch"] = cacheFetch
            cache["local"] = cacheLocal
            let thresholds = ConfigStore.child("thresholds", in: root)
            thresholds["warn"] = warn
            thresholds["danger"] = danger
            let update = ConfigStore.child("update", in: root)
            update["auto"] = autoUpdate
        }
    }

    private static func readBool(_ value: Any?, default defaultValue: Bool) -> Bool {
        guard let number = value as? NSNumber else { return defaultValue }
        return number.boolValue
    }

    private static func readNumber(_ value: Any?, default defaultValue: Int) -> NSNumber {
        guard let number = value as? NSNumber, !isBool(number) else {
            return NSNumber(value: defaultValue)
        }
        return number
    }
}
