# Road segmentation candidate check

This is an exploratory comparison of the currently bundled NVIDIA SegFormer B0
Cityscapes checkpoint and Intel `road-segmentation-adas-0001` FP32. It is not a
ground-truth accuracy test or an iPhone performance test.

Current app branch has been restored to UFLD lanes and SegFormer roads. The
Intel recordings below document a prior candidate build, not the current model.

## Inputs and method

- 16 evenly sampled frames from `v19.mp4` and `V20.mp4` in the user's Downloads
  folder. Both are landscape BrakeCheck screen recordings of a driving video.
- The camera preview was cropped from the screen recordings before inference.
  UI labels and bounding boxes remain on top of the footage, so these are not
  equivalent to clean live camera frames.
- SegFormer used the same checkpoint, RGB normalization, and 640×384 input as
  the app's model exporter. Intel used its documented BGR 896×512 input and
  the sum of its road and marking channels as the road mask. Both masks were
  resized to 640×384 for comparison.
- Intel's download was verified against the SHA-384 checksums in its official
  `model.yml`. SegFormer was loaded from the locally cached pinned revision.

## Observations

| Diagnostic | SegFormer | Intel |
| --- | ---: | ---: |
| Mean inference time on this WSL CPU | 258 ms | 16 ms |
| Mean fraction of pixels above 0.5 road confidence | 26.2% | 24.0% |

The two binary masks disagreed on a mean 12.4% of pixels. This is **not** an
accuracy difference: neither model is ground truth. Intel was visually
plausible in several frames, but in `V20_001075.jpg` it omitted substantial
left-side road that SegFormer retained. Other frames show different regions of
disagreement. The speed numbers include framework and preprocessing overhead
on a PC and do not predict iPhone Core ML latency or battery use.

At the user's request, the Intel candidate was promoted to a test build after
this initial comparison. The original SegFormer package is backed up in the
workspace. Labeled road frames, especially intersections, shadows, parked cars,
nighttime, rain, and road markings, are still needed. Core ML on-device output,
latency and thermal validation remain pending; this comparison does not imply
that Intel is more accurate than SegFormer.

## V22 on-device visual review

`V22.mp4` shows a translucent road overlay across much of the pavement in
clear frames. At approximately 10% and 30% of the recording, it largely tracks
the near road. Around 50%, at the brake-alert segment, no cyan mask is visible.
Near 70%, the overlay extends onto the left barrier and has holes in the road;
near 90%, visible road along the right edge is unmasked. This is a visual review
of a screen recording, not a scored accuracy test. There is no active navigation
route visible, so route-to-camera alignment cannot be judged from it.

The app header in this recording says `N42` because the revision label was
hardcoded even in later builds. Subsequent builds read the actual IPA build
number from Info.plist. The candidate remains in test status; V22 does not
justify removing the diagnostic overlay or calling the model validated for AR.

`V23.mp4` shows build N51 on the device across a straight roadway, a close
lead-vehicle scene, and an urban intersection. The cyan mask covers substantial
visible pavement. It is irregular and incomplete around the lead car and road
edge, while the intersection mask spans a broad paved area with the camera
turned sideways. That is road-surface evidence, not evidence of the correct
travel lane. The recording still has no active route overlay to assess route
alignment. The mask status label was placed inside the aspect-filled camera
layer and is cropped out in this recording; a later build anchors it to the
screen instead. Intel remains a test candidate for AR.

`V24.mp4` shows build N52 playing driving footage on a screen. The screen-anchored
status is now visible. The cyan mask covers much of the straight road at the
start and parts of the later urban road, but reported positive-pixel coverage
varies from roughly 1% behind a near full-frame car to 19% on an open road.
Several frames say `alignment rejected`; the raw mask can still be drawn then
because the connected-road scanner did not accept a usable region. At other
times the status says `waiting`, indicating no fresh diagnostic mask. This
recording does not show active route guidance or a live camera view, so it
cannot validate route alignment, camera calibration, GPS behavior, or on-road
mask accuracy. The Intel model remains a test candidate for AR.

Reproduce with:

```bash
python tools/model_migration/evaluate_road_candidates.py \
  /mnt/c/Users/nickp/Downloads/v19.mp4 \
  /mnt/c/Users/nickp/Downloads/V20.mp4 \
  --output tools/model_migration/artifacts/road-comparison-cropped \
  --frames-per-video 8
```

Source: https://github.com/openvinotoolkit/open_model_zoo/blob/master/models/intel/road-segmentation-adas-0001/model.yml
