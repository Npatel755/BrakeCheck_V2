import Foundation

public extension TripRoute {
    /// GPX 1.1 export: one track segment per continuous GPS run (gaps stay gaps, as on the
    /// map) and one waypoint per flagged moment that has a location. Nil when there is no
    /// valid trail to export.
    func gpxDocument(name: String, creator: String = "BrakeSense") -> String? {
        let segments = continuousSegments.filter { $0.count >= 2 }
        guard !segments.isEmpty else { return nil }
        let stamp = ISO8601DateFormatter()
        var xml = """
        <?xml version="1.0" encoding="UTF-8"?>
        <gpx version="1.1" creator="\(Self.escape(creator))" xmlns="http://www.topografix.com/GPX/1/1">
        <metadata><name>\(Self.escape(name))</name></metadata>

        """
        for event in events {
            guard let lat = event.latitude, let lon = event.longitude else { continue }
            var desc = event.title
            if let speed = event.speedMPH { desc += String(format: " · %.0f mph", speed) }
            if let closing = event.closingSeconds { desc += String(format: " · %.1f s to impact", closing) }
            xml += "<wpt lat=\"\(Self.coordinate(lat))\" lon=\"\(Self.coordinate(lon))\"><time>\(stamp.string(from: event.time))</time>"
            xml += "<name>\(Self.escape(event.kind.title))</name><desc>\(Self.escape(desc))</desc></wpt>\n"
        }
        xml += "<trk><name>\(Self.escape(name))</name>\n"
        for segment in segments {
            xml += "<trkseg>\n"
            for point in segment {
                xml += "<trkpt lat=\"\(Self.coordinate(point.latitude))\" lon=\"\(Self.coordinate(point.longitude))\"><time>\(stamp.string(from: point.time))</time></trkpt>\n"
            }
            xml += "</trkseg>\n"
        }
        xml += "</trk>\n</gpx>\n"
        return xml
    }

    private static func coordinate(_ value: Double) -> String { String(format: "%.6f", value) }

    private static func escape(_ text: String) -> String {
        text.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;").replacingOccurrences(of: "\"", with: "&quot;")
    }
}

extension TripRoute.Event {
    var title: String { kind.title }
}
