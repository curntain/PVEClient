import SwiftUI
import WebKit

/// Hosts the client's web UI inside a native macOS window.
struct WebViewContainer: NSViewRepresentable {
    let url: URL
    let reloadToken: Int

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        configuration.preferences.isElementFullscreenEnabled = true

        let view = WKWebView(frame: .zero, configuration: configuration)
        view.allowsBackForwardNavigationGestures = true
        view.allowsMagnification = true
        view.uiDelegate = context.coordinator
        view.load(freshRequest(for: url))
        context.coordinator.loadedURL = url
        context.coordinator.lastToken = reloadToken
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {
        if context.coordinator.lastToken != reloadToken {
            context.coordinator.lastToken = reloadToken
            view.reloadFromOrigin()
        }
        if context.coordinator.loadedURL != url {
            context.coordinator.loadedURL = url
            view.load(freshRequest(for: url))
        }
    }

    /// The public UI is updated independently from the native shell. Always
    /// revalidate the document on launch so an older cached app.js/style.css
    /// cannot hide newly deployed monitoring or layout changes.
    private func freshRequest(for url: URL) -> URLRequest {
        var request = URLRequest(
            url: url,
            cachePolicy: .reloadIgnoringLocalCacheData,
            timeoutInterval: 30
        )
        request.setValue("no-cache", forHTTPHeaderField: "Cache-Control")
        return request
    }

    final class Coordinator: NSObject, WKUIDelegate {
        var lastToken = -1
        var loadedURL: URL?
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = NSAlert(); alert.messageText = frame.securityOrigin.host; alert.informativeText = message
            alert.addButton(withTitle: "确定")
            if let window = webView.window { alert.beginSheetModal(for: window) { _ in completionHandler() } }
            else { completionHandler() }
        }
        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = NSAlert(); alert.messageText = frame.securityOrigin.host; alert.informativeText = message
            alert.addButton(withTitle: "确定"); alert.addButton(withTitle: "取消")
            if let window = webView.window { alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) } }
            else { completionHandler(false) }
        }
        func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = parameters.allowsMultipleSelection
            panel.canChooseDirectories = parameters.allowsDirectories
            guard let window = webView.window else { completionHandler(nil); return }
            panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
        }
    }
}
