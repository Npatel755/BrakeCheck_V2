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

            // The old Ultralytics (AGPL) detectors stay on disk for comparison only.
            // Without this, Xcode's SwiftPM treats their .mlmodel files as resources
            // (duplicate 'model.mlmodel' error, and AGPL weights in the app).
            exclude: [
                "Resources/YOLOModelData"
            ],

            resources: [
                // Copy permissive RTMDet packages as ordinary resource data.
                // Avoid triggering Apple's coremlc during
                // the Linux/xtool build.
                .copy("Resources/DetectorModelData"),
                .copy("Resources/ModelLicenses"),
                // The trained lane model is also ordinary data on Linux.
                // Core ML compiles it on the iPhone and caches the result.
                .copy("Resources/LaneModelData"),
                .copy("Resources/ViewpointModelData"),
                // Apple's required privacy manifest (required-reason APIs, no tracking).
                .copy("Resources/PrivacyInfo.xcprivacy")
            ]
        )
    ],

    swiftLanguageModes: [
        .v5
    ]
)
