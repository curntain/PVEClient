import Foundation
import NIO
import PVEClientCore

@main
struct PVENativeHelper {
    static func main() async {
        if CommandLine.arguments.contains("--probe") {
            await probe()
        } else if CommandLine.arguments.contains("--terminal") {
            await terminal()
        } else {
            emit(["event": "ready", "backend": "swift"])
            while let line = readLine() {
                await respond(to: line)
            }
        }
    }

    private static func respond(to line: String) async {
        do {
            let request = try JSONDecoder().decode(NativeRequest.self, from: Data(line.utf8))
            let result = try await dispatch(request)
            emit(["id": request.id, "ok": true, "result": result])
        } catch {
            let id = ((try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any])?["id"] ?? 0
            emit(["id": id, "ok": false, "error": error.localizedDescription, "debug": String(reflecting: error)])
        }
    }

    private static func dispatch(_ request: NativeRequest) async throws -> Any {
        let profile = request.profile
        let files = RemoteFileManager()
        switch request.operation {
        case "exec":
            guard let command = request.command else { throw NativeRequestError.missingField }
            let result = try await SSHService.shared.execute(command, profile: profile)
            return ["code": result.code, "stdout": result.stdout, "stderr": result.stderr]
        case "pveGuests":
            return commandJSON(try await PVEOperations().listGuests(profile: profile))
        case "pveMetrics":
            return commandJSON(try await PVEOperations().metrics(profile: profile))
        case "pveHelpers":
            return commandJSON(try await PVEOperations().helperBundle(profile: profile))
        case "pveAction":
            guard let vmid = request.vmid, let kind = PVEGuestKind(rawValue: request.guestKind ?? ""),
                  let action = PVEGuestAction(rawValue: request.action ?? "") else {
                throw NativeRequestError.missingField
            }
            return commandJSON(try await PVEOperations().action(vmid: vmid, kind: kind, action: action, profile: profile))
        case "pveCreateVM":
            let operations = PVEOperations()
            let vmid = try required(request.vmid)
            let name = try required(request.name)
            let commands = try operations.vmCommands(
                vmid: vmid, name: name, cores: request.cores ?? 2,
                memoryMB: request.memory ?? 2048, diskGB: request.diskGB ?? 32,
                storage: request.storage ?? "local-lvm", iso: request.iso,
                bridge: request.bridge ?? "vmbr0", netModel: request.netModel ?? "virtio"
            )
            let results = try await operations.createVM(
                vmid: vmid, name: name, cores: request.cores ?? 2,
                memoryMB: request.memory ?? 2048, diskGB: request.diskGB ?? 32,
                storage: request.storage ?? "local-lvm", iso: request.iso,
                bridge: request.bridge ?? "vmbr0", netModel: request.netModel ?? "virtio",
                profile: profile
            )
            let logs: [[String: Any]] = zip(commands, results).map { command, result in
                ["cmd": command, "code": result.code, "stdout": result.stdout, "stderr": result.stderr]
            }
            let failed = results.last.map { $0.code != 0 } ?? true
            return [
                "ok": !failed, "logs": logs, "vmid": vmid,
                "error": failed ? (results.last?.stderr.isEmpty == false ? results.last!.stderr : results.last?.stdout ?? "") : ""
            ] as [String: Any]
        case "pveCreateCT":
            let operations = PVEOperations()
            let vmid = try required(request.vmid)
            let hostname = try required(request.name)
            let password = request.containerPassword ?? ""
            let command = try operations.containerCommand(
                vmid: vmid, hostname: hostname, template: try required(request.template),
                cores: request.cores ?? 1, memoryMB: request.memory ?? 512,
                diskGB: request.diskGB ?? 8, storage: request.storage ?? "local-lvm",
                unprivileged: request.unprivileged ?? true, password: password
            )
            let result = try await operations.createContainer(
                vmid: vmid, hostname: hostname, template: try required(request.template),
                cores: request.cores ?? 1, memoryMB: request.memory ?? 512,
                diskGB: request.diskGB ?? 8, storage: request.storage ?? "local-lvm",
                unprivileged: request.unprivileged ?? true, password: password,
                profile: profile
            )
            var output: [String: Any] = [
                "ok": result.code == 0, "stdout": result.stdout, "stderr": result.stderr,
                "cmd": password.isEmpty ? command : command.replacingOccurrences(of: " --password " + shellQuote(password), with: " --password [redacted]")
            ]
            if result.code == 0 && request.startAfterCreate == true {
                let started = try await operations.action(vmid: vmid, kind: .lxc, action: .start, profile: profile)
                output["start"] = ["ok": started.code == 0, "code": started.code, "stdout": started.stdout, "stderr": started.stderr]
            }
            return output
        case "fileList":
            let entries = try await files.list(path: request.path ?? "/", profile: profile)
            return entries.map(fileJSON)
        case "fileStat":
            return fileJSON(try await files.attributes(path: try required(request.path), profile: profile))
        case "fileRead":
            let path = try required(request.path)
            let attributes = try await files.attributes(path: path, profile: profile)
            guard attributes.size <= 2 * 1024 * 1024 else { throw NativeRequestError.fileTooLarge }
            let data = try await files.read(path: path, profile: profile)
            return ["data": data.base64EncodedString(), "size": data.count]
        case "fileWrite":
            let data = try decodeData(request.data)
            try await files.write(path: try required(request.path), data: data, profile: profile)
            return ["bytes": data.count]
        case "fileMkdir":
            try await files.createDirectory(path: try required(request.path), profile: profile)
            return ["path": request.path ?? ""]
        case "fileMkdirP":
            try await files.createDirectories(path: try required(request.path), profile: profile)
            return ["path": request.path ?? ""]
        case "fileRemove":
            let path = try required(request.path)
            let attr = try await files.attributes(path: path, profile: profile)
            if attr.isDirectory { try await files.removeEmptyDirectory(path: path, profile: profile) }
            else { try await files.removeFile(path: path, profile: profile) }
            return ["path": path]
        case "fileRemoveRecursive":
            let path = try required(request.path)
            try await files.removeRecursively(path: path, profile: profile)
            return ["path": path]
        case "fileRename":
            try await files.rename(from: try required(request.path), to: try required(request.to), profile: profile)
            return ["src": request.path ?? "", "dst": request.to ?? ""]
        case "fileUpload":
            let local = URL(fileURLWithPath: try required(request.localPath))
            let remote = try required(request.path)
            try await files.upload(localURL: local, remotePath: remote, profile: profile)
            let size = try await files.attributes(path: remote, profile: profile).size
            return ["path": remote, "size": Int(size)]
        case "fileDownload":
            let remote = try required(request.path)
            let local = URL(fileURLWithPath: try required(request.localPath))
            try await files.download(remotePath: remote, localURL: local, profile: profile)
            return ["path": remote, "local": local.path]
        default:
            throw NativeRequestError.unknownOperation
        }
    }

    private static func fileJSON(_ entry: RemoteFileEntry) -> [String: Any] {
        [
            "name": entry.name,
            "path": entry.path,
            "is_dir": entry.isDirectory,
            "size": Int(entry.size),
            "permissions": Int(entry.permissions),
            "mtime": entry.modifiedAt?.timeIntervalSince1970 ?? 0,
        ]
    }

    private static func commandJSON(_ result: CommandResult) -> [String: Any] {
        ["code": result.code, "stdout": result.stdout, "stderr": result.stderr]
    }

    private static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }

    private static func required(_ value: String?) throws -> String {
        guard let value, !value.isEmpty else { throw NativeRequestError.missingField }
        return value
    }

    private static func decodeData(_ value: String?) throws -> Data {
        guard let value, let data = Data(base64Encoded: value) else { throw NativeRequestError.missingField }
        return data
    }

    private static func emit(_ value: [String: Any]) {
        let data = (try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])) ?? Data("{}".utf8)
        print(String(decoding: data, as: UTF8.self))
        fflush(stdout)
    }

    private static func terminal() async {
        let output = TerminalEmitter()
        guard #available(macOS 15.0, *) else {
            await output.send(["event": "error", "data": "Swift 终端需要 macOS 15 或更新版本"])
            return
        }
        guard let line = readLine(), let profile = try? JSONDecoder().decode(SSHProfile.self, from: Data(line.utf8)) else {
            await output.send(["event": "error", "data": "终端连接配置无效"])
            return
        }
        do {
            try await SSHService.shared.withTTY(profile: profile) { inbound, outbound in
                await output.send(["event": "ready"])
                let writer = Task {
                    while let inputLine = readLine() {
                        guard let input = try? JSONDecoder().decode(TerminalInput.self, from: Data(inputLine.utf8)) else {
                            continue
                        }
                        do {
                            switch input.type {
                            case "data":
                                if let data = input.data, !data.isEmpty {
                                    try await outbound.write(ByteBuffer(string: data))
                                }
                            case "resize":
                                try await outbound.changeSize(
                                    cols: max(input.cols ?? 120, 1), rows: max(input.rows ?? 32, 1),
                                    pixelWidth: 0, pixelHeight: 0
                                )
                            case "close":
                                try await outbound.write(ByteBuffer(string: "exit\n"))
                                return
                            default:
                                break
                            }
                        } catch {
                            await output.send(["event": "error", "data": error.localizedDescription])
                            return
                        }
                    }
                }
                do {
                    for try await event in inbound {
                        switch event {
                        case .stdout(let buffer), .stderr(let buffer):
                            let data = String(decoding: buffer.readableBytesView, as: UTF8.self)
                            if !data.isEmpty { await output.send(["event": "data", "data": data]) }
                        }
                    }
                } catch {
                    if !normalTTYClose(error) {
                        await output.send(["event": "error", "data": error.localizedDescription, "debug": String(reflecting: error)])
                    }
                }
                writer.cancel()
            }
            await output.send(["event": "closed"])
            await SSHService.shared.disconnect(profileID: profile.id)
        } catch {
            if normalTTYClose(error) {
                await output.send(["event": "closed"])
            } else {
                await output.send(["event": "error", "data": error.localizedDescription, "debug": String(reflecting: error)])
            }
        }
    }

    private static func normalTTYClose(_ error: Error) -> Bool {
        guard let channelError = error as? ChannelError else { return false }
        switch channelError {
        case .alreadyClosed, .ioOnClosedChannel, .outputClosed, .inputClosed: return true
        default: return false
        }
    }

    private static func probe() async {
        do {
            let data = FileHandle.standardInput.readDataToEndOfFile()
            let profile = try JSONDecoder().decode(SSHProfile.self, from: data)
            let ssh = SSHService.shared
            let host = try await ssh.execute("hostname", profile: profile)
            let guests = try await PVEOperations().listGuests(profile: profile)
            let files = try await RemoteFileManager().list(path: "/var/lib/vz/template", profile: profile)
            let guestCount = ((try? JSONSerialization.jsonObject(with: Data(guests.stdout.utf8))) as? [[String: Any]])?.count ?? 0
            let result: [String: Any] = [
                "ok": host.code == 0 && guests.code == 0,
                "hostname": host.stdout.trimmingCharacters(in: .whitespacesAndNewlines),
                "guest_count": guestCount,
                "file_count": files.count,
                "guest_code": guests.code,
            ]
            let output = try JSONSerialization.data(withJSONObject: result)
            print(String(decoding: output, as: UTF8.self))
            await ssh.disconnect(profileID: profile.id)
        } catch {
            let result = ["ok": false, "error": error.localizedDescription] as [String: Any]
            let output = (try? JSONSerialization.data(withJSONObject: result)) ?? Data("{}".utf8)
            print(String(decoding: output, as: UTF8.self))
            exit(1)
        }
    }
}

private struct NativeRequest: Codable {
    let id: Int
    let operation: String
    let profile: SSHProfile
    let command: String?
    let path: String?
    let to: String?
    let data: String?
    let localPath: String?
    let vmid: String?
    let guestKind: String?
    let action: String?
    let name: String?
    let cores: Int?
    let memory: Int?
    let diskGB: Int?
    let storage: String?
    let iso: String?
    let bridge: String?
    let netModel: String?
    let template: String?
    let containerPassword: String?
    let unprivileged: Bool?
    let startAfterCreate: Bool?
}

private struct TerminalInput: Codable {
    let type: String
    let data: String?
    let cols: Int?
    let rows: Int?
}

private actor TerminalEmitter {
    func send(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }
}

private enum NativeRequestError: LocalizedError {
    case missingField, unknownOperation, fileTooLarge
    var errorDescription: String? {
        switch self {
        case .missingField: "请求参数不完整"
        case .unknownOperation: "未知 Swift 操作"
        case .fileTooLarge: "文件超过编辑器 2MB 上限"
        }
    }
}
