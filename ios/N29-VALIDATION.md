# N29 — Swipe into Navigation and out through its search panel

Restored all three pages to the shared swipe pager: Home, Camera and Navigation. Navigation's destination/search panel participates in normal page swiping, and bottom-bar taps/swipes remain available.

NavigationMapSurface gives the map's single-finger pan and pinch observers priority over the ancestor paging scroll view using failure dependencies. MapKit's pan and pinch gestures can still run simultaneously with the map observers, while the page pager cannot recognize alongside them. The dependencies only apply to touches originating inside the map. Text-field editing, destination buttons and the panel's vertical scrolling retain their native gesture handling. Leaving Navigation dismisses the search keyboard.

Retains N28's battery/UI optimizations and preferred female automatic voice. No Core algorithms changed. Validation: native release compilation and IPA integrity/revision/executable/model-resource checks. Device checks required: Home ↔ Camera ↔ Navigation page swipes, a horizontal swipe over the unfocused destination/search panel, vertical panel scrolling, taps and focused text selection, map panning/pinch/recenter, bottom-bar swipes and rotation.
