# N66 lane model update

N66 uses version 0.4.0, build 66, and preserves N65's camera, trip logging,
navigation, vehicle detection, road model and viewpoint model behavior. The
production Swift changes are confined to lane display decoding and its
confidence threshold.

The lane weights now come from the best epoch-3 post-label-fix checkpoint:
`run-20261020-142147-post-n62-label-fixes/best.pt`, SHA256
`4838156c4bd7c237d9189ff617e150f38d96f5018b48ac53e472ea7d4b23f378`.
Its training manifest is
`7327866bf96b4247cebce245c058b4bb9a72c6a19234a489001d36ff44153ac5`.
The expanded-data reference is still training and has not improved its starting
validation score at packaging time. It is not stopped or automatically
substituted into this app.

The camera display uses recall-budget v4: primary hybrid heads, at least two
positive anchors, mean existence confidence at least 0.70, at least four valid
points, maximum normalized step 0.15, path/chord ratio at most 2, and maximum
turn 150 degrees. The driving corridor retains its original stock span gates.
N65's display fragment splitting, vehicle exclusion, shape filter, smoothing
and capture freshness still apply. These additional presentation filters mean
the dashboard's raw decoder metrics are not the app's measured accuracy.

The selected checkpoint/decoder produced 1,776 true lanes, 237 false lanes and
628 misses on the current 1,586-image validation snapshot: recall 73.88%, F1
80.42%. This was exploratory selection on the same validation set; reserved
tests were not used. The aggregate false-positive budget was 243, but false
alerts on empty-label photos rose from 19 to 22 versus short-span v1. That
budget is a validation selection criterion, not a runtime guarantee. The
calibration manifest is separately recorded as
`487563eebac2fc1a494acc7249316ddd3f7ed8d946f3d5a5b2fb089ae59d8e7d`.

Verification artifacts are in `test-results/n66-ufld-update-20261023/`:
conversion/preprocessing parity on eight real validation frames, comparison
with the original Windows training environment, independent Python-to-Swift
decoder fixtures, release Core test log, native iPhone build log and final IPA
receipt. The original photo `a2d2-018f2db6dd980480` recovers both inner lanes
through the display filters while retaining no stock-gated ego pair. Model
source metadata, checkpoint and resource hashes accompany the package.

The final artifact is `BrakeCheck-N66.ipa` at the main workspace root. N65 and
all earlier IPA files are preserved. Signing/installation and physical iPhone
Core ML execution, alignment, sustained latency and thermal checks remain
device work; no phone installation is performed by packaging.

All 623 release Core tests passed. The native arm64 release build, all three
finished-IPA model verifiers, ZIP CRC, version/revision checks and complete
bundled-resource comparison passed. The 19 detector, road, viewpoint and font
resource files match N65 byte for byte. The IPA is 584,840,795 bytes, SHA256
`ce758e92feeface74fdcf1cc3ecd27293e25d1fd766b1e6fd4eb9e477c1ac247`.
