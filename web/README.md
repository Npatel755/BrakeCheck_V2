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

## Design rules

- Colours are the app's own tokens from `ios/Sources/BrakeCheck/Views/BrakeTheme.swift`: Polar night in dark mode, Polar day in light mode. Blue carries the page. The risk ramp (blue, amber, orange, red) appears only where a warning is being explained. Amber is reserved for the brake-lamp icon.
- Type: Red Hat Display for headings (tight tracking, like the app's logo) and Red Hat Text for reading. Both are self-hosted (`assets/fonts`, OFL), so no visitor data goes to Google.
- One moving thing: the hero illustration. It mirrors the app's real risk meter (`RiskPill`, `DangerSteps`), target box and metrics, stops when off screen, has a pause button, and shows a still frame when the visitor prefers reduced motion. Nothing else animates on scroll.
- A painted lane line joins the steps of a trip. No card grids, no all-caps eyebrow labels, no stock imagery.
- Copy rules come from the Growth Plan: say "driver aid" and "warning", never claim it prevents crashes, and give a source for every stat (the IIHS figure is about built-in systems).

## Regenerating images

The social card (`assets/img/og.png`) and the press-kit logos are rendered from `og/card.html` and `og/logo.html` with `tools/html_shot.py` in the Hackathon Workflow repo. Any headless browser screenshot at 1200 × 630 works too: open `og/card.html` with reduced motion on so the alert frame holds still.
