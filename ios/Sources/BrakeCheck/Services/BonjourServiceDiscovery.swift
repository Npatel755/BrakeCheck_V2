import Foundation

private enum BonjourDiscoveryError: LocalizedError {
    case unavailable

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "BrakeCheck could not find the backend. Put both devices on the same Wi-Fi and allow Local Network access."
        }
    }
}

/// Resolves the backend's DNS-SD service into a URLSession-compatible local URL.
final class BonjourServiceDiscovery: NSObject, NetServiceBrowserDelegate, NetServiceDelegate {
    private let browser = NetServiceBrowser()
    private var continuation: CheckedContinuation<URL, Error>?
    private var candidates: [NetService] = []
    private var resolving = false
    private var timeoutWorkItem: DispatchWorkItem?

    func discoverBackend(timeout: TimeInterval = 8) async throws -> URL {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<URL, Error>) in
            self.continuation = continuation
            browser.delegate = self
            browser.searchForServices(ofType: "_brakecheck._tcp.", inDomain: "local.")
            let timeoutItem = DispatchWorkItem { [weak self] in
                self?.finish(.failure(BonjourDiscoveryError.unavailable))
            }
            timeoutWorkItem = timeoutItem
            DispatchQueue.main.asyncAfter(deadline: .now() + timeout, execute: timeoutItem)
        }
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        guard !candidates.contains(where: { $0.name == service.name && $0.domain == service.domain }) else { return }
        candidates.append(service)
        resolveNextCandidate()
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
        if candidates.isEmpty && !resolving {
            finish(.failure(BonjourDiscoveryError.unavailable))
        }
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        guard let hostName = sender.hostName, sender.port > 0 else {
            resolving = false
            resolveNextCandidate()
            return
        }

        var host = hostName
        while host.hasSuffix(".") { host.removeLast() }
        if !host.contains(".") { host += ".local" }
        var components = URLComponents()
        components.scheme = "http"
        components.host = host
        components.port = sender.port
        guard let url = components.url else {
            resolving = false
            resolveNextCandidate()
            return
        }
        finish(.success(url))
    }

    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        resolving = false
        resolveNextCandidate()
    }

    private func resolveNextCandidate() {
        guard continuation != nil, !resolving, !candidates.isEmpty else { return }
        let service = candidates.removeFirst()
        service.delegate = self
        resolving = true
        service.resolve(withTimeout: 4)
    }

    private func finish(_ result: Result<URL, Error>) {
        guard let continuation else { return }
        self.continuation = nil
        timeoutWorkItem?.cancel()
        timeoutWorkItem = nil
        browser.stop()
        continuation.resume(with: result)
    }
}
