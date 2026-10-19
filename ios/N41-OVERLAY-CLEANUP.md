# N41 camera overlay cleanup

In v21.mp4 (N40), the camera draws dashed road guides around 25–27 seconds
and a dashed retained vehicle box at about 31 seconds. These are app overlays,
not part of the road footage. The video also shows camera gap and closing-time
readouts intermittently pausing while a vehicle remains visible. The phone is
filming driving footage on another screen and has no GPS fix, so this recording
cannot validate route-to-camera alignment or prove the cause of detector gaps.

The camera now defaults to a clean view. The optional **Show lane guides**
setting uses a new key, so old installations with the former road-and-lane
overlay enabled start with guides hidden. When turned on, it draws only solid
neural/hybrid lane boundaries. Provisional paint-only lanes and SegFormer road
outlines are no longer drawn. Held detections and coasting target boxes are
hidden instead of drawn with dashed strokes; manual selection still has a
visible solid outline. Detection, road/lane inference, collision risk and AR
route alignment continue to use the same underlying measurements.

Removing the road outline also removes the corresponding per-frame road
display publication to the main UI model. Lane display publication is skipped
while the optional guide is off. This reduces UI work without changing the
safety pipeline. An unavailable closing-time estimate now says "Sampling
motion" while camera range motion is settling, instead of incorrectly saying
"Needs GPS". The existing Settings navigation hook already releases camera AR
presentation demand while that screen is visible. V21's BRAKE ALERT persists
after measurements disappear
because a previously triggered warning is held; the recording does not prove
an incorrect current warning. Numeric gaps still pause when detections or
range geometry are not trustworthy.

Validation: 414 Core tests passed; the release iPhone app compiled and was
packaged as `xtool/BrakeCheck-N41.ipa` (build 41). The IPA passed archive CRC
and bundled detector, UFLD, SegFormer, and viewpoint model hash checks. The
changes still need an on-device drive with a live GPS fix to assess frame
cadence and AR route alignment.
