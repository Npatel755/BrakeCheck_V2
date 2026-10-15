// swift-tools-version: 6.0

import PackageDescription
import Foundation

// SwiftPM copies model packages without checking their external weights.
// Reject incomplete assets before building a camera whose detector cannot load.
let resourcesRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
    .appendingPathComponent("Sources/BrakeCheck/Resources")
for (packagePath, provenancePath) in [
    ("DetectorModelData/Day", "ModelLicenses/RTMDetTiny-provenance.json"),
    ("DetectorModelData/Night", "ModelLicenses/RTMDetSmall-provenance.json"),
    ("LaneModelData", "LaneModelData/manifest.json")
] {
    let data = try Data(contentsOf: resourcesRoot.appendingPathComponent(provenancePath))
    let record = try JSONSerialization.jsonObject(with: data) as! [String: Any]
    for relative in (record["files"] as! [String: String]).keys {
        let file = resourcesRoot.appendingPathComponent(packagePath).appendingPathComponent(relative)
        guard let size = try? file.resourceValues(forKeys: [.fileSizeKey]).fileSize, size > 0 else {
            fatalError("Incomplete bundled model: \(packagePath)/\(relative). Restore model assets before building.")
        }
    }
}

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

            // The old Ultralytics/YOLOP models aren't shipped. Xcode picks up any .mlmodel in the
            // target automatically, and two of these share the name model.mlmodel, so it can't resolve.
            exclude: [
                "Resources/YOLOModelData"
            ],

            resources: [
                // Copy permissive RTMDet packages as ordinary resource data.
                // Avoid triggering Apple's coremlc during
                // the Linux/xtool build.
                .copy("Resources/DetectorModelData"),
                .copy("Resources/ModelLicenses"),
                // UFLD lanes and SegFormer roads are ordinary data on Linux.
                // Core ML compiles it on the iPhone and caches the result.
                .copy("Resources/LaneModelData"),
                .copy("Resources/ViewpointModelData"),
                // Red Hat Display, the app's typeface (OFL-1.1); registered at runtime by BrakeFont.
                .copy("Resources/Fonts")
            ]
        )
    ],

    swiftLanguageModes: [
        .v5
    ]
)
