import BrakeCheckCore
import Foundation
import CoreMotion

/// Experimental impact CANDIDATE detector. A phone drop can trigger it; a crash can be missed.
final class ImpactService {
    private let manager = CMMotionManager()
    private var hits: [TimeInterval] = []
    private var lastTrigger: TimeInterval = -.infinity
    var onCandidate: ((Double) -> Void)?
    var onStatus: ((String) -> Void)?

    func start() {
        guard manager.isDeviceMotionAvailable else {
            onStatus?("Motion sensing unavailable")
            return
        }
        guard !manager.isDeviceMotionActive else { return }
        hits.removeAll()
        manager.deviceMotionUpdateInterval = 1.0 / 50.0
        manager.startDeviceMotionUpdates(to: .main) { [weak self] motion, error in
            guard let self else { return }
            if let error {
                self.onStatus?("Motion: \(error.localizedDescription)")
                return
            }
            guard let motion else { return }
            let a = motion.userAcceleration // Gravity removed; units are g.
            let magnitude = sqrt(a.x * a.x + a.y * a.y + a.z * a.z)
            self.hits.removeAll { motion.timestamp - $0 > 0.15 }
            if magnitude > 2.8 { self.hits.append(motion.timestamp) }
            if self.hits.count >= 2, motion.timestamp - self.lastTrigger > 60 {
                self.lastTrigger = motion.timestamp
                self.hits.removeAll()
                self.onCandidate?(magnitude)
            }
        }
        onStatus?("Experimental impact sensing active")
    }

    func stop() {
        manager.stopDeviceMotionUpdates()
        hits.removeAll()
        onStatus?("Impact sensing paused")
    }
}
