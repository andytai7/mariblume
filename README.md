# mariblume

Storefront for mariblume (@mariblume.germany) - handgemachte Handyketten aus Koeln.

Live: https://andytai7.github.io/mariblume/

## Editing
- Products: `index.html` section "kollektion", one `<article class="card">` per item (name, price, photo path in `assets/products/`).
- PayPal link: search for "PAYPAL-PLATZHALTER" in `index.html`, swap in the real paypal.me URL.
- Impressum: search for "IMPRESSUM-TODO" (legally required in Germany).

## New products from Instagram (automated)

`tools/update_instagram.py` scrapes the latest posts of @mariblume.germany and turns
every **new** post (shortcode not yet in `instagram-state.json`) into a product card at
the **top of "Kollektion"**: it downloads all photos of the post into
`assets/products/` under the next free `pNN` index, builds the square thumbnails,
prepends the card, renumbers the lightbox indices, and updates the PRODUCTS array.
Post captions supply the name when their first line is usable; otherwise the post date
is used ("Handykette vom DD.MM.YYYY") — rename anything in `index.html` afterwards.

**No new posts → nothing is changed or committed.**
First run ever only seeds `instagram-state.json` (everything already on Instagram is
assumed to be curated by hand); from then on only future posts are added.
Limitation: only the 12 most recent posts are checked, so don't let more than 12 new
posts pile up between runs (weekly CI run covers this comfortably).

Run it:
- **On GitHub**: Actions tab → "New Instagram products" → Run workflow. It commits and
  pushes any additions (GitHub Pages then redeploys). Also runs weekly on Mondays.
- **Locally**: `pip install pillow` once, then `python3 tools/update_instagram.py`,
  review the diff, commit and push.

Options: `--max-posts 12` (default = what Instagram returns in one request),
`--username` (default `mariblume.germany`, or repo variable `IG_USERNAME` in CI).
To re-import a post, delete its shortcode from `instagram-state.json` and delete the
matching `assets/products/pNN-*` files + its card from `index.html`, then run again.

### Data source
- Without credentials the script reads Instagram's public `web_profile_info`
  endpoint. Free and works for public profiles, but unofficial — Instagram may
  rate-limit or change it (the workflow then fails until rerun/fixed).
- Robust alternative: set repo secret `IG_ACCESS_TOKEN` (Settings → Secrets and
  variables → Actions) to a long-lived Instagram Graph API token. Requires the
  account to be an Instagram Business/Creator account linked to a Facebook Page;
  token needs `instagram_basic` + `pages_show_list` and lasts 60 days before it
  must be refreshed. When the secret exists, the Graph API is used automatically.
