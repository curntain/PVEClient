import AppKit
import Combine
import SwiftUI

extension Notification.Name {
    static let pveReload = Notification.Name("pveReload")
}

@main
struct PVEClientShellApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var backend = BackendProcess.shared ?? BackendProcess()

    var body: some Scene {
        WindowGroup("PVE 远程管理客户端") {
            ContentView()
                .environmentObject(backend)
                .frame(minWidth: 1040, minHeight: 700)
                .task { backend.start() }
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(after: .toolbar) {
                Button("刷新页面") { NotificationCenter.default.post(name: .pveReload, object: nil) }
                    .keyboardShortcut("r", modifiers: .command)
                Button("在浏览器中打开") { backend.openInBrowser() }
                    .keyboardShortcut("o", modifiers: [.command, .shift])
                Divider()
                Button("打开数据目录") { backend.revealDataFolder() }
                Button("重启内置服务") { backend.restart() }
            }
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        BackendProcess.shared?.stop()
    }
}

struct ContentView: View {
    @EnvironmentObject private var backend: BackendProcess
    @State private var reloadToken = 0

    var body: some View {
        Group {
            switch backend.status {
            case .idle, .starting:
                VStack(spacing: 14) {
                    ProgressView()
                    Text(backend.statusMessage).foregroundStyle(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)

            case .ready(let url):
                WebViewContainer(url: url, reloadToken: reloadToken)

            case .failed(let message):
                VStack(alignment: .leading, spacing: 12) {
                    Label("启动失败", systemImage: "exclamationmark.triangle.fill")
                        .font(.title3.bold())
                    Text(message).foregroundStyle(.secondary)
                    ScrollView {
                        Text(backend.log)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .frame(maxHeight: 220)
                    HStack {
                        Button("重试") { backend.restart() }
                        Button("打开数据目录") { backend.revealDataFolder() }
                    }
                }
                .padding(24)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: .pveReload)) { _ in
            reloadToken += 1
        }
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if case .ready = backend.status {
                    Button {
                        NotificationCenter.default.post(name: .pveReload, object: nil)
                    } label: {
                        Label("刷新", systemImage: "arrow.clockwise")
                    }
                    Button {
                        backend.openInBrowser()
                    } label: {
                        Label("在浏览器中打开", systemImage: "safari")
                    }
                }
            }
        }
    }
}
