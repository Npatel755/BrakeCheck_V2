# N26 — Dedicated pages and a grouped settings hub

Home now pushes Settings, Emergency contacts, Saved incidents, Trip history, Diagnostics and Replay onto a shared NavigationStack. These are full pages with native Back navigation, rather than pull-up sheets. Home, Camera and Navigate remain the only swipe pages. Removed the separate nested stacks and modal Done actions from those destinations; recording, tracking and the trip model remain shared.

Settings uses grouped category rows on phones and a selected sidebar plus detail pane at widths of 720 points or more. Categories are Appearance, Camera display, Connection, Voice, Safety, Permissions and Try BrakeCheck. Panels, typography, icons and selection accents follow the blue Home palette in both appearances. All existing pairing, connection, voice, impact, permission and demo controls remain, and Camera display uses the same stored preferences as Home. Pairing QR opens full screen; system Messages, sharing and file-selection dialogs retain their native presentation.

Navigation map presentation pauses while a dedicated page covers it and resumes on return. The crash check-in demo returns to the root before presentation; automatic check-ins also clear the page stack. No lead, risk, GPS, lane or persistence algorithms changed.

Validation: native release compilation and IPA integrity, revision, executable identity and eleven model-resource checks. Device checks remain: all six Home destinations and Back; phone category navigation; landscape/sidebar resizing; appearance changes; pairing scan/cancel; replay import/start; trip export; incident details/video; emergency contact composer; check-in interruption while a settings category is open.
