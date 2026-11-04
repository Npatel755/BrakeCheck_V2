import Foundation
import BrakeCheckCore

guard (3...4).contains(CommandLine.arguments.count) else {
    fatalError("Usage: ARMockRunner mock-drive.json swift-results.json [--continuity]")
}
let data = try Data(contentsOf: URL(fileURLWithPath: CommandLine.arguments[1]))
let fixture = try JSONDecoder().decode(MockARFixture.self, from: data)
let results = try MockARReplay.evaluate(fixture, visualContinuity: CommandLine.arguments.last == "--continuity")
let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
try encoder.encode(results).write(to: URL(fileURLWithPath: CommandLine.arguments[2]))
for level in ["aligned", "directional", "lost"] {
    print("\(level): \(results.filter { $0.confidence == level }.count)/\(results.count) mock captures")
}
