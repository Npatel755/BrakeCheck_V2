// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "BrakeCheck",

    platforms: [
        .iOS(.v17)
    ],

    products: [
        .library(
            name: "BrakeCheck",
            targets: ["BrakeCheck"]
        )
    ],

    dependencies: [
        .package(path: "Core")
    ],

    targets: [
        .target(
            name: "BrakeCheck",

            dependencies: [
                .product(
                    name: "BrakeCheckCore",
                    package: "Core"
                )
            ],

            path: "Sources/BrakeCheck",

            resources: [
                // Copy permissive RTMDet packages as ordinary resource data.
                // Avoid triggering Apple's coremlc during
                // the Linux/xtool build.
                .copy("Resources/DetectorModelData"),
                .copy("Resources/ModelLicenses"),
                // The trained lane model is also ordinary data on Linux.
                // Core ML compiles it on the iPhone and caches the result.
                .copy("Resources/LaneModelData"),
                .copy("Resources/ViewpointModelData")
            ]
        )
    ],

    swiftLanguageModes: [
        .v5
    ]
)
