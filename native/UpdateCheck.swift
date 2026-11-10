// 版本检查，以及「立即更新」之前的下载和校验。
//
// 比较、下载、校验都在 lib/release_check.py。
// 这里只负责：启动后（以及每 6 小时）问一次有没有新的正式 Release。
// 有更新时菜单多一行。没打开「允许立即安装」时，点一下用浏览器打开 Release 页。
// 打开之后，点「立即更新」才会下载 zip 并用同页的 .sha256 校验。
// 校验通过后仍要用户再确认，才替换安装目录。失败、超时、JSON 不对，
// 都当成这次没做成，不改额度的 payload。

import Foundation

struct ReleaseNotice: Equatable {
    let tag: String
    let url: URL
    let zip: URL?
    let sha256: URL?
}

enum ReleaseCheckOutcome: Equatable {
    case update(ReleaseNotice)
    case upToDate
    case failed
}

enum StageResult: Equatable {
    case ready(URL)
    case failed(String)
}

enum ReleaseCheck {
    private static let checkTimeout: TimeInterval = 12
    private static let stageTimeout: TimeInterval = 90

    static func currentMarketingVersion() -> String? {
        guard let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String else {
            return nil
        }
        let trimmed = version.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// 在后台队列调用。超时会结束子进程并返回 .failed。
    static func run(scriptPath: String, current: String) -> ReleaseCheckOutcome {
        guard let output = runProcess(
            scriptPath: scriptPath,
            arguments: ["--check-release", current],
            timeout: checkTimeout)
        else { return .failed }
        guard output.status == 0 else { return .failed }
        return parse(output.data)
    }

    /// 下载并校验。不替换安装目录。超时或失败时给出可以直接展示的中文。
    static func stage(scriptPath: String, zip: URL, sha: URL) -> StageResult {
        guard let output = runProcess(
            scriptPath: scriptPath,
            arguments: ["--stage-release", zip.absoluteString, sha.absoluteString],
            timeout: stageTimeout)
        else {
            return .failed("下载超时。没有改动当前安装。")
        }
        guard output.status == 0,
              let body = try? JSONDecoder().decode(StageBody.self, from: output.data)
        else {
            return .failed("更新准备失败。没有改动当前安装。")
        }
        if body.ok, let root = body.root, let staged = stagedRoot(root) {
            return .ready(staged)
        }
        let message = body.error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if message.isEmpty {
            return .failed("更新准备失败。没有改动当前安装。")
        }
        return .failed(message)
    }

    private struct Output {
        let status: Int32
        let data: Data
    }

    private static func runProcess(scriptPath: String, arguments: [String], timeout: TimeInterval) -> Output? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: scriptPath)
        process.arguments = arguments
        process.standardInput = FileHandle.nullDevice
        // stderr 不读。挂上管道又不读，输出一多会把子进程堵死。
        process.standardError = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output

        do {
            try process.run()
        } catch {
            return nil
        }

        let limit = Date().addingTimeInterval(timeout)
        while process.isRunning && Date() < limit {
            Thread.sleep(forTimeInterval: 0.1)
        }
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
            return nil
        }
        process.waitUntilExit()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        return Output(status: process.terminationStatus, data: data)
    }

    private struct Response: Decodable {
        let ok: Bool
        let update: Bool?
        let tag: String?
        let url: String?
        let zip: String?
        let sha256: String?
    }

    private struct StageBody: Decodable {
        let ok: Bool
        let root: String?
        let error: String?
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
        let zip = body.zip.flatMap(assetURL)
        let sha = body.sha256.flatMap(assetURL)
        let pair = zip != nil && sha != nil
        return .update(ReleaseNotice(
            tag: tag,
            url: url,
            zip: pair ? zip : nil,
            sha256: pair ? sha : nil))
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

    /// 只接受本仓库 Release 上的 zip / .sha256。别的地址不下载。
    private static func assetURL(_ string: String) -> URL? {
        guard let components = URLComponents(string: string),
              components.scheme == "https",
              components.host == "github.com",
              components.user == nil,
              components.password == nil,
              components.port == nil,
              components.query == nil
        else { return nil }
        let path = components.path
        let prefix = "/sanjings/ai-quota-bar/releases/download/"
        guard path.hasPrefix(prefix) else { return nil }
        let rest = path.dropFirst(prefix.count)
        let pieces = rest.split(separator: "/", maxSplits: 1, omittingEmptySubsequences: false)
        guard pieces.count == 2 else { return nil }
        let tag = String(pieces[0])
        let name = String(pieces[1])
        guard !tag.isEmpty, !tag.contains(".."), !name.contains(".."), !name.contains("/") else {
            return nil
        }
        let isZip = name.hasPrefix("AI-Quota-Bar-") && name.hasSuffix("-macos.zip")
        let isSha = name.hasPrefix("AI-Quota-Bar-") && name.hasSuffix("-macos.zip.sha256")
        guard isZip || isSha else { return nil }
        return components.url
    }

    /// 只接受临时目录里、由 --stage-release 创建的那一层。
    private static func stagedRoot(_ path: String) -> URL? {
        guard path.hasPrefix("/") else { return nil }
        let resolved = URL(fileURLWithPath: path, isDirectory: true).resolvingSymlinksInPath()
        let temp = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true).resolvingSymlinksInPath()
        let tempPrefix = temp.path.hasSuffix("/") ? temp.path : temp.path + "/"
        guard resolved.path.hasPrefix(tempPrefix) else { return nil }
        guard resolved.deletingLastPathComponent().lastPathComponent.hasPrefix("quota-staged-") else {
            return nil
        }
        let binary = resolved.appendingPathComponent("AI Quota Bar.app/Contents/MacOS/ai-quota-bar").path
        guard FileManager.default.fileExists(atPath: binary) else { return nil }
        return resolved
    }
}
