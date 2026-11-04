// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "ARMockRunner", dependencies: [
    .package(path: "../../../.worktrees/performance-main-v3/ios/Core")
], targets: [.executableTarget(name: "ARMockRunner", dependencies: [
    .product(name: "BrakeCheckCore", package: "Core")
])], swiftLanguageModes: [.v5])
