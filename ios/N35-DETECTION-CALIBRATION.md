# N35 detection and calibration repair

The supplied video15 recording shows persistent `Detection unavailable`, no
vehicle boxes/lead target, unstable paint-only lane outlines, and navigation
remaining in direction-only mode. The calibration settings are not shown.

## Detector failure

Both N34 IPA detector packages contain model definitions but **zero external
weight files**. `weights/` in the root ignore file also excluded the trained
Core ML weights inside resources. Restored day/night `0-weight.bin` files by
exporting the pinned Apache RTMDet source checkpoints; both regenerated weight
hashes match the original provenance exactly. The original model definitions
are retained. Normalized manifest line endings and updated manifest hashes.

The ignore exception now retains bundled weights. SwiftPM's package manifest
rejects missing/empty required assets before compilation. Runtime detector
loading reports a specific missing-weights error. A standard-library-only
`tools/model_migration/verify_model_assets.py` checks ALL perception assets and
optionally every asset in the finished IPA, including hashes and ZIP CRCs.
The earlier road-only verification did not cover these detector weights.

## Calibration and alignment

- Camera AR processing now runs on the visible Camera tab before a route starts.
- Automatic orientation estimation is enabled by default, applies stable yaw,
  and uses lane convergence for pitch/horizon. It no longer depends on a
  contemporaneous road-segmentation result just to estimate orientation.
- Brief cadence gaps do not erase calibration progress. They add no samples;
  duplicate source timestamps still cannot establish confidence, and readiness
  expires without new evidence. Changed geometry resets the assistant.
- Camera settings include a fitted live preview and state the requirement
  blocking verification. Verification uses fresh lane observations and measured
  stationary GPS, independently of segmentation cadence. Full ribbon rendering
  still requires fresh segmentation and obstacle detections.
- The projected ribbon uses the verified horizon instead of recomputing pitch
  from every fluctuating lane fit. Navigation reports specific missing support
  (GPS, trained lanes, calibration, segmentation, vehicle detections, direction,
  route match) and retains direction-only guidance near ambiguous turns.
- Paint-only fallback appears as a faint dashed outline, without a filled road
  corridor that might be mistaken for a verified navigation ribbon. Diagnostic
  road spans do not fill foreground holes.
- Saved mount verification is retained only for consistent camera geometry and
  orientation estimates. Mount height remains measured/manual; visual lane
  convergence does not provide a reliable metric mounting height.

## GPS departure from a stop

The filter previously retained stationary state for new ambiguous departure
fixes, publishing fresh zeros until sufficiently confident movement accumulated.
New nonstationary observations invalidate that zero. Ambiguous low speeds become
unavailable; accurate moving fixes resume measurements. The service also clears
its display/history bridge for this explicit contradiction, preventing the old
zero from being reintroduced as a dropout estimate. Accuracy limits are unchanged.

## Validation

- 396 optimized Core tests passed, including new departure-from-stop and
  calibration-with-cadence-gaps/changed-mount regressions.
- Detector structural/source verification passed for day and night, including
  RGB/BGR preprocessing, COCO labels/NMS, weights and PyTorch/trace parity.
- Source RTMDet finds vehicles in four checked camera crops from video15 at
  10, 25, 55 and 60 seconds (2/3/4/2 vehicles; maximum scores .680/.818/.836/.861).
  These are compressed screen-recording crops, not original camera buffers.
- Native Release build succeeded. The finished IPA passed every model SHA256
  check, both detector-weight presence checks, road-model contracts/licenses, ZIP
  CRC checks and version verification (0.4.0/build 35). On-screen revision is N35.

Apple Core ML execution, physical-phone latency, Settings scrolling and actual
road alignment cannot be verified on this Windows/Linux host. The source-frame
test and structural checks do not substitute for a physical-device test.

Build from `ios` with `xtool dev build -c release --ipa`, then from the repo root:

```powershell
python tools/model_migration/verify_model_assets.py --ipa ios/xtool/BrakeCheck.ipa
```
