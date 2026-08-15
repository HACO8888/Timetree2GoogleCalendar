"""Render assets/avatar.png, the icon the failure alert posts under.

Self-hosted on purpose: an emoji CDN is one more third party that can move a URL
and silently break the alert's appearance. Designed to stay legible at the ~40px
Discord actually renders in a channel, so everything here is bold shapes and
high contrast — fine detail just turns to mush at that size.

    uv run --with pillow python scripts/make_avatar.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
SUPERSAMPLE = 4  # draw large, downscale once — cheap, effective anti-aliasing
OUT = Path(__file__).resolve().parent.parent / "assets" / "avatar.png"

# Indigo to cyan: distinct in a channel list without looking like a warning.
GRADIENT_TOP = (79, 70, 229)
GRADIENT_BOTTOM = (6, 182, 212)
CARD = (255, 255, 255)
# Deeper than GRADIENT_TOP so the header separates from the background behind it
# instead of blending into the top-left corner.
HEADER = (55, 48, 163)


def diagonal_gradient(size: int) -> Image.Image:
    """Return a square diagonal gradient from GRADIENT_TOP to GRADIENT_BOTTOM."""
    image = Image.new("RGB", (size, size))
    pixels = image.load()
    for y in range(size):
        for x in range(size):
            # Distance along the top-left -> bottom-right diagonal, 0..1.
            t = (x + y) / (2 * (size - 1))
            pixels[x, y] = tuple(
                round(top + (bottom - top) * t)
                for top, bottom in zip(GRADIENT_TOP, GRADIENT_BOTTOM, strict=True)
            )
    return image


def draw_calendar(draw: ImageDraw.ImageDraw, s: int) -> None:
    """Draw a bold calendar card centred in an s-by-s canvas."""
    def u(value: float) -> float:
        """Scale a 512-space coordinate to the supersampled canvas."""
        return value * s / SIZE

    # Binding rings, drawn first so the card overlaps their lower half.
    for cx in (188, 324):
        draw.rounded_rectangle(
            [u(cx - 14), u(96), u(cx + 14), u(168)], radius=u(14), fill=CARD
        )

    # The card itself.
    draw.rounded_rectangle([u(112), u(140), u(400), u(410)], radius=u(38), fill=CARD)

    # Header strip: punched back out to the gradient so the card reads as a
    # calendar rather than a plain rectangle.
    draw.rounded_rectangle(
        [u(112), u(140), u(400), u(232)], radius=u(38), fill=HEADER
    )
    draw.rectangle([u(112), u(200), u(400), u(232)], fill=HEADER)

    # Date grid. Three columns of chunky squares survive downscaling; a real
    # 7-column month grid does not.
    for row in range(2):
        for col in range(3):
            x = 156 + col * 82
            y = 268 + row * 74
            draw.rounded_rectangle(
                [u(x), u(y), u(x + 52), u(y + 46)], radius=u(12), fill=GRADIENT_BOTTOM
            )


def main() -> None:
    s = SIZE * SUPERSAMPLE
    image = diagonal_gradient(s).convert("RGBA")
    draw_calendar(ImageDraw.Draw(image), s)

    image = image.resize((SIZE, SIZE), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, "PNG", optimize=True)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
