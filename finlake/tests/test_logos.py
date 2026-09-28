"""Company logo fetching and normalisation.

A logo is decoration, so the properties worth pinning are the ones that stop
it becoming a liability: it must never raise, never retry a known miss
forever, and never hand the UI an image that will render as a ragged
different-sized mark in a column of others.

No network. Every fetch is stubbed.
"""

import io
import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_logos_")

import pytest  # noqa: E402

from finlake.sources import logos  # noqa: E402


def _png(width: int, height: int, colour=(200, 30, 30, 255)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, status: int, content: bytes):
        self.status_code = status
        self.content = content


@pytest.fixture(autouse=True)
def clean_dir():
    directory = logos.logo_dir()
    if directory.exists():
        for path in directory.iterdir():
            path.unlink()
    yield


def _stub(monkeypatch, responses):
    """Serve `responses` in order, one per requested URL."""
    calls = []

    def fake_get(url, **_kw):
        calls.append(url)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    import requests

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def test_a_logo_is_stored_square_whatever_shape_it_arrived(monkeypatch):
    """Favicons arrive 16px or 512px, square or a wide wordmark. Rendered raw
    they are a ragged row of different-sized marks; the table only lines up if
    every one occupies the same box."""
    from PIL import Image

    _stub(monkeypatch, [_Response(200, _png(240, 60))])
    path = logos.fetch_logo("WIDE", "https://example.com")

    assert path is not None
    image = Image.open(path)
    assert image.size == (logos.LOGO_SIZE, logos.LOGO_SIZE)


def test_a_small_source_is_scaled_UP_to_fill_the_tile(monkeypatch):
    """THE BUG THAT SHIPPED. `Image.thumbnail()` only ever shrinks, so a 16x16
    favicon was pasted unresized into the middle of a 128px canvas and
    rendered at an eighth of the tile — Agilent's was the one that got
    noticed, and 250 of 496 logos filled under 55% of theirs.

    The tile only works as a consistent frame if every mark is scaled to it.
    """
    from PIL import Image

    _stub(monkeypatch, [_Response(200, _png(16, 16))])
    path = logos.fetch_logo("TINY", "https://example.com")
    assert path is not None, "a real 16px favicon was rejected outright"

    image = Image.open(path).convert("RGBA")
    box = logos._artwork_box(image)
    filled = max(box[2] - box[0], box[3] - box[1]) / image.width
    assert filled > 0.95, (
        f"a 16px source filled only {filled:.0%} of the tile — thumbnail() "
        f"does not upscale")


def test_a_baked_in_margin_is_trimmed_before_scaling(monkeypatch):
    """Favicons ship with padding of their designer's choosing. Left in place
    it is indistinguishable from artwork, so a logo with a generous margin
    renders half the size of one without, in the same tile."""
    from PIL import Image

    padded = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    padded.paste(Image.new("RGBA", (32, 32), (10, 120, 220, 255)), (48, 48))
    buffer = io.BytesIO()
    padded.save(buffer, format="PNG")

    _stub(monkeypatch, [_Response(200, buffer.getvalue())])
    path = logos.fetch_logo("PADDED", "https://example.com")

    image = Image.open(path).convert("RGBA")
    box = logos._artwork_box(image)
    assert (box[2] - box[0]) / image.width > 0.95


def test_the_largest_available_source_wins(monkeypatch):
    """Resolution is the one property that cannot be recovered afterwards, so
    candidates are compared on how much ARTWORK they carry rather than taking
    whichever answers first. Measured after trimming, because the nominally
    largest response is routinely a big canvas holding a small icon."""
    from PIL import Image

    tiny_in_big_canvas = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    tiny_in_big_canvas.paste(Image.new("RGBA", (16, 16), (255, 0, 0, 255)),
                             (56, 56))
    buffer = io.BytesIO()
    tiny_in_big_canvas.save(buffer, format="PNG")

    # First response is the big-canvas-small-icon; second is genuinely large.
    _stub(monkeypatch, [_Response(200, buffer.getvalue()),
                        _Response(200, _png(120, 120, (0, 0, 255, 255)))])
    path = logos.fetch_logo("BEST", "https://example.com")

    # The blue 120px artwork should have won over the red 16px one.
    image = Image.open(path).convert("RGB")
    centre = image.getpixel((image.width // 2, image.height // 2))
    assert centre[2] > centre[0], (
        "the first response that answered was kept over a much larger one")


def test_a_sharp_enough_source_stops_the_search(monkeypatch):
    """Comparing candidates costs requests. Once one is good enough there is
    nothing to gain by asking the rest."""
    calls = _stub(monkeypatch, [_Response(200, _png(128, 128))])
    assert logos.fetch_logo("SHARP", "https://example.com") is not None
    assert len(calls) == 1


def test_the_artwork_keeps_its_aspect_ratio(monkeypatch):
    """Squashing a wordmark to fill a square is not the company's logo any
    more. It is scaled to fit and centred, so a wide mark keeps transparent
    bands above and below it."""
    from PIL import Image

    _stub(monkeypatch, [_Response(200, _png(240, 60))])
    path = logos.fetch_logo("WIDE", "https://example.com")

    image = Image.open(path).convert("RGBA")
    top_row_alpha = max(px[3] for px in
                        [image.getpixel((x, 0)) for x in range(image.width)])
    middle_alpha = image.getpixel((image.width // 2, image.height // 2))[3]
    assert top_row_alpha == 0, "a 4:1 mark was stretched to fill the square"
    assert middle_alpha > 0


def test_the_stored_image_is_transparent_not_white_backed(monkeypatch):
    """The UI puts each mark on a light tile because a dark wordmark is
    invisible on navy and so is a white one — but that tile belongs to the
    theme. Baking a background in makes the file unusable anywhere else.

    Checked on a WIDE source, because a square one now scales to fill the
    canvas edge to edge and leaves no margin to inspect.
    """
    from PIL import Image

    _stub(monkeypatch, [_Response(200, _png(240, 60))])
    path = logos.fetch_logo("WIDEBG", "https://example.com")

    image = Image.open(path).convert("RGBA")
    assert image.getpixel((image.width // 2, 0))[3] == 0, (
        "the band above a wide mark was filled in rather than left clear")


# ---------------------------------------------------------------------------
# Misses
# ---------------------------------------------------------------------------
def test_a_miss_is_recorded_and_not_re_requested(monkeypatch):
    """Logos do not appear retroactively. Without a marker, every pass spends
    two requests per logo-less company learning the same thing forever."""
    calls = _stub(monkeypatch, [_Response(404, b"")])

    assert logos.fetch_logo("NONE", "https://example.com") is None
    first = len(calls)
    assert first > 0

    assert logos.fetch_logo("NONE", "https://example.com") is None
    assert len(calls) == first, "a known miss was requested again"
    assert logos.has_been_tried("NONE")


def test_a_company_with_no_website_is_a_miss_not_a_crash(monkeypatch):
    _stub(monkeypatch, [_Response(200, _png(60, 60))])
    assert logos.fetch_logo("NOSITE", None) is None
    assert logos.fetch_logo("NOSITE", "") is None


def test_a_tiny_response_is_not_treated_as_artwork(monkeypatch):
    """A 1x1 tracking pixel, or a broken response that still returned 200."""
    _stub(monkeypatch, [_Response(200, b"\x89PNG\r\n")])
    assert logos.fetch_logo("TINY", "https://example.com") is None


def test_an_undecodable_response_falls_through_rather_than_raising(monkeypatch):
    _stub(monkeypatch, [_Response(200, b"x" * 5000)])
    assert logos.fetch_logo("JUNK", "https://example.com") is None


def test_a_dead_network_is_a_miss_not_an_exception(monkeypatch):
    import requests

    def boom(*_a, **_kw):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(requests, "get", boom)
    assert logos.fetch_logo("DEAD", "https://example.com") is None


# ---------------------------------------------------------------------------
# Domain handling
# ---------------------------------------------------------------------------
def test_domain_variants_cover_the_forms_a_service_indexes():
    """The stored website is whatever the company put on its investor page;
    the icon services index one canonical host. Smucker's is registered under
    `jmsmucker.com` but stored as `www.jmsmucker.com`, and Xcel's as
    `www.my.xcelenergy.com` — both resolve once the prefix is dropped."""
    assert logos._domain_variants("www.jmsmucker.com") == [
        "www.jmsmucker.com", "jmsmucker.com"]
    assert "xcelenergy.com" in logos._domain_variants("www.my.xcelenergy.com")
    assert logos._domain_variants("apple.com") == ["apple.com"]


def test_a_website_without_a_scheme_still_yields_a_domain():
    assert logos._domain("apple.com") == "apple.com"
    assert logos._domain("https://www.apple.com/investor") == "www.apple.com"
    assert logos._domain(None) is None


def test_the_second_source_is_tried_when_the_first_has_nothing(monkeypatch):
    """A 404 from the first candidate must not end the search."""
    calls = _stub(monkeypatch, [_Response(404, b""),
                                _Response(200, _png(120, 120))])
    assert logos.fetch_logo("SECOND", "https://example.com") is not None
    assert len(calls) >= 2


def test_the_slow_source_is_asked_last():
    """Order is about LATENCY, not preference — the best result wins wherever
    it came from. The icon services are CDN-backed and answer in
    milliseconds; `apple-touch-icon` goes to the company's own web server,
    which frequently 404s slowly or never answers. Asking it first made a
    full sweep pay a timeout for every company whose site lacks one, which is
    most of them.
    """
    urls = logos._sources("example.com")
    touch = [i for i, u in enumerate(urls) if "/apple-touch-icon" in u]
    services = [i for i, u in enumerate(urls) if "/apple-touch-icon" not in u]
    assert touch and services
    assert min(touch) > max(services), (
        "the company's own web server is being asked before the CDNs")
