import Foundation

/// Each import owns a separate directory so a failed replacement cannot erase a selected clip.
public enum ReplayImportStore {
    public enum ImportError: LocalizedError {
        case notAFile
        public var errorDescription: String? { "Choose a video or speed-log file." }
    }

    public static func copy(_ source: URL, to directory: URL) throws -> URL {
        guard source.isFileURL, directory.isFileURL,
              try source.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true else {
            throw ImportError.notAFile
        }
        let files = FileManager.default
        let folder = directory.appendingPathComponent(UUID().uuidString, isDirectory: true)
        let destination = folder.appendingPathComponent(source.lastPathComponent)
        try files.createDirectory(at: folder, withIntermediateDirectories: true)
        do {
            try files.copyItem(at: source, to: destination)
            return destination
        } catch {
            try? files.removeItem(at: folder)
            throw error
        }
    }
}
