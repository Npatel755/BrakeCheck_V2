# N40 camera warning recovery

V20.mp4 shows N39 in live mode filming crash footage on a separate screen. At
about 38 seconds, the app shows a full vehicle box, an approximate 15 ft gap,
6 mph closing speed, 1.7 s closing time, and **High risk**. On the next frame
the vehicle is clipped, the gap measurement stops, and the prior danger-meter
score is briefly held. The phone has no GPS speed in this recording. The
separate screen's impact is not a physical iPhone impact.

The critical warning still depended on a current critical risk; the N39
"RISK UNAVAILABLE" text only described a held meter while current range was
missing. The warning rules were not removed by AR. A coherent fitted camera
closing rate previously needed detector confidence of at least 0.80 to use
the 2-second critical threshold. Below that it used 1.5 seconds, so a fresh
1.7-second estimate could remain High until the target clipped. AR's extra
lane/road inference may also consume processing time, but the recording does
not establish device latency as the cause.

N40 removes that label. A fresh, confirmed camera approach with a coherent
range-rate fit and confidence of at least 0.70 now uses the 2-second warning
threshold. Missing or inconsistent fits and weaker detections retain the
1.5-second threshold. Stationary proximity alone cannot trigger BRAKE NOW.
The SegFormer nominal/fair cadence is reduced to leave more of the shared
inference budget for vehicle detection; AR remains enabled. Camera-based
closing time does not require GPS, although navigation still does.

Core regression coverage checks the 1.7-second confirmed-lead case, missing
confirmation/freshness, inconsistent or missing fits, low confidence, and
stationary gaps. All 414 Core tests passed. The optimized N40 IPA built; its
model and archive checks passed, and its executable has no "RISK UNAVAILABLE"
label. This is not a measured iPhone driving validation. The clipped target
still pauses new numerical estimates; a warning that already fired stays
latched for the configured alert interval.
