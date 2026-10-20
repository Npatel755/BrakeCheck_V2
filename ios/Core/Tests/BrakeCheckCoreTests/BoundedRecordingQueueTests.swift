import Foundation
import XCTest
@testable import BrakeCheckCore

final class BoundedRecordingQueueTests: XCTestCase {
    func testStartSamplesOrientationAndStopKeepTheirOrderUnderBackpressure() {
        let work = BoundedRecordingQueue(maximumFrames: 2)
        let started = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        var events: [String] = [] // Accessed only on work.queue until its final barrier.
        work.submitCommand { events.append("start") }
        XCTAssertTrue(work.submitFrame {
            events.append("frame 1"); started.signal()
            _ = release.wait(timeout: .now() + 5)
        })
        XCTAssertEqual(started.wait(timeout: .now() + 2), .success)
        XCTAssertTrue(work.submitFrame { events.append("frame 2") })
        XCTAssertFalse(work.submitFrame { events.append("dropped frame") })
        work.submitCommand { events.append("orientation") }
        work.submitCommand { events.append("stop") }
        release.signal()
        work.queue.sync {}
        XCTAssertEqual(events, ["start", "frame 1", "frame 2", "orientation", "stop"])
        work.submitCommand { events.append("restart") }
        XCTAssertTrue(work.submitFrame { events.append("new trip frame") })
        work.queue.sync {}
        XCTAssertEqual(Array(events.suffix(2)), ["restart", "new trip frame"])
    }

    func testConcurrentOffersCannotExceedTheRetainedFrameLimit() {
        let work = BoundedRecordingQueue(maximumFrames: 2)
        let blocked = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        let lock = NSLock()
        var accepted = 0, executed = 0
        work.submitCommand { blocked.signal(); _ = release.wait(timeout: .now() + 5) }
        XCTAssertEqual(blocked.wait(timeout: .now() + 2), .success)
        DispatchQueue.concurrentPerform(iterations: 100) { _ in
            if work.submitFrame({ executed += 1 }) {
                lock.lock(); accepted += 1; lock.unlock()
            }
        }
        XCTAssertEqual(accepted, 2)
        release.signal()
        work.queue.sync {}
        XCTAssertEqual(executed, 2)
        XCTAssertTrue(work.submitFrame { executed += 1 })
        work.queue.sync {}
        XCTAssertEqual(executed, 3)
    }

    func testAcceptedSamplesRemainAliveAndRejectedSamplesReleaseImmediately() {
        final class Sample {
            let onRelease: () -> Void
            init(_ onRelease: @escaping () -> Void) { self.onRelease = onRelease }
            func use() {}
            deinit { onRelease() }
        }
        let work = BoundedRecordingQueue(maximumFrames: 1)
        let blocked = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        let acceptedReleased = expectation(description: "accepted sample released after execution")
        let rejectedReleased = expectation(description: "rejected sample released without execution")
        work.submitCommand { blocked.signal(); _ = release.wait(timeout: .now() + 5) }
        XCTAssertEqual(blocked.wait(timeout: .now() + 2), .success)
        var accepted: Sample? = Sample { acceptedReleased.fulfill() }
        weak var retained = accepted
        XCTAssertTrue(work.submitFrame { [accepted] in accepted?.use() })
        accepted = nil
        XCTAssertNotNil(retained)
        var rejected: Sample? = Sample { rejectedReleased.fulfill() }
        weak var unretained = rejected
        XCTAssertFalse(work.submitFrame { [rejected] in rejected?.use() })
        rejected = nil
        XCTAssertNil(unretained)
        release.signal()
        work.queue.sync {}
        wait(for: [acceptedReleased, rejectedReleased], timeout: 2)
        XCTAssertNil(retained)
    }
}
