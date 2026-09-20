import Citadel
import Foundation
import NIO

public struct RemoteFileManager: Sendable {
    private let ssh: SSHService

    public init(ssh: SSHService = .shared) { self.ssh = ssh }

    public func list(path: String, profile: SSHProfile) async throws -> [RemoteFileEntry] {
        let normalized = try normalize(path)
        return try await ssh.withSFTP(profile: profile) { sftp in
            let responses = try await sftp.listDirectory(atPath: normalized)
            return responses.flatMap(\.components).filter { $0.filename != "." && $0.filename != ".." }.map { item in
                let mode = item.attributes.permissions ?? 0
                return RemoteFileEntry(
                    name: item.filename,
                    path: normalized == "/" ? "/" + item.filename : normalized + "/" + item.filename,
                    isDirectory: mode & 0o170000 == 0o040000,
                    size: item.attributes.size ?? 0,
                    permissions: mode,
                    modifiedAt: item.attributes.accessModificationTime?.modificationTime
                )
            }
        }
    }

    public func read(path: String, profile: SSHProfile) async throws -> Data {
        let normalized = try normalize(path)
        return try await ssh.withSFTP(profile: profile) { sftp in
            let buffer = try await sftp.withFile(filePath: normalized, flags: .read) { file in
                try await file.readAll()
            }
            return Data(buffer.readableBytesView)
        }
    }

    public func write(path: String, data: Data, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        let temporary = normalized + ".pveclient-" + UUID().uuidString + ".tmp"
        try await ssh.withSFTP(profile: profile) { sftp in
            try await sftp.withFile(filePath: temporary, flags: [.write, .create, .truncate]) { file in
                var buffer = ByteBufferAllocator().buffer(capacity: data.count)
                buffer.writeBytes(data)
                try await file.write(buffer)
            }
        }
        do {
            try await rename(from: temporary, to: normalized, profile: profile)
        } catch {
            // OpenSSH SFTP v3 does not always replace an existing destination.
            // A same-directory `mv -T` is atomic on the PVE host and leaves the
            // original file untouched if the replacement fails.
            let command = "mv -T -f -- \(shellQuote(temporary)) \(shellQuote(normalized))"
            do {
                let result = try await ssh.execute(command, profile: profile)
                guard result.code == 0 else { throw RemoteFileError.atomicReplaceFailed(result.stderr) }
            } catch {
                try? await removeFile(path: temporary, profile: profile)
                throw error
            }
        }
    }

    public func createDirectory(path: String, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        try await ssh.withSFTP(profile: profile) { sftp in
            try await sftp.createDirectory(atPath: normalized)
        }
    }

    public func removeFile(path: String, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        try await ssh.withSFTP(profile: profile) { sftp in try await sftp.remove(at: normalized) }
    }

    public func removeEmptyDirectory(path: String, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        guard !["/", "/root", "/etc", "/var", "/home"].contains(normalized) else {
            throw RemoteFileError.invalidPath
        }
        try await ssh.withSFTP(profile: profile) { sftp in try await sftp.rmdir(at: normalized) }
    }

    public func removeRecursively(path: String, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        guard !["/", "/root", "/etc", "/var", "/home", "/usr"].contains(normalized) else {
            throw RemoteFileError.invalidPath
        }
        let parent = (normalized as NSString).deletingLastPathComponent
        guard let entry = try await list(path: parent.isEmpty ? "/" : parent, profile: profile)
            .first(where: { $0.path == normalized }) else {
            throw RemoteFileError.missingPath
        }
        try await removeTree(entry, profile: profile)
    }

    private func removeTree(_ entry: RemoteFileEntry, profile: SSHProfile) async throws {
        if entry.isDirectory {
            for child in try await list(path: entry.path, profile: profile) {
                try await removeTree(child, profile: profile)
            }
            try await removeEmptyDirectory(path: entry.path, profile: profile)
        } else {
            try await removeFile(path: entry.path, profile: profile)
        }
    }

    public func rename(from: String, to: String, profile: SSHProfile) async throws {
        let old = try normalize(from)
        let new = try normalize(to)
        try await ssh.withSFTP(profile: profile) { sftp in try await sftp.rename(at: old, to: new) }
    }

    public func attributes(path: String, profile: SSHProfile) async throws -> RemoteFileEntry {
        let normalized = try normalize(path)
        return try await ssh.withSFTP(profile: profile) { sftp in
            let attr = try await sftp.getAttributes(at: normalized)
            let mode = attr.permissions ?? 0
            return RemoteFileEntry(
                name: (normalized as NSString).lastPathComponent,
                path: normalized,
                isDirectory: mode & 0o170000 == 0o040000,
                size: attr.size ?? 0,
                permissions: mode,
                modifiedAt: attr.accessModificationTime?.modificationTime
            )
        }
    }

    public func createDirectories(path: String, profile: SSHProfile) async throws {
        let normalized = try normalize(path)
        let parts = normalized.split(separator: "/")
        var current = ""
        for part in parts {
            current += "/" + part
            if (try? await attributes(path: current, profile: profile)) == nil {
                try await createDirectory(path: current, profile: profile)
            }
        }
    }

    public func upload(localURL: URL, remotePath: String, profile: SSHProfile) async throws {
        let normalized = try normalize(remotePath)
        try await ssh.withSFTP(profile: profile) { sftp in
            try await sftp.withFile(filePath: normalized, flags: [.write, .create, .truncate]) { remote in
                let local = try FileHandle(forReadingFrom: localURL)
                defer { try? local.close() }
                var offset: UInt64 = 0
                while let chunk = try local.read(upToCount: 32_000), !chunk.isEmpty {
                    var buffer = ByteBufferAllocator().buffer(capacity: chunk.count)
                    buffer.writeBytes(chunk)
                    try await remote.write(buffer, at: offset)
                    offset += UInt64(chunk.count)
                }
            }
        }
    }

    public func download(remotePath: String, localURL: URL, profile: SSHProfile) async throws {
        let normalized = try normalize(remotePath)
        try await ssh.withSFTP(profile: profile) { sftp in
            let attributes = try await sftp.getAttributes(at: normalized)
            guard let size = attributes.size else { throw RemoteFileError.unknownSize }
            guard FileManager.default.createFile(atPath: localURL.path, contents: nil) else {
                throw RemoteFileError.localWriteFailed
            }
            try await sftp.withFile(filePath: normalized, flags: .read) { remote in
                let local = try FileHandle(forWritingTo: localURL)
                defer { try? local.close() }
                var offset: UInt64 = 0
                while offset < size {
                    let buffer = try await remote.read(
                        from: offset,
                        length: UInt32(min(size - offset, 32_000))
                    )
                    let count = buffer.readableBytes
                    guard count > 0 else { throw RemoteFileError.incompleteDownload }
                    local.write(Data(buffer.readableBytesView))
                    offset += UInt64(count)
                }
            }
        }
    }

    private func normalize(_ raw: String) throws -> String {
        guard raw.hasPrefix("/"), !raw.contains("\0") else { throw RemoteFileError.invalidPath }
        let components = raw.split(separator: "/")
        guard !components.contains("..") else { throw RemoteFileError.invalidPath }
        return "/" + components.joined(separator: "/")
    }

    private func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }
}

public struct RemoteFileEntry: Codable, Sendable {
    public let name: String
    public let path: String
    public let isDirectory: Bool
    public let size: UInt64
    public let permissions: UInt32
    public let modifiedAt: Date?
}

public enum RemoteFileError: LocalizedError {
    case invalidPath, missingPath, unknownSize, localWriteFailed, incompleteDownload, atomicReplaceFailed(String)
    public var errorDescription: String? {
        switch self {
        case .invalidPath: "远程路径不合法"
        case .missingPath: "远程路径不存在"
        case .unknownSize: "远程文件大小未知"
        case .localWriteFailed: "本地文件无法写入"
        case .incompleteDownload: "下载未完成"
        case .atomicReplaceFailed(let message): "远程文件替换失败: \(message)"
        }
    }
}
