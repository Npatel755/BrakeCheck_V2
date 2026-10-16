import CoreText
import SwiftUI

/// The app's typeface: Red Hat Display (SIL Open Font License 1.1, Resources/Fonts/OFL.txt),
/// bundled as static weights and registered for this process the first time a font is asked for.
/// SwiftPM resources live in Bundle.module, so Info.plist's UIAppFonts can't list them.
enum BrakeFont {
    private static let registered: Void = {
        for url in Bundle.module.urls(forResourcesWithExtension: "ttf", subdirectory: "Fonts") ?? [] {
            CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
        }
    }()

    static func name(_ weight: Font.Weight) -> String {
        _ = registered
        switch weight {
        case .ultraLight, .thin, .light: return "RedHatDisplay-Light"
        case .medium: return "RedHatDisplay-Medium"
        case .semibold: return "RedHatDisplay-SemiBold"
        case .bold: return "RedHatDisplay-Bold"
        case .heavy: return "RedHatDisplay-ExtraBold"
        case .black: return "RedHatDisplay-Black"
        default: return "RedHatDisplay-Regular"
        }
    }

    /// Apple's default point size and weight for each text style, so swapping fonts keeps the layout.
    fileprivate static func metrics(_ style: Font.TextStyle) -> (size: CGFloat, weight: Font.Weight) {
        switch style {
        case .largeTitle: return (34, .regular)
        case .title: return (28, .regular)
        case .title2: return (22, .regular)
        case .title3: return (20, .regular)
        case .headline: return (17, .semibold)
        case .callout: return (16, .regular)
        case .subheadline: return (15, .regular)
        case .footnote: return (13, .regular)
        case .caption: return (12, .regular)
        case .caption2: return (11, .regular)
        default: return (17, .regular)
        }
    }
}

extension Font {
    /// Replaces `.system(size:weight:)`: a fixed size, as the system call was (the driving HUD
    /// depends on it).
    static func brake(size: CGFloat, weight: Font.Weight = .regular) -> Font {
        .custom(BrakeFont.name(weight), fixedSize: size)
    }

    /// Replaces `.headline`, `.caption` and the other text styles; scales with Dynamic Type.
    static func brake(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> Font {
        let metrics = BrakeFont.metrics(style)
        return .custom(BrakeFont.name(weight ?? metrics.weight), size: metrics.size, relativeTo: style)
    }
}
