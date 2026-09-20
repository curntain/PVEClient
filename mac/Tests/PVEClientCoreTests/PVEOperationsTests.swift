import XCTest
import PVEClientCore

final class PVEOperationsTests: XCTestCase {
    func testVMPlanWithISO() throws {
        let commands = try PVEOperations().vmCommands(
            vmid: "105", name: "test vm", cores: 2, memoryMB: 2048,
            diskGB: 32, storage: "local-lvm", iso: "debian.iso",
            bridge: "vmbr0", netModel: "virtio"
        )
        XCTAssertEqual(commands.count, 3)
        XCTAssertTrue(commands[0].contains("--name 'test vm'"))
        XCTAssertTrue(commands[1].contains("--ide2 'local:iso/debian.iso'"))
        XCTAssertTrue(commands[2].contains("--ide0 'local-lvm:32'"))
    }

    func testVMPlanWithoutISO() throws {
        let commands = try PVEOperations().vmCommands(
            vmid: "106", name: "headless", cores: 4, memoryMB: 4096,
            diskGB: 64, storage: "local-lvm", iso: nil,
            bridge: "vmbr0"
        )
        XCTAssertEqual(commands.count, 2)
        XCTAssertTrue(commands[1].contains("--scsi0 'local-lvm:64'"))
    }

    func testContainerPlanQuotesPassword() throws {
        let command = try PVEOperations().containerCommand(
            vmid: "107", hostname: "test-ct", template: "local:vztmpl/debian.tar.zst",
            cores: 1, memoryMB: 512, diskGB: 8, storage: "local-lvm",
            unprivileged: true, password: "a'b"
        )
        XCTAssertTrue(command.contains("--unprivileged 1"))
        XCTAssertTrue(command.contains("--password 'a'\"'\"'b'"))
    }

    func testRejectsUnsafeParameters() throws {
        XCTAssertThrowsError(try PVEOperations().vmCommands(
            vmid: "1;rm", name: "bad", cores: 2, memoryMB: 2048,
            diskGB: 32, storage: "local-lvm", iso: nil, bridge: "vmbr0"
        ))
        XCTAssertThrowsError(try PVEOperations().vmCommands(
            vmid: "108", name: "bad", cores: 2, memoryMB: 2048,
            diskGB: 32, storage: "local-lvm;rm", iso: nil, bridge: "vmbr0"
        ))
    }
}
