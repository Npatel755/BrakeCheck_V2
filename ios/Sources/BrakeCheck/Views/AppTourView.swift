import SwiftUI

/// A short guided walk through the main screens, offered once after onboarding and
/// available again from Settings → Try BrakeSense.
enum AppTourStep: Int, CaseIterable, Identifiable {
    case startTrip, camera, navigate, clips, safety, done

    var id: Int { rawValue }

    var title: String {
        switch self {
        case .startTrip: return "Start a trip"
        case .camera: return "Your road view"
        case .navigate: return "Navigate"
        case .clips: return "Clips and trips"
        case .safety: return "Safety and settings"
        case .done: return "You're set"
        }
    }

    var text: String {
        switch self {
        case .startTrip:
            return "Press Start trip on Home. BrakeSense watches the car ahead through the camera and warns you before a hard stop."
        case .camera:
            return "The Camera tab shows the live road with your speed, the gap to the car ahead, lane guides and the colour risk ramp. Mount the phone upright, camera facing forward."
        case .navigate:
            return "Press the search field on Navigate, pick a destination and press Go. Turns are spoken and drawn on the camera view, with lane and curve advice on the way."
        case .clips:
            return "Hard stops and impacts save a short clip automatically. Find them under Clips on Home; completed trips are listed in Trip history."
        case .safety:
            return "Press the gear on Home for Settings. Safety holds impact detection, emergency check-ins, Go alerts and Lane alerts; Camera display sets units and overlays."
        case .done:
            return "You can replay this tour any time from Settings → Try BrakeSense. Drive safe."
        }
    }

    var icon: String {
        switch self {
        case .startTrip: return "play.fill"
        case .camera: return "video.fill"
        case .navigate: return "location.north.line.fill"
        case .clips: return "film.stack"
        case .safety: return "shield.fill"
        case .done: return "checkmark.circle.fill"
        }
    }

    var next: AppTourStep? { AppTourStep(rawValue: rawValue + 1) }
    var previous: AppTourStep? { AppTourStep(rawValue: rawValue - 1) }
}

/// The small popup that offers the tour.
struct AppTourOfferView: View {
    var accept: () -> Void
    var skip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Image(systemName: "map.fill")
                .font(.brake(size: 28, weight: .semibold))
                .foregroundStyle(BrakeTheme.accentText)
                .accessibilityHidden(true)
            Text("Want a quick tour?")
                .font(.brake(size: 24, weight: .bold))
                .fixedSize(horizontal: false, vertical: true)
            Text("Six short tips show where to start a trip, read the road view, navigate, find clips and set up safety alerts. About a minute.")
                .font(.brake(.subheadline))
                .foregroundStyle(BrakeTheme.muted)
                .fixedSize(horizontal: false, vertical: true)
            Button(action: accept) {
                Text("Show me around")
                    .font(.brake(size: 16, weight: .bold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
                    .foregroundStyle(BrakeTheme.background)
                    .background(BrakeTheme.accent, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("tour.accept")
            Button(action: skip) {
                Text("Not now")
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("tour.skip")
        }
        .padding(24)
        .frame(maxWidth: 560, maxHeight: .infinity, alignment: .top)
        .frame(maxWidth: .infinity)
        .background(BrakeTheme.background.ignoresSafeArea())
        .foregroundStyle(BrakeTheme.text)
    }
}

/// One tip card, shown over the page the step talks about.
struct AppTourCard: View {
    let step: AppTourStep
    var back: () -> Void
    var advance: () -> Void
    var skip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: step.icon)
                    .font(.brake(size: 18, weight: .semibold))
                    .foregroundStyle(BrakeTheme.accentText)
                    .frame(width: 36, height: 36)
                    .background(BrakeTheme.accent.opacity(0.15), in: Circle())
                    .accessibilityHidden(true)
                Text(step.title)
                    .font(.brake(.headline, weight: .bold))
                Spacer(minLength: 8)
                Button("Skip", action: skip)
                    .font(.brake(.subheadline, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                    .accessibilityIdentifier("tour.end")
            }
            Text(step.text)
                .font(.brake(.subheadline))
                .foregroundStyle(BrakeTheme.text)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 12) {
                Text("\(step.rawValue + 1) of \(AppTourStep.allCases.count)")
                    .font(.brake(.caption, weight: .semibold))
                    .foregroundStyle(BrakeTheme.muted)
                Spacer()
                if step.previous != nil {
                    Button("Back", action: back)
                        .font(.brake(.subheadline, weight: .semibold))
                        .foregroundStyle(BrakeTheme.text)
                        .accessibilityIdentifier("tour.back")
                }
                Button(action: advance) {
                    Text(step.next == nil ? "Done" : "Next")
                        .font(.brake(.subheadline, weight: .bold))
                        .foregroundStyle(BrakeTheme.background)
                        .padding(.horizontal, 20).padding(.vertical, 10)
                        .background(BrakeTheme.accent, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier(step.next == nil ? "tour.done" : "tour.next")
            }
        }
        .padding(18)
        .background(BrakeTheme.panel, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 22, style: .continuous).strokeBorder(BrakeTheme.accent.opacity(0.35), lineWidth: 1))
        .shadow(color: .black.opacity(0.35), radius: 18, y: 8)
        .frame(maxWidth: 520)
        .padding(.horizontal, 16)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("tour.card")
    }
}
