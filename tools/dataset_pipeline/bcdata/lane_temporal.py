"""Experimental post-decoder lane tracking in normalized image coordinates.

No model/label writes. Predictions keep the age of their last real observation.
The position/velocity model is deliberately local: it is not a road/world model.
"""
from dataclasses import asdict, dataclass
import math

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment


MODES = ("raw", "ema", "kalman", "flow_kalman")


@dataclass(frozen=True)
class TrackingConfig:
    rows: tuple = tuple(np.linspace(.20, .98, 40))
    max_input_gap: float = .065
    max_prediction_age: float = .25
    reset_gap: float = .75
    association_distance: float = .065
    measurement_std: float = .008
    acceleration_std: float = .12
    innovation_sigma: float = 3.5
    ema_alpha: float = .65
    confirmations: int = 2

    def __post_init__(self):
        values = np.asarray(self.rows, float)
        if (values.ndim != 1 or len(values) < 3 or not np.isfinite(values).all()
                or not ((values >= 0) & (values <= 1)).all() or not (np.diff(values) > 0).all()):
            raise ValueError("Tracking rows must increase within [0, 1]")
        for name in ("max_input_gap", "max_prediction_age", "reset_gap", "association_distance",
                     "measurement_std", "acceleration_std", "innovation_sigma"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(name + " must be finite and positive")
        if (not 0 < self.ema_alpha <= 1 or self.confirmations < 1
                or self.max_prediction_age > self.reset_gap):
            raise ValueError("Invalid smoothing, confirmation, or expiry settings")


def sample_lane(points, rows, max_gap):
    """Sample only supported segments. A missing span is never extrapolated."""
    points = np.asarray(points, dtype=float)
    result = np.full(len(rows), np.nan)
    if points.size == 0:
        return result
    if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
        raise ValueError("Lane points must be finite [x, y] pairs")
    if not ((points >= 0) & (points <= 1)).all():
        raise ValueError("Lane points must use normalized [0, 1] coordinates")
    points = points[np.argsort(points[:, 1], kind="stable")]
    # Horizontal/multivalued traces cannot be represented as x(y).
    if len(points) < 2 or (np.diff(points[:, 1]) <= 1e-7).any():
        return result
    for a, b in zip(points, points[1:]):
        if b[1] - a[1] > max_gap:
            continue
        selected = (rows >= a[1] - 1e-9) & (rows <= b[1] + 1e-9)
        result[selected] = np.interp(rows[selected], [a[1], b[1]], [a[0], b[0]])
    return result


def segments(x, rows):
    """Separate supported runs so renderers/evaluators cannot bridge holes."""
    indices = np.flatnonzero(np.isfinite(x))
    runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
    return [[[float(x[i]), float(rows[i])] for i in run] for run in runs if len(run) >= 2]


def _gray(frame):
    if frame is None:
        return None
    if frame.ndim == 3:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if frame.ndim != 2 or frame.size == 0:
        raise ValueError("Frame must be a nonempty grayscale or BGR image")
    height = max(32, round(frame.shape[0] * min(1., 640 / frame.shape[1])))
    return cv2.resize(frame, (min(640, frame.shape[1]), height)).astype(np.uint8)


def estimate_alignment(previous, current, lanes, boxes=()):
    """Conservative sparse optical flow near previously supported boundaries.

    Returns a normalized previous-to-current affine, never a lane observation.
    Vehicle boxes are normalized x1,y1,x2,y2 in the current frame.
    """
    if previous is None or current is None or previous.shape != current.shape:
        return None, {"status": "unavailable"}
    height, width = previous.shape
    mask = np.zeros_like(previous)
    for lane in lanes:
        for run in lane["segments"]:
            pixels = np.round(np.asarray(run) * [width - 1, height - 1]).astype(np.int32)
            cv2.polylines(mask, [pixels], False, 255, max(12, round(width * .04)))
    # Also mask the current box positions in the old frame conservatively.
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(mask, (int(x1 * width), int(y1 * height)),
                      (int(x2 * width), int(y2 * height)), 0, -1)
    points = cv2.goodFeaturesToTrack(previous, 200, .01, 5, mask=mask)
    if points is None or len(points) < 8:
        return None, {"status": "too_few_features"}
    moved, ok, _ = cv2.calcOpticalFlowPyrLK(previous, current, points, None)
    if moved is None:
        return None, {"status": "flow_failed"}
    back, back_ok, _ = cv2.calcOpticalFlowPyrLK(current, previous, moved, None)
    if back is None:
        return None, {"status": "flow_failed"}
    source, target = points.reshape(-1, 2), moved.reshape(-1, 2)
    valid = (ok.ravel() > 0) & (back_ok.ravel() > 0)
    valid &= np.linalg.norm(back.reshape(-1, 2) - source, axis=1) < 1.
    valid &= np.isfinite(target).all(axis=1)
    valid &= (target[:, 0] >= 0) & (target[:, 0] < width)
    valid &= (target[:, 1] >= 0) & (target[:, 1] < height)
    for x1, y1, x2, y2 in boxes:
        valid &= ~((target[:, 0] >= x1 * width) & (target[:, 0] <= x2 * width)
                   & (target[:, 1] >= y1 * height) & (target[:, 1] <= y2 * height))
    if valid.sum() < 8:
        return None, {"status": "too_few_matches"}
    affine, inliers = cv2.estimateAffinePartial2D(source[valid], target[valid],
                                                method=cv2.RANSAC, ransacReprojThreshold=1.5)
    if affine is None or inliers is None:
        return None, {"status": "fit_failed"}
    ratio = float(inliers.mean())
    scale = float(np.hypot(affine[0, 0], affine[1, 0]))
    rotation = abs(math.atan2(affine[1, 0], affine[0, 0]))
    shift = np.linalg.norm(affine[:, 2] / [width, height])
    if (inliers.sum() < 8 or ratio < .6 or not .92 <= scale <= 1.08
            or rotation > .10 or shift > .12):
        return None, {"status": "rejected_motion", "inlier_ratio": ratio}
    normalize = np.diag([1 / (width - 1), 1 / (height - 1), 1])
    matrix = normalize @ np.vstack([affine, [0, 0, 1]]) @ np.linalg.inv(normalize)
    return matrix[:2], {"status": "accepted", "inlier_ratio": ratio,
                        "matches": int(valid.sum()), "affine": matrix[:2].tolist()}


@dataclass
class _Track:
    identity: int
    state: np.ndarray
    covariance: np.ndarray
    observed_at: np.ndarray
    hits: int = 1
    confirmed: bool = False
    updated: bool = False


class LaneTracker:
    def __init__(self, mode="kalman", config=None):
        if mode not in MODES:
            raise ValueError("Unknown tracking mode")
        self.mode, self.config = mode, config or TrackingConfig()
        self.rows = np.asarray(self.config.rows)
        self._next_id = 1
        self.reset()

    def reset(self):
        self.tracks = []
        self.last_time = None
        self.last_scene = None
        self.previous_frame = None
        self.previous_boxes = []
        self.original_shape = None
        self.last_output = []
        self.alignment = {"status": "unavailable"}

    def _new(self, x, time, confidence=1.):
        state = np.column_stack([x, np.zeros(len(x))])
        covariance = np.tile(np.diag([(self.config.measurement_std / confidence) ** 2, .02 ** 2]), (len(x), 1, 1))
        seen = np.where(np.isfinite(x), time, -np.inf)
        track = _Track(self._next_id, state, covariance, seen,
                       confirmed=self.config.confirmations == 1, updated=True)
        self._next_id += 1
        self.tracks.append(track)

    def _warp(self, track, affine):
        old_x = track.state[:, 0].copy()
        indices = np.flatnonzero(np.isfinite(old_x))
        runs = np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1)
        state = np.full_like(track.state, np.nan)
        covariance = track.covariance.copy()
        seen = np.full_like(track.observed_at, -np.inf)
        for run in runs:
            if len(run) < 2:
                continue
            points = np.column_stack([old_x[run], self.rows[run], np.ones(len(run))]) @ affine.T
            if not (np.diff(points[:, 1]) > 0).all():
                continue
            selected = np.flatnonzero((self.rows >= points[0, 1]) & (self.rows <= points[-1, 1]))
            for i in selected:
                k = min(len(run) - 2, max(0, np.searchsorted(points[:, 1], self.rows[i]) - 1))
                a, b = run[k], run[k + 1]
                f = (self.rows[i] - points[k, 1]) / (points[k + 1, 1] - points[k, 1])
                state[i, 0] = (1 - f) * points[k, 0] + f * points[k + 1, 0]
                # Image alignment already explains motion; do not add the same
                # measured displacement again as a constant-velocity prediction.
                state[i, 1] = 0.
                covariance[i] = (1 - f) * track.covariance[a] + f * track.covariance[b]
                covariance[i, 0, 0] += self.config.measurement_std ** 2
                seen[i] = min(track.observed_at[a], track.observed_at[b])
        track.state, track.covariance, track.observed_at = state, covariance, seen

    def update(self, lanes, time, *, frame=None, scene_id=None, scene_cut=False, boxes=()):
        if not math.isfinite(time):
            raise ValueError("Capture timestamp must be finite")
        measurements, original = [], []
        for lane in lanes:
            points = lane.get("points", []) if isinstance(lane, dict) else lane
            confidence = float(lane.get("confidence", 1.)) if isinstance(lane, dict) else 1.
            if not math.isfinite(confidence) or not 0 < confidence <= 1:
                raise ValueError("Confidence must be in (0, 1]")
            x = sample_lane(points, self.rows, self.config.max_input_gap)
            if len(points) >= 2:
                original.append(points)
            if np.isfinite(x).sum() >= 3:
                measurements.append((x, confidence))
        current = _gray(frame)
        for box in boxes:
            if (len(box) != 4 or not np.isfinite(box).all()
                    or not all(0 <= v <= 1 for v in box) or box[0] >= box[2] or box[1] >= box[3]):
                raise ValueError("Exclusion boxes must be normalized x1,y1,x2,y2")
        discontinuity = (self.last_time is not None and (time <= self.last_time
                         or time - self.last_time > self.config.reset_gap))
        changed_scene = self.last_time is not None and scene_id != self.last_scene
        shape = frame.shape if frame is not None else None
        resized = shape is not None and self.original_shape is not None and shape != self.original_shape
        appearance_cut = False
        if current is not None and self.previous_frame is not None and current.shape == self.previous_frame.shape:
            # Deliberately conservative hard-cut detector; callers can also mark
            # known camera edits explicitly. Gradual road motion is not a cut.
            difference = np.mean(np.abs(current.astype(float) - self.previous_frame.astype(float))) / 255
            appearance_cut = difference > .45
        if scene_cut or discontinuity or changed_scene or resized or appearance_cut:
            self.reset()
        dt = 0. if self.last_time is None else time - self.last_time
        self.alignment = {"status": "disabled"}
        affine = None
        if self.mode == "flow_kalman":
            affine, self.alignment = estimate_alignment(self.previous_frame, current, self.last_output,
                                                        list(boxes) + self.previous_boxes)
        self.previous_frame, self.previous_boxes = current, list(boxes)
        self.original_shape = shape
        self.last_time, self.last_scene = time, scene_id
        if self.mode == "raw":
            self.last_output = [{"track_id": None, "status": "measured", "last_observed_at": time,
                                 "segments": [np.asarray(points).tolist()], "predicted_rows": [],
                                 "position_std": None} for points in original]
            return self.last_output
        transition = np.array([[1., dt], [0., 1.]])
        noise = self.config.acceleration_std ** 2 * np.array([[dt ** 4 / 4, dt ** 3 / 2],
                                                             [dt ** 3 / 2, dt ** 2]])
        for track in self.tracks:
            track.updated = False
            expired = time - track.observed_at > self.config.max_prediction_age + 1e-9
            track.state[expired, :] = np.nan
            if affine is not None:
                self._warp(track, affine)
            elif self.mode != "ema":
                track.state = track.state @ transition.T
                track.covariance = transition @ track.covariance @ transition.T + noise
            outside = (track.state[:, 0] < 0) | (track.state[:, 0] > 1)
            track.state[outside, :] = np.nan
        self.tracks = [t for t in self.tracks if np.isfinite(t.state[:, 0]).sum() >= 3]
        costs = np.full((len(self.tracks), len(measurements)), 1e6)
        for i, track in enumerate(self.tracks):
            for j, (x, confidence) in enumerate(measurements):
                common = np.isfinite(x) & np.isfinite(track.state[:, 0])
                if common.sum() < 3:
                    continue
                residual = np.abs(x[common] - track.state[common, 0])
                variance = track.covariance[common, 0, 0] + (self.config.measurement_std / confidence) ** 2
                if (np.median(residual) <= self.config.association_distance
                        and (residual <= self.config.innovation_sigma * np.sqrt(variance)).mean() >= .6):
                    costs[i, j] = float(np.mean(residual))
        assigned = set()
        if costs.size:
            ti, mi = linear_sum_assignment(costs)
            for i, j in zip(ti, mi):
                if costs[i, j] >= 1e6:
                    continue
                track = self.tracks[i]
                x, confidence = measurements[j]
                variance = (self.config.measurement_std / confidence) ** 2
                present = np.isfinite(x)
                initialized = present & ~np.isfinite(track.state[:, 0])
                track.state[initialized, 0] = x[initialized]
                track.state[initialized, 1] = 0.
                track.covariance[initialized] = np.diag([variance, .02 ** 2])
                shared = present & ~initialized
                residual = x - track.state[:, 0]
                accepted = shared & (np.abs(residual) <= self.config.innovation_sigma
                                      * np.sqrt(track.covariance[:, 0, 0] + variance))
                if self.mode == "ema":
                    track.state[accepted, 0] += self.config.ema_alpha * residual[accepted]
                else:
                    p = track.covariance[accepted]
                    gain = p[:, :, 0] / (p[:, 0, 0] + variance)[:, None]
                    track.state[accepted] += gain * residual[accepted, None]
                    # Joseph form retains symmetry and positive covariance.
                    a = np.tile(np.eye(2), (len(p), 1, 1))
                    a[:, :, 0] -= gain
                    track.covariance[accepted] = a @ p @ a.transpose(0, 2, 1) + variance * gain[:, :, None] * gain[:, None, :]
                observed = initialized | accepted
                if observed.sum() >= 3:
                    track.observed_at[observed] = time
                    track.hits += 1
                    track.confirmed |= track.hits >= self.config.confirmations
                    track.updated = True
                    assigned.add(j)
        for track in self.tracks:
            if not track.updated:
                track.hits = 0
        for j, (x, confidence) in enumerate(measurements):
            if j not in assigned:
                self._new(x, time, confidence)
        output = []
        for track in self.tracks:
            if not track.confirmed:
                continue
            x = track.state[:, 0].copy()
            x[time - track.observed_at > self.config.max_prediction_age + 1e-9] = np.nan
            runs = segments(x, self.rows)
            if not runs:
                continue
            supported = np.isfinite(x)
            output.append({"track_id": track.identity,
                           "status": "measured" if track.updated else "predicted",
                           "last_observed_at": float(np.max(track.observed_at[supported])),
                           "segments": runs,
                           "predicted_rows": self.rows[supported & (track.observed_at < time)].tolist(),
                           "row_observed_at": [[float(self.rows[k]), float(track.observed_at[k])]
                                               for k in np.flatnonzero(supported)],
                           "position_std": float(np.sqrt(track.covariance[supported, 0, 0]).mean())})
        # Do not publish two crossing tracks as an unambiguous corridor.
        ambiguous = set()
        for i, a in enumerate(output):
            xa = self.tracks_for_output(a)
            for j in range(i + 1, len(output)):
                xb = self.tracks_for_output(output[j])
                shared = np.isfinite(xa) & np.isfinite(xb)
                delta = xa[shared] - xb[shared]
                if len(delta) >= 3 and (np.min(delta) < -.005 and np.max(delta) > .005):
                    ambiguous.update((i, j))
        self.last_output = [lane for i, lane in enumerate(output) if i not in ambiguous]
        return self.last_output

    def tracks_for_output(self, lane):
        x = np.full(len(self.rows), np.nan)
        for run in lane["segments"]:
            sampled = sample_lane(run, self.rows, self.config.max_input_gap)
            valid = np.isfinite(sampled)
            x[valid] = sampled[valid]
        return x

    def settings(self):
        return {"mode": self.mode, **asdict(self.config)}
