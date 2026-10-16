# N53 comma.ai + Intel model trial

The candidate IPA uses comma.ai `comma10k-segnet` for lane-marking pixels and
Intel `road-segmentation-adas-0001` for road support. The comma model emits
five-class semantic logits at 512×384; class 1 corresponds to lane markings on
three original comma10k image/mask pairs. A row scanner extracts narrow paint
runs and passes them to BrakeCheck's existing temporally confirmed lane fitter.
The Intel model retains its earlier two-channel road/non-road Core ML contract.

This is a model test, not validated lane-aware AR guidance. Semantic marking
pixels do not tell the app which adjacent lane belongs to the vehicle. Full
camera-aligned guidance still depends on observed paired boundaries, road
support, route alignment and calibration confidence. No road/lane benchmark on
the target iPhone has been completed, and Core ML runtime performance can only
be measured on-device.

Build validation: comma class mapping checked against original masks; two lane
decoder tests pass; both Core ML package input/output contracts and resource
SHA-256 hashes checked. The finished IPA should be checked with
`model_candidates/verify_candidate.py --ipa <path>` before installing.

Bundled camera packages: `CommaLane.mlpackage`, `IntelRoad.mlpackage`.
UFLD/CULane and NVIDIA SegFormer packages are absent from this candidate IPA.
