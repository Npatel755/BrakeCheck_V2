import Foundation
import Darwin
import BrakeCheckCore

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
@MainActor
final class BonjourServiceDiscovery: NSObject, @preconcurrency NetServiceBrowserDelegate, @preconcurrency NetServiceDelegate {
    private let browser = NetServiceBrowser()
    private var continuation: CheckedContinuation<URL, Error>?
    private var candidates: [NetService] = []
    private var resolving = false
    private var activeService: NetService?
    private var timeoutWorkItem: DispatchWorkItem?

    func discoverBackend(timeout: TimeInterval = 8) async throws -> URL {
        try Task.checkCancellation()
        guard continuation == nil else { throw BonjourDiscoveryError.unavailable }
        return try await withTaskCancellationHandler {
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
        } onCancel: {
            Task { @MainActor [weak self] in self?.finish(.failure(CancellationError())) }
        }
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didFind service: NetService, moreComing: Bool) {
        guard continuation != nil,
              activeService?.name != service.name || activeService?.domain != service.domain,
              !candidates.contains(where: { $0.name == service.name && $0.domain == service.domain }) else { return }
        candidates.append(service)
        resolveNextCandidate()
    }

    func netServiceBrowser(_ browser: NetServiceBrowser, didNotSearch errorDict: [String: NSNumber]) {
        if candidates.isEmpty && !resolving {
            finish(.failure(BonjourDiscoveryError.unavailable))
        }
    }

    func netServiceDidResolveAddress(_ sender: NetService) {
        guard continuation != nil, sender === activeService else { return }
        // Resolve to the advertised private IPv4 address. A .local hostname
        // cannot pass the same HTTP policy used for manual/QR connections.
        for address in sender.addresses ?? [] {
            let host: String? = address.withUnsafeBytes { bytes in
                guard bytes.count >= MemoryLayout<sockaddr_in>.size,
                      let base = bytes.baseAddress else { return nil }
                let socketAddress = base.assumingMemoryBound(to: sockaddr.self)
                guard socketAddress.pointee.sa_family == sa_family_t(AF_INET) else { return nil }
                var buffer = [CChar](repeating: 0, count: Int(NI_MAXHOST))
                guard getnameinfo(socketAddress, socklen_t(bytes.count), &buffer,
                                  socklen_t(buffer.count), nil, 0, NI_NUMERICHOST) == 0 else { return nil }
                return String(cString: buffer)
            }
            if let host, let url = BackendAddress.validatedURL("http://\(host):\(sender.port)") {
                finish(.success(url))
                return
            }
        }
        resolving = false
        activeService = nil
        resolveNextCandidate()
    }

    func netService(_ sender: NetService, didNotResolve errorDict: [String: NSNumber]) {
        guard sender === activeService else { return }
        resolving = false
        activeService = nil
        resolveNextCandidate()
    }

    private func resolveNextCandidate() {
        guard continuation != nil, !resolving, !candidates.isEmpty else { return }
        let service = candidates.removeFirst()
        activeService = service
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
        browser.delegate = nil
        activeService?.delegate = nil
        activeService?.stop()
        activeService = nil
        candidates.forEach { $0.delegate = nil; $0.stop() }
        candidates.removeAll()
        resolving = false
        continuation.resume(with: result)
    }
}
