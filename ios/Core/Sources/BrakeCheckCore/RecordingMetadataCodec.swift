import Foundation

/// Preserve millisecond timing for speed-log/replay alignment while accepting
/// existing whole-second incident and segment metadata.
public enum RecordingMetadataCodec {
    public static func encoder() -> JSONEncoder {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .custom { date, encoder in
            var container = encoder.singleValueContainer()
            try container.encode(formatter.string(from: date))
        }
        return encoder
    }

    public static func decoder() -> JSONDecoder {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let legacy = ISO8601DateFormatter()
        legacy.formatOptions = [.withInternetDateTime]
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            guard let date = fractional.date(from: text) ?? legacy.date(from: text) else {
                throw DecodingError.dataCorruptedError(in: container, debugDescription: "Invalid recording timestamp")
            }
            return date
        }
        return decoder
    }
}
