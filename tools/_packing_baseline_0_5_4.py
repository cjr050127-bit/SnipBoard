"""Frozen 0.5.4 benchmark baseline; never import from application runtime.

Only the layouts import was made absolute so this standalone snapshot can run.
Bounded fixed-window photo packing: justified strips and MaxRects candidates.

Algorithm references (independent implementation):
https://flickr.github.io/justified-layout/
https://github.com/juj/RectangleBinPack
All coordinates are logical viewport pixels. No crop or automatic rotation.
"""
import math
import time


class BudgetEnded(Exception):
    pass


def compact(items, width, height, gap=6, seconds=1.2, cancelled=lambda: False):
    rows = [dict(item) for item in items]
    if not rows:
        return {'positions': {}, 'occupancy': 0., 'method': 'empty'}
    if not all(math.isfinite(v) for v in (width,height,gap)) or not (width > 0 and height > 0 and gap >= 0):
        raise ValueError('窗口尺寸或图片间距无效')
    for row in rows:
        if not all(math.isfinite(row[key]) and row[key] > 0 for key in ('w', 'h')):
            raise ValueError('图片尺寸无效')
        row['fill'] = row.get('fill', 1.)
        if not math.isfinite(row['fill']) or not 0 < row['fill'] <= 1.000001:
            raise ValueError('图片面积无效')
    canvas = width * height
    low, high = 0., canvas
    end = time.monotonic() + seconds
    best = None
    def check():
        if cancelled() or time.monotonic() >= end:
            raise BudgetEnded()
    def consider(positions, method):
        nonlocal best
        if positions is None:
            return
        areas = [positions[r['id']][2] * positions[r['id']][3] * r['fill'] for r in rows]
        if min(areas) < low - 1e-5 or max(areas) > high + 1e-5:
            return
        occupancy = sum(areas) / canvas
        mean = sum(areas)/len(areas)
        if mean <= 1e-6:
            return
        variation = math.sqrt(sum((area/mean-1)**2 for area in areas)/len(areas))
        log_spread = math.sqrt(sum(math.log(area/mean)**2 for area in areas)/len(areas))
        # Penalize both overall imbalance and an isolated tiny image that a
        # mean/variance score alone can hide in a large collection.
        score = occupancy / (1 + .25*variation + .15*log_spread + .12*math.log(max(areas)/min(areas)) +
                             .4*max(0., .4-min(areas)/mean) + .05*max(0., max(areas)/mean-2.5))
        if best is None or score > best['score'] + 1e-9:
            best = dict(positions=positions, occupancy=occupancy, method=method,
                        score=score, area_variation=variation, min_relative_area=min(areas)/mean,
                        max_relative_area=max(areas)/mean)

    # Keep the previous shelf layout as a candidate, without privileging its
    # unequal sizes. Every candidate uses the same coverage/balance objective.
    from snipboard.layouts import arrange
    old = arrange(rows, 'optimal', width / height, gap)
    old_w = max(x + w for x, y, w, h in old.values())
    old_h = max(y + h for x, y, w, h in old.values())
    scale = min(width / old_w, height / old_h,
                min(math.sqrt(high / (r['w'] * r['h'] * r['fill'])) for r in rows))
    # Shelf spacing is in scene units. Only accept it when its displayed spacing
    # is at least the requested viewport spacing.
    if scale >= 1 or gap == 0:
        consider({key: ((x * scale + (width-old_w*scale)/2), (y * scale + (height-old_h*scale)/2), w*scale, h*scale)
                  for key, (x,y,w,h) in old.items()}, 'shelf-baseline')
    try:
        # Fast equal-area shelves give large collections a balanced candidate
        # even when a full MaxRects search would exceed the response budget.
        for transposed in (False, True):
            W,H = (height,width) if transposed else (width,height)
            for exponent in (0., .25):
                weights = [(r['w']*r['h']*r['fill'])**exponent for r in rows]
                total = sum(weights)
                lower,upper = 0.,canvas*2
                for _ in range(16):
                    check()
                    amount = (lower+upper)/2
                    rectangles = []
                    for row,weight in zip(rows,weights):
                        factor = min(math.sqrt(amount*weight/total/(row['w']*row['h']*row['fill'])),
                                     width/row['w'],height/row['h'])
                        w,h = row['w']*factor,row['h']*factor
                        rectangles.append((row['id'],h if transposed else w,w if transposed else h))
                    rectangles.sort(key=lambda r:(-r[2],-r[1],str(r[0])))
                    positions = {}
                    x = y = shelf_h = 0.
                    for key,w,h in rectangles:
                        if x and x+w > W+1e-8:
                            x,y,shelf_h = 0.,y+shelf_h+gap,0.
                        if y+h > H+1e-8:
                            break
                        positions[key] = (y,x,h,w) if transposed else (x,y,w,h)
                        x += w+gap
                        shelf_h = max(shelf_h,h)
                    if len(positions)==len(rows):
                        consider(positions,'balanced-shelves')
                        lower = amount
                    else:
                        upper = amount
        # Full-width rows and full-height columns: generate alternative groupings
        # while respecting each member's individual maximum area.
        for transposed in (False, True):
            W, H = (height, width) if transposed else (width, height)
            ratios = {r['id']: (r['h']/r['w'] if transposed else r['w']/r['h']) for r in rows}
            ordered = sorted(rows, key=lambda r: (ratios[r['id']], str(r['id'])))
            interleaved = []
            left, right = 0, len(ordered)-1
            while left <= right:
                interleaved.append(ordered[left]); left += 1
                if left <= right:
                    interleaved.append(ordered[right]); right -= 1
            orders = [rows, ordered, list(reversed(ordered)), interleaved]
            typical = math.sqrt(W * H / max(1., sum(ratios.values())))
            targets = [typical * 2 ** (step / 4) for step in range(-12, 13)]
            for order in orders:
                balanced_seen = set()
                for target in targets:
                    check()
                    strips, start = [], 0
                    while start < len(order):
                        total_ratio, max_height = 0., float('inf')
                        best_strip = None
                        # Limit the per-row grouping search; linear overall for
                        # large collections, with a range large enough for real windows.
                        for stop in range(start, min(len(order), start + 80)):
                            r = order[stop]
                            ratio = ratios[r['id']]
                            total_ratio += ratio
                            max_height = min(max_height, math.sqrt(high / (ratio * r['fill'])))
                            available = W - gap * (stop-start)
                            if available <= 0:
                                break
                            h = min(available / total_ratio, max_height)
                            score = abs(math.log(max(h, 1e-12) / target))
                            if best_strip is None or score < best_strip[0]:
                                best_strip = (score, stop+1, h)
                            if h < target:
                                break
                        _, stop, h = best_strip
                        strips.append((order[start:stop], h))
                        start = stop
                    variants = [strips]
                    if len(strips) not in balanced_seen:
                        balanced_seen.add(len(strips))
                        # Redistribute a short final row across all rows instead
                        # of magnifying a lone last image to fill the row.
                        balanced, start, remaining = [], 0, sum(ratios.values())
                        for slots in range(len(strips),0,-1):
                            desired, total_ratio, stop = remaining/slots, 0., start
                            while stop < len(order)-(slots-1):
                                addition = ratios[order[stop]['id']]
                                if stop > start and abs(total_ratio-desired) < abs(total_ratio+addition-desired):
                                    break
                                total_ratio += addition
                                stop += 1
                            group = order[start:stop]
                            h = (W-gap*(len(group)-1))/total_ratio
                            if h <= 0:
                                balanced = []
                                break
                            balanced.append((group,h))
                            remaining -= total_ratio
                            start = stop
                        if balanced:
                            variants.append(balanced)
                    for candidate in variants:
                        available_h = H - gap * (len(candidate)-1)
                        if available_h <= 0:
                            continue
                        factor = min(1., available_h / sum(h for group,h in candidate))
                        total_h = sum(h*factor for group,h in candidate) + gap * (len(candidate)-1)
                        y, positions = (H-total_h)/2, {}
                        for group, h in candidate:
                            h *= factor
                            row_width = sum(ratios[r['id']]*h for r in group) + gap*(len(group)-1)
                            x = (W-row_width)/2
                            for r in group:
                                w = ratios[r['id']] * h
                                positions[r['id']] = (y,x,h,w) if transposed else (x,y,w,h)
                                x += w + gap
                            y += h + gap
                        consider(positions, 'justified-columns' if transposed else 'justified-rows')

        # Larger collections use the linear strip candidates above. MaxRects
        # searches both equal-area and mildly size-weighted allocations.
        if len(rows) <= 160:
            for exponent in (0., .25, .5):
                weights = [(r['w']*r['h']*r['fill']) ** exponent for r in rows]
                total = sum(weights)
                weights = [w/total for w in weights]
                for heuristic in ('short', 'area'):
                    for ordering in ('area', 'side', 'ratio', 'reverse'):
                        lower, upper = 0., min(canvas*4, max(high / weight for weight in weights))
                        for iteration in range(13):
                            check()
                            amount = 0. if iteration == 0 else (lower+upper)/2
                            rectangles = []
                            for r, weight in zip(rows, weights):
                                fit_area = r['w']*r['h']*r['fill']*min(width/r['w'],height/r['h'])**2
                                area = max(low, min(high, fit_area, amount*weight))
                                # A zero lower bound still needs a positive rectangle.
                                scale = math.sqrt(max(area, 1e-12)/(r['w']*r['h']*r['fill']))
                                rectangles.append((r['id'], r['w']*scale, r['h']*scale))
                            if ordering == 'area':
                                rectangles.sort(key=lambda r: (-round(r[1]*r[2],6), str(r[0])))
                            elif ordering == 'side':
                                rectangles.sort(key=lambda r: (-max(r[1:]), str(r[0])))
                            elif ordering == 'ratio':
                                rectangles.sort(key=lambda r: (-r[1]/r[2],str(r[0])))
                            else:
                                rectangles.reverse()
                            positions = maxrects(rectangles, width, height, gap, heuristic, check)
                            if positions is None:
                                upper = amount
                                if iteration == 0:
                                    break
                            else:
                                consider(positions, 'maxrects-' + heuristic)
                                lower = amount
    except BudgetEnded:
        pass
    if cancelled():
        return {'positions': None, 'cancelled': True}
    if best:
        values = best['positions'].values()
        left = min(r[0] for r in values); top = min(r[1] for r in values)
        right = max(r[0]+r[2] for r in values); bottom = max(r[1]+r[3] for r in values)
        dx,dy = (width-right+left)/2-left, (height-bottom+top)/2-top
        best['positions'] = {key:(x+dx,y+dy,w,h) for key,(x,y,w,h) in best['positions'].items()}
    return best or {'positions': None, 'reason': '当前窗口与图片间距下未找到可行排列，请减小间距后重试。'}


def maxrects(rectangles, width, height, gap, heuristic, check=lambda: None):
    free = [(0., 0., width+gap, height+gap)]
    result = {}
    for key, image_w, image_h in rectangles:
        check()
        w, h = image_w+gap, image_h+gap
        fits = []
        for x,y,fw,fh in free:
            if w <= fw + 1e-8 and h <= fh + 1e-8:
                short = min(fw-w, fh-h)
                fits.append(((short if heuristic == 'short' else fw*fh-w*h, max(fw-w,fh-h),y,x), x,y))
        if not fits:
            return None
        _, x, y = min(fits)
        result[key] = (x,y,image_w,image_h)
        pieces = []
        for fx,fy,fw,fh in free:
            if x >= fx+fw-1e-8 or x+w <= fx+1e-8 or y >= fy+fh-1e-8 or y+h <= fy+1e-8:
                pieces.append((fx,fy,fw,fh))
                continue
            if x > fx+1e-8: pieces.append((fx,fy,x-fx,fh))
            if x+w < fx+fw-1e-8: pieces.append((x+w,fy,fx+fw-x-w,fh))
            if y > fy+1e-8: pieces.append((fx,fy,fw,y-fy))
            if y+h < fy+fh-1e-8: pieces.append((fx,y+h,fw,fy+fh-y-h))
        # Remove contained rectangles; capping the candidate set bounds memory
        # and time, never validity (discarding free space cannot create overlap).
        pieces = sorted(set(pieces), key=lambda r: -r[2]*r[3])[:256]
        free = []
        for piece in pieces:
            px,py,pw,ph = piece
            if not any(px>=fx-1e-8 and py>=fy-1e-8 and px+pw<=fx+fw+1e-8 and py+ph<=fy+fh+1e-8
                       for fx,fy,fw,fh in free):
                free.append(piece)
    return result
