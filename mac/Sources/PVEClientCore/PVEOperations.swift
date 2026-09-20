import Foundation

public struct PVEOperations: Sendable {
    private let ssh: SSHService

    public init(ssh: SSHService = .shared) { self.ssh = ssh }

    public func listGuests(profile: SSHProfile) async throws -> CommandResult {
        try await ssh.execute("pvesh get /cluster/resources --type vm --output-format json", profile: profile)
    }

    public func metrics(profile: SSHProfile) async throws -> CommandResult {
        try await listGuests(profile: profile)
    }

    public func action(vmid: String, kind: PVEGuestKind, action: PVEGuestAction, profile: SSHProfile) async throws -> CommandResult {
        try validate(vmid: vmid)
        if kind == .lxc && action == .reset { throw PVEOperationError.unsupportedContainerReset }
        let tool = kind == .lxc ? "pct" : "qm"
        let first = try await ssh.execute("\(tool) \(action.rawValue) \(vmid)", profile: profile)
        if first.code != 0 && kind == .lxc && action == .reboot {
            return try await ssh.execute("pct shutdown \(vmid); sleep 1; pct start \(vmid)", profile: profile)
        }
        return first
    }

    public func helperBundle(profile: SSHProfile) async throws -> CommandResult {
        try await ssh.execute(
            "echo '===NEXTID==='; (pvesh get /cluster/nextid 2>/dev/null || echo 100); " +
            "echo '===STORAGES==='; (pvesm status --output-format json 2>/dev/null || pvesm status 2>/dev/null); " +
            "echo '===ISOS==='; (ls /var/lib/vz/template/iso 2>/dev/null || true); " +
            "echo '===TEMPLATES==='; (ls /var/lib/vz/template/cache 2>/dev/null || true); echo '===END==='",
            profile: profile
        )
    }

    public func createVM(
        vmid: String, name: String, cores: Int, memoryMB: Int,
        diskGB: Int, storage: String, iso: String?, bridge: String,
        netModel: String = "virtio",
        profile: SSHProfile
    ) async throws -> [CommandResult] {
        let commands = try vmCommands(
            vmid: vmid, name: name, cores: cores, memoryMB: memoryMB,
            diskGB: diskGB, storage: storage, iso: iso, bridge: bridge, netModel: netModel
        )
        var output: [CommandResult] = []
        for command in commands {
            let result = try await ssh.execute(command, profile: profile)
            output.append(result)
            if result.code != 0 { break }
        }
        return output
    }

    public func vmCommands(
        vmid: String, name: String, cores: Int, memoryMB: Int,
        diskGB: Int, storage: String, iso: String?, bridge: String,
        netModel: String = "virtio"
    ) throws -> [String] {
        try validate(vmid: vmid)
        guard !name.isEmpty, cores > 0, memoryMB > 0, diskGB > 0,
              safeIdentifier(storage), safeIdentifier(bridge), safeIdentifier(netModel) else {
            throw PVEOperationError.invalidParameters
        }
        var commands = [
            "qm create \(vmid) --name \(quote(name)) --memory \(memoryMB) --cores \(cores) " +
            "--net0 \(quote(netModel + ",bridge=" + bridge))"
        ]
        if let iso, !iso.isEmpty {
            guard !iso.contains("/") && !iso.contains("\\") else { throw PVEOperationError.invalidParameters }
            commands.append("qm set \(vmid) --ide2 \(quote("local:iso/" + iso)) --boot order=ide2")
            commands.append("qm set \(vmid) --ide0 \(quote(storage + ":" + String(diskGB)))")
        } else {
            commands.append("qm set \(vmid) --scsihw virtio-scsi-pci --scsi0 \(quote(storage + ":" + String(diskGB)))")
        }
        return commands
    }

    public func createContainer(
        vmid: String, hostname: String, template: String, cores: Int,
        memoryMB: Int, diskGB: Int, storage: String, unprivileged: Bool,
        password: String = "",
        profile: SSHProfile
    ) async throws -> CommandResult {
        let command = try containerCommand(
            vmid: vmid, hostname: hostname, template: template, cores: cores,
            memoryMB: memoryMB, diskGB: diskGB, storage: storage,
            unprivileged: unprivileged, password: password
        )
        return try await ssh.execute(command, profile: profile)
    }

    public func containerCommand(
        vmid: String, hostname: String, template: String, cores: Int,
        memoryMB: Int, diskGB: Int, storage: String, unprivileged: Bool,
        password: String = ""
    ) throws -> String {
        try validate(vmid: vmid)
        guard !hostname.isEmpty, !template.isEmpty, cores > 0, memoryMB > 0, diskGB > 0,
              safeIdentifier(storage), template.contains(":vztmpl/") else {
            throw PVEOperationError.invalidParameters
        }
        return "pct create \(vmid) \(quote(template)) --hostname \(quote(hostname)) " +
            "--memory \(memoryMB) --cores \(cores) --rootfs \(quote(storage + ":" + String(diskGB))) " +
            "--net0 name=eth0,bridge=vmbr0,ip=dhcp --unprivileged \(unprivileged ? 1 : 0)" +
            (password.isEmpty ? "" : " --password \(quote(password))")
    }

    private func validate(vmid: String) throws {
        guard !vmid.isEmpty, vmid.allSatisfy(\.isNumber) else { throw PVEOperationError.invalidVMID }
    }

    private func quote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\"'\"'") + "'"
    }


    private func safeIdentifier(_ value: String) -> Bool {
        !value.isEmpty && value.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }
    }
}

public enum PVEOperationError: LocalizedError {
    case invalidVMID
    case invalidParameters
    case unsupportedContainerReset
    public var errorDescription: String? {
        switch self {
        case .invalidVMID: "VMID 必须是数字"
        case .invalidParameters: "创建参数不合法"
        case .unsupportedContainerReset: "容器不支持 reset，请用强制停止后再启动"
        }
    }
}
