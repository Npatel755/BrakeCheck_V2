# N22 — Clearer vehicle boxes

Removed per-vehicle confidence, track IDs, class names, viewpoint probabilities and diagnostic captions from the camera overlay. Thin detection boxes remain; the existing heavier tracking box still identifies the tracked target. Lead and hazard accents remain, and held detections stay faint and dashed. Ordinary detections use a neutral white outline rather than coloring every viewpoint check.

This is a presentation change only. Detection, tracking, lead eligibility and risk calculation are unchanged. Camera speed/gap readouts and the danger meter remain available. Validation: native release compilation and IPA integrity, revision, executable identity and eleven model-resource checks. Device check: confirm unobstructed boxes in live camera and replay, including held tracks and warnings.
