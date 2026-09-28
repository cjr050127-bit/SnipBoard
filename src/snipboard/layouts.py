"""Deterministic aspect-preserving board arrangements, independent of Qt."""
import math
import random
import re


def natural(value):
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r'(\d+)', str(value))]


def arrange(items, mode='optimal', ratio=1.5, gap=18, seed=None):
    """Return id -> (x,y,width,height), preserving current dimensions."""
    rows = list(items)
    if not rows:
        return {}
    if mode == 'name':
        rows.sort(key=lambda i: natural(i['name']))
    elif mode == 'addition':
        rows.sort(key=lambda i: i['id'])
    elif mode == 'path':
        rows.sort(key=lambda i: natural(i['path']))
    elif mode == 'order':
        rows.sort(key=lambda i: i['order'])
    elif mode == 'random':
        random.Random(seed).shuffle(rows)
    else:
        rows.sort(key=lambda i: (-i['h'], -i['w'], i['id']))
    target = max(max(i['w'] for i in rows), math.sqrt(sum((i['w'] + gap) * (i['h'] + gap) for i in rows) * ratio))
    def pack(width):
        result = {}
        x = y = height = 0.
        for row in rows:
            if x and x + row['w'] > width:
                x, y, height = 0., y + height + gap, 0.
            result[row['id']] = (x, y, row['w'], row['h'])
            x += row['w'] + gap
            height = max(height, row['h'])
        return result
    if mode != 'optimal':
        return pack(target)
    def screen_area(result):
        width = max(x + w for x, y, w, h in result.values())
        height = max(y + h for x, y, w, h in result.values())
        return max(width / ratio, height) ** 2 * ratio
    candidates = [pack(target * scale) for scale in (.6, .8, 1., 1.2, 1.4, 1.6, 1.8, 2., 2.4)]
    return min(candidates, key=screen_area)
