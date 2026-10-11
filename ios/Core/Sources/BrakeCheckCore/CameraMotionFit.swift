import Foundation

/// Motion of the gap, not the other vehicle's absolute speed or acceleration.
public struct CameraMotionEstimate {
    public let rangeRate: Double
    public let closingAcceleration: Double?
}

public enum CameraMotionFit {
    /// Linear motion stays available while there is insufficient evidence for
    /// curvature. A quadratic is used only when it substantially improves the
    /// fit and its acceleration exceeds the uncertainty in the observations.
    public static func estimate(_ samples: [TimedValue], distance: Double) -> CameraMotionEstimate? {
        guard samples.count >= 5, let first = samples.first, let last = samples.last,
              distance.isFinite, (1...150).contains(distance) else { return nil }
        let span = last.time - first.time
        guard span >= 0.9 - 1e-6 else { return nil }
        for index in samples.indices {
            let sample = samples[index]
            guard sample.time.isFinite, sample.value.isFinite, (1...150).contains(sample.value) else { return nil }
            if index > 0 {
                let gap = sample.time - samples[index - 1].time
                guard gap > 0, gap <= 0.6 + 1e-6 else { return nil }
            }
        }
        guard let linearRate = DriveMath.slope(samples, minimumSpan: 0.9 - 1e-6) else { return nil }
        let count = Double(samples.count)
        var intercept = 0.0
        for sample in samples { intercept += sample.value - linearRate * (sample.time - last.time) }
        intercept /= count
        var linearError = 0.0
        for sample in samples {
            let error = sample.value - intercept - linearRate * (sample.time - last.time)
            linearError += error * error
        }
        let maximumRMS = max(0.25, distance * 0.025)
        let linear: CameraMotionEstimate? = abs(linearRate) <= 60 && sqrt(linearError / count) <= maximumRMS
            ? CameraMotionEstimate(rangeRate: linearRate, closingAcceleration: nil) : nil
        guard samples.count >= 7, span >= 1.2 - 1e-6 else { return linear }

        // Normalize relative time to [-1, 0], avoiding uptime precision and
        // poorly scaled coefficients when solving the small normal equations.
        var s1 = 0.0, s2 = 0.0, s3 = 0.0, s4 = 0.0
        var y0 = 0.0, y1 = 0.0, y2 = 0.0
        for sample in samples {
            let x = (sample.time - last.time) / span, square = x * x
            s1 += x; s2 += square; s3 += square * x; s4 += square * square
            y0 += sample.value; y1 += x * sample.value; y2 += square * sample.value
        }
        let determinant = count * (s2 * s4 - s3 * s3)
            - s1 * (s1 * s4 - s3 * s2) + s2 * (s1 * s3 - s2 * s2)
        guard determinant > 1e-9,
              let coefficients = solve([[count, s1, s2, y0], [s1, s2, s3, y1], [s2, s3, s4, y2]]) else { return linear }
        let rate = coefficients[1] / span
        let acceleration = -2 * coefficients[2] / (span * span)
        var quadraticError = 0.0
        for sample in samples {
            let x = (sample.time - last.time) / span
            let prediction = coefficients[0] + coefficients[1] * x + coefficients[2] * x * x
            let error = sample.value - prediction
            quadraticError += error * error
        }
        let inverseAccelerationDiagonal = (count * s2 - s1 * s1) / determinant
        let variance = quadraticError / Double(samples.count - 3)
        let accelerationError = 2 * sqrt(max(0, variance * inverseAccelerationDiagonal)) / (span * span)
        let recent = samples.filter { last.time - $0.time <= 0.6 + 1e-6 }
        guard rate.isFinite, acceleration.isFinite, abs(rate) <= 60, abs(acceleration) <= 12,
              abs(acceleration) >= 0.8, abs(acceleration) > 2.5 * accelerationError,
              quadraticError <= linearError * 0.65,
              sqrt(quadraticError / count) <= maximumRMS,
              let recentRate = DriveMath.slope(recent, minimumSpan: 0.3 - 1e-6),
              abs(rate - recentRate) <= max(1.5, abs(acceleration) * 0.4) else { return linear }
        return CameraMotionEstimate(rangeRate: rate, closingAcceleration: acceleration)
    }

    private static func solve(_ equations: [[Double]]) -> [Double]? {
        var matrix = equations
        for column in 0..<3 {
            var pivot = column
            for row in column..<3 where abs(matrix[row][column]) > abs(matrix[pivot][column]) { pivot = row }
            guard abs(matrix[pivot][column]) > 1e-9 else { return nil }
            if pivot != column { matrix.swapAt(pivot, column) }
            let divisor = matrix[column][column]
            for index in column...3 { matrix[column][index] /= divisor }
            for row in 0..<3 where row != column {
                let factor = matrix[row][column]
                for index in column...3 { matrix[row][index] -= factor * matrix[column][index] }
            }
        }
        let result = matrix.map { $0[3] }
        return result.allSatisfy(\.isFinite) ? result : nil
    }
}
