import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

public struct SpeedLogPoint: Sendable {
    public let time: Date
    public let metersPerSecond: Double
    public let startsNewSegment: Bool

    public init(time: Date, metersPerSecond: Double, startsNewSegment: Bool = false) {
        self.time = time
        self.metersPerSecond = metersPerSecond
        self.startsNewSegment = startsNewSegment
    }

    var valid: Bool {
        (-62_135_596_800...253_402_300_799).contains(time.timeIntervalSince1970)
            && metersPerSecond.isFinite && (0...90).contains(metersPerSecond)
    }
}

/// Ego speed over time for replay mode. Points are sorted by time.
public struct SpeedTrack: Sendable {
    public let name: String
    public let points: [SpeedLogPoint]

    public init(name: String, points: [SpeedLogPoint]) {
        self.name = name
        // Repeated timestamps are common in exported logs. Last valid row wins.
        var unique: [Date: SpeedLogPoint] = [:]
        for point in points where point.valid {
            unique[point.time] = SpeedLogPoint(time: point.time, metersPerSecond: point.metersPerSecond,
                startsNewSegment: point.startsNewSegment || unique[point.time]?.startsNewSegment == true)
        }
        self.points = unique.values.sorted { $0.time < $1.time }
    }

    public var start: Date? { points.first?.time }
    public var end: Date? { points.last?.time }

    /// Linear interpolation; nil outside the log (2 s tolerance) or across gaps longer than 5 s.
    public func speed(at date: Date) -> Double? {
        guard date.timeIntervalSince1970.isFinite,
              let first = points.first, let last = points.last else { return nil }
        if date <= first.time { return first.time.timeIntervalSince(date) <= 2 ? first.metersPerSecond : nil }
        if date >= last.time { return date.timeIntervalSince(last.time) <= 2 ? last.metersPerSecond : nil }
        var low = 0, high = points.count - 1
        while high - low > 1 {
            let mid = (low + high) / 2
            if points[mid].time <= date { low = mid } else { high = mid }
        }
        let a = points[low], b = points[high]
        if date == a.time { return a.metersPerSecond }
        if date == b.time { return b.metersPerSecond }
        let span = b.time.timeIntervalSince(a.time)
        guard span > 0, span <= 5, !b.startsNewSegment else { return nil }
        let f = date.timeIntervalSince(a.time) / span
        return a.metersPerSecond + f * (b.metersPerSecond - a.metersPerSecond)
    }

    public func covers(_ date: Date) -> Bool {
        speed(at: date) != nil
    }
}

/// Writes one CSV per trip with the filtered GPS speed, so a recorded drive can be replayed indoors.
/// Use on the main thread.
public final class SpeedLogWriter {
    private var handle: FileHandle?
    private var lastWrite = Date.distantPast
    public private(set) var url: URL?
    private let outputDirectory: URL

    public init(directory: URL = SpeedLogWriter.directory) {
        outputDirectory = directory
    }

    public static var directory: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("SpeedLogs", isDirectory: true)
    }

    public func start(at date: Date = Date()) {
        stop()
        let files = FileManager.default
        guard SpeedLogPoint(time: date, metersPerSecond: 0).valid else { return }
        do { try files.createDirectory(at: outputDirectory, withIntermediateDirectories: true) }
        catch { return }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let fileURL = outputDirectory.appendingPathComponent("trip_\(formatter.string(from: date))_\(UUID().uuidString).csv")
        guard files.createFile(atPath: fileURL.path, contents: Data("unix_time,speed_mps,accuracy_mps\n".utf8)),
              let newHandle = try? FileHandle(forWritingTo: fileURL) else { return }
        _ = try? newHandle.seekToEnd()
        handle = newHandle
        url = fileURL
    }

    public func append(speed: Double, accuracy: Double, at date: Date) {
        guard let handle, SpeedLogPoint(time: date, metersPerSecond: speed).valid,
              accuracy.isFinite, accuracy >= 0, date.timeIntervalSince(lastWrite) >= 0.2 else { return }
        let line = String(format: "%.3f,%.3f,%.3f\n", locale: Locale(identifier: "en_US_POSIX"),
                          date.timeIntervalSince1970, speed, accuracy)
        do {
            try handle.write(contentsOf: Data(line.utf8))
            lastWrite = date
        } catch { stop() }
    }

    public func stop() {
        try? handle?.close()
        handle = nil
        url = nil
        lastWrite = .distantPast
    }
}

public enum SpeedLogReader {
    public enum ReadError: LocalizedError {
        case empty, unsupported(String)
        public var errorDescription: String? {
            switch self {
            case .empty: return "No usable speed points were found in this file."
            case .unsupported(let detail): return "Unsupported speed log: \(detail)"
            }
        }
    }

    /// The app's own trip logs, newest first.
    public static func savedLogs() -> [URL] {
        let urls = (try? FileManager.default.contentsOfDirectory(
            at: SpeedLogWriter.directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        return urls.filter { $0.pathExtension.lowercased() == "csv" }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
    }

    public static func load(_ url: URL) throws -> SpeedTrack {
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
        let track = SpeedTrack(name: url.lastPathComponent, points: points)
        guard track.points.count >= 2 else { throw ReadError.empty }
        return track
    }

    /// Accepts the app's own CSV and most GPS-logger CSVs: a time column (unix seconds/ms or ISO 8601)
    /// plus either a speed column (m/s, km/h or mph by header name) or latitude/longitude columns.
    static func parseCSV(_ text: String) throws -> [SpeedLogPoint] {
        let lines = text.trimmingCharacters(in: CharacterSet(charactersIn: "\u{FEFF}"))
            .split(whereSeparator: \.isNewline).map(String.init).filter { !$0.isEmpty }
        guard let headerLine = lines.first else { throw ReadError.empty }
        guard let commaHeader = splitCSV(headerLine, separator: ","),
              let semicolonHeader = splitCSV(headerLine, separator: ";") else {
            throw ReadError.unsupported("invalid CSV header")
        }
        let separator: Character = semicolonHeader.count > commaHeader.count ? ";" : ","
        let header = (separator == ";" ? semicolonHeader : commaHeader).map { $0.lowercased() }
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
        var discontinuity = true
        for line in lines.dropFirst() {
            guard let cells = splitCSV(line, separator: separator),
                  timeColumn < cells.count, let time = parseTime(cells[timeColumn]) else {
                discontinuity = true; continue
            }
            if let speedColumn, speedColumn < cells.count, let value = Double(cells[speedColumn]) {
                let point = SpeedLogPoint(time: time, metersPerSecond: value * scale, startsNewSegment: discontinuity)
                guard point.valid else { discontinuity = true; continue }
                points.append(point)
                discontinuity = false
            } else if let latColumn, let lonColumn, latColumn < cells.count, lonColumn < cells.count,
                      let lat = Double(cells[latColumn]), let lon = Double(cells[lonColumn]) {
                fixes.append((time, lat, lon))
                discontinuity = true
            } else { discontinuity = true }
        }
        if points.isEmpty { points = speeds(fromFixes: fixes) }
        return points
    }

    static func speeds(fromFixes fixes: [(Date, Double, Double)]) -> [SpeedLogPoint] {
        let sorted = fixes.filter { $0.0.timeIntervalSince1970.isFinite }.sorted { $0.0 < $1.0 }
        guard sorted.count >= 2 else { return [] }
        var points: [SpeedLogPoint] = []
        var discontinuity = true
        for i in 1..<sorted.count {
            let a = sorted[i - 1], b = sorted[i]
            let dt = b.0.timeIntervalSince(a.0)
            guard dt > 0.2, dt <= 5,
                  a.1.isFinite, a.2.isFinite, b.1.isFinite, b.2.isFinite,
                  (-90...90).contains(a.1), (-90...90).contains(b.1),
                  (-180...180).contains(a.2), (-180...180).contains(b.2) else {
                discontinuity = true; continue
            }
            let meters = haversine(a.1, a.2, b.1, b.2)
            let point = SpeedLogPoint(time: a.0.addingTimeInterval(dt / 2), metersPerSecond: meters / dt,
                                      startsNewSegment: discontinuity)
            guard point.valid else { discontinuity = true; continue }
            points.append(point)
            discontinuity = false
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
            guard number.isFinite else { return nil }
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

    private static func splitCSV(_ line: String, separator: Character) -> [String]? {
        let characters = Array(line)
        var cells: [String] = [], cell = "", quoted = false, index = 0
        while index < characters.count {
            let character = characters[index]
            if character == "\"" {
                if quoted && index + 1 < characters.count && characters[index + 1] == "\"" {
                    cell.append("\""); index += 1
                } else { quoted.toggle() }
            } else if character == separator && !quoted {
                cells.append(cell.trimmingCharacters(in: .whitespaces)); cell = ""
            } else { cell.append(character) }
            index += 1
        }
        guard !quoted else { return nil }
        cells.append(cell.trimmingCharacters(in: .whitespaces))
        return cells
    }
}

/// Reads <trkpt> times plus <speed> (GPX 1.0 or Garmin extensions). Without speed, derives it from positions.
private final class GPXSpeedParser: NSObject, XMLParserDelegate {
    private var fixes: [(Date, Double, Double)] = []
    private var segments: [[(Date, Double, Double)]] = []
    private var speeds: [SpeedLogPoint] = []
    private var lat: Double?
    private var lon: Double?
    private var time: Date?
    private var speed: Double?
    private var text = ""
    private var inPoint = false
    private var nextSpeedStartsSegment = true

    static func parse(_ data: Data) throws -> [SpeedLogPoint] {
        let delegate = GPXSpeedParser()
        let parser = XMLParser(data: data)
        parser.delegate = delegate
        parser.shouldProcessNamespaces = true
        parser.shouldResolveExternalEntities = false
        guard parser.parse() else { throw SpeedLogReader.ReadError.unsupported(parser.parserError?.localizedDescription ?? "invalid GPX") }
        delegate.finishSegment()
        // Only trust per-point speed if most points carry it.
        let speeds = delegate.speeds.filter(\.valid)
        if speeds.count >= 2 && speeds.count * 2 >= max(1, delegate.segments.reduce(0) { $0 + $1.count }) { return speeds }
        return delegate.segments.flatMap { SpeedLogReader.speeds(fromFixes: $0) }
    }

    private func finishSegment() {
        if !fixes.isEmpty { segments.append(fixes); fixes.removeAll() }
    }

    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI: String?,
                qualifiedName qName: String?, attributes: [String: String] = [:]) {
        text = ""
        if elementName == "trkseg" || elementName == "trk" {
            finishSegment(); nextSpeedStartsSegment = true
        }
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
        if elementName == "trkseg" { finishSegment() }
        guard inPoint else { return }
        if elementName == "time" {
            time = SpeedLogReader.parseTime(value)
        } else if elementName.lowercased() == "speed" {
            speed = Double(value)
        } else if elementName == "trkpt" {
            inPoint = false
            if let time {
                fixes.append((time, lat ?? .nan, lon ?? .nan))
                if let lat, let lon, let speed, (-90...90).contains(lat), (-180...180).contains(lon) {
                    let point = SpeedLogPoint(time: time, metersPerSecond: speed, startsNewSegment: nextSpeedStartsSegment)
                    if point.valid {
                        speeds.append(point)
                        nextSpeedStartsSegment = false
                    } else { nextSpeedStartsSegment = true }
                } else { nextSpeedStartsSegment = true }
            } else { finishSegment(); nextSpeedStartsSegment = true }
        }
    }
}
