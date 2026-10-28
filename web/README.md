# BrakeSense website

Static site: plain HTML, one stylesheet, one small script, no build step. Upload the `web/` folder to any static host.

| Page | File |
|---|---|
| Home (waitlist, how it works, Free vs Premium, FAQ) | `index.html` |
| Privacy policy (App Store Connect needs this URL) | `privacy.html` |
| Terms of use (Apple Standard EULA plus our terms) | `terms.html` |
| Support (App Store Connect needs this URL) | `support.html` |
| Press kit | `press.html` |
| Not found | `404.html` |

Also: `robots.txt`, `sitemap.xml`, `llms.txt`, `site.webmanifest`, `_headers` (security and cache headers, read by Netlify and Cloudflare Pages), icons, and `og/` (sources for the social card and logo exports; not linked, `noindex`).

## Preview locally

```
cd web
python -m http.server 8000
```
Then open http://localhost:8000.

## Before going live

1. **Domain.** Every absolute URL uses `https://brakesense.app` (canonical tags, Open Graph, sitemap, robots, llms.txt, JSON-LD). If you pick another domain, find and replace it across `web/`.
2. **Support email.** The site uses `support@brakesense.app`. Forward it to the BrakeSense Gmail with Cloudflare Email Routing (the privacy policy says Cloudflare forwards it and Google stores it), or replace the address everywhere.
3. **Waitlist.** Netlify Forms don't work on Cloudflare Pages. The privacy policy assumes the waitlist lives in the Cloudflare account (a Pages Function writing to D1 or KV). If you use a form service instead, change the waitlist row and bullet in `privacy.html`. Then set `<meta name="waitlist-endpoint">` in `index.html` to:
   - `netlify` when hosted on Netlify (the forms already carry `data-netlify`; turn on form detection in the site settings, then redeploy), or
   - the form's URL. If it's on another origin, add that origin to `connect-src` and `form-action` in `_headers`.
   While it's empty, the form says signups aren't connected instead of pretending to work.
4. **Backend facts in the privacy policy** (Nick). `privacy.html` describes today's single-driver backend (`backend/`, `photon-agent/`). Check that the production backend matches it:
   - The app sends nothing to the server until a contact is added.
   - **Check-in records are deleted after 30 days.** The policy promises this, and the current code keeps them forever.
   - The host and its region. The policy says "our cloud host" and "in the US". Name it once it's picked.
   - The name contacts see in messages comes from the app. Today it's `DRIVER_NAME` on the server.
   - **Trip summaries.** After every trip the app posts minutes, top speed, hard brakes and close calls, and any confirmed contact can text "drive" to get them. Keep it, make it a setting, or drop it. If it goes, delete "After each trip" and its table row in `privacy.html`, and the summary mentions in `index.html` and `llms.txt`.
   - Whether the optional ElevenLabs voice ships in v1.
   - The production server accepts HTTPS only.
   - Photon's terms protect data at least as well as the policy. App Review guideline 5.1.1(i) asks for this.
5. **Features shown.** The Premium list on the home page and in `llms.txt` matches the October 21 decision (Go alerts, full trip history with weekly trends, compact turn banner; Lane Departure and Cut-In aren't in v1). Keep `index.html`, `press.html`, `llms.txt` and `terms.html` in step with the app.
6. **Legal review.** The privacy policy and terms are plain-English drafts written from the app's code, not legal advice. Questions for the reviewer:
   - Governing law and venue, left out of the terms until someone qualified picks them.
   - Whether to add an arbitration clause.
   - The liability cap in terms section 10 (the greater of 12 months of Premium or $50).
   - The Lifetime wording in terms section 5.
   - Whether the check-in's "Send help" answer counts as health data under Washington's My Health My Data Act, which applies at any size.
7. **Screenshots** come from the Simulator screen tour (`.github/workflows/ios-screens.yml`). Re-shoot them when the UI changes.
8. **Links in the app** (before the first App Review submission, not after). Set `LegalLinks.privacyPolicy` to `https://brakesense.app/privacy.html`, and point `LegalLinks.termsOfUse` at `https://brakesense.app/terms.html`, because today it opens Apple's EULA, which leaves out our terms. Guideline 3.1.2 needs both links in the app. The App Store description needs the Terms link too, plus Apple's Standard EULA.
9. **App Privacy answers** in App Store Connect must match `privacy.html`. With emergency contacts on, that's Contacts (contact names and numbers), Name (the name in messages), Precise Location (check-ins), User ID (the phone's key) and Other Data (trip summaries), all for App Functionality, linked to the user and not used for tracking. Check each against Apple's definitions. `PrivacyInfo.xcprivacy` declares no collected data, so update it to match.
10. **The launch email** to the waitlist needs an unsubscribe link and a postal address (CAN-SPAM). A PO box works.

## Deploy

Pick one; all are free for a site this size.

- **Netlify:** drag the `web` folder onto https://app.netlify.com/drop, or connect this repo with base directory `web` and no build command. Gives the waitlist without a backend (step 3).
- **Cloudflare Pages:** connect the repo (build command none, output directory `web`), or run `npx wrangler pages deploy web`.
- **GitHub Pages:** publish the `web` folder from a branch. `_headers` is ignored there.

## After App Store approval

- Swap the waitlist forms for Apple's "Download on the App Store" badge (follow Apple's badge guidelines).
- Add the Smart App Banner: `<meta name="apple-itunes-app" content="app-id=YOUR_APP_ID">`.

## The drive at the top of the page

`assets/video/drive.webm` / `drive.mp4` (and `drive-poster.webp`) are 9.4 seconds of a real test drive: the screen recording from October 11, 2026 (build N11, 169.0 to 178.4 s into `ScreenRecording_10-11-2026 07-31-46_1.mp4`), approaching a car that brakes for a red light. N11's overlays are removed and the detection boxes redrawn the way the current app draws them (Hackathon Workflow repo, `.tmp/apps/brakecheck/site_footage/clean_n11.py`). Plates are blurred.

The readouts over it are live HTML (`site.js`, `ROWS` and `METER`), drawn in the current camera-screen design (`ContentView` landscape HUD, `RiskPill`) from the values that build measured every 0.2 s. The meter follows the current build's rule (`LeadDangerContinuity`: rises at once, drops after 0.35 s of lower readings, brief misses hold). The caption says it was an early build and the screen is redrawn.

- **Better later:** a new screen recording with the current build, and a passenger-held clip of the phone on its mount with the road behind it. Then the redraw goes away.
- The drawn-road illustration from the first version was dropped once the real drive was chosen (2026-10-21); it's in git history (726abe8) if it's ever needed.

## Design rules

- Colours are the app's own tokens from `ios/Sources/BrakeCheck/Views/BrakeTheme.swift`: Polar night in dark mode, Polar day in light mode. Blue carries the page. The risk ramp (blue, amber, orange, red) appears only on the camera screen and in the meter list. The camera band is always dark, as the app's camera page is at night.
- Type: Red Hat Display for headings and Red Hat Text for reading, self-hosted (`assets/fonts`, OFL). Headings at 700-800, not 900, with light negative tracking.
- Real product only: footage, the app's own pills and notice text, and screenshots shown as screenshots. No drawn phone frames, no illustrations standing in for the product, no stock imagery.
- One moving thing: the drive band. It stops when off screen, has a Pause button, and shows the poster frame (High risk, 30 ft, 2.4 s) when the visitor prefers reduced motion.
- No card grids, eyebrow labels, button shadows or alternating section fills. Sections change shape (split, stacked, single column) so the page doesn't repeat one template.
- Copy rules come from the Growth Plan: say "driver aid" and "warning", never claim it prevents crashes, and give a source for every stat (the IIHS figure is about built-in systems). Write as Harrison and Nick, with real details (Hacklanta, the test drives), and no AI tells (`copywriting` skill).

## Regenerating images

The social card (`assets/img/og.png`) and the press-kit logos are rendered from `og/card.html` and `og/logo.html` with `tools/html_shot.py` in the Hackathon Workflow repo. Any headless browser screenshot at 1200 × 630 works too: open `og/card.html` with reduced motion on so the alert frame holds still.
