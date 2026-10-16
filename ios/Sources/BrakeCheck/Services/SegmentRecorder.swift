import BrakeCheckCore
import AVFoundation
import Foundation

/// All methods and state belong to the capture queue. Finalization callbacks return to it.
final class SegmentRecorder {
    private let queue: DispatchQueue
    private let files = FileManager.default
    private let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }()
    private let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return decoder
    }()
    private var ringURL: URL
    let incidentsURL: URL
    private var ring: [VideoSegment] = []
    private var incidents: [UUID: IncidentManifest] = [:]
    private var pendingEvents = Set<UUID>()
    private var prepared = false
    private var enabled = false
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var segmentURL: URL?
    private var segmentStart: Date?
    private var startPTS: CMTime = .invalid
    private var lastPTS: CMTime = .invalid
    private var encoderWidth = 0
    private var encoderHeight = 0
    private var finishGroup = DispatchGroup()
    private var lastPublish = Date.distantPast
    private var lastStorageCheck = Date.distantPast
    private var pendingFinalizations = 0
    private var storageScanRunning = false
    private var storageBytes: Int64?
    private var storageGeneration = 0
    private var lastStorageScan = Date.distantPast
    private let storageQueue = DispatchQueue(label: "BrakeCheck.videoStorage", qos: .utility)
    var onStatus: ((String) -> Void)?
    var onIncidents: (([IncidentManifest]) -> Void)?
    var onStorage: ((Int64?) -> Void)?

    init(queue: DispatchQueue) {
        self.queue = queue
        let documents = files.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let support = files.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        incidentsURL = documents.appendingPathComponent("Incidents", isDirectory: true)
        ringURL = support.appendingPathComponent("RollingVideo", isDirectory: true)
    }

    func prepareIfNeeded() throws {
        guard !prepared else { return }
        for var url in [ringURL, incidentsURL] {
            try files.createDirectory(at: url, withIntermediateDirectories: true,
                                      attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try url.setResourceValues(values)
        }
        ring.removeAll()
        incidents.removeAll()
        pendingEvents.removeAll()
        for url in try files.contentsOfDirectory(at: ringURL, includingPropertiesForKeys: nil)
        where url.pathExtension == "json" {
            if let data = try? Data(contentsOf: url),
               let segment = try? decoder.decode(VideoSegment.self, from: data),
               files.fileExists(atPath: ringURL.appendingPathComponent(segment.filename).path) {
                ring.append(segment)
            }
        }
        ring.sort { $0.start < $1.start }
        // An unfinalized movie after force-quit cannot be promised as recoverable.
        let known = Set(ring.map(\.filename))
        for url in try files.contentsOfDirectory(at: ringURL, includingPropertiesForKeys: nil)
        where url.pathExtension == "mp4" && !known.contains(url.lastPathComponent) {
            try? files.removeItem(at: url)
        }
        for directory in try files.contentsOfDirectory(at: incidentsURL, includingPropertiesForKeys: nil) {
            let manifestURL = directory.appendingPathComponent("manifest.json")
            guard let data = try? Data(contentsOf: manifestURL),
                  var event = try? decoder.decode(IncidentManifest.self, from: data) else { continue }
            if event.state == "Collecting available footage" {
                event.notes.append("App restarted. Video is unavailable during the interruption.")
                if Date() < event.requestedEnd { pendingEvents.insert(event.id) }
                else { event.state = "Saved available footage; interrupted" }
            }
            incidents[event.id] = event
            try persist(event)
        }
        // Recover an event's last completed segment if termination happened before it was copied.
        for segment in ring { try copyToMatchingEvents(segment) }
        prepared = true
        prune(now: Date())
        publishIncidents()
    }

    func refresh() {
        do { try prepareIfNeeded(); applyStoragePreferences(); publishIncidents(); refreshStorageUsage() }
        catch { onStatus?("Storage unavailable: \(error.localizedDescription)") }
    }

    func applyStoragePreferences() {
        // Cleanup is opt-in and waits until capture and all writers are idle.
        guard prepared else { refresh(); return }
        if !enabled, writer == nil, pendingFinalizations == 0, pendingEvents.isEmpty {
            let days = UserDefaults.standard.integer(forKey: "recording.retentionDays")
            let expired = incidents.values.filter {
                RecordingStoragePolicy.shouldDelete($0, now: Date(), retentionDays: days, protectedIDs: pendingEvents)
            }.map(\.id)
            for id in expired { deleteIncident(id) }
        }
        refreshStorageUsage(force: true)
    }

    private func refreshStorageUsage(force: Bool = false) {
        guard !storageScanRunning,
              force || Date().timeIntervalSince(lastStorageScan) >= 30 else { return }
        storageScanRunning = true
        lastStorageScan = Date()
        let roots = [ringURL, incidentsURL]
        let generation = storageGeneration
        storageQueue.async {
            var bytes: Int64 = 0
            var seen = Set<String>()
            for root in roots {
                guard let entries = FileManager.default.enumerator(at: root,
                    includingPropertiesForKeys: [.isRegularFileKey]) else { continue }
                for case let file as URL in entries where file.pathExtension == "mp4" {
                    guard let attributes = try? FileManager.default.attributesOfItem(atPath: file.path),
                          let size = attributes[.size] as? NSNumber else { continue }
                    // Hard-linked ring/incident clips occupy disk space once.
                    let key = (attributes[.systemFileNumber] as? NSNumber)?.stringValue ?? file.path
                    if seen.insert(key).inserted { bytes += size.int64Value }
                }
            }
            let measuredBytes = bytes
            self.queue.async {
                self.storageScanRunning = false
                guard self.storageGeneration == generation else {
                    self.refreshStorageUsage(force: true)
                    return
                }
                self.storageBytes = measuredBytes
                self.onStorage?(measuredBytes)
                if self.enabled, let limit = RecordingStoragePolicy.limitBytes(
                    gigabytes: UserDefaults.standard.integer(forKey: "recording.limitGB")), measuredBytes >= limit {
                    self.pause(reason: "Recording limit reached. Saved incident videos are preserved.")
                }
            }
        }
    }

    func start() {
        do {
            try prepareIfNeeded()
            applyStoragePreferences()
            for segment in ring { try copyToMatchingEvents(segment) }
            prune(now: Date())
            guard storageAvailable() else {
                onStatus?(storageStopReason)
                return
            }
            enabled = true
            lastStorageCheck = Date()
            onStatus?("Waiting for camera frames…")
        } catch { onStatus?("Recorder: \(error.localizedDescription)") }
    }

    func append(_ sample: CMSampleBuffer) {
        guard enabled, CMSampleBufferDataIsReady(sample),
              let buffer = CMSampleBufferGetImageBuffer(sample) else { return }
        if writer != nil, (CVPixelBufferGetWidth(buffer) != encoderWidth || CVPixelBufferGetHeight(buffer) != encoderHeight) {
            finishSegment()
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        guard pts.isValid, !pts.isIndefinite else { return }
        let now = Date()
        if now.timeIntervalSince(lastStorageCheck) > 5 {
            lastStorageCheck = now
            refreshStorageUsage()
            guard storageAvailable() else {
                pause(reason: storageStopReason)
                return
            }
        }
        if writer != nil, lastPTS.isValid, CMTimeCompare(pts, lastPTS) <= 0 { return }
        if writer != nil, CMTimeGetSeconds(CMTimeSubtract(pts, startPTS)) >= RetentionPolicy.segmentSeconds {
            finishSegment()
        }
        if writer == nil {
            do { try beginSegment(sample, at: now) }
            catch {
                enabled = false
                onStatus?("Recording failed: \(error.localizedDescription)")
                return
            }
        }
        guard let writer, let input else { return }
        guard writer.status == .writing else {
            pause(reason: "Video encoder failed.")
            onStatus?("Encoder failure: \(writer.error?.localizedDescription ?? "unknown error")")
            return
        }
        if input.isReadyForMoreMediaData {
            if input.append(sample) { lastPTS = pts }
            else {
                pause(reason: "Video frame write failed.")
                onStatus?("Recording stopped: video write failed.")
                return
            }
        }
        if now.timeIntervalSince(lastPublish) > 1 {
            lastPublish = now
            let oldest = ring.first?.start ?? segmentStart ?? now
            let minutes = min(15, max(0, now.timeIntervalSince(oldest) / 60))
            let suffix = pendingEvents.isEmpty ? "rolling" : "saving post-event video"
            onStatus?(String(format: "REC · %.1f / 15 min · %@", minutes, suffix))
        }
        completeExpiredEvents(now: now)
    }

    private func beginSegment(_ sample: CMSampleBuffer, at now: Date) throws {
        guard let buffer = CMSampleBufferGetImageBuffer(sample) else { throw RecorderError.noFrame }
        let url = ringURL.appendingPathComponent(UUID().uuidString).appendingPathExtension("mp4")
        let nextWriter = try AVAssetWriter(outputURL: url, fileType: .mp4)
        let settings: [String: Any] = [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: CVPixelBufferGetWidth(buffer),
            AVVideoHeightKey: CVPixelBufferGetHeight(buffer),
            AVVideoCompressionPropertiesKey: [
                AVVideoAverageBitRateKey: 2_000_000,
                AVVideoMaxKeyFrameIntervalKey: 60,
                AVVideoExpectedSourceFrameRateKey: 30
            ]
        ]
        let nextInput = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        nextInput.expectsMediaDataInRealTime = true
        guard nextWriter.canAdd(nextInput) else { throw RecorderError.cannotWrite }
        nextWriter.add(nextInput)
        guard nextWriter.startWriting() else { throw nextWriter.error ?? RecorderError.cannotWrite }
        startPTS = CMSampleBufferGetPresentationTimeStamp(sample)
        nextWriter.startSession(atSourceTime: startPTS)
        writer = nextWriter
        input = nextInput
        encoderWidth = CVPixelBufferGetWidth(buffer)
        encoderHeight = CVPixelBufferGetHeight(buffer)
        segmentURL = url
        segmentStart = now
        lastPTS = .invalid
    }

    /// Keeps incident windows open and begins a new encoder for the next upright frame.
    func beginOrientationSegment() { finishSegment() }

    private func finishSegment() {
        guard let currentWriter = writer, let currentInput = input,
              let url = segmentURL, let start = segmentStart else { return }
        let duration = lastPTS.isValid ? max(0, CMTimeGetSeconds(CMTimeSubtract(lastPTS, startPTS))) : 0
        let segment = VideoSegment(filename: url.lastPathComponent, start: start,
                                   end: start.addingTimeInterval(duration + 1.0 / 30.0))
        writer = nil
        input = nil
        segmentURL = nil
        segmentStart = nil
        startPTS = .invalid
        lastPTS = .invalid
        guard currentWriter.status == .writing, duration > 0 else {
            currentWriter.cancelWriting()
            try? files.removeItem(at: url)
            return
        }
        currentInput.markAsFinished()
        pendingFinalizations += 1
        finishGroup.enter()
        currentWriter.finishWriting {
            self.queue.async {
                defer {
                    self.pendingFinalizations -= 1
                    self.finishGroup.leave()
                    self.refreshStorageUsage()
                }
                guard currentWriter.status == .completed else {
                    try? self.files.removeItem(at: url)
                    self.onStatus?("A video segment could not be saved.")
                    self.notePending("A video segment failed to finalize; footage may be incomplete.")
                    return
                }
                do {
                    try self.encoder.encode(segment).write(to: url.appendingPathExtension("json"), options: .atomic)
                    self.ring.append(segment)
                    self.ring.sort { $0.start < $1.start }
                    try self.copyToMatchingEvents(segment)
                    self.prune(now: Date())
                    self.publishIncidents()
                } catch {
                    self.onStatus?("Saving video failed: \(error.localizedDescription)")
                }
            }
        }
    }

    func markIncident(reason: String) {
        do {
            try prepareIfNeeded()
            let now = Date()
            var event = IncidentManifest(at: now, reason: reason)
            let oldest = ring.first?.start ?? segmentStart ?? now
            let preEventSeconds = min(RetentionPolicy.preEventSeconds, max(0, now.timeIntervalSince(oldest)))
            if preEventSeconds < RetentionPolicy.preEventSeconds - 1 {
                event.notes.append(String(format: "At the trigger, the recorded history spans only %.1f of the requested 15 pre-event minutes; gaps may also exist.", preEventSeconds / 60))
            }
            // Persist the event first: a process termination must not erase the trigger.
            try persist(event)
            incidents[event.id] = event
            pendingEvents.insert(event.id)
            for segment in ring { try copyToMatchingEvents(segment) }
            finishSegment()
            if !enabled { notePending("Camera was not recording at the event time.") }
            onStatus?("Incident marked · collecting the next 15 minutes while recording")
            publishIncidents()
        } catch { onStatus?("Could not preserve incident: \(error.localizedDescription)") }
    }

    /// Suspends capture, but leaves requested event windows open in case recording resumes.
    func pause(reason: String, completion: (() -> Void)? = nil) {
        enabled = false
        notePending(reason)
        finishSegment()
        finishGroup.notify(queue: queue) { completion?() }
        onStatus?(reason)
    }

    func stop(completion: (() -> Void)? = nil) {
        enabled = false
        notePending("Trip ended. The full post-event window may not be available.")
        finishSegment()
        finishGroup.notify(queue: queue) {
            for id in Array(self.pendingEvents) {
                guard var event = self.incidents[id] else { continue }
                event.state = "Saved available footage; trip ended"
                self.incidents[id] = event
                self.saveOrReport(event)
            }
            self.pendingEvents.removeAll()
            // Only event footage survives an explicit End Trip.
            let removable = self.ring.filter { !self.hasUnarchivedEventCopy($0) }
            for segment in removable { self.removeFromRing(segment) }
            self.ring.removeAll { !self.hasUnarchivedEventCopy($0) }
            if !self.ring.isEmpty {
                self.onStatus?("Some incident copies failed; original clips were kept for recovery.")
            }
            self.publishIncidents()
            if self.ring.isEmpty { self.onStatus?("Recorder stopped") }
            self.applyStoragePreferences()
            completion?()
        }
    }

    func deleteIncident(_ id: UUID) {
        guard !pendingEvents.contains(id) else {
            onStatus?("End the trip before deleting an active incident.")
            return
        }
        do {
            try files.removeItem(at: incidentsURL.appendingPathComponent(id.uuidString))
            storageGeneration += 1
            storageBytes = nil
            incidents.removeValue(forKey: id)
            publishIncidents()
            refreshStorageUsage(force: true)
        } catch { onStatus?("Delete failed: \(error.localizedDescription)") }
    }

    private func copyToMatchingEvents(_ segment: VideoSegment) throws {
        for id in Array(incidents.keys) {
            guard var event = incidents[id], RetentionPolicy.overlaps(segment, event),
                  !event.segments.contains(where: { $0.filename == segment.filename }) else { continue }
            let destination = incidentsURL.appendingPathComponent(id.uuidString).appendingPathComponent(segment.filename)
            if !files.fileExists(atPath: destination.path) {
                let source = ringURL.appendingPathComponent(segment.filename)
                // Hard-link if possible: pruning the ring won't delete the incident's data.
                do { try files.linkItem(at: source, to: destination) }
                catch { try files.copyItem(at: source, to: destination) }
            }
            event.segments.append(segment)
            event.segments.sort { $0.start < $1.start }
            try persist(event)
            incidents[id] = event
        }
    }

    private func notePending(_ note: String) {
        for id in Array(pendingEvents) {
            guard var event = incidents[id] else { continue }
            if !event.notes.contains(note) { event.notes.append(note) }
            incidents[id] = event
            saveOrReport(event)
        }
        publishIncidents()
    }

    private func completeExpiredEvents(now: Date) {
        for id in Array(pendingEvents) {
            guard var event = incidents[id], now > event.requestedEnd else { continue }
            // A final in-flight segment can still be copied after this state transition.
            event.state = "Saved available footage"
            incidents[id] = event
            pendingEvents.remove(id)
            saveOrReport(event)
            publishIncidents()
        }
    }

    private func persist(_ event: IncidentManifest) throws {
        let directory = incidentsURL.appendingPathComponent(event.id.uuidString, isDirectory: true)
        try files.createDirectory(at: directory, withIntermediateDirectories: true)
        try encoder.encode(event).write(to: directory.appendingPathComponent("manifest.json"), options: .atomic)
    }

    private func saveOrReport(_ event: IncidentManifest) {
        do { try persist(event) }
        catch { onStatus?("Incident metadata write failed: \(error.localizedDescription)") }
    }

    private func prune(now: Date) {
        let expired = ring.filter { RetentionPolicy.shouldDeleteFromRing($0, now: now) && !hasUnarchivedEventCopy($0) }
        expired.forEach(removeFromRing)
        let expiredNames = Set(expired.map(\.filename))
        ring.removeAll { expiredNames.contains($0.filename) }
    }

    private func hasUnarchivedEventCopy(_ segment: VideoSegment) -> Bool {
        incidents.values.contains { event in
            RetentionPolicy.overlaps(segment, event) && !event.segments.contains(where: { $0.filename == segment.filename })
        }
    }

    private func removeFromRing(_ segment: VideoSegment) {
        storageGeneration += 1
        storageBytes = nil
        let url = ringURL.appendingPathComponent(segment.filename)
        try? files.removeItem(at: url)
        try? files.removeItem(at: url.appendingPathExtension("json"))
    }

    private func storageAvailable() -> Bool {
        if let used = storageBytes,
           let limit = RecordingStoragePolicy.limitBytes(gigabytes: UserDefaults.standard.integer(forKey: "recording.limitGB")),
           used >= limit { return false }
        guard let attributes = try? files.attributesOfFileSystem(forPath: ringURL.path),
              let free = attributes[.systemFreeSize] as? NSNumber else { return false }
        return free.int64Value > 500_000_000
    }

    private var storageStopReason: String {
        if let used = storageBytes,
           let limit = RecordingStoragePolicy.limitBytes(gigabytes: UserDefaults.standard.integer(forKey: "recording.limitGB")),
           used >= limit { return "Recording limit reached. Saved incident videos are preserved." }
        return "Recording stopped: keep at least 500 MB free. Saved incidents are preserved."
    }

    private func publishIncidents() {
        onIncidents?(incidents.values.sorted { $0.triggerTime > $1.triggerTime })
    }

    enum RecorderError: Error { case noFrame, cannotWrite }
}
