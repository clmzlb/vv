"""Render the icon bitmaps that the vector sources cannot cover.

Two outputs share one set of numbers:

* ``mipmap-*/ic_launcher.png`` — legacy launchers (API 21-25). Adaptive icons only
  exist from API 26, so these mirror ``drawable/ic_launcher_foreground.xml``. The
  launcher shows the central 72dp of the 108dp canvas, so the bitmaps crop to that
  box before scaling.
* ``docs/icon.png`` — the mark used by README and store listings, mirroring
  ``drawable/arcaenbox_artwork.xml`` (same frame and V, scaled up by the inverse of
  that crop).

Geometry is duplicated from the vectors on purpose; keep the four of them in sync.
Run from anywhere:

    python buildScript/tools/gen_launcher_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

BG = (0x0F, 0x3A, 0x44, 255)      # @color/ic_launcher_background
FRAME = (0x4F, 0x95, 0x93, 255)   # rounded box around the V
RIGHT = (0x9F, 0xE0, 0xD6, 255)   # dimmer arm, gives the fold its depth
LEFT = (255, 255, 255, 255)

S = 8                             # supersample factor
CANVAS = 108 * S

# drawable/ic_launcher_foreground.xml, in the 108dp adaptive canvas
BOX = (26, 26, 82, 82)
BOX_RADIUS = 15
BOX_STROKE = 2.5
STROKE = 9 * S
APEX = (54, 70)
TOP_Y = 40
SPAN = 14
CROP = 18 * S                     # central 72dp of the canvas

# drawable/arcaenbox_artwork.xml, same shapes pre-scaled by 108/72
MARK_BOX = (12, 12, 96, 96)
MARK_BOX_RADIUS = 22.5
MARK_BOX_STROKE = 3.75
MARK_STROKE = round(13.5 * S)
MARK_APEX = (54, 78)
MARK_TOP_Y = 33
MARK_SPAN = 21

ROOT = Path(__file__).resolve().parents[2]
RES = ROOT / 'app/src/main/res'
DOCS = ROOT / 'docs'
DENSITIES = {'mdpi': 48, 'hdpi': 72, 'xhdpi': 96, 'xxhdpi': 144, 'xxxhdpi': 192}
MARK_SIZE = 512


def arm(draw, start, end, color, width):
    draw.line([start, end], fill=color, width=width)
    radius = width / 2
    for x, y in (start, end):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color)


def draw_box(draw, box, radius, stroke, color=FRAME):
    left, top, right, bottom = (value * S for value in box)
    draw.rounded_rectangle(
        [left, top, right, bottom],
        radius=radius * S,
        outline=color,
        width=round(stroke * S))


def draw_v(draw, apex, top_y, span, stroke, dim=RIGHT, bright=LEFT):
    center = (apex[0] * S, apex[1] * S)
    arm(draw, center, ((apex[0] + span) * S, top_y * S), dim, stroke)
    arm(draw, ((apex[0] - span) * S, top_y * S), center, bright, stroke)


def round_corners(art, size):
    # Neither legacy icons nor the README image are masked, so round them here.
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=max(2, round(size * 0.2)), fill=255)
    out = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    out.paste(art, (0, 0), mask)
    return out


def render(size):
    art = Image.new('RGBA', (CANVAS, CANVAS), BG)
    draw = ImageDraw.Draw(art)
    draw_box(draw, BOX, BOX_RADIUS, BOX_STROKE)
    draw_v(draw, APEX, TOP_Y, SPAN, STROKE)
    art = art.crop((CROP, CROP, CANVAS - CROP, CANVAS - CROP)).resize((size, size), Image.LANCZOS)
    return round_corners(art, size)


def render_mark(size):
    art = Image.new('RGBA', (CANVAS, CANVAS), BG)
    draw = ImageDraw.Draw(art)
    draw_box(draw, MARK_BOX, MARK_BOX_RADIUS, MARK_BOX_STROKE)
    draw_v(draw, MARK_APEX, MARK_TOP_Y, MARK_SPAN, MARK_STROKE)
    return round_corners(art.resize((size, size), Image.LANCZOS), size)


for density, pixels in DENSITIES.items():
    folder = RES / f'mipmap-{density}'
    folder.mkdir(parents=True, exist_ok=True)
    render(pixels).save(folder / 'ic_launcher.png', optimize=True)
    print(f'mipmap-{density}/ic_launcher.png  {pixels}x{pixels}')

DOCS.mkdir(parents=True, exist_ok=True)
render_mark(MARK_SIZE).save(DOCS / 'icon.png', optimize=True)
print(f'docs/icon.png  {MARK_SIZE}x{MARK_SIZE}')
