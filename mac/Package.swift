// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "PVEClientShell",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .executable(name: "PVEClientShell", targets: ["PVEClientShell"]),
        .library(name: "PVEClientCore", targets: ["PVEClientCore"]),
        .executable(name: "PVENativeHelper", targets: ["PVENativeHelper"])
    ],
    dependencies: [
        .package(url: "https://github.com/orlandos-nl/Citadel.git", from: "0.12.1"),
        .package(url: "https://github.com/apple/swift-nio.git", from: "2.81.0")
    ],
    targets: [
        .executableTarget(
            name: "PVEClientShell",
            path: "Sources/PVEClientShell"
        ),
        .target(
            name: "PVEClientCore",
            dependencies: [
                .product(name: "Citadel", package: "Citadel"),
                .product(name: "NIO", package: "swift-nio")
            ],
            path: "Sources/PVEClientCore"
        ),
        .executableTarget(
            name: "PVENativeHelper",
            dependencies: [
                "PVEClientCore",
                .product(name: "NIO", package: "swift-nio")
            ],
            path: "Sources/PVENativeHelper"
        ),
        .testTarget(
            name: "PVEClientCoreTests",
            dependencies: ["PVEClientCore"],
            path: "Tests/PVEClientCoreTests"
        )
    ]
)
