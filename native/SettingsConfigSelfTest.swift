// 不编进 App。本机：
// swiftc -parse-as-library -o /tmp/settings-config-test \
//   native/SettingsConfig.swift native/SettingsConfigSelfTest.swift && /tmp/settings-config-test

import Foundation

private struct SettingsConfigTestFailure: Error, CustomStringConvertible {
    let description: String
}

extension ConfigStore {
    static func selfTest() -> Int32 {
        do {
            try runSelfTest()
            print("settings config self-test ok")
            return 0
        } catch {
            fputs("\(error)\n", stderr)
            return 1
        }
    }

    private static func runSelfTest() throws {
        if parseNumber("abc") != nil || parseNumber("  ") != nil {
            throw SettingsConfigTestFailure(description: "非数字应被拒绝")
        }
        if parseNumber(" 8 ")?.intValue != 8 || parseNumber("20.5")?.doubleValue != 20.5 {
            throw SettingsConfigTestFailure(description: "数字解析不正确")
        }
        if configPath(besideScript: "quota.1m.sh") != nil {
            throw SettingsConfigTestFailure(description: "没有目录的脚本路径不应猜 config.json")
        }

        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("quota-bar-settings-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let realDir = root.appendingPathComponent("real", isDirectory: true)
        let linkDir = root.appendingPathComponent("link", isDirectory: true)
        try FileManager.default.createDirectory(at: realDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: linkDir, withIntermediateDirectories: true)
        let script = realDir.appendingPathComponent("quota.1m.sh")
        try Data("#!/bin/sh\n".utf8).write(to: script)
        let link = linkDir.appendingPathComponent("quota.1m.sh")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: script)
        let resolvedConfig = realDir.appendingPathComponent("config.json").resolvingSymlinksInPath().path
        guard let linked = configPath(besideScript: link.path) else {
            throw SettingsConfigTestFailure(description: "软链脚本没有解析出 config 路径")
        }
        if URL(fileURLWithPath: linked).resolvingSymlinksInPath().path != resolvedConfig {
            throw SettingsConfigTestFailure(description: "config 路径没有跟着脚本软链走：\(linked)")
        }

        let missing = root.appendingPathComponent("missing.json").path
        let empty = try load(from: missing)
        if FileManager.default.fileExists(atPath: missing) {
            throw SettingsConfigTestFailure(description: "读取不存在的文件时不应创建它")
        }
        let defaults = Form.read(from: empty)
        if !defaults.showBars || defaults.barWidth.intValue != 8
            || defaults.keychainService != "deepseek-api-key" || !defaults.codexEnabled {
            throw SettingsConfigTestFailure(description: "缺省配置与 Python DEFAULT_CONFIG 不一致")
        }

        let bad = root.appendingPathComponent("bad.json")
        try "not-json".write(to: bad, atomically: true, encoding: .utf8)
        var rejected = false
        do {
            _ = try load(from: bad.path)
        } catch {
            rejected = true
        }
        let badText = try String(contentsOf: bad, encoding: .utf8)
        if !rejected || badText != "not-json" {
            throw SettingsConfigTestFailure(description: "坏 JSON 应拒绝且不改文件")
        }
        try "[]".write(to: bad, atomically: true, encoding: .utf8)
        rejected = false
        do {
            _ = try load(from: bad.path)
        } catch let error as ConfigStoreError {
            if case .notObject = error { rejected = true }
        }
        if !rejected {
            throw SettingsConfigTestFailure(description: "顶层数组应拒绝")
        }

        let configURL = root.appendingPathComponent("config.json")
        let original = """
        {
          "cursor": {"enabled": true, "note": "keep"},
          "codex": {"enabled": false},
          "deepseek": {
            "enabled": true,
            "keychainService": "deepseek-api-key",
            "lowBalanceThreshold": 20.0
          },
          "display": {
            "cacheSeconds": {"fetch": 300, "local": 20, "extra": 7},
            "showBars": true,
            "barWidth": 8
          },
          "thresholds": {"warn": 50.0, "danger": 80.0},
          "future": {"on": true}
        }
        """
        try original.write(to: configURL, atomically: true, encoding: .utf8)
        let loaded = try load(from: configURL.path)
        var form = Form.read(from: loaded)
        guard form.showBars, !form.codexEnabled else {
            throw SettingsConfigTestFailure(description: "没有读到原配置")
        }
        form.showBars = false
        form.lowBalanceThreshold = try mustParse(formatNumber(form.lowBalanceThreshold))
        form.barWidth = try mustParse(formatNumber(form.barWidth))
        form.cacheFetch = try mustParse(formatNumber(form.cacheFetch))
        form.cacheLocal = try mustParse(formatNumber(form.cacheLocal))
        form.warn = try mustParse(formatNumber(form.warn))
        form.danger = try mustParse(formatNumber(form.danger))
        form.write(into: loaded)
        try save(loaded, to: configURL.path)

        let saved = try load(from: configURL.path)
        let display = saved["display"] as? NSDictionary
        let cache = display?["cacheSeconds"] as? NSDictionary
        let cursor = saved["cursor"] as? NSDictionary
        let future = saved["future"] as? NSDictionary
        if (display?["showBars"] as? NSNumber)?.boolValue != false
            || (cache?["extra"] as? NSNumber)?.intValue != 7
            || cursor?["note"] as? String != "keep"
            || (future?["on"] as? NSNumber)?.boolValue != true {
            throw SettingsConfigTestFailure(description: "保存后丢了字段或 showBars 没写成 false")
        }
        let text = try String(contentsOf: configURL, encoding: .utf8)
        if !text.hasSuffix("\n") {
            throw SettingsConfigTestFailure(description: "保存结果应以换行结尾")
        }
        if let message = pythonReadsShowBarsFalse(configURL.path) {
            throw SettingsConfigTestFailure(description: message + "\n" + text)
        }
    }

    private static func mustParse(_ text: String) throws -> NSNumber {
        guard let number = parseNumber(text) else {
            throw SettingsConfigTestFailure(description: "无法把 \(text) 解析回数字")
        }
        return number
    }

    /// 与 lib/report.py 一样用 json.load。false 必须是 JSON bool，不能是 0。
    private static func pythonReadsShowBarsFalse(_ path: String) -> String? {
        let source = """
        import json, sys
        with open(sys.argv[1], encoding="utf-8") as handle:
            cfg = json.load(handle)
        assert cfg["display"]["showBars"] is False
        assert cfg["display"]["barWidth"] == 8
        assert cfg["display"]["cacheSeconds"]["fetch"] == 300
        assert cfg["display"]["cacheSeconds"]["local"] == 20
        assert cfg["display"]["cacheSeconds"]["extra"] == 7
        assert cfg["cursor"]["enabled"] is True
        assert cfg["cursor"]["note"] == "keep"
        assert cfg["codex"]["enabled"] is False
        assert cfg["deepseek"]["keychainService"] == "deepseek-api-key"
        assert cfg["deepseek"]["lowBalanceThreshold"] == 20
        assert cfg["thresholds"]["warn"] == 50
        assert cfg["thresholds"]["danger"] == 80
        assert cfg["future"]["on"] is True
        """
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", source, path]
        process.standardOutput = FileHandle.nullDevice
        let errors = Pipe()
        process.standardError = errors
        do {
            try process.run()
        } catch {
            return "无法启动 python3：\(error.localizedDescription)"
        }
        let stderr = errors.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        if process.terminationStatus == 0 { return nil }
        let text = String(data: stderr, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return text.isEmpty ? "python 退出码 \(process.terminationStatus)" : text
    }
}

@main
struct SettingsConfigSelfTestMain {
    static func main() {
        exit(ConfigStore.selfTest())
    }
}
