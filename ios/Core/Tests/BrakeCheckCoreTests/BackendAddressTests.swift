import XCTest
@testable import BrakeCheckCore

final class BackendAddressTests: XCTestCase {
    func testPrivateLANAndHostedHTTPSAddressesAreAccepted() {
        for address in ["http://10.0.0.1:8000", "http://192.168.1.2", "http://172.16.0.1/",
                        "http://172.31.255.254:65535", "https://backend.example.com", "https://[::1]:443/"] {
            XCTAssertNotNil(BackendAddress.validatedURL(address), address)
        }
        XCTAssertEqual(BackendAddress.validatedURL("  http://10.0.0.1:8000 \n")?.host, "10.0.0.1")
    }

    func testPublicAndAmbiguousHTTPHostsAreRejected() {
        for host in ["10.1.2.3.example.com", "10.1.2.3.foo", "10..1.2.3", "010.1.2.3",
                     "10.01.2.3", "10.1.2.3.", "10.1.2.3.4", "10.1.2", "10.1.2.256",
                     "8.8.8.8", "127.0.0.1", "0.0.0.0", "169.254.1.2", "172.15.1.2",
                     "172.32.1.2", "192.169.1.2", "computer.local", "localhost", "[::1]"] {
            XCTAssertNil(BackendAddress.validatedURL("http://\(host):8000"), host)
        }
    }

    func testCredentialsQueriesPathsAndInvalidPortsAreRejected() {
        for address in ["https://user:secret@example.com", "https://example.com?token=secret",
                        "https://example.com#fragment", "https://example.com/api", "ftp://example.com",
                        "http://10.0.0.1:0", "http://10.0.0.1:65536", "https://example.com:9999999999999999999999",
                        "http://10.0.0.1:-1", "http://10.0.0.1:abc", "", "https://"] {
            XCTAssertNil(BackendAddress.validatedURL(address), address)
        }
    }
}
