"""Verify every user-facing brand mark in a release APK.

Four surfaces have to agree: the adaptive launcher icon, its legacy bitmaps, the
in-app mark (About header / drawer) and the status-bar glyph. All of them are the V
monogram, and none of them may fall back to a raster.
"""
import os
import re
import shutil
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile
from PIL import Image

RES = Path('app/src/main/res')
CANVAS = 108.0


def read(name):
    return (RES / name).read_text(encoding='utf-8')


def v_geometry(vector, name):
    """Return ((apex), (left top), (right top)) for a two-arm V vector."""
    arms = re.findall(
        r'android:pathData="M\s*([\d.]+)\s*,\s*([\d.]+)\s*L\s*([\d.]+)\s*,\s*([\d.]+)\s*"',
        vector)
    assert len(arms) == 2, f'{name}: expected two arm paths, found {len(arms)}'
    segments = [((float(a), float(b)), (float(c), float(d))) for a, b, c, d in arms]
    for start, end in segments:
        assert 'strokeLineCap="round"' in vector, f'{name}: arms must have round caps'
    # The arms meet at the apex and their tops are level and symmetric about the canvas.
    apexes = [{start, end} for start, end in segments]
    apex = next(iter(apexes[0] & apexes[1]), None)
    assert apex is not None, f'{name}: arms do not share the apex'
    tops = [start if end == apex else end for start, end in segments]
    assert abs(tops[0][1] - tops[1][1]) < 0.01, f'{name}: tops are not level'
    assert abs(tops[0][0] + tops[1][0] - CANVAS) < 0.01, f'{name}: V is not centred'
    return apex, tops[0], tops[1]


# --- launcher: adaptive layers, then the five legacy bitmaps -------------------------
adaptive = read('mipmap-anydpi-v26/ic_launcher.xml')
for element in ('<adaptive-icon', '@drawable/ic_launcher_foreground',
                '@drawable/ic_launcher_monochrome', '@color/ic_launcher_background'):
    assert element in adaptive, f'Adaptive icon is missing {element}'

foreground = read('drawable/ic_launcher_foreground.xml')

sources = {path.parent.name.split('-', 1)[1]: path for path in RES.glob('mipmap-*/ic_launcher.png')}
assert set(sources) == {'mdpi', 'hdpi', 'xhdpi', 'xxhdpi', 'xxxhdpi'}, sorted(sources)
stray = sorted(str(p) for p in RES.rglob('*.png') if p not in sources.values())
assert not stray, f'Raster art must not ship inside res/: {stray}'

# --- in-app mark: vector twin of the launcher, same V, edge to edge ------------------
artwork = read('drawable/arcaenbox_artwork.xml')
assert '<bitmap' not in artwork, 'The in-app mark must stay a vector'
assert 'fillColor="#36656F"' in artwork, 'The in-app mark lost its teal tile'
apex, left, _ = v_geometry(artwork, 'arcaenbox_artwork')
assert 'strokeColor="#FFFFFF"' in foreground and 'strokeColor="#C6E2E8"' in foreground, \
    'Launcher and in-app mark no longer share the two-tone arms'


def stroke_width(vector):
    values = {float(v) for v in re.findall(r'android:strokeWidth="([\d.]+)"', vector)}
    assert len(values) == 1, f'Expected one stroke width, found {values}'
    return values.pop()


def uncrop(value):
    """Map a coordinate from the launcher's 72dp visible box onto this 108dp canvas."""
    return (value - 18) * 108 / 72


launcher_apex, launcher_left, _ = v_geometry(foreground, 'ic_launcher_foreground')
for got, want in ((apex[0], launcher_apex[0]), (apex[1], launcher_apex[1]),
                  (left[0], launcher_left[0]), (left[1], launcher_left[1])):
    assert abs(got - uncrop(want)) < 0.02, f'In-app V drifted from the launcher V: {got} vs {uncrop(want)}'
assert abs(stroke_width(artwork) - stroke_width(foreground) * 108 / 72) < 0.02, \
    'In-app stroke no longer matches the launcher stroke'

# --- status bar: the old A glyph must not survive anywhere ---------------------------
legacy = sorted(str(p.relative_to(RES)) for p in RES.rglob('*.xml') if 'L 2 21' in p.read_text(encoding='utf-8'))
assert not legacy, f'Legacy A glyph still in {legacy}'

for glyph in ('ic_service_active', 'ic_service_busy', 'ic_service_idle'):
    body = read(f'drawable/{glyph}.xml')
    assert '12,17.8' in body, f'{glyph} is not the V'
    assert 'strokeColor="#' in body, f'{glyph} must be a stroked silhouette'

# The mark is stroked, so the pulse animations have to drive strokeAlpha.
for animated in ('ic_service_connected', 'ic_service_stopping'):
    body = read(f'drawable/{animated}.xml')
    assert 'propertyName="strokeAlpha"' in body, f'{animated} no longer pulses the stroke'
    assert 'propertyName="fillAlpha"' not in body, f'{animated} pulses a fill that does not exist'

# --- README ------------------------------------------------------------------------
readme = Path('README.md').read_text(encoding='utf-8')
assert 'docs/icon.png' in readme, 'README no longer shows the mark'
assert Path('docs/icon.png').exists(), 'docs/icon.png is missing'
assert 'arcaenbox_artwork.png' not in readme, 'README still points at the retired bitmap'

# --- what actually ships -----------------------------------------------------------
# AGP shortens packaged resource paths (mipmap/ic_launcher becomes res/BW.xml), so the
# APK is queried through its resource table instead of by file name.
DENSITY_PIXELS = {48: 'mdpi', 72: 'hdpi', 96: 'xhdpi', 144: 'xxhdpi', 192: 'xxxhdpi'}


def find_aapt():
    for candidate in (os.environ.get('AAPT'), shutil.which('aapt')):
        if candidate and Path(candidate).exists():
            return candidate
    for variable in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        sdk = os.environ.get(variable)
        if sdk:
            builds = sorted((Path(sdk) / 'build-tools').glob('*/aapt'), reverse=True)
            if builds:
                return str(builds[0])
    raise SystemExit('aapt not found; set AAPT to build-tools/aapt')


def packaged_files(apk_path):
    """Map resource name -> packaged paths, e.g. mipmap/ic_launcher -> res/BW.xml.

    `aapt dump --values resources` prints one block per resource:
        resource 0x7f100000 com.vv.android:mipmap/ic_launcher: t=0x03 ...
          (string8) "res/BW.xml"
    """
    dump = subprocess.run([find_aapt(), 'dump', '--values', 'resources', apk_path],
                          check=True, capture_output=True, text=True,
                          encoding='utf-8', errors='replace').stdout
    table = {}
    current = None
    for line in dump.splitlines():
        match = re.match(r'\s*(?:spec )?resource 0x[0-9a-f]+ [^:]+:(\S+?):', line)
        if match:
            current = match.group(1)
            table.setdefault(current, [])
            continue
        match = re.search(r'\(file\) (\S+)', line) or \
            re.search(r'\(string(?:8|16)\) "(res/[^"]+)"', line)
        if match and current:
            table[current].append(match.group(1))
    return table


with ZipFile(sys.argv[1]) as apk:
    table = packaged_files(sys.argv[1])
    for name in ('mipmap/ic_launcher', 'drawable/ic_launcher_foreground',
                 'drawable/ic_launcher_monochrome', 'drawable/arcaenbox_artwork',
                 'drawable/ic_arcaenbox', 'drawable/ic_service_active',
                 'drawable/ic_service_busy', 'drawable/ic_service_idle'):
        assert table.get(name), f'{name} missing from APK'

    assert any(path.endswith('.xml') for path in table['mipmap/ic_launcher']), \
        'Adaptive icon missing from APK'
    assert all(path.endswith('.xml') for path in table['drawable/arcaenbox_artwork']), \
        'Retired bitmap artwork is still packaged'

    retired = [path for name, paths in table.items() if 'arcaenbox' in name
               for path in paths if path.endswith('.png')]
    assert not retired, f'Retired bitmap artwork is still packaged: {retired}'

    verified = set()
    for path in table['mipmap/ic_launcher']:
        if not path.endswith('.png'):
            continue
        packed = Image.open(BytesIO(apk.read(path))).convert('RGBA')
        assert packed.size[0] == packed.size[1], f'{path}: icon is not square'
        density = DENSITY_PIXELS.get(packed.size[0])
        assert density, f'{path}: unexpected legacy icon size {packed.size}'
        source = Image.open(sources[density]).convert('RGBA')
        # AAPT discards RGB data in fully transparent pixels. Compare alpha and
        # visible pixels over both black and white, not invisible RGB padding.
        assert packed.getchannel('A').tobytes() == source.getchannel('A').tobytes(), \
            f'{path}: alpha changed'
        for color in ('black', 'white'):
            background = Image.new('RGBA', source.size, color)
            assert Image.alpha_composite(background, packed).tobytes() == \
                Image.alpha_composite(background, source).tobytes(), f'{path}: visible pixels changed'
        verified.add(density)

    assert verified == set(sources), f'Missing densities in APK: {sorted(set(sources) - verified)}'
    print('Verified adaptive icon, in-app mark, status-bar glyph and',
          len(verified), 'legacy bitmaps:', ', '.join(sorted(verified)))
