import Foundation

public struct SSHProfile: Codable, Sendable, Equatable {
    public let id: String
    public let host: String
    public let port: Int
    public let username: String
    public let password: String?
    public let privateKey: String?
    public let keyPassphrase: String?
    public let authMethod: String?
    public let acceptUnknownHost: Bool?

    public init(
        id: String, host: String, port: Int = 22, username: String = "root",
        password: String? = nil, privateKey: String? = nil,
        keyPassphrase: String? = nil, authMethod: String? = nil,
        acceptUnknownHost: Bool? = nil
    ) {
        self.id = id
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.privateKey = privateKey
        self.keyPassphrase = keyPassphrase
        self.authMethod = authMethod
        self.acceptUnknownHost = acceptUnknownHost
    }
}

public struct CommandResult: Codable, Sendable {
    public let code: Int
    public let stdout: String
    public let stderr: String
}

public enum PVEGuestKind: String, Codable, Sendable {
    case qemu
    case lxc
}

public enum PVEGuestAction: String, Codable, Sendable {
    case start, shutdown, stop, reboot, reset, suspend, resume
}
