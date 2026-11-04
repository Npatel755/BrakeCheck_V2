import Foundation

/// Coalesces a manual resume's storage check and invalidates delayed answers at
/// recorder lifecycle boundaries. All access belongs to the serial recorder queue.
public struct RecordingResumeGate {
    public struct Request: Equatable { fileprivate let id: UUID }
    public private(set) var pending: Request?
    public init() {}
    public mutating func begin() -> Request? {
        guard pending == nil else { return nil }
        let request = Request(id: UUID()); pending = request; return request
    }
    public mutating func take(_ request: Request) -> Bool {
        guard pending == request else { return false }
        pending = nil; return true
    }
    public mutating func invalidate() { pending = nil }
}
