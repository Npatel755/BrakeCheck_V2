# N68 filled lane corridor

The requested tunnel is a filled road corridor like the supplied cyan reference,
not a wireframe tunnel or just temporal smoothing. N68 adds this presentation
while retaining N67's Raw/Averaging/Kalman comparison and identical model assets.
No Kalman tuning was changed in this update.

Install `BrakeCheck-N68.ipa` from the main workspace root. In Settings > Camera
display, enable Show lane guides and choose Filled corridor under Lane guide
style (the default). The cyan floor follows ego-left slot 1 and ego-right slot 2,
with yellow/white borders. Lines restores the previous presentation. The status
shows the selected tracking mode and whether both boundaries support a corridor.
The existing fresh accepted corridor can supply a filled fallback when raw
display traces are unavailable; it retains its existing freshness requirement.

`LaneCorridorDisplay` creates separate polygons from the overlapping observed
span of each unambiguous left/right fragment pair. It does not extend endpoints,
join large gaps, use outer neighbors to invent an ego side, or fill crossed and
ambiguous branches. Individual lines remain visible when no paired corridor is
available. The raw-prediction canvas clips fill and strokes around detected
vehicle boxes. The effect is a 2D camera overlay, not calibrated metric 3D.

This presentation is separate from LanePath driving evidence. Lead selection,
collision warnings, calibration, training and bundled weights remain unchanged.
Curvature radius and centimeter deviation from the example are not added: this
change implements its visual corridor, not those calibrated measurements.

All 641 release Core tests pass, including seven new corridor tests covering
curves, support overlap, missing/outer boundaries, freshness, disconnected gaps,
reversed/duplicate points, crossed sides and ambiguous branches. An illustrative
comparison was rendered using geometry exported from the actual Swift core:
`test-results/n68-corridor-20261023/corridor-preview.png`. It is a renderer smoke
preview, not an iPhone screenshot or accuracy measurement.

Build and final IPA validation evidence is under
`test-results/n68-corridor-20261023/`, including source hashes, test/build logs,
package/resource verification and `n68-ipa-receipt.json`. Physical iPhone Core ML
execution, projection alignment and sustained latency still require device tests.
