"""Generate warmap's app icon (SVG + 256x256 PNG, plus the webapp sizes)
from one shared geometry, rendered with Pillow.

Glyph: a grid of small dots (every access point warmap has ever plotted)
with a single map pin dropped on top. Flat fills, one shared dark stroke,
no gradients, reads fine down to taskbar size.

Usage:
    python3 tools/gen_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
WEBAPP_DIR = REPO_ROOT / "warmap" / "webapp"

SIZE = 256
SUPERSAMPLE = 4  # draw big, downsample with LANCZOS for anti-aliased edges

ACCENT = "#1fb6c9"      # warmap/ui/theme.py's dark-mode accent
STROKE = "#0d2b30"      # a near-black shade of the accent hue, shared outline
DOT_COLOR = "#3a4a4d"   # muted, so the grid reads as background, not the subject
PIN_DOT = "#f5fbfc"     # the classic pin "hole" dot, near-white

GRID_SPACING = 40
DOT_RADIUS = 5

PIN_CX, PIN_CY = 128, 108   # head center
PIN_RADIUS = 62
LEG_ANGLE_DEG = 40          # how far around the circle the tip's two legs attach
TIP_EXTRA = 96               # how far below the head center the tip point reaches
STROKE_WIDTH = 10
HEAD_DOT_RADIUS = 22


def _pin_polygon(cx: float, cy: float, radius: float, tip_y: float) -> list[tuple[float, float]]:
    """One continuous polygon: a circle with a wedge cut out at the bottom
    replaced by a sharp point, i.e. a classic map-pin silhouette. Angles are
    in the image's y-down convention (theta=90 is straight down, theta=270
    is straight up)."""
    right_leg_theta = 90 - LEG_ANGLE_DEG
    left_leg_theta = 90 + LEG_ANGLE_DEG

    def pt(theta_deg: float) -> tuple[float, float]:
        rad = math.radians(theta_deg)
        return (cx + radius * math.cos(rad), cy + radius * math.sin(rad))

    tip = (cx, tip_y)
    right_leg = pt(right_leg_theta)
    left_leg = pt(left_leg_theta)

    points = [tip, right_leg]
    # Sweep the long way around (through the top), avoiding the bottom gap
    # between the two legs: decreasing angle from the right leg down to
    # the left leg's equivalent negative angle.
    theta = right_leg_theta
    end = left_leg_theta - 360
    while theta > end:
        points.append(pt(theta))
        theta -= 4
    points.append(left_leg)
    return points


def _draw_grid(draw: ImageDraw.ImageDraw, size: int, scale: int) -> None:
    spacing = GRID_SPACING * scale
    radius = DOT_RADIUS * scale
    y = spacing // 2
    while y < size:
        x = spacing // 2
        while x < size:
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=DOT_COLOR)
            x += spacing
        y += spacing


def _draw_pin(draw: ImageDraw.ImageDraw, scale: int) -> None:
    cx, cy = PIN_CX * scale, PIN_CY * scale
    radius = PIN_RADIUS * scale
    tip_y = (PIN_CY + TIP_EXTRA) * scale

    # Silhouette (stroke color, slightly larger) drawn first, then the
    # accent-filled pin inset on top. That gives a uniform outline on an
    # arbitrary polygon without needing true path-outline support.
    outline_pts = _pin_polygon(cx, cy, radius + STROKE_WIDTH * scale, tip_y + STROKE_WIDTH * scale)
    draw.polygon(outline_pts, fill=STROKE)

    fill_pts = _pin_polygon(cx, cy, radius, tip_y)
    draw.polygon(fill_pts, fill=ACCENT)

    dot_r = HEAD_DOT_RADIUS * scale
    draw.ellipse((cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r), fill=PIN_DOT)


def _render(size: int, background: Optional[str] = None, inset: float = 0.0) -> "Image.Image":
    """The icon at any size.

    `background` fills the canvas instead of leaving it transparent, and
    `inset` shrinks the artwork toward the middle by that fraction of the
    canvas on each side. Both exist for the maskable variant: Android crops a
    maskable icon to whatever shape the launcher uses, so the glyph has to sit
    inside the middle ~80% and the corners have to be painted rather than
    transparent, or the crop eats the pin and leaves ragged edges.
    """
    scale = SUPERSAMPLE
    big = SIZE * scale
    img = Image.new("RGBA", (big, big),
                    background if background else (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    _draw_grid(draw, big, scale)
    _draw_pin(draw, scale)

    if inset > 0:
        keep = int(big * (1.0 - 2 * inset))
        art = img.resize((keep, keep), Image.LANCZOS)
        canvas = Image.new("RGBA", (big, big),
                           background if background else (0, 0, 0, 0))
        offset = (big - keep) // 2
        canvas.paste(art, (offset, offset), art)
        img = canvas

    return img.resize((size, size), Image.LANCZOS)


def _write_png(path: Path, size: int = SIZE, background: Optional[str] = None,
               inset: float = 0.0) -> None:
    _render(size, background=background, inset=inset).save(path)


def _write_svg(path: Path) -> None:
    circles = []
    y = GRID_SPACING // 2
    while y < SIZE:
        x = GRID_SPACING // 2
        while x < SIZE:
            circles.append(f'  <circle cx="{x}" cy="{y}" r="{DOT_RADIUS}" fill="{DOT_COLOR}"/>')
            x += GRID_SPACING
        y += GRID_SPACING

    tip_y = PIN_CY + TIP_EXTRA
    outline_pts = _pin_polygon(PIN_CX, PIN_CY, PIN_RADIUS + STROKE_WIDTH, tip_y + STROKE_WIDTH)
    fill_pts = _pin_polygon(PIN_CX, PIN_CY, PIN_RADIUS, tip_y)
    outline_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in outline_pts)
    fill_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in fill_pts)

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}">\n'
        + "\n".join(circles) + "\n"
        + f'  <polygon points="{outline_str}" fill="{STROKE}"/>\n'
        + f'  <polygon points="{fill_str}" fill="{ACCENT}"/>\n'
        + f'  <circle cx="{PIN_CX}" cy="{PIN_CY}" r="{HEAD_DOT_RADIUS}" fill="{PIN_DOT}"/>\n'
        + "</svg>\n"
    )
    path.write_text(svg, encoding="utf-8")


# The mobile app's icon set. A phone home screen shows this at real size next
# to professionally-made icons, so it gets the sizes the platforms actually
# ask for rather than one PNG scaled by the browser.
WEBAPP_ICONS = (
    # (filename, size, background, inset)
    ("icon-192.png", 192, None, 0.0),
    ("icon-512.png", 512, None, 0.0),
    # Maskable: painted corners + the glyph pulled into the safe zone.
    ("icon-maskable.png", 512, "#0d0d0d", 0.10),
    # iOS composites onto white if the icon is transparent, which would show
    # the grid dots on a light square; give it the app's own background.
    ("apple-touch-icon.png", 180, "#0d0d0d", 0.06),
)


def main() -> int:
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    _write_svg(BIN_DIR / "warmap.svg")
    _write_png(BIN_DIR / "warmap.png")
    print(f"wrote {BIN_DIR / 'warmap.svg'}")
    print(f"wrote {BIN_DIR / 'warmap.png'} ({SIZE}x{SIZE})")

    WEBAPP_DIR.mkdir(parents=True, exist_ok=True)
    for name, size, background, inset in WEBAPP_ICONS:
        _write_png(WEBAPP_DIR / name, size=size, background=background, inset=inset)
        print(f"wrote {WEBAPP_DIR / name} ({size}x{size})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
