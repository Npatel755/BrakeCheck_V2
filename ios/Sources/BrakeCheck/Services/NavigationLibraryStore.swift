import BrakeCheckCore
import Foundation

/// Serializes archive work away from the main actor. Callers queue mutations so
/// an older save cannot overwrite a newer save or a route deletion.
actor NavigationLibraryStore {
    nonisolated static let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Navigation", isDirectory: true)

    func load() -> (routes: [SavedNavigationRoute], graphURL: URL?) {
        let routesURL = Self.directory.appendingPathComponent("routes.json")
        let routes = (try? Data(contentsOf: routesURL, options: .mappedIfSafe))
            .flatMap { try? NavigationRouteArchive.decode($0) } ?? []
        let graphURL = Self.directory.appendingPathComponent("roads.json")
        return (routes, FileManager.default.fileExists(atPath: graphURL.path) ? graphURL : nil)
    }
    func save(_ proposed: [SavedNavigationRoute], trimToFit: Bool) throws -> [SavedNavigationRoute] {
        var routes = proposed
        var data = try? NavigationRouteArchive.encode(routes)
        while data == nil, trimToFit, routes.count > 1 {
            routes.removeLast(); data = try? NavigationRouteArchive.encode(routes)
        }
        guard let data else { throw NavigationArchiveError.tooLarge }
        try FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        try data.write(to: Self.directory.appendingPathComponent("routes.json"), options: .atomic)
        return routes
    }
    func saveGraph(_ data: Data) throws {
        try FileManager.default.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        try data.write(to: Self.directory.appendingPathComponent("roads.json"), options: .atomic)
    }
}
