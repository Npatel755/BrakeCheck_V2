import Foundation

public struct SpeechVoiceCandidate {
    public let name: String
    public let quality: Int
    public let isFemale: Bool
    public init(name: String, quality: Int, isFemale: Bool) {
        self.name = name; self.quality = quality; self.isFemale = isFemale
    }
}

public enum NaturalVoiceSelection {
    private static let preferredNames = ["Ava", "Samantha", "Zoe"]
    public static func prefers(_ first: SpeechVoiceCandidate, over second: SpeechVoiceCandidate) -> Bool {
        let firstFemale = first.isFemale || preferredNames.contains(first.name)
        let secondFemale = second.isFemale || preferredNames.contains(second.name)
        if firstFemale != secondFemale { return firstFemale }
        if first.quality != second.quality { return first.quality > second.quality }
        let firstRank = preferredNames.firstIndex(of: first.name) ?? preferredNames.count
        let secondRank = preferredNames.firstIndex(of: second.name) ?? preferredNames.count
        if firstRank != secondRank { return firstRank < secondRank }
        return first.name.localizedStandardCompare(second.name) == .orderedAscending
    }
}
