"""Generate DimFort branding assets.

Run from the repo root:
    python scripts/make_branding.py

Produces:
    social_preview.png — 1280x640 GitHub social-preview banner.

The visual design (palette, glyph, watermark F, rounded frame) is
kept in sync with the VSCode companion's ``make_icon.py`` by
duplicating the small set of helpers below. They're tiny; a shared
package would be overkill across two separately-cloneable repos.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Palette — kept identical to the VSCompanion script.
BG_TOP = (32, 50, 78)
BG_BOTTOM = (18, 28, 46)
ACCENT = (118, 194, 255)
TEXT = (240, 244, 252)
RULE = (255, 184, 76)
WATERMARK_ALPHA = 46


# ---------------------------------------------------------------------------
# Low-level helpers (duplicated from DimFort-VSCompanion/scripts/make_icon.py)
# ---------------------------------------------------------------------------


def _vertical_gradient(w, h, top, bot):
    img = Image.new("RGB", (w, h), top)
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        draw.line([(0, y), (w, y)], fill=c)
    return img


def _load_font(px: int) -> ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/SFNSMono.ttf",
        "/System/Library/Fonts/Menlo.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def _load_clarendon(px: int, *, index: int = 5) -> ImageFont.ImageFont:
    path = "/System/Library/Fonts/Supplemental/SuperClarendon.ttc"
    try:
        return ImageFont.truetype(path, px, index=index)
    except OSError:
        return _load_font(px)


def _round_corners(img: Image.Image, radius: int) -> Image.Image:
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, img.size[0] - 1, img.size[1] - 1],
        radius=radius,
        fill=255,
    )
    out = img.copy()
    out.putalpha(mask)
    return out


# ---------------------------------------------------------------------------
# Icon tile (used as the left-hand glyph in the social preview)
# ---------------------------------------------------------------------------


def _icon_background(size: int) -> Image.Image:
    img = _vertical_gradient(size, size, BG_TOP, BG_BOTTOM).convert("RGBA")
    scale = size / 256

    pad = round(14 * scale)
    frame_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(frame_layer).rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=round(36 * scale),
        outline=(ACCENT[0], ACCENT[1], ACCENT[2], WATERMARK_ALPHA),
        width=max(1, round(5 * scale)),
    )
    img = Image.alpha_composite(img, frame_layer)

    f_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    f_draw = ImageDraw.Draw(f_layer)
    f_font = _load_clarendon(round(260 * scale))
    fb = f_draw.textbbox((0, round(-7 * scale)), "F", font=f_font)
    fw = fb[2] - fb[0]
    fh = fb[3] - fb[1]
    f_draw.text(
        ((size - fw) / 2 - fb[0], (size - fh) / 2 - fb[1] - round(6 * scale)),
        "F",
        font=f_font,
        fill=(ACCENT[0], ACCENT[1], ACCENT[2], WATERMARK_ALPHA),
    )
    img = Image.alpha_composite(img, f_layer)
    return img


def build_equation_tile(size: int = 256) -> Image.Image:
    """[m·s⁻²] motif scaled to ``size``×``size``."""
    img = _icon_background(size)
    draw = ImageDraw.Draw(img)
    scale = size / 256

    big_font = _load_font(round(58 * scale))
    sup_font = _load_font(round(36 * scale))
    bracket_font = _load_font(round(77 * scale))

    open_text, body_text, sup_text, close_text = "[", "m·s", "-2", "]"

    open_box = draw.textbbox((0, 0), open_text, font=bracket_font)
    body_box = draw.textbbox((0, 0), body_text, font=big_font)
    sup_box = draw.textbbox((0, 0), sup_text, font=sup_font)
    close_box = draw.textbbox((0, 0), close_text, font=bracket_font)

    open_w = open_box[2] - open_box[0]
    body_w = body_box[2] - body_box[0]
    sup_w = sup_box[2] - sup_box[0]
    close_w = close_box[2] - close_box[0]
    gap_to_sup = round(2 * scale)
    gap_to_close = round(2 * scale)
    bracket_inset = round(14 * scale)
    total_w = (
        open_w + body_w + gap_to_sup + sup_w + gap_to_close + close_w
        - 2 * bracket_inset
    )

    bracket_h = open_box[3] - open_box[1]
    body_glyph_h = body_box[3] - body_box[1]
    x = (size - total_w) / 2
    bracket_y = (size - bracket_h) / 2 - round(4 * scale)
    body_y = bracket_y + (bracket_h - body_glyph_h) / 2

    draw.text(
        (x - open_box[0], bracket_y - open_box[1]),
        open_text, font=bracket_font, fill=RULE,
    )
    body_x = x + open_w - bracket_inset
    draw.text(
        (body_x - body_box[0], body_y - body_box[1]),
        body_text, font=big_font, fill=TEXT,
    )
    sup_x = body_x + body_w + gap_to_sup
    draw.text(
        (sup_x - sup_box[0], body_y - sup_box[1] - round(12 * scale)),
        sup_text, font=sup_font, fill=TEXT,
    )
    close_x = sup_x + sup_w + gap_to_close - bracket_inset
    draw.text(
        (close_x - close_box[0], bracket_y - close_box[1]),
        close_text, font=bracket_font, fill=RULE,
    )

    return img


# ---------------------------------------------------------------------------
# Social-preview banner
# ---------------------------------------------------------------------------


SOCIAL_W = 1280
SOCIAL_H = 640
SOCIAL_CORNER_RADIUS = 64


def build_social() -> Image.Image:
    """1280x640 GitHub social-preview banner: icon tile + ``DimFort`` wordmark."""
    img = _vertical_gradient(
        SOCIAL_W, SOCIAL_H, BG_TOP, BG_BOTTOM
    ).convert("RGBA")

    frame_layer = Image.new("RGBA", (SOCIAL_W, SOCIAL_H), (0, 0, 0, 0))
    ImageDraw.Draw(frame_layer).rounded_rectangle(
        [36, 36, SOCIAL_W - 36, SOCIAL_H - 36],
        radius=64,
        outline=(ACCENT[0], ACCENT[1], ACCENT[2], WATERMARK_ALPHA),
        width=8,
    )
    img = Image.alpha_composite(img, frame_layer)

    tile_size = 380
    tile_x = 100
    tile_y = (SOCIAL_H - tile_size) // 2
    tile = build_equation_tile(tile_size)
    img.paste(tile, (tile_x, tile_y), tile)

    draw = ImageDraw.Draw(img)
    wordmark = "DimFort"
    word_font = _load_clarendon(126)
    word_box = draw.textbbox((0, 0), wordmark, font=word_font)
    word_h = word_box[3] - word_box[1]
    word_x = tile_x + tile_size + 70
    word_y = (SOCIAL_H - word_h) / 2 - 8
    draw.text(
        (word_x - word_box[0], word_y - word_box[1]),
        wordmark, font=word_font, fill=TEXT,
    )
    return img


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    out = repo_root / "social_preview.png"
    _round_corners(build_social(), SOCIAL_CORNER_RADIUS).save(
        out, "PNG", optimize=True,
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
