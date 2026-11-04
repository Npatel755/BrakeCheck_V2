# Review from anywhere

Keep this PC awake and online, with the reviewer and tunnel running.

1. Refresh http://127.0.0.1:8765 on the PC.
2. Choose **Pair phone**, then expand **Use away from home**.
3. Scan the private QR or copy its private link. It works over cellular or another Wi-Fi network.
4. Enter a reviewer name. Phone sessions last seven days; use the original private link again if sign-in expires.

The phone has two photo views, Exclude/Approve/Skip, and Use model lanes. Adopting model lanes starts a correction; Approve saves the result as a correction. Skip persists in Review later. The trophy button opens the leaderboard; the desktop header also has a Leaderboard button.

The phone app's section tabs are **Photos**, **Model errors**, and **Experiments**. Model errors opens the separate validation error reviewer, including missed lanes, false positives, prediction acceptance, label questions and undo. These reviews do not change training labels or measured F1. The tabs use the existing phone session and work in portrait, landscape and the installed review web app. Refresh or reopen the app to load a new navigation version.

The leaderboard counts current human-reviewed photos once, with approved/corrected/excluded breakdowns. Skips are separate. Automatic source validation does not earn human review counts. Names come from sign-in.

## Restart or revoke

- Start or check remote access with `Start-Remote-Review.cmd`. An existing running tunnel is reused.
- Stop it with `tools/dataset_pipeline/Start-Remote-Review.ps1 -Stop`. Local review, imports and training continue.
- On the PC, **Disconnect all phones and change code** also replaces the private remote access key.
- Starting a new tunnel generates a different temporary address. Get the new private link from the PC.

External requests never inherit desktop login, even though the tunnel connects through loopback. Photos and review APIs require authentication. The six-digit LAN code cannot authenticate through the public tunnel. Private links use a random 256-bit key in the browser fragment. Host controls and QR codes stay PC-only. The five-minute retry lockout remains removed.

The official cloudflared Windows binary is pinned to 2026.10.1 and checked against its release SHA-256 before launch. Runtime access keys stay under the ignored dataset reports directory.

[Cloudflare quick tunnel documentation](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

Verification: remote/review/deferral tests plus the real external browser check in `test-results/phone-review-20261019/remote_browser_check.json`. That check signed in over HTTPS, loaded real photo views, opened the leaderboard, and verified host controls were denied. It submitted no production review decisions.
