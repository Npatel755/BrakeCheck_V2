import Foundation

/// Explains why an optional reminder is waiting. This is presentation only;
/// it never grants Premium access or authorizes an engine observation.
public struct DrivingAssistReadiness {
    public var premium = false
    public var enabled = false
    public var liveTrip = false
    public var replayOrDemo = false
    public var foreground = true
    public var cameraReady = false
    public var detectorReady = false
    public var priorityAlert = false
    public var aligned = false
    public var maneuver = false
    public var freshStationaryGPS = false
    public var freshMovingSpeed: Double?
    public var lane: LanePath?
    public var freshLeadRange = false
    public var freshRearVehicle = false
    public var freshSignal = false
    public var now: TimeInterval = 0
    public init() {}

    public func message(for feature: DrivingAssistFeature) -> String {
        guard premium else { return "Premium access is off" }
        guard enabled else { return "Off" }
        guard !replayOrDemo else { return "Requires a live drive; unavailable in replay or demo" }
        guard liveTrip else { return "Start a live drive to use this alert" }
        guard foreground else { return "Keep BrakeSense visible" }
        guard cameraReady else { return "Waiting for the camera" }
        guard detectorReady else { return "Waiting for vehicle detection" }
        guard !priorityAlert else { return "Paused while a collision warning or check-in takes priority" }
        switch feature {
        case .trafficStart:
            guard freshStationaryGPS else { return "Waiting for GPS to confirm you are stopped" }
            guard freshLeadRange else { return "Waiting for a confirmed lead and fresh gap measurements" }
            return "Watching for a stopped lead to move away"
        case .greenLight:
            guard freshStationaryGPS else { return "Waiting for GPS to confirm you are stopped" }
            guard freshSignal else { return "Waiting for one clear vertical traffic signal" }
            return "Watching for a sustained red-to-green change"
        case .followingDistance:
            guard let speed = freshMovingSpeed, speed.isFinite, speed >= 10 else { return "Waiting for reliable GPS above 22 mph / 36 km/h" }
            guard freshLeadRange else { return "Waiting for a confirmed lead and fresh gap measurements" }
            return "Watching for sustained close following"
        case .laneDeparture, .cutIn:
            guard aligned else { return "Camera alignment required; open Camera setup" }
            let minimum = feature == .laneDeparture ? 8.0 : 2.0
            guard let speed = freshMovingSpeed, speed.isFinite, speed >= minimum else {
                return feature == .laneDeparture
                    ? "Waiting for reliable GPS above 18 mph / 29 km/h"
                    : "Waiting for reliable moving GPS"
            }
            if feature == .laneDeparture && maneuver { return "Paused during a navigation turn or route rejoin" }
            guard let lane, lane.isUsable(at: now), lane.source != .markings, lane.confidence >= 0.85 else {
                return "Waiting for a fresh, reliable lane pair"
            }
            if feature == .laneDeparture {
                guard let band = lane.bands.first, band.y <= 0.3,
                      (0.15...0.95).contains(band.right - band.left) else {
                    return "Waiting for visible lane boundaries near the vehicle"
                }
                return "Watching for gradual drift after stable centered driving"
            }
            guard freshRearVehicle else { return "Waiting for a rear-facing adjacent vehicle track" }
            return "Watching for sustained movement into your lane"
        }
    }
}

public enum DrivingAssistPresentation {
    public static func showsAlert(premium: Bool, liveTrip: Bool, foreground: Bool,
                                  replayOrDemo: Bool, critical: Bool, checkin: Bool,
                                  selecting: Bool, capturedAt: TimeInterval, now: TimeInterval) -> Bool {
        premium && liveTrip && foreground && !replayOrDemo && !critical && !checkin && !selecting
            && capturedAt.isFinite && now.isFinite && (0...3).contains(now - capturedAt)
    }
}
