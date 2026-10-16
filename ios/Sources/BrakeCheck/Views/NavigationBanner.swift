import BrakeCheckCore
import SwiftUI

/// Turn-by-turn card over the camera, like the Apple Maps / Google Maps top banner: the maneuver arrow,
/// a large distance, the street, and a "Then" strip for the following step. Landscape keeps it top-right
/// beside the risk pill; portrait spans the width above it. The road ribbon stays in CameraNavigationOverlay.
struct NavigationBanner: View {
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("display.accent") private var accentChoice = BrakeTheme.defaultAccent
    @ObservedObject var navigation: NavigationService
    @ObservedObject var presentation: CameraARPresentation
    /// During a brake warning the card drops to one line so the alert reads first.
    var compact = false
    var large = false
    var onSetup: (() -> Void)? = nil

    /// Dark accents are light, so they take dark ink; light-mode accents are deep enough for white.
    private var ink: Color { colorScheme == .dark ? Color(red: 0.03, green: 0.06, blue: 0.13) : .white }
    private var fill: Color { BrakeTheme.accentColor(named: accentChoice) }
    private var distance: (value: String, unit: String)? {
        guard !navigation.hasArrived, !navigation.isCalculating,
              let meters = navigation.maneuverDistance else { return nil }
        return Self.formatted(meters, miles: navigation.useMPH)
    }
    private var symbol: String {
        if navigation.hasArrived { return "flag.checkered" }
        if navigation.isCalculating { return "arrow.triangle.2.circlepath" }
        return NavigationManeuver.symbol(for: primary)
    }
    private var primary: String {
        if navigation.hasArrived { return navigation.destinationTitle.map { "Arrived · \($0)" } ?? "You have arrived" }
        if navigation.isCalculating { return "Calculating route" }
        if navigation.isPaused || navigation.isRejoiningRoute { return navigation.lastRouteInstruction }
        return navigation.instruction
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            card
            #if DEBUG
            // Developer builds keep the road-alignment reason that the old status box showed.
            if presentation.level != .aligned && !compact {
                Text(presentation.guidanceDetail).font(.brake(.caption)).lineLimit(2)
                    .foregroundStyle(.white).padding(.horizontal, 10).padding(.vertical, 4)
                    .background(.black.opacity(0.65), in: Capsule())
            }
            #endif
        }
    }

    private var card: some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: large ? 16 : 14) {
                Image(systemName: symbol)
                    .font(.brake(size: compact ? 24 : large ? 40 : 36, weight: .bold))
                    .frame(width: compact ? 30 : large ? 50 : 44)
                if compact {
                    Text(compactLine).font(.brake(size: 17, weight: .bold)).lineLimit(1).minimumScaleFactor(0.75)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        if let distance {
                            HStack(alignment: .firstTextBaseline, spacing: 4) {
                                Text(distance.value).font(.brake(size: large ? 46 : 42, weight: .heavy))
                                    .monospacedDigit().contentTransition(.numericText())
                                Text(distance.unit).font(.brake(size: large ? 22 : 20, weight: .bold))
                            }
                            .animation(.easeOut(duration: 0.2), value: distance.value)
                        }
                        Text(primary).font(.brake(size: distance == nil ? (large ? 24 : 22) : (large ? 19 : 18), weight: .semibold))
                            .lineLimit(2).minimumScaleFactor(0.8).fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, large ? 18 : 16).padding(.vertical, compact ? 10 : large ? 14 : 12)
            if !compact { strip }
        }
        .foregroundStyle(ink)
        .background(fill, in: RoundedRectangle(cornerRadius: compact ? 18 : 24, style: .continuous))
        .clipShape(RoundedRectangle(cornerRadius: compact ? 18 : 24, style: .continuous))
        .shadow(color: .black.opacity(0.35), radius: 14, y: 6)
        .animation(.easeOut(duration: 0.2), value: compact)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilitySummary)
    }

    /// The following step, or the trip's remaining time and distance when there is none.
    private var strip: some View {
        HStack(spacing: 10) {
            if let next = navigation.upcomingInstruction, !navigation.hasArrived {
                Text("Then").font(.brake(size: 15, weight: .semibold)).opacity(0.75)
                Image(systemName: NavigationManeuver.symbol(for: next)).font(.brake(size: 15, weight: .bold))
                Text(Self.road(in: next)).lineLimit(1).minimumScaleFactor(0.8)
            } else if let summary = tripSummary {
                Text(summary).lineLimit(1).minimumScaleFactor(0.8)
            } else if let title = navigation.destinationTitle {
                Text(title).lineLimit(1)
            }
            Spacer(minLength: 0)
            if let onSetup {
                Button(action: onSetup) {
                    Image(systemName: presentation.alignmentLocked ? "lock.fill" : "viewfinder")
                        .font(.brake(size: 14, weight: .bold))
                        .frame(width: 32, height: 32)
                        .background(ink.opacity(0.14), in: Circle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(presentation.alignmentLocked ? "Alignment locked. Adjust camera setup" : "Camera setup")
            }
        }
        .font(.brake(size: 16, weight: .semibold))
        .padding(.horizontal, large ? 18 : 16).frame(minHeight: 42)
        .background(Color.black.opacity(colorScheme == .dark ? 0.14 : 0.18))
    }

    private var compactLine: String {
        guard let distance else { return Self.road(in: primary) }
        return "\(distance.value) \(distance.unit) · \(Self.road(in: primary))"
    }

    private var tripSummary: String? {
        guard let seconds = navigation.remainingTime, let meters = navigation.remainingDistance, !navigation.hasArrived else { return nil }
        let minutes = Int((seconds / 60).rounded())
        let time = minutes >= 60 ? "\(minutes / 60) h \(minutes % 60) min" : "\(max(minutes, 1)) min"
        let left = Self.formatted(meters, miles: navigation.useMPH)
        return "\(time) · \(left.value) \(left.unit) to go"
    }

    private var accessibilitySummary: String {
        var parts = [distance.map { navigation.isPaused
            ? "Last known distance \($0.value) \($0.unit)" : "In \($0.value) \($0.unit)" }, primary].compactMap { $0 }
        if !navigation.isPaused, let next = navigation.upcomingInstruction { parts.append("Then \(next)") }
        return parts.joined(separator: ". ")
    }

    /// "Turn right onto Peachtree St NE" → "Peachtree St NE"; other phrasing is kept whole.
    static func road(in instruction: String) -> String {
        guard let range = instruction.range(of: " onto ", options: .caseInsensitive) else { return instruction }
        let road = instruction[range.upperBound...].trimmingCharacters(in: .whitespaces)
        return road.isEmpty ? instruction : road
    }

    /// Rounded the way map apps announce distances: 50 ft steps above 300 ft, tenths of a mile from 0.1 mi.
    static func formatted(_ meters: Double, miles: Bool) -> (value: String, unit: String) {
        let meters = max(0, meters)
        if miles {
            if meters >= 160.934 {
                let value = meters / 1609.344
                return (value >= 10 ? String(format: "%.0f", value) : String(format: "%.1f", value), "mi")
            }
            let feet = meters * 3.28084
            let step: Double = feet >= 300 ? 50 : 10
            return ("\(Int((feet / step).rounded() * step))", "ft")
        }
        if meters >= 1000 {
            let value = meters / 1000
            return (value >= 10 ? String(format: "%.0f", value) : String(format: "%.1f", value), "km")
        }
        let step: Double = meters >= 100 ? 10 : 5
        return ("\(Int((meters / step).rounded() * step))", "m")
    }
}
