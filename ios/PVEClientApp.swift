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
        case setup = "设置服务器地址"
        case connected = "已连接"
    }

    @Published private(set) var selectedURL: URL?
    @Published var urlText = UserDefaults.standard.string(forKey: "serverURL") ?? ""
    @Published private(set) var mode: Mode = .setup

    func start() {
        if !urlText.isEmpty { connect() }
    }

    @discardableResult
    func connect() -> Bool {
        let text = urlText.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalized = text.contains("://") ? text : "https://" + text
        guard let components = URLComponents(string: normalized),
              let host = components.host, !host.isEmpty,
              let scheme = components.scheme?.lowercased(),
              scheme == "https" || (scheme == "http" && isPrivateHost(host)),
              let url = components.url else { return false }
        urlText = normalized
        UserDefaults.standard.set(normalized, forKey: "serverURL")
        selectedURL = url
        mode = .connected
        return true
    }

    private func isPrivateHost(_ host: String) -> Bool {
        let value = host.lowercased()
        if value == "localhost" || value.hasSuffix(".local") { return true }
        let parts = value.split(separator: ".").compactMap { Int($0) }
        guard parts.count == 4, parts.allSatisfy({ (0...255).contains($0) }) else { return false }
        return parts[0] == 10 || parts[0] == 127 ||
            (parts[0] == 192 && parts[1] == 168) ||
            (parts[0] == 172 && (16...31).contains(parts[1]))
    }
}

struct ClientRootView: View {
    @EnvironmentObject private var endpoint: EndpointSelector
    @State private var reloadToken = 0
    @State private var editingAddress = false
    @State private var showInvalidAddress = false

    var body: some View {
        ZStack(alignment: .top) {
            Color(red: 0.06, green: 0.08, blue: 0.10).ignoresSafeArea()
            if let url = endpoint.selectedURL {
                ClientWebView(url: url, reloadToken: reloadToken)
                    .ignoresSafeArea(edges: .bottom)
            } else {
                VStack(spacing: 14) {
                    Image(systemName: "server.rack").font(.largeTitle).foregroundStyle(.orange)
                    Text("输入 PVEClient 服务地址")
                    addressForm
                }
                .padding(24)
            }
        }
        .safeAreaInset(edge: .top, spacing: 0) {
            HStack(spacing: 10) {
                Circle().fill(endpoint.selectedURL == nil ? Color.gray : Color.green).frame(width: 8, height: 8)
                Text(endpoint.mode.rawValue).font(.caption.weight(.medium))
                Spacer()
                Button { editingAddress = true } label: { Image(systemName: "gearshape") }
                Button { reloadToken += 1 } label: { Image(systemName: "arrow.clockwise") }
            }
            .padding(.horizontal, 14).frame(height: 38).foregroundStyle(.white)
            .background(Color(red: 0.08, green: 0.11, blue: 0.14))
        }
        .sheet(isPresented: $editingAddress) { addressForm.padding(24).presentationDetents([.medium]) }
        .alert("地址无效", isPresented: $showInvalidAddress) {
            Button("好", role: .cancel) { }
        } message: {
            Text("公网地址请使用 HTTPS。HTTP 只允许 localhost 或内网 IP。")
        }
        .preferredColorScheme(.dark)
    }

    private var addressForm: some View {
        VStack(spacing: 14) {
            TextField("https://你的域名 或 http://192.168.1.10:8765", text: $endpoint.urlText)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .keyboardType(.URL).textFieldStyle(.roundedBorder)
            Button("连接") {
                if endpoint.connect() { editingAddress = false } else { showInvalidAddress = true }
            }
            .buttonStyle(.borderedProminent).tint(.orange)
        }
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
