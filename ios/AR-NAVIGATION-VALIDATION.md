# Camera navigation validation — October 14, 2026

Implemented trained UFLD ResNet18/CULane lane inference and NVIDIA SegFormer
B0/Cityscapes road inference, with bundled FP16 Core ML packages. Removed the
old YOLOPLane resource from the active app bundle. Added the Camera-tab route
ribbon, forward chevrons, MapKit route-progress projection, mount calibration,
current road clipping, vehicle occlusion and freshness/accuracy gates.

Verified in Ubuntu 24.04 WSL:

- Full `ios/Core` Swift suite: **389 tests passed, zero failures**.
- Camera-navigation suites: **22 tests passed, zero failures**.
  Covers UFLD absence class, anchors/positions, NaNs and occluded
  peaks; straight and turning routes; absent/stale observations; wrong course
  and invalid calibration; occlusion splitting; and smoothing loss/reset. Also
  verifies lane-centered curvature, raw semantic mask holes, visual road warps,
  bounded non-recursive prediction and negative-inference invalidation,
  confidence levels/recovery, thermal/latency suspension, distinct-frame
  calibration estimates, mount verification signatures and GPS course/branch
  disambiguation on parallel/overlapping route legs.
- Complete iOS `xtool dev build --ipa`: **passed**. Final app artifact is
  `ios/xtool/BrakeCheck.ipa`. Existing framework deprecation warnings remain.
- Eager PyTorch / TorchScript parity assertions: **passed** for both export
  wrappers. UFLD's checkpoint hash and every inference weight key were checked.
- Model verifier: **passed** for RGB inputs, exact tensor contracts, source and
  license metadata, resource SHA256 hashes, both packages in the final IPA,
  matching packaged hashes, both model licenses, ZIP CRCs and absence of YOLOP.
- Python export/verifier syntax compilation and `git diff --check`: **passed**.

The NVIDIA road checkpoint has a custom non-commercial research/evaluation
license. This build bundles the original license and checkpoint model card and
records the restriction in model metadata and third-party notices. It is not a
commercial-license replacement for the previous road model.

Not verified here: Apple Core ML prediction execution/parity, model compilation
and cache reuse on an actual iPhone, capture-to-result latency, thermal behavior,
camera mounting/calibration accuracy or physical road alignment. Linux Core ML
Tools reports unavailable Apple runtime proxies and a newer-than-tested PyTorch
version; conversion and structural verification succeeded. This implementation
uses a fixed-mount ground-plane approximation with visual constraints, not a full
ARKit pose/world-tracking pipeline. See `AR-NAVIGATION.md` for usage, architecture,
source provenance and the required physical-device checks.
