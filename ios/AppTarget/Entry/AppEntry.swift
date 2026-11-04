// The app's entry point (@main BrakeCheckApp) lives in the BrakeCheck package, the
// same code xtool builds. This file only gives the Xcode app target a source file.
import BrakeCheck
import AppIntents

/// The Siri/Shortcuts intents live in the package; this tells Xcode to extract their metadata.
struct BrakeCheckAppIntentsPackage: AppIntentsPackage {
    static var includedPackages: [any AppIntentsPackage.Type] { [BrakeCheckIntentsPackage.self] }
}
