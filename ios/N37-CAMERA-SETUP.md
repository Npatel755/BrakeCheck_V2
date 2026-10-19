# N37 camera setup

Video17 shows the preview going black around setup presentation/dismissal while
detection overlays continue, and orientation progress disappearing after a lane
gap. Setup now overlays the original preview instead of creating a second live
preview layer. Preview attachment is refreshed on updates and detached on teardown.

Parked setup saves the fixed forward-facing mount and measured lens height with
one acknowledgment, then closes automatically. It needs no lane markings and no
positive stationary GPS fix; fresh reliable movement vetoes the acknowledgment.
The app finishes visual alignment automatically from eight stable distinct lane
observations when a usable road view becomes available later. Pending setup is
shown separately from completed alignment. Saved orientation persists across
missing paint and app restarts; road bends do not overwrite the mount pose.
Explicit reset or camera/height changes invalidate the saved configuration.

Existing live freshness, road-mask, obstacle, route-matching and confidence gates
remain required for the road ribbon. Ordinary turn guidance remains available
while alignment is pending. Physical iPhone testing remains necessary to confirm
preview continuity and alignment behavior.

Validation: 413 Core tests pass, including eight N37 regressions for persistent
orientation, geometry changes, invalid estimates, restoration, missing live
tracking and reliable movement detection. Release IPA build and asset integrity
checks are performed for the N37 download.
