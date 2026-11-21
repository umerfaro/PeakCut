"""Offline emoji artwork and the full-width stacked title divider."""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ['PEAKCUT_DATA'] = tempfile.mkdtemp(prefix='pc-hook-art-')
sys.path.insert(0, '.')
import numpy as np
from PIL import Image
from studio import hook_art, render


def check(name, condition):
    assert condition, name
    print('PASS', name)


for emoji in ('😭', '🐊', '👍🏽', '👨‍👩‍👧‍👦', '🇵🇰', '1️⃣', '❤️'):
    tokens = hook_art.runs(emoji)
    check(f'{emoji} stays one complete colour picture', len(tokens) == 1 and tokens[0][1])
check('ordinary numbers and words stay text', hook_art.runs('GTA 6 #1') == [('GTA 6 #1', False)])
check('empty text produces no divider', hook_art.make('   ') is None)
image = hook_art.make('SPEED VS THE GATOR 😭🐊')
pixels = np.asarray(image)
check('the divider spans the full frame', image.width == 1080 and pixels[image.height // 2, 0, 3] == 255
      and pixels[image.height // 2, -1, 3] == 255)
check('the brushed edge has transparent gaps', np.any(pixels[:20, :, 3] == 0) and np.any(pixels[:20, :, 3] == 255))
colour = (np.max(pixels[:, :, :3], axis=2).astype(int) - np.min(pixels[:, :, :3], axis=2).astype(int) > 70) & (pixels[:, :, 3] > 200)
check('emoji pixels have colour rather than missing glyph boxes', colour.sum() > 1000)
check('repeated exports keep the same divider silhouette', image.tobytes() == hook_art.make('SPEED VS THE GATOR 😭🐊').tobytes())
for text in ('A LONG HOOK WITH A SETUP AND A PAYOFF THAT SHOULD FIT 😭🐊', 'W' * 60, '😭' * 30):
    drawn = np.asarray(hook_art.make(text))
    ink = (drawn[:, :, :3].min(axis=2) < 120) & (drawn[:, :, 3] > 128)
    check('long title stays inside the frame', not ink[:, :20].any() and not ink[:, -20:].any())

clip = {'start': 0, 'end': 3, 'hook': 'SPEED VS THE GATOR 😭🐊', 'layout': 'stacked'}
def encode(work, src, out_dir, name, start, end, layout, graph, ass, **kwargs):
    check('the video encoder receives the colour title image', (work / 'hook.png').is_file())
    check('the title image sits on the camera divider', kwargs['hook_y'] == render.FACE_H)
    check('the old rounded title is not also burned in', ',Hook,,' not in ass)
    check('spoken captions still reach the encoder', ',Cap,,' in ass)
    return {'file': 'clip1.mp4'}
with tempfile.TemporaryDirectory() as folder, patch.object(render.stacked, 'detect', return_value=(0, .4, .2, .2)), \
     patch.object(render, '_render_in', side_effect=encode):
    render.render_clip(Path('source.mp4'), Path(folder), 'clip1', clip, [{'w': 'hello', 's': .2, 'e': 1}], 1920, 1080)
print('ALL PASSED')
