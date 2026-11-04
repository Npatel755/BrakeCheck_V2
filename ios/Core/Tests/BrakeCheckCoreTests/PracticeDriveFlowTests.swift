import Foundation
import XCTest
@testable import BrakeCheckCore

final class PracticeDriveFlowTests: XCTestCase {
    private func context() -> PracticeDriveFlow.Context {
        var state = PracticeDriveFlow.Context()
        state.visible = true; state.foreground = true; state.noticeAccepted = true
        return state
    }
    private func offered() throws -> (PracticeDriveFlow, PracticeDriveFlow.Context) {
        var flow = PracticeDriveFlow(), state = context()
        flow.reconcile(state, now: 100)
        let request = try XCTUnwrap(flow.request)
        XCTAssertEqual(flow.fire(request, context: state, now: request.deadline), [.showOffer])
        state.previouslyOffered = true
        return (flow, state)
    }
    private func running() throws -> (PracticeDriveFlow, PracticeDriveFlow.Context, Double) {
        var (flow, state) = try offered()
        flow.accept(state, now: 101)
        let start = try XCTUnwrap(flow.request)
        let actions = flow.fire(start, context: state, now: start.deadline)
        let id = try XCTUnwrap(flow.sessionID)
        XCTAssertEqual(actions, [.startDemo(id)])
        state.demoSessionID = id; state.onHome = false
        return (flow, state, start.deadline)
    }
    func testOfferWaitsUntilNoticeForegroundAndIdleHomeAreReady() throws {
        let changes: [(inout PracticeDriveFlow.Context) -> Void] = [
            { $0.visible = false }, { $0.foreground = false }, { $0.noticeAccepted = false },
            { $0.previouslyOffered = true }, { $0.onHome = false }, { $0.liveTrip = true },
            { $0.finalizing = true }, { $0.replay = true }, { $0.navigation = true },
            { $0.checkin = true }, { $0.presentationBusy = true }, { $0.demoSessionID = UUID() }
        ]
        for change in changes {
            var flow = PracticeDriveFlow(), state = context(); change(&state)
            XCTAssertTrue(flow.reconcile(state, now: 10).isEmpty)
            XCTAssertNil(flow.request)
        }
    }
    func testInterruptedUnofferedDelayCanRetryAndOldCompletionCannotPresent() throws {
        var flow = PracticeDriveFlow(), state = context()
        flow.reconcile(state, now: 100); let old = try XCTUnwrap(flow.request)
        state.liveTrip = true
        flow.reconcile(state, now: 100.2)
        XCTAssertNil(flow.request)
        state.liveTrip = false
        flow.reconcile(state, now: 200); let current = try XCTUnwrap(flow.request)
        XCTAssertNotEqual(current.id, old.id)
        XCTAssertTrue(flow.fire(old, context: state, now: 201).isEmpty)
        XCTAssertEqual(flow.fire(current, context: state, now: 201), [.showOffer])
    }
    func testEarlyWakeDoesNotPresentOffer() throws {
        var flow = PracticeDriveFlow(); let state = context()
        flow.reconcile(state, now: 100); let request = try XCTUnwrap(flow.request)
        XCTAssertTrue(flow.fire(request, context: state, now: 100.1).isEmpty)
        XCTAssertEqual(flow.request?.id, request.id)
        XCTAssertEqual(flow.fire(request, context: state, now: request.deadline), [.showOffer])
    }
    func testDelayedStartCannotLaunchDuringTripReplayOrOtherPresentation() throws {
        let changes: [(inout PracticeDriveFlow.Context) -> Void] = [
            { $0.visible = false }, { $0.foreground = false }, { $0.liveTrip = true },
            { $0.finalizing = true }, { $0.replay = true }, { $0.navigation = true },
            { $0.checkin = true }, { $0.presentationBusy = true }, { $0.demoSessionID = UUID() }
        ]
        for change in changes {
            var (flow, state) = try offered()
            flow.accept(state, now: 101); let request = try XCTUnwrap(flow.request)
            change(&state)
            XCTAssertTrue(flow.fire(request, context: state, now: 102).isEmpty)
            XCTAssertNil(flow.sessionID); XCTAssertNil(flow.request)
        }
    }
    func testSheetDismissalAfterAcceptDoesNotCancelTheExplicitStart() throws {
        var (flow, state) = try offered()
        flow.accept(state, now: 101); let request = try XCTUnwrap(flow.request)
        flow.skip() // SwiftUI's onDismiss after the Start button closes the sheet.
        XCTAssertEqual(flow.request?.id, request.id)
        XCTAssertEqual(flow.fire(request, context: state, now: request.deadline).count, 1)
        XCTAssertNotNil(flow.sessionID)
    }
    func testSkippingOfferDoesNotScheduleDemoOrPaywall() throws {
        var (flow, state) = try offered()
        flow.skip(); flow.accept(state, now: 101)
        XCTAssertNil(flow.request); XCTAssertNil(flow.sessionID)
        XCTAssertTrue(flow.reconcile(state, now: 200).isEmpty)
        XCTAssertNil(flow.request)
    }
    func testCompletePracticeUsesTwentyForegroundSecondsAndOnePaywall() throws {
        var (flow, state, began) = try running()
        let finish = try XCTUnwrap(flow.request), id = try XCTUnwrap(flow.sessionID)
        XCTAssertEqual(finish.deadline, began + 20, accuracy: 0.001)
        XCTAssertTrue(flow.fire(finish, context: state, now: began + 19).isEmpty)
        XCTAssertEqual(flow.fire(finish, context: state, now: began + 20), [.stopDemo(id), .returnHome])
        state.demoSessionID = nil; state.onHome = true
        flow.reconcile(state, now: began + 20)
        let premium = try XCTUnwrap(flow.request)
        XCTAssertEqual(flow.fire(premium, context: state, now: premium.deadline), [.showPremium])
        XCTAssertTrue(flow.fire(premium, context: state, now: premium.deadline + 1).isEmpty)
    }
    func testEarlyManualEndDoesNotUpsellAndCompletedWatchMayDoSo() throws {
        for elapsed in [5.0, 11.999, 12.0, 19.0] {
            var (flow, state, began) = try running()
            let id = try XCTUnwrap(flow.sessionID)
            XCTAssertEqual(flow.finish(state, now: began + elapsed), [.stopDemo(id), .returnHome])
            state.demoSessionID = nil; state.onHome = true
            flow.reconcile(state, now: began + elapsed)
            XCTAssertEqual(flow.request != nil, elapsed >= 12)
        }
    }
    func testBackgroundCancelsOwnedDemoWithoutCountingBackgroundAsWatchTime() throws {
        var (flow, state, began) = try running()
        let request = try XCTUnwrap(flow.request), id = try XCTUnwrap(flow.sessionID)
        state.foreground = false
        XCTAssertEqual(flow.reconcile(state, now: began + 8), [.stopDemo(id)])
        state.foreground = true; state.demoSessionID = nil; state.onHome = true
        XCTAssertTrue(flow.fire(request, context: state, now: began + 100).isEmpty)
        flow.reconcile(state, now: began + 100)
        XCTAssertNil(flow.request)
    }
    func testReplacementDemoIsNeverStoppedByOldPracticeDeadline() throws {
        var (flow, state, began) = try running()
        let finish = try XCTUnwrap(flow.request)
        state.demoSessionID = UUID()
        XCTAssertTrue(flow.fire(finish, context: state, now: began + 21).isEmpty)
        XCTAssertNil(flow.request); XCTAssertNil(flow.sessionID)
    }
    func testStoppedDemoCannotCompleteOldPracticeOrOpenPaywall() throws {
        var (flow, state, began) = try running()
        let finish = try XCTUnwrap(flow.request)
        state.demoSessionID = nil
        XCTAssertTrue(flow.fire(finish, context: state, now: began + 21).isEmpty)
        flow.reconcile(state, now: began + 21)
        XCTAssertNil(flow.request)
    }
    func testSafetyCheckinStopsOnlyTheOwnedPracticeAndDismissesOffer() throws {
        var (flow, state, began) = try running(); let id = try XCTUnwrap(flow.sessionID)
        state.checkin = true
        XCTAssertEqual(flow.reconcile(state, now: began + 14), [.stopDemo(id)])
        XCTAssertNil(flow.request)
        (flow, state) = try offered(); state.checkin = true
        XCTAssertEqual(flow.reconcile(state, now: 101), [.dismissOffer])
    }
    func testDeferredPaywallCannotInterruptTripBackgroundOrPurchaseStateChange() throws {
        let changes: [(inout PracticeDriveFlow.Context) -> Void] = [
            { $0.liveTrip = true }, { $0.foreground = false }, { $0.checkin = true },
            { $0.presentationBusy = true }, { $0.subscribed = true }, { $0.demoSessionID = UUID() }
        ]
        for change in changes {
            var (flow, state, began) = try running()
            _ = flow.finish(state, now: began + 14)
            state.demoSessionID = nil; state.onHome = true
            flow.reconcile(state, now: began + 14)
            let request = try XCTUnwrap(flow.request)
            change(&state)
            XCTAssertTrue(flow.fire(request, context: state, now: began + 15).isEmpty)
            XCTAssertNil(flow.request)
        }
    }
    func testInvalidClockCannotRunDelayedActionOrKeepDemoAlive() throws {
        var (flow, state, _) = try running(); let id = try XCTUnwrap(flow.sessionID)
        XCTAssertEqual(flow.reconcile(state, now: .nan), [.stopDemo(id)])
        XCTAssertNil(flow.request)
    }
    func testLeavingHomeInvalidatesPaywallEvenIfUserReturnsBeforeDeadline() throws {
        var (flow, state, began) = try running()
        _ = flow.finish(state, now: began + 14)
        state.demoSessionID = nil; state.onHome = true
        flow.reconcile(state, now: began + 14); let request = try XCTUnwrap(flow.request)
        state.onHome = false
        flow.reconcile(state, now: began + 14.1)
        state.onHome = true
        flow.reconcile(state, now: began + 14.2)
        XCTAssertTrue(flow.fire(request, context: state, now: began + 15).isEmpty)
        XCTAssertNil(flow.request)
    }

}
