import Foundation

public struct UnindexedRecording: Identifiable {
    public let url: URL
    public let detail: String
    public var id: URL { url }
}

public struct RecordingLibrarySnapshot {
    public var ring: [VideoSegment] = []
    public var incidents: [IncidentManifest] = []
    public var unindexed: [UnindexedRecording] = []
    public var warnings: [String] = []
    public var protectedIncidentIDs = Set<UUID>()
    /// A damaged incident might still need any ring clip; defer ring cleanup.
    public var preserveRing = false
}

/// Reads recovery state without rewriting metadata or deleting footage.
public enum RecordingLibraryRecovery {
    public static func isRegularFile(_ url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey]) else { return false }
        return values.isRegularFile == true && values.isSymbolicLink != true
    }

    public static func load(ringDirectory: URL, incidentsDirectory: URL) throws -> RecordingLibrarySnapshot {
        let files = FileManager.default
        let decoder = RecordingMetadataCodec.decoder()
        var result = RecordingLibrarySnapshot()
        let ringFiles = try files.contentsOfDirectory(at: ringDirectory, includingPropertiesForKeys: nil)
        var knownRing = Set<String>()
        for metadata in ringFiles where metadata.pathExtension.lowercased() == "json" {
            do {
                guard isRegularFile(metadata) else { throw RecoveryError.invalidMetadata }
                let segment = try decoder.decode(VideoSegment.self, from: Data(contentsOf: metadata))
                guard segment.valid, metadata.lastPathComponent == segment.filename + ".json",
                      isRegularFile(ringDirectory.appendingPathComponent(segment.filename)) else {
                    throw RecoveryError.invalidMetadata
                }
                result.ring.append(segment)
                knownRing.insert(segment.filename)
            } catch {
                result.warnings.append("Rolling-video metadata could not be recovered: \(metadata.lastPathComponent). Existing files were kept.")
            }
        }
        for movie in ringFiles where movie.pathExtension.lowercased() == "mp4" && !knownRing.contains(movie.lastPathComponent) {
            if isRegularFile(movie) {
                result.unindexed.append(UnindexedRecording(url: movie,
                    detail: "Rolling clip without usable timing metadata. Playback may be incomplete."))
            }
        }
        for directory in try files.contentsOfDirectory(at: incidentsDirectory, includingPropertiesForKeys: nil) {
            let values = try directory.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            guard values.isDirectory == true, values.isSymbolicLink != true else {
                result.warnings.append("An unexpected item in the incident library was kept: \(directory.lastPathComponent).")
                result.preserveRing = true
                continue
            }
            // A directory listing failure prevents safe recovery, so the caller
            // must not mark the library prepared or start cleanup.
            let entries = try files.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            let manifestURL = directory.appendingPathComponent("manifest.json")
            var event: IncidentManifest?
            do {
                guard isRegularFile(manifestURL) else { throw RecoveryError.invalidMetadata }
                let decoded = try decoder.decode(IncidentManifest.self, from: Data(contentsOf: manifestURL))
                guard decoded.valid, directory.lastPathComponent == decoded.id.uuidString else {
                    throw RecoveryError.invalidMetadata
                }
                event = decoded
            } catch {
                result.warnings.append("An incident has missing or damaged metadata: \(directory.lastPathComponent). Its files were kept.")
                result.preserveRing = true
            }
            let indexed = Set(event?.segments.map(\.filename) ?? [])
            for movie in entries where movie.pathExtension.lowercased() == "mp4" && !indexed.contains(movie.lastPathComponent) {
                guard isRegularFile(movie) else { continue }
                result.unindexed.append(UnindexedRecording(url: movie,
                    detail: "Incident clip without usable timing metadata. Export it before repairing the library."))
                if let event { result.protectedIncidentIDs.insert(event.id) }
            }
            if let event {
                if event.segments.contains(where: { !isRegularFile(directory.appendingPathComponent($0.filename)) }) {
                    result.protectedIncidentIDs.insert(event.id)
                    result.warnings.append("An incident references missing or unavailable clips: \(directory.lastPathComponent). Metadata was kept for recovery.")
                }
                result.incidents.append(event)
            }
        }
        result.ring.sort { $0.start < $1.start }
        result.incidents.sort { $0.triggerTime > $1.triggerTime }
        result.unindexed.sort { $0.url.path < $1.url.path }
        return result
    }

    private enum RecoveryError: Error { case invalidMetadata }
}
