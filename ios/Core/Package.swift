// swift-tools-version: 6.0
import PackageDescription
let package = Package(
    name: "BrakeCheckCore",
    products: [.library(name: "BrakeCheckCore", targets: ["BrakeCheckCore"])],
    targets: [.target(name: "BrakeCheckCore"),
              .testTarget(name: "BrakeCheckCoreTests", dependencies: ["BrakeCheckCore"])],
    swiftLanguageModes: [.v5]
)
