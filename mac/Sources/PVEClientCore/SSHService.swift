import Citadel
import Crypto
import Foundation
import NIO
import NIOSSH

public actor SSHService {
    public static let shared = SSHService()
    private var clients: [String: SSHClient] = [:]
    private var clientProfiles: [String: SSHProfile] = [:]

    public init() {}

    public func connect(_ profile: SSHProfile) async throws {
        if clients[profile.id] != nil && clientProfiles[profile.id] == profile { return }
        if clients[profile.id] != nil { await disconnect(profileID: profile.id) }
        let authentication: SSHAuthenticationMethod
        if profile.authMethod == "key", let key = profile.privateKey, !key.isEmpty {
            let passphrase = profile.keyPassphrase.map { Data($0.utf8) }
            let kind = try SSHKeyDetection.detectPrivateKeyType(from: key)
            switch kind {
            case .ed25519:
                authentication = .ed25519(
                    username: profile.username,
                    privateKey: try Curve25519.Signing.PrivateKey(sshEd25519: key, decryptionKey: passphrase)
                )
            case .rsa:
                authentication = .rsa(
                    username: profile.username,
                    privateKey: try Insecure.RSA.PrivateKey(sshRsa: key, decryptionKey: passphrase)
                )
            default:
                throw NativeSSHError.unsupportedAuthentication
            }
        } else if let password = profile.password, !password.isEmpty {
            authentication = .passwordBased(username: profile.username, password: password)
        } else {
            throw NativeSSHError.unsupportedAuthentication
        }
        let validator: SSHHostKeyValidator = profile.acceptUnknownHost == false
            ? .trustedKeys(try trustedHostKeys(profile))
            : .acceptAnything()
        let settings = SSHClientSettings(
            host: profile.host,
            port: profile.port,
            authenticationMethod: { authentication },
            hostKeyValidator: validator
        )
        clients[profile.id] = try await SSHClient.connect(to: settings)
        clientProfiles[profile.id] = profile
    }

    public func disconnect(profileID: String) async {
        guard let client = clients.removeValue(forKey: profileID) else { return }
        clientProfiles.removeValue(forKey: profileID)
        try? await client.close()
    }

    public func execute(_ command: String, profile: SSHProfile) async throws -> CommandResult {
        try await connect(profile)
        guard let client = clients[profile.id] else { throw NativeSSHError.notConnected }
        var stdout = ""
        var stderr = ""
        do {
            let stream = try await client.executeCommandStream(command)
            for try await event in stream {
                switch event {
                case .stdout(var chunk): stdout += chunk.readString(length: chunk.readableBytes) ?? ""
                case .stderr(var chunk): stderr += chunk.readString(length: chunk.readableBytes) ?? ""
                }
            }
            return CommandResult(code: 0, stdout: stdout, stderr: stderr)
        } catch let failed as SSHClient.CommandFailed {
            return CommandResult(code: failed.exitCode, stdout: stdout, stderr: stderr)
        } catch {
            clients.removeValue(forKey: profile.id)
            clientProfiles.removeValue(forKey: profile.id)
            throw error
        }
    }

    public func withSFTP<T: Sendable>(
        profile: SSHProfile,
        operation: @escaping @Sendable (SFTPClient) async throws -> T
    ) async throws -> T {
        try await connect(profile)
        guard let client = clients[profile.id] else { throw NativeSSHError.notConnected }
        return try await client.withSFTP(operation)
    }

    @available(macOS 15.0, *)
    public func withTTY(
        profile: SSHProfile,
        perform: (_ inbound: TTYOutput, _ outbound: TTYStdinWriter) async throws -> Void
    ) async throws {
        try await connect(profile)
        guard let client = clients[profile.id] else { throw NativeSSHError.notConnected }
        try await client.withTTY(perform: perform)
    }

    private func trustedHostKeys(_ profile: SSHProfile) throws -> Set<NIOSSHPublicKey> {
        let knownHosts = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".ssh/known_hosts")
        guard let text = try? String(contentsOf: knownHosts, encoding: .utf8) else {
            throw NativeSSHError.unknownHostKey
        }
        let target = profile.port == 22 ? profile.host : "[\(profile.host)]:\(profile.port)"
        var keys: Set<NIOSSHPublicKey> = []
        for line in text.split(whereSeparator: \.isNewline) {
            let fields = line.split(whereSeparator: \.isWhitespace).map(String.init)
            guard fields.count >= 3, !fields[0].hasPrefix("#") else { continue }
            let marker = fields[0].hasPrefix("@") ? fields[0] : ""
            let index = marker.isEmpty ? 0 : 1
            guard fields.count >= index + 3 else { continue }
            let hosts = fields[index].split(separator: ",").map(String.init)
            guard hosts.contains(where: { knownHostMatches(String($0), target: target) }) else { continue }
            if marker == "@revoked" { throw NativeSSHError.revokedHostKey }
            if !marker.isEmpty { continue }
            if let key = try? NIOSSHPublicKey(openSSHPublicKey: fields[index + 1] + " " + fields[index + 2]) {
                keys.insert(key)
            }
        }
        guard !keys.isEmpty else { throw NativeSSHError.unknownHostKey }
        return keys
    }

    private func knownHostMatches(_ entry: String, target: String) -> Bool {
        if entry == target { return true }
        guard entry.hasPrefix("|1|") else { return false }
        let parts = entry.split(separator: "|", omittingEmptySubsequences: false)
        guard parts.count == 4,
              let salt = Data(base64Encoded: String(parts[2])),
              let expected = Data(base64Encoded: String(parts[3])) else { return false }
        let digest = HMAC<Insecure.SHA1>.authenticationCode(
            for: Data(target.utf8), using: SymmetricKey(data: salt)
        )
        return Data(digest) == expected
    }
}

public enum NativeSSHError: LocalizedError {
    case unsupportedAuthentication
    case notConnected
    case unknownHostKey
    case revokedHostKey

    public var errorDescription: String? {
        switch self {
        case .unsupportedAuthentication: return "Swift SSH 支持密码和 OpenSSH 格式的 Ed25519/RSA 密钥"
        case .notConnected: return "SSH 未连接"
        case .unknownHostKey: return "该主机不在 Mac 的 ~/.ssh/known_hosts 中"
        case .revokedHostKey: return "该 SSH 主机密钥已被撤销"
        }
    }
}
