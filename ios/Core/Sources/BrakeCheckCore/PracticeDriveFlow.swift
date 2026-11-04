import Foundation

/// The first-run demonstration owns its delayed transitions and one demo session.
/// Every deadline is monotonic and every completion rechecks the current app state.
public struct PracticeDriveFlow {
    public struct Context: Equatable {
        public var visible = false
        public var foreground = false
        public var noticeAccepted = false
        public var previouslyOffered = false
        public var onHome = true
        public var liveTrip = false
        public var finalizing = false
        public var replay = false
        public var navigation = false
        public var checkin = false
        public var presentationBusy = false
        public var subscribed = false
        public var demoSessionID: UUID?
        public init() {}
        private var available: Bool {
            visible && foreground && noticeAccepted && !liveTrip && !finalizing
                && !replay && !navigation && !checkin && !presentationBusy
        }
        fileprivate var canOffer: Bool { available && onHome && demoSessionID == nil }
        fileprivate var canRun: Bool { available }
    }
    public enum Action: Equatable {
        case showOffer, dismissOffer, startDemo(UUID), stopDemo(UUID), returnHome, showPremium
    }
    public struct Request: Equatable {
        public let id: UUID
        public let deadline: TimeInterval
        fileprivate let kind: Kind
    }
    fileprivate enum Kind: Equatable { case offer, start, finish, premium }
    private enum Phase { case idle, waitingOffer, offered, waitingStart, running, waitingPremium, finished }
    private var phase = Phase.idle
    public private(set) var request: Request?
    public private(set) var sessionID: UUID?
    private var startedAt: TimeInterval?
    public init() {}

    @discardableResult public mutating func reconcile(_ context: Context, now: TimeInterval) -> [Action] {
        guard now.isFinite, now >= 0 else { return cancel(context) }
        switch phase {
        case .idle:
            if context.canOffer && !context.previouslyOffered {
                phase = .waitingOffer; schedule(.offer, after: 0.6, now: now)
            }
        case .waitingOffer:
            if !context.canOffer || context.previouslyOffered { request = nil; phase = .idle }
        case .offered:
            if !context.canOffer { request = nil; phase = .finished; return [.dismissOffer] }
        case .waitingStart:
            if !context.canOffer { request = nil; phase = .finished }
        case .running:
            if !context.canRun || context.demoSessionID != sessionID { return cancel(context) }
        case .waitingPremium:
            // A dismissed/blocked flow must not surprise the driver later.
            guard context.canOffer, !context.subscribed else { return cancel(context) }
            if request == nil { schedule(.premium, after: 0.5, now: now) }
        case .finished: break
        }
        return []
    }

    /// The user accepted the visible offer. Dismissal must finish before demo start.
    public mutating func accept(_ context: Context, now: TimeInterval) {
        guard case .offered = phase, context.canOffer, now.isFinite, now >= 0 else { return }
        phase = .waitingStart; schedule(.start, after: 0.4, now: now)
    }
    public mutating func skip() {
        if case .offered = phase { phase = .finished; request = nil }
    }
    /// Manual end and deadline expiry share the same ownership and watch-time rules.
    public mutating func finish(_ context: Context, now: TimeInterval) -> [Action] {
        guard case .running = phase, let id = sessionID,
              context.canRun, context.demoSessionID == id, let startedAt,
              now.isFinite, now >= startedAt else { return cancel(context) }
        request = nil; sessionID = nil; self.startedAt = nil
        phase = now - startedAt >= 12 && !context.subscribed ? .waitingPremium : .finished
        return [.stopDemo(id), .returnHome]
    }
    public mutating func fire(_ completion: Request, context: Context, now: TimeInterval) -> [Action] {
        guard request?.id == completion.id, now.isFinite, now >= completion.deadline else { return [] }
        let invalidated = reconcile(context, now: now)
        guard request?.id == completion.id else { return invalidated }
        switch completion.kind {
        case .offer:
            guard context.canOffer, !context.previouslyOffered else { return cancel(context) }
            request = nil; phase = .offered; return [.showOffer]
        case .start:
            guard context.canOffer else { return cancel(context) }
            let id = UUID(); sessionID = id; startedAt = now; phase = .running
            schedule(.finish, after: 20, now: now)
            return [.startDemo(id)]
        case .finish: return finish(context, now: now)
        case .premium:
            guard context.canOffer, !context.subscribed else { return cancel(context) }
            request = nil; phase = .finished; return [.showPremium]
        }
    }
    private mutating func schedule(_ kind: Kind, after delay: TimeInterval, now: TimeInterval) {
        request = Request(id: UUID(), deadline: now + delay, kind: kind)
    }
    private mutating func cancel(_ context: Context) -> [Action] {
        var actions: [Action] = []
        if case .offered = phase { actions.append(.dismissOffer) }
        if let sessionID, context.demoSessionID == sessionID { actions.append(.stopDemo(sessionID)) }
        let unoffered: Bool
        switch phase { case .idle, .waitingOffer: unoffered = true; default: unoffered = false }
        request = nil; sessionID = nil; startedAt = nil
        phase = unoffered ? .idle : .finished
        return actions
    }
}
