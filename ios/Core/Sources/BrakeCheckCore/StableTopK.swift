import Foundation

/// Selects the few candidates needed by patch verification without sorting the
/// full search window. Original order breaks ties, matching Swift's stable sort.
enum StableTopK {
    static func smallest<Element>(_ elements: [Element], count: Int,
                                  by ordered: (Element, Element) -> Bool) -> [Element] {
        guard count > 0, !elements.isEmpty else { return [] }
        let limit = min(count, elements.count)
        typealias Entry = (index: Int, value: Element)
        func before(_ left: Entry, _ right: Entry) -> Bool {
            if ordered(left.value, right.value) { return true }
            if ordered(right.value, left.value) { return false }
            return left.index < right.index
        }
        var heap: [Entry] = []
        heap.reserveCapacity(limit)
        for (index, value) in elements.enumerated() {
            let entry = (index: index, value: value)
            if heap.count < limit {
                heap.append(entry)
                var child = heap.count - 1
                while child > 0 {
                    let parent = (child - 1) / 2
                    guard before(heap[parent], heap[child]) else { break }
                    heap.swapAt(parent, child); child = parent
                }
            } else if before(entry, heap[0]) {
                heap[0] = entry
                var parent = 0
                while parent * 2 + 1 < heap.count {
                    var child = parent * 2 + 1
                    if child + 1 < heap.count, before(heap[child], heap[child + 1]) { child += 1 }
                    guard before(heap[parent], heap[child]) else { break }
                    heap.swapAt(parent, child); parent = child
                }
            }
        }
        return heap.sorted(by: before).map { $0.value }
    }
}
