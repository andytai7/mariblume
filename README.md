# mariblume

Storefront for mariblume (@mariblume.germany) - handgemachte Handyketten aus Koeln.

Live: https://andytai7.github.io/mariblume/

## Editing
- Products: `index.html` section "kollektion", one `<article class="card">` per item (name, price, photo path in `assets/products/`).
- PayPal link: search for "PAYPAL-PLATZHALTER" in `index.html`, swap in the real paypal.me URL.
- Impressum: search for "IMPRESSUM-TODO" (legally required in Germany).

## Instagram feed (automated)

The section "Neu auf Instagram" in `index.html` is generated — do not edit it by hand.
Everything between `<!-- IGF:BEGIN -->` and `<!-- IGF:END -->` is rewritten by
`tools/update_instagram.py`, which also downloads the cover images into `assets/ig/`.

Run it:
- **On GitHub**: Actions tab → "Update Instagram feed" → Run workflow. It commits any
  changes and pushes (GitHub Pages then redeploys). Also runs weekly on Mondays.
- **Locally**: `python3 tools/update_instagram.py` (requires Python 3.10+; optional
  `pip install pillow` enables automatic cropping of black letterbox bars), then
  commit and push yourself.

Options: `--max-posts 6` (default), `--username` (default `mariblume.germany`, or repo
variable `IG_USERNAME` in CI).

### Data source
- Without credentials the script reads Instagram's public `web_profile_info`
  endpoint. Free and works for public profiles, but unofficial — Instagram may
  rate-limit or change it (the workflow then fails until rerun/fixed).
- Robust alternative: set repo secret `IG_ACCESS_TOKEN` (Settings → Secrets and
  variables → Actions) to a long-lived Instagram Graph API token. Requires the
  account to be an Instagram Business/Creator account linked to a Facebook Page;
  token needs `instagram_basic` + `pages_show_list` and lasts 60 days before it
  must be refreshed. When the secret exists, the Graph API is used automatically.
