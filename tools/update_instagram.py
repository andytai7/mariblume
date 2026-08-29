#!/usr/bin/env python3
"""Scrape new Instagram product posts and add them to the shop.

A "new product" is a post by @mariblume.germany whose shortcode is not yet in
`instagram-state.json`. For every new post the script
  1. downloads all of its photos into assets/products/ (next free pNN index,
     black letterbox bars cropped when Pillow is installed),
  2. generates the square 700px thumbnails under assets/products/thumbs/,
  3. prepends a product card to the "Kollektion" grid in index.html (newest
     post first, i.e. on top), renumbers all data-index attributes and updates
     the PRODUCTS array used by the lightbox,
  4. records the post in instagram-state.json.

If there are no new posts, the script changes nothing at all.
On the very first run (no state file) everything currently on Instagram is
assumed to be already curated by hand: the state file is seeded and no
products are added.

Data source: public web_profile_info endpoint by default; when the env var
IG_ACCESS_TOKEN (long-lived token for a Business/Creator account linked to a
Facebook Page) is set, the official Instagram Graph API is used instead.

Usage:  python3 tools/update_instagram.py [--max-posts 12]
"""

import argparse
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GRAPH = "https://graph.facebook.com/v21.0"
WEB_PROFILE = "https://www.instagram.com/api/v1/users/web_profile_info/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
THUMB_SIZE = 700
NAME_MAX = 40
EXT_FOR_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

try:
    from PIL import Image
    import io
except ImportError:
    Image = None


# ---------------------------------------------------------------- fetching

def http_get(url, headers=None, params=None, retries=3):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode("utf-8", "replace")
            if e.code in (400, 401, 403, 404):
                raise SystemExit(f"HTTP {e.code} for {url}\n{body}")
            if attempt == retries:
                raise SystemExit(f"HTTP {e.code} for {url} after {retries} attempts\n{body}")
        except urllib.error.URLError as e:
            if attempt == retries:
                raise SystemExit(f"Network error for {url}: {e}")
        time.sleep(3 * attempt)


def http_get_json(url, headers=None, params=None):
    body, _ = http_get(url, headers=headers, params=params)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise SystemExit(
            f"No JSON from {url} (Instagram likely served a login wall or rate-limit page). "
            "Try again later or configure IG_ACCESS_TOKEN (see README).")


def fetch_graph_api(token, username, limit):
    accounts = http_get_json(f"{GRAPH}/me/accounts", params={
        "fields": "instagram_business_account{id,username}",
        "access_token": token, "limit": 100,
    }).get("data", [])
    ig_id = None
    for page in accounts:
        iba = page.get("instagram_business_account") or {}
        if iba.get("username", "").lower() == username.lower():
            ig_id = iba["id"]
            break
    if ig_id is None:
        ig_id = next((p["instagram_business_account"]["id"] for p in accounts
                      if p.get("instagram_business_account")), None)
    if ig_id is None:
        raise SystemExit("IG_ACCESS_TOKEN given, but no Instagram Business/Creator "
                         "account is linked to a Facebook Page for this token.")

    media = http_get_json(f"{GRAPH}/{ig_id}/media", params={
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,"
                  "children.limit(10){media_type,media_url,thumbnail_url}",
        "access_token": token, "limit": limit,
    }).get("data", [])
    posts = []
    for m in media:
        cover = m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
        children = (m.get("children") or {}).get("data") or []
        urls = [c.get("thumbnail_url") if c.get("media_type") == "VIDEO"
                else c.get("media_url") for c in children]
        urls = [u for u in urls if u] or ([cover] if cover else [])
        if not urls:
            continue
        posts.append({
            "shortcode": m["permalink"].rstrip("/").rsplit("/", 1)[-1],
            "permalink": m["permalink"],
            "caption": m.get("caption") or "",
            "ts": int(datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp()),
            "photo_urls": urls,
        })
    return "graph", posts


def fetch_web_scrape(username, limit):
    data = http_get_json(WEB_PROFILE, params={"username": username},
                         headers={"X-IG-App-ID": "936619743392459",
                                  "Accept": "application/json"})
    user = (data.get("data") or {}).get("user")
    if not user:
        raise SystemExit(f"Profile @{username} not found or not public.")
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges", [])
    posts = []
    for edge in edges[:limit]:
        n = edge["node"]
        cap_edges = (n.get("edge_media_to_caption") or {}).get("edges", [])
        if n["__typename"] == "GraphSidecar":
            urls = [c["node"]["display_url"] for c in
                    (n.get("edge_sidecar_to_children") or {}).get("edges", [])]
        else:
            urls = [n["display_url"]]
        posts.append({
            "shortcode": n["shortcode"],
            "permalink": f"https://www.instagram.com/p/{n['shortcode']}/",
            "caption": cap_edges[0]["node"]["text"] if cap_edges else "",
            "ts": int(n["taken_at_timestamp"]),
            "photo_urls": urls,
        })
    return "web", posts


# ------------------------------------------------------------- images

def crop_black_bars(body, thresh=10, min_frac=0.03):
    """Trim uniform near-black letterbox/pillarbox margins, if any."""
    im = Image.open(io.BytesIO(body))
    gray = im.convert("L")
    px = gray.load()
    w, h = gray.size
    dark_col = lambda x: max(px[x, y] for y in range(0, h, 4)) < thresh
    dark_row = lambda y: max(px[x, y] for x in range(0, w, 4)) < thresh
    l = 0
    while l < w * 0.125 and dark_col(l): l += 1
    r = w
    while r > l and dark_col(r - 1): r -= 1
    t = 0
    while t < h * 0.125 and dark_row(t): t += 1
    b = h
    while b > t and dark_row(b - 1): b -= 1
    if (l < w * min_frac and (w - r) < w * min_frac
            and t < h * min_frac and (h - b) < h * min_frac):
        return im, False
    return im.crop((l, t, r, b)), True


def square_thumb(im):
    w, h = im.size
    s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2)) \
             .resize((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)


def download_product_photos(post, pnn, products_dir, thumbs_dir):
    names = []
    for i, url in enumerate(post["photo_urls"]):
        fname = f"{pnn}-{i:02d}"
        full = next(products_dir.glob(f"{fname}.*"), None)
        if full is None:
            body, headers = http_get(url)
            if len(body) < 1024:
                raise SystemExit(f"Suspiciously small image for post "
                                 f"{post['shortcode']} photo {i} ({len(body)} bytes)")
            im, _cropped = crop_black_bars(body)
            full = products_dir / f"{fname}.jpg"
            im.convert("RGB").save(full, "JPEG", quality=90)
        thumb = thumbs_dir / f"{fname}.jpg"
        if not thumb.exists():
            square_thumb(Image.open(full).convert("RGB")).save(thumb, "JPEG", quality=88)
        names.append(full.name)
    return names


# ------------------------------------------------------------- html

def product_name(post):
    for line in post["caption"].splitlines():
        line = line.strip()
        if line and not line.startswith("#") and len(line) <= NAME_MAX:
            return line
    date = datetime.fromtimestamp(post["ts"], tz=timezone.utc).strftime("%d.%m.%Y")
    return f"Handykette vom {date}"


def render_card(post, name, cover_thumb, n_photos):
    n = html.escape(name)
    name_q = urllib.parse.quote(name)
    link = html.escape(post["permalink"], quote=True)
    mailto = (f"mailto:info@mariblume.de?subject=Anfrage%3A%20{name_q}"
              f"&body=Hallo%20Marianne%2C%0A%0Aich%20interessiere%20mich%20fuer%20die%20{name_q}"
              f".%20Ist%20sie%20noch%20verfuegbar%3F%0A%0AVielen%20Dank%21%0A")
    count = "1 Foto" if n_photos == 1 else f"{n_photos} Fotos"
    return [
        '        <article class="card" data-index="0">',
        f'          <button class="cardimg" type="button" data-index="0" aria-label="Alle Fotos von {n} ansehen">',
        f'            <img src="assets/products/thumbs/{cover_thumb}" alt="Handgefertigte Handykette {n} von mariblume" loading="lazy" decoding="async">',
        f'            <span class="photocount">{count}</span>',
        '          </button>',
        '          <div class="card-body">',
        f'            <h3>{n}</h3>',
        '            <!-- PREIS: Preis hier eintragen, z.B. <p class="price">24,00 &euro;</p> -->',
        '            <p class="price">Preis auf Anfrage</p>',
        f'            <a class="btn" href="{mailto}">Per E-Mail anfragen</a>',
        f'            <a class="btn outline" href="{link}" target="_blank" rel="noopener">Auf Instagram ansehen</a>',
        '          </div>',
        '        </article>',
    ]


def insert_into_html(html_path, cards):
    """cards: list of card-line-blocks, newest first. Prepends them to the grid."""
    text = html_path.read_text(encoding="utf-8")
    k_pos = text.index('id="kollektion"')
    grid_pos = text.index('<div class="grid">', k_pos)
    line_end = text.index("\n", grid_pos) + 1
    insert = "\n".join(line for card in cards for line in card) + "\n"
    text = text[:line_end] + insert + text[line_end:]

    # renumber every data-index (article + its button) sequentially, two per card
    idx = -1
    def repl(m):
        nonlocal idx
        idx += 1
        return f'data-index="{idx // 2}"'
    text = re.sub(r'data-index="\d+"', repl, text)
    html_path.write_text(text, encoding="utf-8")
    return idx + 1


def update_products_array(html_path, new_entries):
    text = html_path.read_text(encoding="utf-8")
    m = re.search(r"const PRODUCTS = (\[.*?\]);", text, re.S)
    if not m:
        raise SystemExit("PRODUCTS array not found in index.html")
    prods = json.loads(m.group(1))
    prods = new_entries + prods
    text = text.replace(m.group(0),
                        "const PRODUCTS = " + json.dumps(prods, ensure_ascii=False) + ";", 1)
    html_path.write_text(text, encoding="utf-8")
    return len(prods)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--username", default=os.environ.get("IG_USERNAME", "mariblume.germany"))
    ap.add_argument("--max-posts", type=int, default=12)
    ap.add_argument("--html", type=Path, default=Path("index.html"))
    ap.add_argument("--products-dir", type=Path, default=Path("assets/products"))
    ap.add_argument("--state-file", type=Path, default=Path("instagram-state.json"))
    args = ap.parse_args()
    thumbs_dir = args.products_dir / "thumbs"

    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    source, posts = (fetch_graph_api(token, args.username, args.max_posts) if token
                     else fetch_web_scrape(args.username, args.max_posts))
    if not posts:
        raise SystemExit(f"No posts returned for @{args.username} (source: {source}).")

    if not args.state_file.exists():
        args.state_file.write_text(json.dumps(
            {p["shortcode"]: {"permalink": p["permalink"], "seeded": True} for p in posts},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"First run: seeded {len(posts)} existing posts into {args.state_file}; "
              "nothing added. Only posts published from now on become products.")
        return

    state = json.loads(args.state_file.read_text(encoding="utf-8"))
    new_posts = [p for p in posts if p["shortcode"] not in state]
    if not new_posts:
        print(f"No new posts for @{args.username} (source: {source}) — nothing to do.")
        return

    if Image is None:
        raise SystemExit("New posts found, but Pillow is not installed "
                         "(pip install pillow) — cannot generate thumbnails.")

    args.products_dir.mkdir(parents=True, exist_ok=True)
    thumbs_dir.mkdir(exist_ok=True)

    next_nn = 1 + max((int(m.group(1)) for f in args.products_dir.glob("p*-*.jpg")
                       for m in [re.match(r"p(\d+)-", f.name)] if m), default=-1)

    # number photos oldest-first so older posts get the lower product index
    for post in reversed(new_posts):
        post["pnn"] = f"p{next_nn:02d}"
        next_nn += 1
        post["files"] = download_product_photos(post, post["pnn"], args.products_dir, thumbs_dir)
        post["name"] = product_name(post)

    cards = [render_card(p, p["name"], p["files"][0], len(p["files"]))
             for p in new_posts]  # newest first
    n_cards = insert_into_html(args.html, cards)
    entries = [{"name": p["name"],
                "photos": [f"assets/products/{fn}" for fn in p["files"]]}
               for p in new_posts]
    n_pd = update_products_array(args.html, entries)
    if n_cards // 2 != n_pd:
        raise SystemExit(f"Mismatch after edit: {n_cards // 2} cards vs {n_pd} PRODUCTS — "
                         "index.html may be inconsistent, please check git diff.")

    for p in new_posts:
        state[p["shortcode"]] = {"permalink": p["permalink"], "name": p["name"],
                                 "product": p["pnn"],
                                 "added": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                                 "source": source}
    args.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    print(f"Added {len(new_posts)} new product(s) from Instagram (source: {source}): "
          + ", ".join(f"{p['name']} ({p['pnn']})" for p in new_posts))


if __name__ == "__main__":
    main()
