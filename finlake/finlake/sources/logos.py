"""Company logos, fetched once and cached as files.

A logo is decoration, and this module treats it that way: nothing here can
fail in a way that matters. A company with no logo renders a monogram in the
UI, which is a deliberate design rather than a gap, so there is never a reason
to retry hard, block a page, or let a dead icon service affect anything real.

WHERE THEY COME FROM. Each company's own website, which the profile table
already stores — so this is a favicon lookup, not a logo database. The site's
own `apple-touch-icon` and two free keyless icon services are tried, and the
BEST result wins rather than the first: resolution is the only property that
cannot be recovered afterwards, and the candidates differ enormously (Agilent
serves a 16px favicon; Microsoft serves 128px). A miss is recorded so it is
not retried on every pass — logos do not appear retroactively.

WHY THEY ARE NORMALISED, AND WHY THAT MEANS SCALING UP. Favicons arrive as
ICO, JPEG or PNG, square or not, 16px or 512px, with margins of their
designer's choosing. Every one is trimmed to its actual artwork and then
scaled — up as well as down — to fill a square canvas of a known size, aspect
ratio preserved and nothing cropped.

The upward half of that was missing at first, and it was the visible bug:
`Image.thumbnail()` only ever shrinks, so a 16px favicon was pasted unresized
into the middle of a 128px canvas and rendered at an eighth of the tile. 250
of 496 logos filled under 55% of theirs. A tile is only a consistent frame if
what goes in it is consistently sized; otherwise it is just a box that makes
some logos look deliberately tiny.

WHY THE STORED IMAGE IS TRANSPARENT, NOT WHITE-BACKED. The UI puts each logo
on a light tile, because a dark wordmark on a navy page is invisible and so is
a white one — but that tile belongs to the theme, not to the data. Baking a
background into the file would make these unusable against any other surface.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

from .. import config

# The stored master. 128px is enough for a 40px header logo on a retina
# display, and small enough that 500 of them are a few megabytes on disk.
LOGO_SIZE = 128

# A floor on the RESPONSE, only to skip an empty or truncated body. It is
# deliberately far below any real icon: judging artwork by byte count rejects
# a legitimate flat-colour mark that simply compresses well, which is most of
# them. The real test is MIN_ARTWORK_PX, applied after decoding — what the
# image IS, rather than how many bytes it took to say it.
MIN_BYTES = 40

# Below this the response is an artifact rather than a logo: a 1x1 tracking
# pixel, or a spacer image standing in for a missing icon.
MIN_ARTWORK_PX = 8


def logo_dir() -> Path:
    return config.DATA_DIR / "logos"


def logo_path(ticker: str) -> Path | None:
    """The cached logo file, or None if there isn't one.

    A `.miss` marker means "asked, nothing there" — distinct from "never
    asked", which is what an absent file means. The caller does not care, but
    the fetcher does.
    """
    path = logo_dir() / f"{ticker.upper()}.png"
    return path if path.exists() else None


def _miss_path(ticker: str) -> Path:
    return logo_dir() / f"{ticker.upper()}.miss"


def has_been_tried(ticker: str) -> bool:
    return logo_path(ticker) is not None or _miss_path(ticker).exists()


def _domain(website: str | None) -> str | None:
    if not website:
        return None
    raw = str(website).strip()
    if not raw:
        return None
    if "//" not in raw:
        raw = f"https://{raw}"
    host = urlparse(raw).netloc.lower()
    return host or None


def _domain_variants(domain: str) -> list[str]:
    """The host as stored, then the forms an icon service is likelier to know.

    Worth doing because the stored website is whatever the company put on its
    investor page, and the icon services index one canonical host. Smucker's
    is registered under `jmsmucker.com` but stored as `www.jmsmucker.com`, and
    Xcel's is stored as `www.my.xcelenergy.com` — a subdomain of a subdomain.
    Both resolve once the prefix is dropped, and two more logos for one extra
    request on a miss is a good trade.
    """
    variants = [domain]
    if domain.startswith("www."):
        variants.append(domain[4:])
    # The registrable domain, for a stored host several levels deep.
    parts = [p for p in domain.split(".") if p]
    if len(parts) > 2:
        variants.append(".".join(parts[-2:]))
    seen, out = set(), []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            out.append(variant)
    return out


def _sources(domain: str) -> list[str]:
    """Every URL to try, best first.

    Google's service returns a real 404 for a domain it has nothing for, so a
    miss is detectable — rather than arriving as a generic globe, which would
    give every unknown company the same icon and look like a bug.

    ORDER IS ABOUT LATENCY, NOT PREFERENCE — the best result wins regardless
    of where it came from, so the only thing sequence controls is how long a
    company takes. The two icon services are CDN-backed and answer in
    milliseconds; `apple-touch-icon` goes to the company's own web server,
    which frequently 404s slowly or does not respond at all. Asking it first
    made a full sweep pay a timeout for every company whose site lacks one,
    which was most of them.

    So the cheap sources go first, and the search stops as soon as one of
    them is sharp enough (`GOOD_ENOUGH_PX`). The site is only reached for the
    minority whose favicon is genuinely too small — exactly the names that
    stand to gain from a 180px touch icon.
    """
    out = []
    for variant in _domain_variants(domain):
        # Carries the real brand asset for many companies rather than a
        # favicon — often the vector original, which rasterises at any size.
        # Agilent's favicon is 16px everywhere else and an SVG here, and
        # Agilent is the first ticker in the list.
        out.append(f"https://unavatar.io/{variant}?fallback=false")
        out.append(
            f"https://www.google.com/s2/favicons?domain={variant}"
            f"&sz={LOGO_SIZE}")
        out.append(f"https://icons.duckduckgo.com/ip3/{variant}.ico")
    for variant in _domain_variants(domain):
        out.append(f"https://{variant}/apple-touch-icon.png")
    return out


# Rendered size for a vector source. Larger than LOGO_SIZE so the raster is
# downsampled into the canvas rather than fitted exactly — downsampling is
# what makes an edge look clean.
SVG_RENDER_PX = 512


def _rasterise_svg(raw: bytes) -> bytes | None:
    """An SVG to PNG bytes, or None if SVG support is not installed.

    Worth the dependency because it is the difference between a sharp mark
    and a blurred one at any size: a 16px favicon upscaled to fill the tile
    can only ever be soft, and no processing puts detail back. A vector
    source has no such ceiling.

    Degrades to None rather than raising — a company whose only asset is an
    SVG simply keeps whatever raster source came second.
    """
    try:
        import io as _io

        from reportlab.graphics import renderPM
        from svglib.svglib import svg2rlg

        drawing = svg2rlg(_io.BytesIO(raw))
        if drawing is None or not drawing.width or not drawing.height:
            return None
        scale = SVG_RENDER_PX / max(drawing.width, drawing.height)
        drawing.scale(scale, scale)
        drawing.width *= scale
        drawing.height *= scale
        return renderPM.drawToString(drawing, fmt="PNG")
    except Exception:
        return None


def _is_svg(raw: bytes) -> bool:
    head = raw[:400].lstrip()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml")
                                        and b"<svg" in raw[:2000])


# Artwork at least this wide (in the stored canvas) is good enough to stop
# looking for a better source. Below it, the remaining candidates are tried in
# case one of them has a larger original — resolution is the one property that
# cannot be recovered by processing.
GOOD_ENOUGH_PX = int(LOGO_SIZE * 0.75)


def _artwork_box(image):
    """The bounding box of the actual mark, ignoring its margin.

    Favicons routinely ship with padding baked in — sometimes transparent,
    sometimes a solid field the same colour as the corners. Left in place it
    is indistinguishable from artwork, so a logo with a generous margin
    renders half the size of one without, in the same tile, and the row stops
    looking deliberate.
    """
    from PIL import Image, ImageChops

    alpha = image.getchannel("A")
    if alpha.getextrema()[0] < 255:          # has real transparency
        return alpha.getbbox()

    # Fully opaque: trim a uniform border instead, judged from the corner.
    background = Image.new("RGBA", image.size, image.getpixel((0, 0)))
    difference = ImageChops.difference(image.convert("RGB"),
                                       background.convert("RGB"))
    return difference.getbbox() or image.getbbox()


def _normalise(raw: bytes) -> bytes | None:
    """Any icon format -> a square transparent PNG with the art centred."""
    if _is_svg(raw):
        rendered = _rasterise_svg(raw)
        if rendered is None:
            return None
        raw = rendered

    try:
        from PIL import Image

        image = Image.open(io.BytesIO(raw))
        # An ICO holds several resolutions; Pillow opens the first, which is
        # often the smallest. Ask for the largest it has.
        if getattr(image, "n_frames", 1) > 1 and image.format == "ICO":
            sizes = getattr(image, "ico", None)
            if sizes is not None:
                try:
                    image = sizes.getimage((LOGO_SIZE, LOGO_SIZE))
                except Exception:
                    pass
        image = image.convert("RGBA")

        # Trim the mark's own margin, so every logo is measured by its
        # artwork rather than by whatever padding its designer shipped.
        box = _artwork_box(image)
        if box:
            image = image.crop(box)
        if not image.width or not image.height:
            return None

        # SCALE TO FIT — UP AS WELL AS DOWN. `thumbnail()` only ever shrinks,
        # which is the bug this replaces: Agilent's favicon is 16x16, so it
        # was pasted unresized into the middle of a 128px canvas and rendered
        # at an eighth of the tile. 250 of 496 logos filled under 55% of
        # theirs, and the tile stopped being a consistent frame — which was
        # the entire reason for having one.
        #
        # Aspect ratio is preserved and nothing is cropped: a wordmark
        # squashed to fill a square is no longer the company's logo.
        scale = min(LOGO_SIZE / image.width, LOGO_SIZE / image.height)
        target = (max(1, round(image.width * scale)),
                  max(1, round(image.height * scale)))
        image = image.resize(target, Image.LANCZOS)

        canvas = Image.new("RGBA", (LOGO_SIZE, LOGO_SIZE), (0, 0, 0, 0))
        canvas.paste(image,
                     ((LOGO_SIZE - image.width) // 2,
                      (LOGO_SIZE - image.height) // 2),
                     image)
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        return None


def _source_artwork_px(raw: bytes) -> int:
    """The size of the real mark in a raw response, for comparing candidates.

    Measured after trimming, so a 128px canvas holding a 16px icon scores 16
    — which is the whole point: the response that is nominally largest is
    routinely not the one with the most artwork in it.
    """
    if _is_svg(raw):
        rendered = _rasterise_svg(raw)
        if rendered is None:
            return 0
        raw = rendered

    try:
        from PIL import Image

        image = Image.open(io.BytesIO(raw)).convert("RGBA")
        box = _artwork_box(image)
        if not box:
            return 0
        return max(box[2] - box[0], box[3] - box[1])
    except Exception:
        return 0


def fetch_logo(ticker: str, website: str | None, *,
               force: bool = False) -> Path | None:
    """Fetch and cache one company's logo. Never raises."""
    import requests

    ticker = ticker.upper()
    logo_dir().mkdir(parents=True, exist_ok=True)

    if not force and has_been_tried(ticker):
        return logo_path(ticker)

    domain = _domain(website)
    if not domain:
        _miss_path(ticker).touch()
        return None

    # THE BEST CANDIDATE, NOT THE FIRST ONE THAT ANSWERS. Resolution is the
    # only property that cannot be recovered afterwards: a 16px source
    # upscaled to fill the tile is soft, and no amount of processing puts
    # detail back. So the candidates are compared on how much ARTWORK they
    # carry — measured after trimming, because the nominally largest response
    # is routinely a big canvas holding a small icon.
    best: tuple[int, bytes] | None = None
    for url in _sources(domain):
        # A company's own web server gets a short leash — it is the slow
        # source and the optional one, and a sweep of 500 names cannot afford
        # to wait ten seconds each on the ones that will not answer.
        timeout = 4 if "/apple-touch-icon" in url else 10
        try:
            response = requests.get(
                url, timeout=timeout, allow_redirects=True,
                headers={"User-Agent": config.SEC_USER_AGENT})
        except Exception:
            continue
        if response.status_code != 200 or len(response.content) < MIN_BYTES:
            continue

        artwork = _source_artwork_px(response.content)
        if artwork < MIN_ARTWORK_PX:
            continue
        if best is None or artwork > best[0]:
            best = (artwork, response.content)
        if best[0] >= GOOD_ENOUGH_PX:
            break        # already sharp enough; stop spending requests

    if best is not None:
        png = _normalise(best[1])
        if png is not None:
            path = logo_dir() / f"{ticker}.png"
            path.write_bytes(png)
            _miss_path(ticker).unlink(missing_ok=True)
            return path

    # Recorded, so the next pass does not spend two requests learning the
    # same thing. Logos do not appear retroactively.
    _miss_path(ticker).touch()
    return None


def load_logos(conn: sqlite3.Connection, tickers: list[str] | None = None, *,
               force: bool = False, limit: int | None = None) -> dict[str, int]:
    """Fetch logos for companies that don't have one yet.

    Reads each company's website from the profile table the market loader
    already populates, so this costs no extra provider calls.
    """
    rows = conn.execute(
        "SELECT ticker, website FROM profile WHERE website IS NOT NULL"
    ).fetchall()
    wanted = {t.upper() for t in tickers} if tickers else None

    counts = {"fetched": 0, "already": 0, "missing": 0}
    for row in rows:
        ticker = str(row["ticker"]).upper()
        if wanted is not None and ticker not in wanted:
            continue
        if not force and has_been_tried(ticker):
            counts["already"] += 1
            continue
        if fetch_logo(ticker, row["website"], force=force):
            counts["fetched"] += 1
        else:
            counts["missing"] += 1
        if limit and counts["fetched"] + counts["missing"] >= limit:
            break
    return counts


def coverage() -> dict[str, int]:
    """How many logos are cached, for the data-health readout."""
    directory = logo_dir()
    if not directory.exists():
        return {"logos": 0, "misses": 0}
    return {
        "logos": len(list(directory.glob("*.png"))),
        "misses": len(list(directory.glob("*.miss"))),
    }
