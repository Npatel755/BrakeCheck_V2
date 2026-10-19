"""Frame extraction for videos whose source rights have been cleared."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import cv2
import numpy as np
from PIL import Image

from .pipeline import dhash, license_registry, require_clearance, setup, split_for


def extract_video(project: Path, root: Path, source: str, video: Path, session: str,
                  seconds_between: float = 1.0, scene_threshold: float = 0.22,
                  max_frames: int = 10000) -> dict:
    """Extract baseline 1-fps frames plus scene changes at half-interval probes.

    Rights clearance is checked before reading source bytes. This keeps local
    or externally downloaded recordings out of the commercial pool by default.
    """
    require_clearance(project, source)
    if license_registry(project)[source].get("media") != "video":
        raise PermissionError(f"{source} is not registered as a cleared video source")
    if not video.is_file() or not session or any(c in session for c in "/\\:"):
        raise ValueError("A real video and safe session identifier are required")
    if seconds_between < 0.2 or max_frames < 1:
        raise ValueError("Invalid sampling budget")
    db = setup(root)
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        raise ValueError(f"Cannot decode {video}")
    fps = capture.get(cv2.CAP_PROP_FPS)
    if not np.isfinite(fps) or fps <= 0:
        raise ValueError("Video has no valid frame rate")
    digest = hashlib.sha256()
    with video.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1 << 20), b""):
            digest.update(chunk)
    source_sha = digest.hexdigest()
    stride = max(1, round(fps * seconds_between / 2))
    last_baseline = -float("inf")
    previous_histogram = None
    selected = 0
    sampled = 0
    frame_number = 0
    while selected < max_frames:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        success, frame = capture.read()
        if not success:
            break
        sampled += 1
        timestamp = frame_number / fps
        miniature = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
        histogram = cv2.calcHist([cv2.cvtColor(miniature, cv2.COLOR_BGR2HSV)], [0, 1], None,
                                 [16, 8], [0, 180, 0, 256])
        cv2.normalize(histogram, histogram)
        difference = 1 - cv2.compareHist(histogram, previous_histogram, cv2.HISTCMP_CORREL) \
            if previous_histogram is not None else 1.0
        baseline = timestamp - last_baseline >= seconds_between - 1e-6
        if baseline or difference >= scene_threshold:
            encoded, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not encoded:
                frame_number += stride
                continue
            image_bytes = jpeg.tobytes()
            image_sha = hashlib.sha256(image_bytes).hexdigest()
            image_id = hashlib.sha256(f"{source}:{source_sha}:{frame_number}".encode()).hexdigest()[:24]
            destination = root / "extracted_frames" / source / session / f"{frame_number:09d}.jpg"
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists() or hashlib.sha256(destination.read_bytes()).hexdigest() != image_sha:
                destination.write_bytes(image_bytes)
            image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            perceptual = dhash(image)
            duplicate = db.execute("SELECT image_id FROM images WHERE image_sha256=? AND image_id<>?",
                                   (image_sha, image_id)).fetchone()
            near = None
            if not duplicate:
                for other_id, other_hash in db.execute("SELECT image_id,dhash FROM images WHERE session_id=?", (session,)):
                    if other_id != image_id and (perceptual ^ int(other_hash, 16)).bit_count() <= 4:
                        near = other_id
                        break
            flags = (["exact_duplicate"] if duplicate else []) + (["near_duplicate_review"] if near else [])
            db.execute("""INSERT OR IGNORE INTO images
                (image_id,source,session_id,source_commit,image_path,mask_path,image_sha256,
                 mask_sha256,dhash,width,height,split,quality_flags,near_duplicate_of,
                 annotation_provenance,review_status)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (image_id, source, session, f"video-sha256:{source_sha}", destination.relative_to(root).as_posix(),
                 None, image_sha, None, f"{perceptual:016x}", frame.shape[1], frame.shape[0],
                 split_for(source, session), json.dumps(flags), near, "none; source video frame",
                 "needs_annotation"))
            db.commit()
            selected += 1
            if baseline:
                last_baseline = timestamp
        previous_histogram = histogram
        frame_number += stride
    capture.release()
    db.close()
    return {"source": source, "session": session, "sampled": sampled, "selected": selected,
            "video_sha256": source_sha}
