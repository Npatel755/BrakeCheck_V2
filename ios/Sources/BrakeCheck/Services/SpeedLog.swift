import Foundation

struct SpeedLogPoint {
    let time: Date
    let metersPerSecond: Double
}

/// Ego speed over time for replay mode. Points are sorted by time.
struct SpeedTrack {
    let name: String
    let points: [SpeedLogPoint]

    var start: Date? { points.first?.time }
    var end: Date? { points.last?.time }

    /// Linear interpolation; nil outside the log (2 s tolerance) or across gaps longer than 5 s.
    func speed(at date: Date) -> Double? {
        guard let first = points.first, let last = points.last else { return nil }
        if date <= first.time { return first.time.timeIntervalSince(date) <= 2 ? first.metersPerSecond : nil }
        if date >= last.time { return date.timeIntervalSince(last.time) <= 2 ? last.metersPerSecond : nil }
        var low = 0, high = points.count - 1
        while high - low > 1 {
            let mid = (low + high) / 2
            if points[mid].time <= date { low = mid } else { high = mid }
        }
        let a = points[low], b = points[high]
        let span = b.time.timeIntervalSince(a.time)
        guard span > 0, span <= 5 else { return nil }
        let f = date.timeIntervalSince(a.time) / span
        return a.metersPerSecond + f * (b.metersPerSecond - a.metersPerSecond)
    }

    func covers(_ date: Date) -> Bool {
        guard let start, let end else { return false }
        return date >= start.addingTimeInterval(-2) && date <= end.addingTimeInterval(2)
    }
}

/// Writes one CSV per trip with the filtered GPS speed, so a recorded drive can be replayed indoors.
/// Use on the main thread.
final class SpeedLogWriter {
    private var handle: FileHandle?
    private var lastWrite = Date.distantPast
    private(set) var url: URL?

    static var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SpeedLogs", isDirectory: true)
    }

    func start(at date: Date = Date()) {
        stop()
        let files = FileManager.default
        try? files.createDirectory(at: Self.directory, withIntermediateDirectories: true)
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let fileURL = Self.directory.appendingPathComponent("trip_\(formatter.string(from: date)).csv")
        guard files.createFile(atPath: fileURL.path, contents: Data("unix_time,speed_mps,accuracy_mps\n".utf8)),
              let newHandle = try? FileHandle(forWritingTo: fileURL) else { return }
        _ = try? newHandle.seekToEnd()
        handle = newHandle
        url = fileURL
    }

    func append(speed: Double, accuracy: Double, at date: Date) {
        guard let handle, speed.isFinite, date.timeIntervalSince(lastWrite) >= 0.2 else { return }
        lastWrite = date
        let line = String(format: "%.3f,%.3f,%.3f\n", date.timeIntervalSince1970, speed, accuracy)
        try? handle.write(contentsOf: Data(line.utf8))
    }

    func stop() {
        try? handle?.close()
        handle = nil
        url = nil
        lastWrite = .distantPast
    }
}

enum SpeedLogReader {
    enum ReadError: LocalizedError {
        case empty, unsupported(String)
        var errorDescription: String? {
            switch self {
            case .empty: return "No usable speed points were found in this file."
            case .unsupported(let detail): return "Unsupported speed log: \(detail)"
            }
        }
    }

    /// The app's own trip logs, newest first.
    static func savedLogs() -> [URL] {
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: SpeedLogWriter.directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        return urls.filter { $0.pathExtension.lowercased() == "csv" }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    static func load(_ url: URL) throws -> SpeedTrack {
        let data = try Data(contentsOf: url)
        let points: [SpeedLogPoint]
        switch url.pathExtension.lowercased() {
        case "gpx": points = try GPXSpeedParser.parse(data)
        default:
            guard let text = String(data: data, encoding: .utf8) ?? String(data: data, encoding: .isoLatin1) else {
                throw ReadError.unsupported("not text")
            }
            points = try parseCSV(text)
        }
        let clean = points.filter { $0.metersPerSecond.isFinite && (0...90).contains($0.metersPerSecond) }
            .sorted { $0.time < $1.time }
        guard clean.count >= 2 else { throw ReadError.empty }
        return SpeedTrack(name: url.lastPathComponent, points: clean)
    }

    /// Accepts the app's own CSV and most GPS-logger CSVs: a time column (unix seconds/ms or ISO 8601)
    /// plus either a speed column (m/s, km/h or mph by header name) or latitude/longitude columns.
    static func parseCSV(_ text: String) throws -> [SpeedLogPoint] {
        let lines = text.split(whereSeparator: \.isNewline).map(String.init).filter { !$0.isEmpty }
        guard let headerLine = lines.first else { throw ReadError.empty }
        let header = splitCSV(headerLine).map { $0.lowercased() }
        func column(_ test: (String) -> Bool) -> Int? { header.firstIndex(where: test) }
        guard let timeColumn = column({ $0.contains("time") || $0 == "date" || $0.contains("timestamp") }) else {
            throw ReadError.unsupported("no time column")
        }
        let speedColumn = column { $0.contains("speed") }
        let latColumn = column { $0 == "lat" || $0.hasPrefix("latitude") }
        let lonColumn = column { $0 == "lon" || $0 == "lng" || $0.hasPrefix("longitude") }
        var scale = 1.0
        if let speedColumn {
            let name = header[speedColumn]
            if name.contains("km") { scale = 1 / 3.6 } else if name.contains("mph") { scale = 0.44704 }
        } else if latColumn == nil || lonColumn == nil {
            throw ReadError.unsupported("no speed or latitude/longitude columns")
        }
        var points: [SpeedLogPoint] = []
        var fixes: [(Date, Double, Double)] = []
        for line in lines.dropFirst() {
            let cells = splitCSV(line)
            guard timeColumn < cells.count, let time = parseTime(cells[timeColumn]) else { continue }
            if let speedColumn, speedColumn < cells.count, let value = Double(cells[speedColumn]) {
                points.append(SpeedLogPoint(time: time, metersPerSecond: value * scale))
            } else if let latColumn, let lonColumn, latColumn < cells.count, lonColumn < cells.count,
                      let lat = Double(cells[latColumn]), let lon = Double(cells[lonColumn]) {
                fixes.append((time, lat, lon))
            }
        }
        if points.isEmpty { points = speeds(fromFixes: fixes) }
        return points
    }

    static func speeds(fromFixes fixes: [(Date, Double, Double)]) -> [SpeedLogPoint] {
        let sorted = fixes.sorted { $0.0 < $1.0 }
        guard sorted.count >= 2 else { return [] }
        var points: [SpeedLogPoint] = []
        for i in 1..<sorted.count {
            let a = sorted[i - 1], b = sorted[i]
            let dt = b.0.timeIntervalSince(a.0)
            guard dt > 0.2 else { continue }
            let meters = haversine(a.1, a.2, b.1, b.2)
            points.append(SpeedLogPoint(time: a.0.addingTimeInterval(dt / 2), metersPerSecond: meters / dt))
        }
        return points
    }

    static func haversine(_ lat1: Double, _ lon1: Double, _ lat2: Double, _ lon2: Double) -> Double {
        let r = 6_371_000.0
        let dLat = (lat2 - lat1) * .pi / 180, dLon = (lon2 - lon1) * .pi / 180
        let h = sin(dLat / 2) * sin(dLat / 2)
            + cos(lat1 * .pi / 180) * cos(lat2 * .pi / 180) * sin(dLon / 2) * sin(dLon / 2)
        return 2 * r * asin(min(1, sqrt(h)))
    }

    static func parseTime(_ raw: String) -> Date? {
        let value = raw.trimmingCharacters(in: CharacterSet(charactersIn: "\" "))
        if let number = Double(value) {
            // Unix seconds or milliseconds.
            return Date(timeIntervalSince1970: number > 1e12 ? number / 1000 : number)
        }
        let iso = ISO8601DateFormatter()
        iso.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = iso.date(from: value) { return date }
        iso.formatOptions = [.withInternetDateTime]
        if let date = iso.date(from: value) { return date }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        for format in ["yyyy-MM-dd HH:mm:ss.SSS", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ss"] {
            formatter.dateFormat = format
            if let date = formatter.date(from: value) { return date }
        }
        return nil
    }

    private static func splitCSV(_ line: String) -> [String] {
        let separator: Character = line.contains(",") ? "," : ";"
        return line.split(separator: separator, omittingEmptySubsequences: false)
            .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: "\" \t\r")) }
    }
}

/// Reads <trkpt> times plus <speed> (GPX 1.0 or Garmin extensions). Without speed, derives it from positions.
private final class GPXSpeedParser: NSObject, XMLParserDelegate {
    private var fixes: [(Date, Double, Double)] = []
    private var speeds: [SpeedLogPoint] = []
    private var lat: Double?
    private var lon: Double?
    private var time: Date?
    private var speed: Double?
    private var text = ""
    private var inPoint = false

    static func parse(_ data: Data) throws -> [SpeedLogPoint] {
        let delegate = GPXSpeedParser()
        let parser = XMLParser(data: data)
        parser.delegate = delegate
        guard parser.parse() else { throw SpeedLogReader.ReadError.unsupported(parser.parserError?.localizedDescription ?? "invalid GPX") }
        // Only trust per-point speed if most points carry it.
        if delegate.speeds.count * 2 >= max(1, delegate.fixes.count) { return delegate.speeds }
        return SpeedLogReader.speeds(fromFixes: delegate.fixes)
    }

    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?, attributes: [String: String] = [:]) {
        text = ""
        if elementName == "trkpt" {
            inPoint = true
            lat = attributes["lat"].flatMap(Double.init)
            lon = attributes["lon"].flatMap(Double.init)
            time = nil
            speed = nil
        }
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) { text += string }

    func parser(_ parser: XMLParser, didEndElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?) {
        let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard inPoint else { return }
        if elementName == "time" {
            time = SpeedLogReader.parseTime(value)
        } else if elementName.lowercased().hasSuffix("speed") {
            speed = Double(value)
        } else if elementName == "trkpt" {
            inPoint = false
            if let time, let lat, let lon {
                fixes.append((time, lat, lon))
                if let speed { speeds.append(SpeedLogPoint(time: time, metersPerSecond: speed)) }
            }
        }
    }
}
