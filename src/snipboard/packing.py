"""Fixed-window, uncropped collage layout with balanced image sizes.

A slicing tree joins two rectangles horizontally or vertically. Every subtree
obeys W = aspect * H + offset * gap, so fixed pixel gutters remain exact even
through nested joins. Tree search changes the joins and image assignment instead
of leaving fixed-size rectangles in the holes of a bin packer.

Independent implementation of slicing-tree collage ideas; background reference:
https://github.com/mendrik/diorama-2023
Coordinates and gaps use logical viewport pixels. Rotated images are represented
by their existing bounding boxes; ``fill`` measures visible area inside a box.
"""
import math
import random
import time


class _BudgetEnded(Exception):
    pass


class _Node:
    __slots__ = ('aspect', 'offset', 'count', 'fill', 'squares', 'smallest',
                 'largest', 'logs', 'log_squares', 'min_height', 'left', 'right',
                 'horizontal', 'key')

    def __init__(self, aspect, key=None, fill=1.):
        self.aspect = aspect
        self.offset = 0.
        self.count = 1
        self.fill = fill
        self.squares = fill * fill
        self.smallest = self.largest = fill
        self.logs = math.log(fill)
        self.log_squares = self.logs * self.logs
        self.min_height = 0.
        self.left = self.right = None
        self.horizontal = False
        self.key = key


def _join(left, right, horizontal):
    node = object.__new__(_Node)
    if horizontal:
        node.aspect = left.aspect + right.aspect
        node.offset = left.offset + right.offset + 1.
        portion = left.aspect / node.aspect
        node.min_height = max(left.min_height, right.min_height)
    else:
        node.aspect = left.aspect * right.aspect / (left.aspect + right.aspect)
        node.offset = node.aspect * (left.offset / left.aspect + right.offset / right.aspect - 1.)
        portion = right.aspect / (left.aspect + right.aspect)
        # Minimum parent height, measured in gap units, that leaves every
        # descendant a positive image rectangle.
        node.min_height = max(
            (left.min_height * left.aspect - node.offset + left.offset) / node.aspect,
            (right.min_height * right.aspect - node.offset + right.offset) / node.aspect)
    other = 1. - portion
    lp, rp = math.log(portion), math.log(other)
    node.count = left.count + right.count
    node.fill = portion * left.fill + other * right.fill
    node.squares = portion ** 2 * left.squares + other ** 2 * right.squares
    node.smallest = min(portion * left.smallest, other * right.smallest)
    node.largest = max(portion * left.largest, other * right.largest)
    node.logs = left.logs + left.count * lp + right.logs + right.count * rp
    node.log_squares = (left.log_squares + 2 * lp * left.logs + left.count * lp * lp +
                        right.log_squares + 2 * rp * right.logs + right.count * rp * rp)
    node.left, node.right = left, right
    node.horizontal = horizontal
    node.key = None
    return node


def _fit(tree, width, height, gap):
    h = min(height, (width - tree.offset * gap) / tree.aspect)
    if h <= max(0., tree.min_height * gap) + 1e-9:
        return None
    return tree.aspect * h + tree.offset * gap, h


def _penalty(variation, spread, smallest, largest):
    # Soft size balance: favor similar areas without imposing hard percentages
    # that make extreme panorama/portrait collections impossible to arrange.
    return .20 * variation + .055 * math.log(largest / smallest) + .05 * spread


def _estimate(tree, width, height, gap):
    bounds = _fit(tree, width, height, gap)
    if bounds is None:
        return -float('inf')
    w, h = bounds
    variation = math.sqrt(max(0., tree.count * tree.squares / tree.fill ** 2 - 1.))
    mean_log = math.log(tree.count / tree.fill)
    spread = math.sqrt(max(0., tree.log_squares / tree.count +
                           2 * mean_log * tree.logs / tree.count + mean_log ** 2))
    return w * h * tree.fill / (width * height) - _penalty(
        variation, spread, tree.smallest, tree.largest)


def _positions(tree, width, height, gap):
    bounds = _fit(tree, width, height, gap)
    if bounds is None:
        return None
    w, h = bounds
    pending = [(tree, (width - w) / 2, (height - h) / 2, w, h)]
    result = {}
    while pending:
        node, x, y, w, h = pending.pop()
        if min(w, h) <= 1e-9:
            return None
        if node.count == 1:
            result[node.key] = (x, y, w, h)
        elif node.horizontal:
            left_width = node.left.aspect * h + node.left.offset * gap
            pending.append((node.right, x + left_width + gap, y, w - left_width - gap, h))
            pending.append((node.left, x, y, left_width, h))
        else:
            top_height = (w - node.left.offset * gap) / node.left.aspect
            pending.append((node.right, x, y + top_height + gap, w, h - top_height - gap))
            pending.append((node.left, x, y, w, top_height))
    return result


def _merge_all(nodes, horizontal):
    if len(nodes) == 1:
        return nodes[0]
    middle = len(nodes) // 2
    return _join(_merge_all(nodes[:middle], horizontal),
                 _merge_all(nodes[middle:], horizontal), horizontal)


def _build(nodes, target, rng, random_axes=False):
    if len(nodes) == 1:
        return nodes[0]
    middle = len(nodes) // 2
    horizontal = (bool(rng.randrange(2)) if random_axes else
                  target > math.exp(sum(math.log(n.aspect) for n in nodes) / len(nodes)))
    portion = middle / len(nodes)
    left = _build(nodes[:middle], target * portion if horizontal else target / portion, rng, random_axes)
    right = _build(nodes[middle:], target * (1 - portion) if horizontal else target / (1 - portion), rng, random_axes)
    return _join(left, right, horizontal)


def _strip_trees(nodes, width, height, gap, check):
    """Area-guided strips seed the tree search, especially for large sets."""
    for transposed in (False, True):
        w, h = (height, width) if transposed else (width, height)
        order = sorted(nodes, key=lambda n: (1 / n.aspect if transposed else n.aspect, str(n.key)))
        ratios = [1 / node.aspect if transposed else node.aspect for node in order]
        seen = set()
        balanced_counts = set()
        for step in range(80):
            check()
            target_area = width * height / len(nodes) * 2 ** ((step - 40) / 25)
            groups, boundaries, start = [], [], 0
            while start < len(order):
                total, chosen = 0., None
                for stop in range(start, len(order)):
                    total += ratios[stop]
                    count = stop - start + 1
                    strip_height = (w - gap * (count - 1)) / total
                    if strip_height <= 0:
                        break
                    area = total * strip_height ** 2 / count
                    error = abs(math.log(area / target_area))
                    if chosen is None or error < chosen[0]:
                        chosen = error, stop + 1
                    if area < target_area:
                        break
                if chosen is None:
                    break
                stop = chosen[1]
                groups.append(order[start:stop])
                boundaries.append(stop)
                start = stop
            key = tuple(boundaries)
            if start == len(order) and key not in seen:
                seen.add(key)
                yield _merge_all([_merge_all(group, not transposed) for group in groups], transposed)
                # Redistribute an incomplete final strip across all strips.
                # This also preserves the near-uniform 4/4/5 grid for 13 square
                # images instead of allowing a lone oversized final tile.
                strips = len(groups)
                if strips not in balanced_counts:
                    balanced_counts.add(strips)
                    small, extra = divmod(len(order), strips)
                    balanced, offset = [], 0
                    for index in range(strips):
                        length = small + (index >= strips - extra)
                        balanced.append(_merge_all(order[offset:offset + length], not transposed))
                        offset += length
                    yield _merge_all(balanced, transposed)


def _clustered(nodes, target, rng):
    nodes = list(nodes)
    while len(nodes) > 1:
        nodes.sort(key=lambda n: n.aspect)
        joined = []
        for index in range(0, len(nodes) - 1, 2):
            left, right = nodes[index:index + 2]
            vertical = _join(left, right, False)
            horizontal = _join(left, right, True)
            prefer_vertical = abs(math.log(vertical.aspect / target)) < abs(math.log(horizontal.aspect / target))
            if rng.random() < .3:
                prefer_vertical = not prefer_vertical
            joined.append(vertical if prefer_vertical else horizontal)
        if len(nodes) % 2:
            joined.append(nodes[-1])
        nodes = joined
    return nodes[0]


def _leaf(tree, index):
    while tree.count > 1:
        if index < tree.left.count:
            tree = tree.left
        else:
            index -= tree.left.count
            tree = tree.right
    return tree


def _replace(tree, index, leaf):
    if tree.count == 1:
        return leaf
    if index < tree.left.count:
        return _join(_replace(tree.left, index, leaf), tree.right, tree.horizontal)
    return _join(tree.left, _replace(tree.right, index - tree.left.count, leaf), tree.horizontal)


def _reshape(tree, rng):
    if tree.count == 1:
        return tree
    if tree.count == 2 or rng.random() < 1 / tree.count:
        if tree.count > 2 and rng.random() < .6:
            if tree.left.count > 1 and (tree.right.count == 1 or rng.random() < .5):
                child = tree.left
                return _join(child.left, _join(child.right, tree.right, tree.horizontal), child.horizontal)
            child = tree.right
            return _join(_join(tree.left, child.left, tree.horizontal), child.right, child.horizontal)
        return _join(tree.left, tree.right, not tree.horizontal)
    if rng.random() < tree.left.count / tree.count:
        return _join(_reshape(tree.left, rng), tree.right, tree.horizontal)
    return _join(tree.left, _reshape(tree.right, rng), tree.horizontal)


def _mutate(tree, rng):
    if tree.count > 2 and rng.random() < .4:
        first, second = rng.sample(range(tree.count), 2)
        a, b = _leaf(tree, first), _leaf(tree, second)
        return _replace(_replace(tree, first, b), second, a)
    return _reshape(tree, rng)



def _pair_layouts(nodes, width, height, gap):
    """Compare independently scaled pairs as well as aligned slicing joins.

    Include equal visible areas and the corners where an image touches the
    viewport limit; forcing equal heights/widths can waste space for two images.
    """
    sizes = [(math.sqrt(n.aspect / n.fill), math.sqrt(1 / (n.aspect * n.fill)))
             for n in nodes]
    (aw, ah), (bw, bh) = sizes
    ratios = {0., 2 * math.log(aw / bw), 2 * math.log(ah / bh)}
    for horizontal in (True, False):
        limit, cross = (width, height) if horizontal else (height, width)
        a, ac = (aw, ah) if horizontal else (ah, aw)
        b, bc = (bw, bh) if horizontal else (bh, bw)
        if limit <= gap:
            continue
        anchors = set(ratios)
        # One image reaches the cross-axis limit and the pair spans the other.
        sa = cross / ac
        sb = (limit - gap - a * sa) / b
        if sb > 0:
            anchors.add(2 * math.log(sb / sa))
        sb = cross / bc
        sa = (limit - gap - b * sb) / a
        if sa > 0:
            anchors.add(2 * math.log(sb / sa))
        low, high = min(anchors), max(anchors)
        anchors.update(low + (high - low) * step / 64 for step in range(65))
        for log_ratio in sorted(anchors):
            relative = math.exp(log_ratio / 2)
            scale = min((limit - gap) / (a + b * relative),
                        cross / max(ac, bc * relative))
            first_w, first_h = aw * scale, ah * scale
            second_w, second_h = bw * relative * scale, bh * relative * scale
            if horizontal:
                left = (width - first_w - second_w - gap) / 2
                first = left, (height - first_h) / 2, first_w, first_h
                second = left + first_w + gap, (height - second_h) / 2, second_w, second_h
            else:
                top = (height - first_h - second_h - gap) / 2
                first = (width - first_w) / 2, top, first_w, first_h
                second = (width - second_w) / 2, top + first_h + gap, second_w, second_h
            yield {nodes[0].key: first, nodes[1].key: second}


def compact(items, width, height, gap=3, seconds=1.2, cancelled=lambda: False):
    rows = list(items)
    if not rows:
        return {'positions': {}, 'occupancy': 0., 'method': 'empty'}
    if not all(math.isfinite(value) for value in (width, height, gap)) or not (width > 0 and height > 0 and gap >= 0):
        raise ValueError('窗口尺寸或图片间距无效')
    nodes = []
    for row in rows:
        if not all(math.isfinite(row[key]) and row[key] > 0 for key in ('w', 'h')):
            raise ValueError('图片尺寸无效')
        fill = row.get('fill', 1.)
        if not math.isfinite(fill) or not 0 < fill <= 1.000001:
            raise ValueError('图片面积无效')
        # Repeated arrange commands pass dimensions produced by the previous
        # layout. Normalize their ratios so floating-point noise cannot change
        # ordering/search; source pixel dimensions never determine image sizes.
        ratio = float(format(row['w'] / row['h'], '.11g'))
        fill = float(format(min(fill, 1.), '.11g'))
        nodes.append(_Node(ratio, row['id'], fill))
    nodes.sort(key=lambda node: (node.aspect, str(node.key)))
    if len({node.key for node in nodes}) != len(nodes):
        raise ValueError('图片标识重复')
    end = time.monotonic() + max(0., seconds)
    rng = random.Random(2107)
    canvas = width * height
    best = None
    search_best, search_score = None, -float('inf')
    def check():
        if cancelled() or time.monotonic() >= end:
            raise _BudgetEnded()
    def consider_positions(positions, method='slicing-tree'):
        nonlocal best
        areas = [positions[node.key][2] * positions[node.key][3] * node.fill for node in nodes]
        total = sum(areas)
        mean = total / len(areas)
        variation = math.sqrt(sum((area / mean - 1.) ** 2 for area in areas) / len(areas))
        spread = math.sqrt(sum(math.log(area / mean) ** 2 for area in areas) / len(areas))
        smallest, largest = min(areas) / mean, max(areas) / mean
        occupancy = total / canvas
        score = occupancy - _penalty(variation, spread, smallest, largest)
        if best is None or score > best['score'] + 1e-10:
            best = dict(positions=positions, occupancy=occupancy, method=method,
                        score=score, area_variation=variation, min_relative_area=smallest,
                        max_relative_area=largest)
    def consider(tree):
        nonlocal search_best, search_score
        estimated = _estimate(tree, width, height, gap)
        if estimated <= search_score + 1e-10:
            return
        positions = _positions(tree, width, height, gap)
        if positions is None:
            return
        search_best, search_score = tree, estimated
        consider_positions(positions)
    try:
        if cancelled():
            raise _BudgetEnded()
        # A bounded, deterministic candidate exists before iterative search.
        consider(_build(nodes, width / height, rng))
        if len(nodes) <= 2:
            if len(nodes) == 2:
                consider(_join(nodes[0], nodes[1], True))
                consider(_join(nodes[0], nodes[1], False))
                for positions in _pair_layouts(nodes, width, height, gap):
                    consider_positions(positions, 'balanced-pair')
            if cancelled():
                return {'positions': None, 'cancelled': True}
            return best or {'positions': None, 'reason': '当前窗口与图片间距下未找到可行排列，请减小间距后重试。'}
        for candidate in _strip_trees(nodes, width, height, gap, check):
            consider(candidate)
        if len(nodes) > 1:
            for restart in range(12):
                check()
                if restart == 1:
                    nodes = nodes[::2] + nodes[1::2]
                elif restart > 1:
                    rng.shuffle(nodes)
                if restart < 4 and search_best is not None:
                    tree = search_best
                elif restart % 2 == 0:
                    tree = _clustered(nodes, width / height, rng)
                else:
                    tree = _build(nodes, width / height, rng, bool(restart % 3))
                current = _estimate(tree, width, height, gap)
                consider(tree)
                for step in range(2500):
                    if step % 32 == 0:
                        check()
                    candidate = _mutate(tree, rng)
                    score = _estimate(candidate, width, height, gap)
                    temperature = max(.00001, .01 * (1 - step / 2500))
                    if score > current or (math.isfinite(score) and rng.random() < math.exp(min(0., (score - current) / temperature))):
                        tree, current = candidate, score
                        consider(tree)
    except _BudgetEnded:
        pass
    if cancelled():
        return {'positions': None, 'cancelled': True}
    return best or {'positions': None, 'reason': '当前窗口与图片间距下未找到可行排列，请减小间距后重试。'}
