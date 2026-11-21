"""Rebuild the offline colour emoji pack; not needed to run PeakCut.

Install fonttools and uharfbuzz in a temporary environment, then pass a Noto
Emoji repository tree JSON and its matching 2D/fonts/NotoColorEmoji.ttf.
Source revision and licenses are recorded in studio/assets/README.md.
"""
import json
import sys
import zipfile
from pathlib import Path

import uharfbuzz as hb
from fontTools.ttLib import TTFont


def build(tree_file, font_file, output):
    tree = json.loads(Path(tree_file).read_text())
    data = Path(font_file).read_bytes()
    face = hb.Face(data)
    shaping = hb.Font(face)
    font = TTFont(font_file)
    bitmaps = font['CBDT'].strikeData[0]
    index, images = {}, {}
    for row in tree['tree']:
        path = row['path']
        if not path.startswith(('2D/png/128/emoji_u', '3D/png/128/emoji_u')) or not path.endswith('.png'):
            continue
        sequence = ''.join(chr(int(c, 16)) for c in Path(path).stem.removeprefix('emoji_u').split('_'))
        sequence = sequence.replace('\ufe0f', '')
        # Plain numbers and punctuation must remain in the title font.
        if all(ord(c) < 128 for c in sequence):
            continue
        buf = hb.Buffer()
        buf.add_str(sequence)
        buf.guess_segment_properties()
        hb.shape(shaping, buf)
        glyphs = [g.codepoint for g in buf.glyph_infos]
        if len(glyphs) != 1 or glyphs[0] == 0:
            continue
        name = font.getGlyphName(glyphs[0])
        if name not in bitmaps:
            continue
        image = bitmaps[name].imageData
        if not image.startswith(b'\x89PNG'):
            raise ValueError('Unexpected emoji bitmap format')
        filename = f'{glyphs[0]}.png'
        images[filename] = image
        index[sequence] = filename
    # Fixed timestamps and ordering keep the asset reproducible.
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted({**images, 'index.json': json.dumps(index, ensure_ascii=False, sort_keys=True).encode()}.items()):
            archive.writestr(zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0)), data)
    print(f'{len(index)} sequences, {len(images)} colour images, {Path(output).stat().st_size} bytes')


if __name__ == '__main__':
    build(*sys.argv[1:])
