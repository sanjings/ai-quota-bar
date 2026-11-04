// 轻量版本检查。比较和网络都在 lib/release_check.py。
//
// 这里只负责：启动后（以及每 6 小时）调一次脚本，把结果留在内存里。
// 有更新时，菜单顶部多一行，点一下用浏览器打开 Release 页。
// 不下载、不替换 .app。脚本失败、超时、JSON 不对，都当成这次没问成，
// 不改额度的 payload，也不弹窗。

import Foundation

struct ReleaseNotice: Equatable {
    let tag: String
    let url: URL
}

enum ReleaseCheckOutcome: Equatable {
    case update(ReleaseNotice)
    case upToDate
    case failed
}

enum ReleaseCheck {
    private static let timeout: TimeInterval = 12

    static func currentMarketingVersion() -> String? {
        guard let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String else {
            return nil
        }
        let trimmed = version.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// 在后台队列调用。超时会结束子进程并返回 .failed。
    static func run(scriptPath: String, current: String) -> ReleaseCheckOutcome {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = ["--check-release", current]
        process.standardInput = FileHandle.nullDevice
        // stderr 不读。挂上管道又不读，输出一多会把子进程堵死。
        process.standardError = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output

        do {
            try process.run()
        } catch {
            return .failed
        }

        let limit = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < limit {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
            return .failed
        }
        process.waitUntilExit()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0 else { return .failed }
        return parse(data)
    }

    private struct Response: Decodable {
        let ok: Bool
        let update: Bool?
        let tag: String?
        let url: String?
    }

    private static func parse(_ data: Data) -> ReleaseCheckOutcome {
        guard let body = try? JSONDecoder().decode(Response.self, from: data), body.ok else {
            return .failed
        }
        guard let update = body.update else { return .failed }
        guard update else { return .upToDate }
        guard let tag = body.tag, let urlString = body.url, let url = releaseURL(urlString) else {
            return .failed
        }
        return .update(ReleaseNotice(tag: tag, url: url))
    }

    /// 只打开本仓库的 https Release 页。别的地址当成这次没问成。
    private static func releaseURL(_ string: String) -> URL? {
        guard let components = URLComponents(string: string),
              components.scheme == "https",
              components.host == "github.com",
              components.user == nil,
              components.password == nil,
              components.port == nil,
              components.query == nil
        else { return nil }
        let path = components.path
        let prefix = "/sanjings/ai-quota-bar/releases/tag/"
        guard path.hasPrefix(prefix) else { return nil }
        let tag = String(path.dropFirst(prefix.count))
        guard !tag.isEmpty, !tag.contains("/"), !tag.contains("..") else { return nil }
        return components.url
    }
}
