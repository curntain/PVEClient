import AppKit
import Combine
import Foundation

/// Runs the bundled Python backend (`--no-window --print-url`) and publishes the
/// local URL it reports, so the SwiftUI shell can point a WKWebView at it.
/// All @Published mutations happen on the main actor (see the Task hops below).
final class BackendProcess: ObservableObject {
    enum Status: Equatable {
        case idle
        case starting
        case ready(URL)
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var log: String = ""
    @Published private(set) var statusMessage: String = "正在启动内置服务…"

    static var shared: BackendProcess?

    private var process: Process?
    private var output = ""
    private var stopped = false

    init() {
        BackendProcess.shared = self
    }

    var currentURL: URL? {
        if case .ready(let url) = status { return url }
        return nil
    }

    // MARK: - lifecycle

    func start() {
        guard process == nil else { return }
        stopped = false
        log = ""

        let environment = BackendProcess.childEnvironment()
        if environment["PVE_CLIENT_FORCE_LOCAL"] != "1",
           let publicURL = BackendProcess.publicURL(in: environment) {
            if let localURL = BackendProcess.localURL(in: environment) {
                statusMessage = "正在检测内网连接…"
                status = .starting
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    let onLAN = await BackendProcess.endpointIsReachable(localURL)
                    guard !self.stopped else { return }
                    let selectedURL = onLAN ? localURL : publicURL
                    self.log = onLAN
                        ? "内网直连模式：\(selectedURL.absoluteString)\n"
                        : "外网安全模式：\(selectedURL.absoluteString)\n"
                    self.status = .ready(selectedURL)
                }
                return
            }
            // Public deployments already provide the API, SSH bridge and web UI.
            // Loading them directly avoids unpacking and booting the bundled
            // Python sidecar before the first window can render.
            log = "公网极速模式：\(publicURL.absoluteString)\n"
            status = .ready(publicURL)
            return
        }

        statusMessage = "正在启动内置服务…"
        status = .starting

        guard let executable = BackendProcess.backendExecutable() else {
            status = .failed("找不到内置后端程序（Contents/Resources/backend/pve-client-backend）")
            return
        }

        let task = Process()
        task.executableURL = executable
        task.arguments = ["--no-window", "--print-url"]
        task.environment = environment
        if let dir = Bundle.main.resourceURL?.appendingPathComponent("backend") {
            task.currentDirectoryURL = dir
        }

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in BackendProcess.shared?.consume(text) }
        }
        task.terminationHandler = { finished in
            Task { @MainActor in
                guard let self = BackendProcess.shared else { return }
                self.process = nil
                if self.stopped { return }
                if case .ready = self.status {
                    self.status = .failed("后端已退出（退出码 \(finished.terminationStatus)）")
                } else if case .starting = self.status {
                    self.status = .failed("后端启动失败，请看下方日志")
                }
            }
        }

        do {
            try task.run()
        } catch {
            status = .failed("启动后端失败：\(error.localizedDescription)")
            return
        }
        process = task

        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 40_000_000_000)
            guard let self else { return }
            if case .starting = self.status {
                self.status = .failed("后端启动超时（40 秒），请查看日志")
            }
        }
    }

    func stop() {
        stopped = true
        process?.terminationHandler = nil
        process?.terminate()
        process = nil
    }

    func restart() {
        stop()
        status = .idle
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.start() }
    }

    func openInBrowser() {
        if let url = currentURL { NSWorkspace.shared.open(url) }
    }

    func revealDataFolder() {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dir = home.appendingPathComponent("Library/Application Support/PVEClient")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.activateFileViewerSelecting([dir])
    }

    // MARK: - backend output

    private func consume(_ text: String) {
        output += text
        if output.count > 20000 { output = String(output.suffix(12000)) }
        log = output

        while let index = output.firstIndex(of: "\n") {
            let line = String(output[output.startIndex..<index]).trimmingCharacters(in: .whitespacesAndNewlines)
            output = String(output[output.index(after: index)...])
            handle(line)
        }
    }

    private func handle(_ line: String) {
        guard !line.isEmpty, line.hasPrefix("{") else { return }
        guard let data = line.data(using: .utf8),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              object["event"] as? String == "ready",
              let urlString = object["url"] as? String,
              let url = URL(string: urlString)
        else { return }
        status = .ready(url)
    }

    // MARK: - locating the sidecar / environment

    static func backendExecutable() -> URL? {
        if let resources = Bundle.main.resourceURL {
            let names = ["pve-client-backend", "PVE远程管理客户端"]
            for name in names {
                let candidate = resources.appendingPathComponent("backend/\(name)")
                if FileManager.default.isExecutableFile(atPath: candidate.path) { return candidate }
            }
        }
        // development fallback: PVE_CLIENT_REPO=/path/to/pve-client and a venv python
        if let repo = ProcessInfo.processInfo.environment["PVE_CLIENT_REPO"] {
            let python = URL(fileURLWithPath: repo).appendingPathComponent(".venv-mac/bin/python3")
            if FileManager.default.isExecutableFile(atPath: python.path) { return python }
        }
        return nil
    }

    static func publicURL(in environment: [String: String]) -> URL? {
        return webURL(environment["PVE_CLIENT_PUBLIC_URL"])
    }

    static func localURL(in environment: [String: String]) -> URL? {
        return webURL(environment["PVE_CLIENT_LOCAL_URL"])
    }

    private static func webURL(_ value: String?) -> URL? {
        guard let rawValue = value?
            .trimmingCharacters(in: .whitespacesAndNewlines),
              !rawValue.isEmpty,
              let url = URL(string: rawValue),
              let scheme = url.scheme?.lowercased(),
              scheme == "https" || scheme == "http",
              url.host != nil
        else { return nil }
        return url
    }

    private static func endpointIsReachable(_ baseURL: URL) async -> Bool {
        let healthURL = baseURL.appendingPathComponent("api/health")
        var request = URLRequest(url: healthURL, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 0.8)
        request.httpMethod = "GET"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 0.8
        configuration.timeoutIntervalForResource = 1.0
        configuration.waitsForConnectivity = false
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return false }
            guard http.statusCode == 200,
                  let health = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return false }
            return health["ok"] as? Bool == true && health["auth"] as? Bool == false
        } catch {
            return false
        }
    }

    /// Child environment: inherited, plus optional `~/.pveclient.env` overrides
    /// (that is where PVE_CLIENT_AUTH / PVE_CLIENT_HOST / ... are configured).
    static func childEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let resources = Bundle.main.resourceURL {
            let helper = resources.appendingPathComponent("backend/PVENativeHelper")
            if FileManager.default.isExecutableFile(atPath: helper.path) {
                environment["PVE_CLIENT_NATIVE_HELPER"] = helper.path
            }
        }
        if let repo = ProcessInfo.processInfo.environment["PVE_CLIENT_REPO"] {
            environment["PYTHONPATH"] = repo
        }
        let file = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".pveclient.env")
        guard let text = try? String(contentsOf: file, encoding: .utf8) else { return environment }
        for rawLine in text.split(separator: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let equals = line.firstIndex(of: "=") else { continue }
            let key = String(line[line.startIndex..<equals]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: equals)...]).trimmingCharacters(in: .whitespaces)
            if value.count >= 2, (value.hasPrefix("\"") && value.hasSuffix("\"")) || (value.hasPrefix("'") && value.hasSuffix("'")) {
                value = String(value.dropFirst().dropLast())
            }
            if !key.isEmpty { environment[key] = value }
        }
        // the shell itself hosts the window, so the sidecar must not spawn one
        environment["PVE_CLIENT_NO_WINDOW"] = "1"
        return environment
    }
}
