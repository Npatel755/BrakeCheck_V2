# N30 — Restore the original voice defaults

Restored the original navigation language/default-device-voice selection from commit 88cfdbb and standard speech rate, pitch and volume. Automatic no longer chooses a ranked female voice. Original delivery is the default, and the original ElevenLabs brake-alert path is restored when the connected service is available; its device fallback uses the original language-based voice. Existing backend voices and generation settings are unchanged.

A one-time preferences reset selects Original on upgrading, including previously saved experimental voice choices. Later user choices remain persistent. Settings → Voice keeps manual voices, delivery options and preview, and adds Restore original voice. All N29 swiping and N28 battery improvements remain.

Navigation retains the last measured maneuver, distance and journey metrics during GPS interruptions. The map already uses the last known location and its accuracy circle. Removed the Navigation paused maneuver banner. Route progress and spoken maneuvers still require a fresh reliable fix; retained metrics do not extrapolate movement. Stopping guidance or selecting a different route resets these values through the existing route lifecycle.

Validation: original settings compared with repository history, native release compilation and IPA integrity/revision/executable/model-resource checks. Phone checks required: original navigation voice, backend brake voice when configured, offline device fallback, preview and manual selection after the one-time reset; a temporary GPS interruption and recovery without a paused banner or stale spoken turn.
