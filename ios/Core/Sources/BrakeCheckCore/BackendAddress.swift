import Foundation

/// Backend URLs carry credentials; HTTP is limited to canonical private IPv4 addresses.
public enum BackendAddress {
    public static func validatedURL(_ raw: String) -> URL? {
        guard let parts = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              parts.port.map({ (1...65535).contains($0) }) ?? true,
              let url = parts.url else { return nil }
        // Foundation reports nil for a port that overflows Int, just as it
        // does for an omitted port. Validate the original component as well.
        if let range = parts.rangeOfPort {
            guard let serialized = parts.string, let port = Int(serialized[range]),
                  (1...65535).contains(port) else { return nil }
        }
        if scheme == "http" {
            let labels = host.split(separator: ".", omittingEmptySubsequences: false)
            guard labels.count == 4 else { return nil }
            let octets = labels.compactMap { Int($0) }
            guard octets.count == 4,
                  zip(labels, octets).allSatisfy({ String($0.1) == String($0.0) && (0...255).contains($0.1) }),
                  octets[0] == 10 || (octets[0] == 192 && octets[1] == 168)
                    || (octets[0] == 172 && (16...31).contains(octets[1])) else { return nil }
        }
        return url
    }
}
