import XCTest
@testable import BrakeCheckCore

final class RoadVisibilityMaskTests: XCTestCase {
    private func reference(_ pixels: [UInt8], width: Int, height: Int, x: Double, y: Double) -> Bool {
        guard x.isFinite, y.isFinite, (0...1).contains(x), (0...1).contains(y) else { return false }
        let column = min(width - 1, Int(x * Double(width)))
        let row = min(height - 1, Int((1 - y) * Double(height)))
        for dy in -1...1 { for dx in -1...1 {
            let xx = column + dx, yy = row + dy
            guard xx >= 0, xx < width, yy >= 0, yy < height,
                  pixels[yy * width + xx] != 0 else { return false }
        } }
        return true
    }

    private func rasterize(_ spans: [RoadVisibilitySpan], width: Int, height: Int) -> [UInt8] {
        var result = [UInt8](repeating: 0, count: width * height)
        for span in spans {
            for column in span.firstColumn..<span.endColumn { result[span.row * width + column] = 1 }
        }
        return result
    }

    func testPreparedSpansAndQueriesPreserveConservativePixelMembership() throws {
        let width = 31, height = 19
        let patterns: [[UInt8]] = [
            [UInt8](repeating: 0, count: width * height),
            [UInt8](repeating: 1, count: width * height),
            (0..<(width * height)).map { index in
                let row = index / width, column = index % width
                return row >= 3 && row <= 15 && column >= 3 && column <= 27
                    && !(row >= 7 && row <= 10 && column >= 12 && column <= 16) ? 255 : 0
            },
            (0..<(width * height)).map { $0 % 7 == 0 || $0 % 13 == 0 ? 0 : 1 }
        ]
        for pixels in patterns {
            let mask = try XCTUnwrap(RoadVisibilityMask(width: width, height: height, pixels: pixels))
            XCTAssertEqual(mask.occupiedPixelCount, pixels.filter { $0 != 0 }.count)
            XCTAssertEqual(rasterize(mask.rawSpans, width: width, height: height), pixels.map { $0 == 0 ? 0 : 1 })
            let visible = rasterize(mask.visibilitySpans, width: width, height: height)
            for row in 0..<height { for column in 0..<width {
                let x = (Double(column) + 0.5) / Double(width)
                let y = 1 - (Double(row) + 0.5) / Double(height)
                let expected = reference(pixels, width: width, height: height, x: x, y: y)
                XCTAssertEqual(mask.contains(x: x, y: y), expected, "pixel \(column),\(row)")
                XCTAssertEqual(visible[row * width + column] != 0, expected)
            } }
            for x in [-0.001, 0, 0.25, 0.5, 0.999999, 1, 1.001, .nan, .infinity] {
                for y in [-0.001, 0, 0.25, 0.5, 0.999999, 1, 1.001, .nan, -.infinity] {
                    XCTAssertEqual(mask.contains(x: x, y: y), reference(pixels, width: width, height: height, x: x, y: y))
                }
            }
        }
    }

    func testCopiesKeepCacheIdentityAndNewMasksInvalidateIt() throws {
        let pixels = [UInt8](repeating: 1, count: 64)
        let original = try XCTUnwrap(RoadVisibilityMask(width: 8, height: 8, pixels: pixels))
        let copy = original
        let newMask = try XCTUnwrap(RoadVisibilityMask(width: 8, height: 8, pixels: pixels))
        XCTAssertEqual(original.renderIdentity, copy.renderIdentity)
        XCTAssertNotEqual(original.renderIdentity, newMask.renderIdentity)
        XCTAssertEqual(original.visibilitySpans, copy.visibilitySpans)
        XCTAssertEqual(original.visibilitySpans, newMask.visibilitySpans)
    }

    func testHolesAndBordersStayExcludedFromRenderSpans() throws {
        let width = 12, height = 10
        var pixels = [UInt8](repeating: 1, count: width * height)
        pixels[5 * width + 6] = 0
        let mask = try XCTUnwrap(RoadVisibilityMask(width: width, height: height, pixels: pixels))
        let visible = rasterize(mask.visibilitySpans, width: width, height: height)
        for row in 4...6 { for column in 5...7 { XCTAssertEqual(visible[row * width + column], 0) } }
        for column in 0..<width {
            XCTAssertEqual(visible[column], 0)
            XCTAssertEqual(visible[(height - 1) * width + column], 0)
        }
        for row in 0..<height {
            XCTAssertEqual(visible[row * width], 0)
            XCTAssertEqual(visible[row * width + width - 1], 0)
        }
    }
}
