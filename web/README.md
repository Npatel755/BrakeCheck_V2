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
2. **Support email.** The site uses `support@brakesense.app`. Set up forwarding to a real inbox first (Cloudflare Email Routing is free), or replace the address everywhere.
3. **Waitlist.** Set `<meta name="waitlist-endpoint">` in `index.html`:
   - `netlify` when hosted on Netlify (the forms already carry `data-netlify`; turn on form detection in the site settings, then redeploy), or
   - a form service URL (Formspree, Basin...). Then add that origin to `connect-src` and `form-action` in `_headers`.
   While it's empty, the form says signups aren't connected instead of pretending to work.
4. **Backend facts in the privacy policy** (Nick): the messaging provider (the policy names Photon), deleting check-in records after 30 days, HTTPS-only in production, and whether optional ElevenLabs voice alerts ship in v1. Update the policy if any differ.
5. **Features shown.** The home page describes the crash check-in with emergency contacts and the four Premium alerts. If either isn't in the first release, remove it from `index.html`, `press.html` and `llms.txt`.
6. **Legal review.** The privacy policy and terms are plain-English drafts written from the app's code, not legal advice. Have someone qualified read them.
7. **Screenshots** come from the Simulator screen tour (`.github/workflows/ios-screens.yml`). Re-shoot them when the UI changes.

## Deploy

Pick one; all are free for a site this size.

- **Netlify:** drag the `web` folder onto https://app.netlify.com/drop, or connect this repo with base directory `web` and no build command. Gives the waitlist without a backend (step 3).
- **Cloudflare Pages:** connect the repo (build command none, output directory `web`), or run `npx wrangler pages deploy web`.
- **GitHub Pages:** publish the `web` folder from a branch. `_headers` is ignored there.

## After App Store approval

- Swap the waitlist forms for Apple's "Download on the App Store" badge (follow Apple's badge guidelines).
- Add the Smart App Banner: `<meta name="apple-itunes-app" content="app-id=YOUR_APP_ID">`.
- Set `LegalLinks.privacyPolicy` in the app to the live privacy URL.

## The drive at the top of the page

`assets/video/drive.webm` / `drive.mp4` (and `drive-poster.webp`) are 9.4 seconds of a real test drive: the screen recording from October 11, 2026 (build N11, 169.0 to 178.4 s into `ScreenRecording_10-11-2026 07-31-46_1.mp4`), approaching a car that brakes for a red light. N11's overlays are removed and the detection boxes redrawn the way the current app draws them (Hackathon Workflow repo, `.tmp/apps/brakecheck/site_footage/clean_n11.py`). Plates are blurred.

The readouts over it are live HTML (`site.js`, `ROWS` and `METER`), drawn in the current camera-screen design (`ContentView` landscape HUD, `RiskPill`) from the values that build measured every 0.2 s. The meter follows the current build's rule (`LeadDangerContinuity`: rises at once, drops after 0.35 s of lower readings, brief misses hold). The caption says it was an early build and the screen is redrawn.

- **Better later:** a new screen recording with the current build, and a passenger-held clip of the phone on its mount with the road behind it. Then the redraw goes away.
- **The illustrated hero** (the drawn road from the first version) is still in the code: `<html data-hero="illustration">`, or add `?hero=illustration` to the URL. Delete whichever path isn't chosen, and the video files if it's the illustration.

## Design rules

- Colours are the app's own tokens from `ios/Sources/BrakeCheck/Views/BrakeTheme.swift`: Polar night in dark mode, Polar day in light mode. Blue carries the page. The risk ramp (blue, amber, orange, red) appears only on the camera screen and in the meter list. The camera band is always dark, as the app's camera page is at night.
- Type: Red Hat Display for headings and Red Hat Text for reading, self-hosted (`assets/fonts`, OFL). Headings at 700-800, not 900, with light negative tracking.
- Real product only: footage, the app's own pills and notice text, and screenshots shown as screenshots. No drawn phone frames, no illustrations standing in for the product, no stock imagery.
- One moving thing: the drive band. It stops when off screen, has a Pause button, and shows the poster frame (High risk, 30 ft, 2.4 s) when the visitor prefers reduced motion.
- No card grids, eyebrow labels, button shadows or alternating section fills. Sections change shape (split, stacked, single column) so the page doesn't repeat one template.
- Copy rules come from the Growth Plan: say "driver aid" and "warning", never claim it prevents crashes, and give a source for every stat (the IIHS figure is about built-in systems). Write as Harrison and Nick, with real details (Hacklanta, the test drives), and no AI tells (`copywriting` skill).

## Regenerating images

The social card (`assets/img/og.png`) and the press-kit logos are rendered from `og/card.html` and `og/logo.html` with `tools/html_shot.py` in the Hackathon Workflow repo. Any headless browser screenshot at 1200 × 630 works too: open `og/card.html` with reduced motion on so the alert frame holds still.
