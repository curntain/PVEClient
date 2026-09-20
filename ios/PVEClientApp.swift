import Network
import SwiftUI
import WebKit

@main
struct PVEClientIOSApp: App {
    @StateObject private var endpoint = EndpointSelector()

    var body: some Scene {
        WindowGroup {
            ClientRootView()
                .environmentObject(endpoint)
                .task { endpoint.start() }
        }
    }
}

@MainActor
final class EndpointSelector: ObservableObject {
    enum Mode: String {
        case checking = "正在检测网络"
        case lan = "内网直连"
        case internet = "外网安全登录"
    }

    @Published private(set) var selectedURL: URL?
    @Published private(set) var mode: Mode = .checking

    private var localURL: URL? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "PVEClientLocalURL") as? String,
              !value.isEmpty else { return nil }
        return URL(string: value)
    }
    private var publicURL: URL? {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "PVEClientPublicURL") as? String,
              !value.isEmpty else { return nil }
        return URL(string: value)
    }
    private let monitor = NWPathMonitor()
    private let monitorQueue = DispatchQueue(label: "com.example.pveclient.network")
    private var started = false
    private var selectionTask: Task<Void, Never>?

    func start() {
        guard !started else { return }
        started = true
        monitor.pathUpdateHandler = { [weak self] _ in
            Task { @MainActor in self?.selectEndpoint() }
        }
        monitor.start(queue: monitorQueue)
        selectEndpoint()
    }

    func selectEndpoint() {
        selectionTask?.cancel()
        mode = .checking
        selectionTask = Task { [weak self] in
            guard let self else { return }
            let useLAN = await self.localEndpointIsReachable()
            guard !Task.isCancelled else { return }
            self.selectedURL = useLAN ? self.localURL : (self.publicURL ?? self.localURL)
            self.mode = useLAN ? .lan : .internet
        }
    }

    private func localEndpointIsReachable() async -> Bool {
        guard let localURL else { return false }
        var request = URLRequest(
            url: localURL.appendingPathComponent("api/health"),
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 0.9
        )
        request.httpMethod = "GET"
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 0.9
        configuration.timeoutIntervalForResource = 1.1
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
}

struct ClientRootView: View {
    @EnvironmentObject private var endpoint: EndpointSelector
    @State private var reloadToken = 0

    var body: some View {
        ZStack(alignment: .top) {
            Color(red: 0.06, green: 0.08, blue: 0.10).ignoresSafeArea()
            if let url = endpoint.selectedURL {
                ClientWebView(url: url, reloadToken: reloadToken)
                    .ignoresSafeArea(edges: .bottom)
            } else {
                VStack(spacing: 14) {
                    ProgressView().tint(.orange)
                    Text(endpoint.mode == .checking ? "正在检测内外网…" : "请在 ios/Info.plist 配置服务地址后重新构建")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            HStack(spacing: 10) {
                Circle()
                    .fill(endpoint.mode == .lan ? Color.green : endpoint.mode == .internet ? Color.orange : Color.gray)
                    .frame(width: 8, height: 8)
                Text(endpoint.mode.rawValue)
                    .font(.caption.weight(.medium))
                Spacer()
                Button {
                    endpoint.selectEndpoint()
                } label: {
                    Image(systemName: "network")
                }
                Button {
                    reloadToken += 1
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .padding(.horizontal, 14)
            .frame(height: 38)
            .foregroundStyle(.white)
            .background(Color(red: 0.08, green: 0.11, blue: 0.14))
        }
        .preferredColorScheme(.dark)
    }
}

struct ClientWebView: UIViewRepresentable {
    let url: URL
    let reloadToken: Int
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsBackForwardNavigationGestures = true
        view.scrollView.keyboardDismissMode = .interactive
        view.load(freshRequest(url))
        context.coordinator.loadedURL = url
        context.coordinator.reloadToken = reloadToken
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        if context.coordinator.loadedURL != url {
            context.coordinator.loadedURL = url
            view.load(freshRequest(url))
        } else if context.coordinator.reloadToken != reloadToken {
            context.coordinator.reloadToken = reloadToken
            view.reloadFromOrigin()
        }
    }

    private func freshRequest(_ url: URL) -> URLRequest {
        var request = URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 30)
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        return request
    }

    final class Coordinator {
        var loadedURL: URL?
        var reloadToken = -1
    }
}
