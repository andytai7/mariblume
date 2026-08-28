#!/usr/bin/env python3
"""Update the 'Neu auf Instagram' section of index.html with the latest posts.

Data sources (tried in order):
  1. Instagram Graph API — if env IG_ACCESS_TOKEN is set (requires an Instagram
     Business/Creator account linked to a Facebook Page; token = long-lived user
     access token with instagram_basic + pages_show_list permissions).
  2. Public web_profile_info endpoint — no credentials, works for public
     profiles, but unofficial and rate-limited.

The script downloads one cover image per post into assets/ig/, rewrites the
HTML between the <!-- IGF:BEGIN --> / <!-- IGF:END --> markers in index.html,
writes assets/ig/feed.json, and removes stale covers. Idempotent: a second run
with unchanged posts leaves the working tree untouched.

Usage:  python3 tools/update_instagram.py [--max-posts 6]
"""

import argparse
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
try:
    from PIL import Image
    import io
except ImportError:
    Image = None

GRAPH = "https://graph.facebook.com/v21.0"
WEB_PROFILE = "https://www.instagram.com/api/v1/users/web_profile_info/"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
BEGIN = "<!-- IGF:BEGIN -->"
END = "<!-- IGF:END -->"
EXT_FOR_MIME = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
CAPTION_MAX = 140


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
            "Try again later or configure IG_ACCESS_TOKEN (see README)."
        )


def fetch_graph_api(token, username, limit):
    accounts = http_get_json(f"{GRAPH}/me/accounts", params={
        "fields": "instagram_business_account{id,username}",
        "access_token": token,
        "limit": 100,
    }).get("data", [])
    ig_id = None
    for page in accounts:
        iba = page.get("instagram_business_account") or {}
        if iba.get("username", "").lower() == username.lower():
            ig_id = iba["id"]
            break
    if ig_id is None:  # fall back to the first linked account, if any
        ig_id = next((p["instagram_business_account"]["id"] for p in accounts
                      if p.get("instagram_business_account")), None)
    if ig_id is None:
        raise SystemExit("IG_ACCESS_TOKEN given, but no Instagram Business/Creator "
                         "account is linked to a Facebook Page for this token.")

    data = http_get_json(f"{GRAPH}/{ig_id}/media", params={
        "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp",
        "access_token": token,
        "limit": limit,
    }).get("data", [])
    posts = []
    for m in data:
        image_url = m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
        if not image_url:
            continue
        ts = int(datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").timestamp())
        posts.append({
            "shortcode": m["permalink"].rstrip("/").rsplit("/", 1)[-1],
            "permalink": m["permalink"],
            "caption": m.get("caption") or "",
            "alt": None,
            "ts": ts,
            "image_url": image_url,
        })
    return "graph", posts


def fetch_web_scrape(username, limit):
    data = http_get_json(WEB_PROFILE, params={"username": username},
                         headers={"X-IG-App-ID": "936619743392459", "Accept": "application/json"})
    user = (data.get("data") or {}).get("user")
    if not user:
        raise SystemExit(f"Profile @{username} not found or not public.")
    edges = (user.get("edge_owner_to_timeline_media") or {}).get("edges", [])
    posts = []
    for edge in edges[:limit]:
        n = edge["node"]
        cap_edges = (n.get("edge_media_to_caption") or {}).get("edges", [])
        posts.append({
            "shortcode": n["shortcode"],
            "permalink": f"https://www.instagram.com/p/{n['shortcode']}/",
            "caption": cap_edges[0]["node"]["text"] if cap_edges else "",
            "alt": n.get("accessibility_caption"),
            "ts": int(n["taken_at_timestamp"]),
            "image_url": n["display_url"],
        })
    return "web", posts


def display_caption(caption):
    for line in caption.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return (line[:CAPTION_MAX - 1] + "…") if len(line) > CAPTION_MAX else line
    return ""


def download_cover(post, assets_dir):
    existing = [p for p in assets_dir.glob(f"{post['shortcode']}.*") if p.is_file()]
    if existing:
        return existing[0].name
    body, headers = http_get(post["image_url"])
    ext = EXT_FOR_MIME.get((headers.get("Content-Type") or "").split(";")[0].strip(), ".jpg")
    if len(body) < 1024:
        raise SystemExit(f"Suspiciously small image for post {post['shortcode']} ({len(body)} bytes)")
    if Image is not None:
        body = crop_black_bars(body)
        ext = ".jpg"
    name = f"{post['shortcode']}{ext}"
    (assets_dir / name).write_bytes(body)
    return name


def crop_black_bars(body, thresh=10, min_frac=0.03):
    """Trim uniform near-black letterbox/pillarbox margins, if any."""
    im = Image.open(io.BytesIO(body))
    a = im.convert("L")
    px = a.load()
    w, h = a.size
    dark_col = lambda x: max(px[x, y] for y in range(0, h, 4)) < thresh
    dark_row = lambda y: max(px[x, y] for x in range(0, w, 4)) < thresh
    l = 0
    while l < w * min_frac * 4 and dark_col(l):
        l += 1
    r = w
    while r > l and dark_col(r - 1):
        r -= 1
    t = 0
    while t < h * min_frac * 4 and dark_row(t):
        t += 1
    b = h
    while b > t and dark_row(b - 1):
        b -= 1
    box = (l, t, r, b)
    if box == (0, 0, w, h):
        return body
    if box[0] < w * min_frac and (w - box[2]) < w * min_frac \
            and box[1] < h * min_frac and (h - box[3]) < h * min_frac:
        return body  # bars too thin to crop reliably
    out = io.BytesIO()
    im.crop(box).save(out, format="JPEG", quality=90)
    print(f"cropped black bars: {w}x{h} -> {r - l}x{b - t}")
    return out.getvalue()


def render_cards(posts):
    lines = []
    for p in posts:
        date = datetime.fromtimestamp(p["ts"], tz=timezone.utc).strftime("%d.%m.%Y")
        caption = html.escape(display_caption(p["caption"]), quote=False)
        alt = html.escape(p["alt"] or f"Instagram-Foto vom {date}", quote=True)
        link = html.escape(p["permalink"], quote=True)
        src = html.escape(p["image_file"], quote=True)
        lines += [
            f'        <article class="card igcard">',
            f'          <a class="igcardimg" href="{link}" target="_blank" rel="noopener">',
            f'            <img src="assets/ig/{src}" alt="{alt}" loading="lazy" decoding="async">',
            f'          </a>',
            f'          <div class="card-body">',
            f'            <p class="igdate">{date}</p>',
        ]
        if caption:
            lines.append(f'            <p class="igcaption">{caption}</p>')
        lines += [
            f'            <a class="btn outline" href="{link}" target="_blank" rel="noopener">Auf Instagram ansehen</a>',
            f'          </div>',
            f'        </article>',
        ]
    return lines


def replace_feed(html_path, card_lines):
    text = html_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    try:
        bi = next(i for i, line in enumerate(lines) if BEGIN in line)
        ei = next(i for i, line in enumerate(lines[bi + 1:], start=bi + 1) if END in line)
    except StopIteration:
        raise SystemExit(f"Markers {BEGIN} / {END} not found in {html_path}")
    html_path.write_text("\n".join(lines[:bi + 1] + card_lines + lines[ei:]), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--username", default=os.environ.get("IG_USERNAME", "mariblume.germany"))
    ap.add_argument("--max-posts", type=int, default=6)
    ap.add_argument("--html", type=Path, default=Path("index.html"))
    ap.add_argument("--assets-dir", type=Path, default=Path("assets/ig"))
    args = ap.parse_args()

    args.assets_dir.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if token:
        source, posts = fetch_graph_api(token, args.username, args.max_posts)
    else:
        source, posts = fetch_web_scrape(args.username, args.max_posts)
    if not posts:
        raise SystemExit(f"No posts returned for @{args.username} (source: {source}).")

    for p in posts:
        p["image_file"] = download_cover(p, args.assets_dir)

    # Remove covers whose posts dropped out of the feed window.
    keep = {p["image_file"] for p in posts} | {"feed.json"}
    removed = [f.name for f in args.assets_dir.iterdir() if f.is_file() and f.name not in keep]
    for name in removed:
        (args.assets_dir / name).unlink()

    manifest = {
        "source": source,
        "username": args.username,
        "updated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "posts": [{k: p[k] for k in ("shortcode", "permalink", "ts", "image_file",
                                     "caption", "alt")} for p in posts],
    }
    (args.assets_dir / "feed.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    replace_feed(args.html, render_cards(posts))
    print(f"Instagram feed updated: {len(posts)} posts (source: {source})"
          + (f", removed: {', '.join(removed)}" if removed else ""))


if __name__ == "__main__":
    main()
