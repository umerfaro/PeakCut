"""Paint the divider title and colour emoji once, then overlay it on the video.

The subtitle renderer cannot draw colour emoji reliably. Bundled PNG artwork
also handles joined families, flags and skin tones without system emoji fonts.
"""
import io
import json
import math
import random
import zipfile
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).with_name('assets') / 'emoji.zip'
FONT = Path(__file__).with_name('fonts') / 'Poppins-ExtraBold.ttf'


@lru_cache(maxsize=1)
def _index():
    with zipfile.ZipFile(ASSETS) as archive:
        index = json.loads(archive.read('index.json'))
    starts = {}
    for sequence in index:
        starts.setdefault(sequence[0], []).append(sequence)
    return index, {char: sorted(keys, key=len, reverse=True) for char, keys in starts.items()}


def runs(text):
    """Longest matches keep multi-codepoint emoji together as one picture."""
    _, starts = _index()
    text = text.replace('\ufe0f', '')
    result, plain, i = [], '', 0
    while i < len(text):
        sequence = next((s for s in starts.get(text[i], ()) if text.startswith(s, i)), None)
        if sequence:
            if plain:
                result.append((plain, False))
                plain = ''
            result.append((sequence, True))
            i += len(sequence)
        else:
            plain += text[i]
            i += 1
    if plain:
        result.append((plain, False))
    return result


def has_emoji(text):
    return any(is_emoji for _, is_emoji in runs(text))


@lru_cache(maxsize=128)
def _emoji(sequence):
    index, _ = _index()
    with zipfile.ZipFile(ASSETS) as archive:
        with Image.open(io.BytesIO(archive.read(index[sequence]))) as image:
            image = image.convert('RGBA')
            return image.crop(image.getbbox())


@lru_cache(maxsize=64)
def _font(size):
    return ImageFont.truetype(str(FONT), size)


def _parts(text, size):
    font = _font(size)
    parts = []
    for content, is_emoji in runs(text):
        if is_emoji:
            image = _emoji(content)
            height = round(size * .88)
            width = round(height * image.width / image.height)
            parts.append((image.resize((width, height), Image.Resampling.LANCZOS), width))
        else:
            parts.append((content, font.getlength(content)))
    return parts


def _width(text, size):
    return sum(width for _, width in _parts(text, size))


def make(text, width=1080, brush=True):
    """Transparent title strip; one or two lines fitted inside the frame."""
    words = text.split()
    if not words:
        return None
    text = ' '.join(words)
    max_width = width - 72
    size = 84
    lines = [text]
    if _width(text, size) > max_width * 1.45 and len(words) > 1:
        cut = min(range(1, len(words)), key=lambda i: max(
            _width(' '.join(words[:i]), size), _width(' '.join(words[i:]), size)))
        lines = [' '.join(words[:cut]), ' '.join(words[cut:])]
    while max(_width(line, size) for line in lines) > max_width and size > 8:
        size -= 1
    line_h = math.ceil(size * 1.2)
    height = line_h * len(lines) + 58
    image = Image.new('RGBA', (width, height))
    draw = ImageDraw.Draw(image)
    if brush:
        # Fixed seed gives every export the same torn-paper silhouette.
        rng = random.Random(41)
        xs = list(range(0, width, 12)) + [width]
        top = [(x, 15 + round(5 * math.sin(x / 87)) + rng.randrange(-7, 8)) for x in xs]
        bottom = [(x, height - 15 + round(5 * math.sin(x / 73)) + rng.randrange(-7, 8)) for x in reversed(xs)]
        draw.polygon(top + bottom, fill='white')
        # Small detached bristles make the edge look brushed rather than a box.
        for x in range(10, width - 16, 31):
            y = rng.choice((7, height - 9))
            draw.polygon([(x, y), (x + rng.randrange(9, 25), y - 2), (x + 5, y + 2)], fill='white')
    else:
        widest = max(_width(line, size) for line in lines)
        left = max(0, round((width - widest - 64) / 2))
        draw.rounded_rectangle((left, 4, width - left, height - 4), radius=26, fill='white')
    for row, line in enumerate(lines):
        parts = _parts(line, size)
        x = (width - sum(w for _, w in parts)) / 2
        cy = (height - line_h * len(lines)) / 2 + (row + .5) * line_h
        for content, advance in parts:
            if isinstance(content, Image.Image):
                image.alpha_composite(content, (round(x), round(cy - content.height / 2)))
            else:
                box = draw.textbbox((0, 0), content, font=_font(size))
                draw.text((x, cy - (box[1] + box[3]) / 2), content, font=_font(size), fill='#0e1014')
            x += advance
    return image
