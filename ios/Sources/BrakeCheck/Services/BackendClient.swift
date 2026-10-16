import Foundation
import Combine
import Security

// Scan the complete JSON string from the QR code and pass it to pair(qrString:deviceName:).
// QR format: {"app":"BrakeCheck","version":1,"backend_url":"http://10.x.x.x:8000","code":"..."}
struct PairingQRPayload: Decodable {
    let app: String
    let version: Int
    let backend_url: String
    let code: String
}

private struct PairingExchangeResponse: Decodable {
    let device_token: String
    let device_id: String
    let device_name: String
}

enum APIError: LocalizedError {
    case message(String)
    var errorDescription: String? {
        if case let .message(value) = self { return value }
        return nil
    }
}

@MainActor
final class ConnectionSettings: ObservableObject {
    @Published var address: String {
        didSet { UserDefaults.standard.set(address, forKey: "backend.address") }
    }
    @Published var appKey: String
    @Published private(set) var deviceToken: String
    @Published var language: String {
        didSet { UserDefaults.standard.set(language, forKey: "voice.language") }
    }
    @Published var voiceEnabled: Bool {
        didSet { UserDefaults.standard.set(voiceEnabled, forKey: "voice.enabled") }
    }
    @Published var autoCheckin: Bool {
        didSet { UserDefaults.standard.set(autoCheckin, forKey: "crash.auto") }
    }

    init() {
        address = UserDefaults.standard.string(forKey: "backend.address") ?? ""
        appKey = Self.readCredential(account: "app-key")
        deviceToken = Self.readCredential(account: "device-token")
        language = UserDefaults.standard.string(forKey: "voice.language") ?? "en"
        voiceEnabled = UserDefaults.standard.object(forKey: "voice.enabled") as? Bool ?? true
        autoCheckin = UserDefaults.standard.bool(forKey: "crash.auto")
    }

    var isPaired: Bool { !deviceToken.isEmpty }
    var credentialForSignature: String { isPaired ? deviceToken : appKey }

    private static func validatedURL(_ raw: String) -> URL? {
        guard let parts = URLComponents(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty,
              parts.user == nil, parts.password == nil,
              parts.query == nil, parts.fragment == nil,
              parts.path.isEmpty || parts.path == "/",
              let url = parts.url else { return nil }

        if scheme == "http" {
            // Unencrypted HTTP is allowed only for LAN testing, not cloud hosts.
            let octets = host.split(separator: ".").compactMap { Int($0) }
            let isPrivate = octets.count == 4 &&
                octets.allSatisfy { (0...255).contains($0) } &&
                (octets[0] == 10 ||
                 (octets[0] == 192 && octets[1] == 168) ||
                 (octets[0] == 172 && (16...31).contains(octets[1])))
            guard isPrivate else { return nil }
        }
        return url
    }

    var client: BackendClient? {
        let credential = credentialForSignature
        guard let url = Self.validatedURL(address), credential.count >= 24 else { return nil }
        return BackendClient(
            baseURL: url,
            credential: credential,
            credentialHeader: isPaired ? "X-BrakeCheck-Device" : "X-BrakeCheck-Key"
        )
    }

    func saveKey() throws {
        guard appKey.count >= 24 else {
            throw APIError.message("Enter a valid backend key or pair using a QR code.")
        }
        try Self.storeCredential(appKey, account: "app-key")
    }

    // Preferred entry point: pass the exact QR string the scanner receives.
    func pair(qrString: String, deviceName: String) async throws {
        guard let data = qrString.data(using: .utf8) else {
            throw APIError.message("QR code is not valid UTF-8.")
        }
        let payload: PairingQRPayload
        do {
            payload = try JSONDecoder().decode(PairingQRPayload.self, from: data)
        } catch {
            throw APIError.message("QR code is missing its backend_url or pairing code. Generate a new QR with the updated pair.py.")
        }
        guard payload.app == "BrakeCheck", payload.version == 1 else {
            throw APIError.message("This is not a supported BrakeCheck pairing QR code.")
        }
        try await pair(code: payload.code, deviceName: deviceName, backendURL: payload.backend_url)
    }

    // Older callers cannot provide the backend URL. Use an already-saved URL if one exists.
    // For first-time QR pairing, change the scanner to call pair(qrString:deviceName:).
    func pair(code: String, deviceName: String) async throws {
        guard Self.validatedURL(address) != nil else {
            throw APIError.message("Scanner must pass the full QR text to pair(qrString:deviceName:), including backend_url.")
        }
        try await pair(code: code, deviceName: deviceName, backendURL: address)
    }

    func pair(code: String, deviceName: String, backendURL: String) async throws {
        guard let baseURL = Self.validatedURL(backendURL) else {
            throw APIError.message("Invalid backend address. For local HTTP use your PC's private Wi-Fi IPv4, not 0.0.0.0 or 127.0.0.1.")
        }
        guard (32...128).contains(code.count), !deviceName.isEmpty else {
            throw APIError.message("Invalid pairing code or device name. Generate a new QR code.")
        }
        let endpoint = baseURL.appendingPathComponent("pairing/exchange")
        var request = URLRequest(url: endpoint, timeoutInterval: 12)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "code": code, "device_name": String(deviceName.prefix(80))
        ])
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw APIError.message("Cannot connect to \(baseURL.absoluteString): \(error.localizedDescription)")
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.message("Invalid response from backend.")
        }
        guard (200...299).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.message(detail ?? "Pairing failed (HTTP \(http.statusCode)).")
        }
        let paired = try JSONDecoder().decode(PairingExchangeResponse.self, from: data)
        guard paired.device_token.count >= 24 else {
            throw APIError.message("Server returned an invalid device token.")
        }
        try Self.storeCredential(paired.device_token, account: "device-token")
        // A paired phone no longer needs to retain the admin app key.
        try? Self.deleteCredential(account: "app-key")
        appKey = ""
        deviceToken = paired.device_token
        address = baseURL.absoluteString
    }

    private static func keychainQuery(account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: "com.brakecheck.connection",
         kSecAttrAccount as String: account]
    }

    private static func storeCredential(_ value: String, account: String) throws {
        let query = keychainQuery(account: account)
        let bytes = Data(value.utf8)
        let status = SecItemUpdate(query as CFDictionary,
                                   [kSecValueData as String: bytes] as CFDictionary)
        if status == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = bytes
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else {
                throw APIError.message("Could not save pairing in Keychain.")
            }
        } else if status != errSecSuccess {
            throw APIError.message("Could not update pairing in Keychain.")
        }
    }

    private static func deleteCredential(account: String) throws {
        let status = SecItemDelete(keychainQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw APIError.message("Could not delete old Keychain credential.")
        }
    }

    private static func readCredential(account: String) -> String {
        var query = keychainQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
}

struct BackendClient {
    let baseURL: URL
    let credential: String
    let credentialHeader: String

    func data(_ path: String, method: String = "GET", json: [String: Any]? = nil,
              body: Data? = nil, contentType: String? = nil,
              timeout: TimeInterval = 18) async throws -> Data {
        let normalizedPath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let url = baseURL.appendingPathComponent(normalizedPath)
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue(credential, forHTTPHeaderField: credentialHeader)
        if let json {
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        } else if let body {
            request.httpBody = body
            if let contentType { request.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        }
        let (responseData, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let detail = (try? JSONSerialization.jsonObject(with: responseData) as? [String: Any])?["detail"] as? String
            throw APIError.message(detail ?? "Backend request failed. Check the address and credential.")
        }
        return responseData
    }

    func request<T: Decodable>(_ path: String, method: String = "GET",
                               json: [String: Any]? = nil,
                               timeout: TimeInterval = 18) async throws -> T {
        let responseData = try await data(path, method: method, json: json, timeout: timeout)
        return try JSONDecoder().decode(T.self, from: responseData)
    }

    func reply(crashID: String, audio: Data) async throws -> CrashResponse {
        let boundary = "BrakeCheck-\(UUID().uuidString)"
        var body = Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"reply.m4a\"\r\nContent-Type: audio/mp4\r\n\r\n".utf8)
        body.append(audio)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        let response = try await data("crash/\(crashID)/reply", method: "POST", body: body,
                                      contentType: "multipart/form-data; boundary=\(boundary)")
        return try JSONDecoder().decode(CrashResponse.self, from: response)
    }
}

struct BackendHealth: Decodable {
    let ok: Bool
    let voice_configured: Bool
    let photon_configured: Bool
    let confirmed_contacts: Int
}
struct CrashStartResponse: Decodable { let crash_id: String }
struct CrashResponse: Decodable {
    let id: String
    let status: String
    let dry_run: Bool
    let alerted: Bool
    let delivery_pending: Bool?
    let failed_count: Int?
    let deadline: Double?
    let sent: Int?
    let alert_error: String?
    let transcript: String?
    let ack_audio_url: String?
}
struct EmergencyContact: Decodable, Identifiable {
    var id: String { phone }
    let phone: String
    let first_name: String
    let confirmed: Bool
    let removal_pending: Bool?
    var removalPending: Bool { removal_pending == true }
    let invite_text: String
    let sms_url: String
}
struct ContactTestResponse: Decodable { let accepted: Bool }
struct CrashLocation {
    let latitude: Double
    let longitude: Double
    let measuredAt: Date
}
