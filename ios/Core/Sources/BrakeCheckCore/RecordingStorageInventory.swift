import Foundation

/// Measures real movie files without following links outside the library. Nil
/// means the scan was incomplete; it must not authorize a paused recorder resume.
public enum RecordingStorageInventory {
    public static func bytes(in roots: [URL]) -> Int64? {
        let files = FileManager.default
        var bytes: Int64 = 0, complete = true
        var seen: Set<String> = []
        for root in roots {
            guard let attributes = try? files.attributesOfItem(atPath: root.path),
                  attributes[.type] as? FileAttributeType == .typeDirectory,
                  let entries = files.enumerator(at: root, includingPropertiesForKeys: nil,
                    errorHandler: { _, _ in complete = false; return true }) else { return nil }
            for case let file as URL in entries where file.pathExtension.lowercased() == "mp4" {
                guard let attributes = try? files.attributesOfItem(atPath: file.path) else { complete = false; continue }
                guard attributes[.type] as? FileAttributeType == .typeRegular else { continue }
                guard let size = attributes[.size] as? NSNumber, size.int64Value >= 0 else { complete = false; continue }
                let identity: String
                if let inode = attributes[.systemFileNumber] as? NSNumber,
                   let device = attributes[.systemNumber] as? NSNumber {
                    identity = device.stringValue + ":" + inode.stringValue
                } else { identity = file.standardizedFileURL.path }
                if seen.insert(identity).inserted {
                    let sum = bytes.addingReportingOverflow(size.int64Value)
                    guard !sum.overflow else { return nil }
                    bytes = sum.partialValue
                }
            }
        }
        return complete ? bytes : nil
    }
}
