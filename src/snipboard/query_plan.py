"""Small, explicit natural-language query plans for local filtering.

This module parses catalog conditions, not image contents. Unknown descriptions
become metadata keywords so that the UI can explain exactly what was searched.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re


METADATA_WARNING = '内容词仅匹配已有标签、文件名和备注；轻量模式不识别图片里的姿态、物体或画风。'


@dataclass(frozen=True)
class Condition:
    id: str
    kind: str
    value: object
    text: str
    exclude: bool = False


@dataclass(frozen=True)
class QueryPlan:
    query: str = ''
    conditions: tuple[Condition, ...] = ()
    warnings: tuple[str, ...] = ()
    history: tuple[str, ...] = ()

    @property
    def chips(self):
        return self.conditions

    def without(self, condition_id):
        conditions = tuple(c for c in self.conditions if c.id != condition_id)
        warnings = self.warnings
        if not any(c.kind == 'keyword' or (c.kind == 'any' and any(k.kind == 'keyword' for k in c.value))
                   for c in conditions):
            warnings = tuple(w for w in warnings if w != METADATA_WARNING)
        return replace(self, conditions=conditions, warnings=warnings)


def _condition(kind, value, text, exclude=False):
    ident = hashlib.sha256(repr((kind, value, exclude)).encode('utf-8')).hexdigest()[:16]
    return Condition(ident, kind, value, ('排除：' if exclude else '') + text, exclude)


def _negative(text, position):
    # Negation lasts until punctuation or an explicit positive command.
    prefix = re.split(r'[,，;；。\n]', text[:position])[-1]
    markers = list(re.finditer(r'不要|排除|不含|不包含|没有|不看|去掉|剔除|只看|只留|保留|寻找|筛选|找', prefix))
    return bool(markers and markers[-1].group() in ('不要', '排除', '不含', '不包含', '没有', '不看', '去掉', '剔除'))


def parse_query(text, known_labels=(), groups=(), previous=None):
    """Parse Chinese requests without network, model loading or image access.

    Additional requests narrow a previous plan. Explicit ``重新找``/``清空``
    start a new plan; ``改成`` replaces the corresponding property. Labels and
    groups are the dictionaries returned by Catalog.label_rows/work_groups.
    """
    text = str(text).strip()[:1000]
    if not text:
        return previous if previous is not None else QueryPlan()
    reset = bool(re.match(r'^(?:重新(?:找|搜索|筛选)?|从头|重来|清空|新搜索)', text))
    base = () if previous is None or reset else previous.conditions
    warnings = [] if previous is None or reset else list(previous.warnings)
    matches = []
    occupied = [False] * len(text)

    def add(start, end, kind, value, title, exclude=None):
        if any(occupied[start:end]):
            return False
        for i in range(start, end):
            occupied[i] = True
        negative = _negative(text, start) if exclude is None else exclude
        matches.append((start, end, _condition(kind, value, title, negative)))
        return True

    def pattern(expression, factory):
        for match in re.finditer(expression, text, re.I):
            result = factory(match)
            if result:
                add(match.start(), match.end(), *result)

    # Explicit keyword quotes preserve complete phrases, including spaces.
    pattern(r'(?:关键词|关键字)\s*[:：]?\s*["“「]([^"”」]+)["”」]',
            lambda m: ('keyword', m[1], '关键词：' + m[1]))

    for match in re.finditer(r'当前(?:工作组|参考组)', text):
        current = next((g for g in groups if g.get('current')), None)
        if current:
            add(match.start(), match.end(), 'work', current['id'], '工作组：' + current['name'])
        else:
            add(match.start(), match.end(), 'unsupported', match.group(), '未设置当前工作组')
            warnings.append('尚未设置当前工作组，请先选择工作组。')

    # Require group context so a group called "人物" does not steal a tag name.
    for group in sorted(groups, key=lambda g: len(g['name']), reverse=True):
        name = group['name']
        for match in re.finditer(re.escape(name), text, re.I):
            before, after = text[:match.start()], text[match.end():]
            if (re.search(r'(?:在|工作组[:：]?|参考组[:：]?)[「“\"\s]*$', before)
                    or re.match(r'[」”\"\s]*(?:工作组|参考组|组)', after)):
                end = match.end()
                suffix = re.match(r'[」”\"\s]*(?:工作组|参考组|组)', after)
                if suffix:
                    end += suffix.end()
                add(match.start(), end, 'work', group['id'], '工作组：' + name)

    # Named labels have priority over property aliases (e.g. a tag "红衣").
    # A duplicated name is represented as a union rather than choosing one ID.
    label_names = {}
    for label in known_labels:
        label_names.setdefault(label['name'], []).append(label['id'])
    for name in sorted(label_names, key=len, reverse=True):
        if not name:
            continue
        expression = re.escape(name)
        if name.isascii():
            expression = r'(?<![\w])' + expression + r'(?![\w])'
        for match in re.finditer(expression, text, re.I):
            references = label_names[name]
            if len(references) == 1:
                add(match.start(), match.end(), 'label', references[0], '标签：' + name)
            else:
                children = tuple(_condition('label', ref, '标签：' + name) for ref in references)
                add(match.start(), match.end(), 'any', children, '标签：' + name + '（同名任一）')

    orientations = [('landscape', r'横向|横图|横版|横构图'),
                    ('portrait', r'竖向|竖图|竖版|竖构图'),
                    ('square', r'正方形|方形|方图|正方')]
    for value, expression in orientations:
        pattern(expression, lambda m, v=value: ('orientation', v, {'landscape': '横向', 'portrait': '竖向', 'square': '方形'}[v]))

    comparisons = {'至少': '>=', '不小于': '>=', '大于等于': '>=', '不低于': '>=',
                   '最多': '<=', '至多': '<=', '不大于': '<=', '不超过': '<=', '小于等于': '<=',
                   '大于': '>', '超过': '>', '小于': '<', '低于': '<', '等于': '=',
                   '是': '=', '为': '=', '>=': '>=', '<=': '<=', '≥': '>=', '≤': '<=',
                   '>': '>', '<': '<', '=': '=', '': '='}
    operator = '|'.join(re.escape(k) for k in sorted(comparisons, key=len, reverse=True) if k)
    dimension_re = rf'(宽度|高度|宽|高)\s*({operator})?\s*(\d+(?:\.\d+)?)\s*(?:像素|px)?'
    def dimension(match):
        field = 'width' if match[1].startswith('宽') else 'height'
        number = float(match[3])
        op = comparisons[match[2] or '']
        return 'dimension', (field, op, number), f'{"宽" if field == "width" else "高"}{op}{number:g} px'
    pattern(dimension_re, dimension)

    for match in re.finditer(r'(?:比例\s*(?:为|是|=|：|:)?\s*)?(\d+(?:\.\d+)?)\s*[:：/]\s*(\d+(?:\.\d+)?)', text):
        first, second = float(match[1]), float(match[2])
        if first <= 0 or second <= 0:
            add(match.start(), match.end(), 'unsupported', match.group(), '无效比例：' + match.group())
            warnings.append('图片比例的两边都必须大于 0。')
        else:
            add(match.start(), match.end(), 'ratio', (first / second, .05), f'比例 {first:g}:{second:g}（±5%）')

    def brightness(match):
        number = float(match[2])
        if not 0 <= number <= 100:
            warnings.append('亮度范围为 0–100，请修改条件。')
            return 'unsupported', match.group(), '无效亮度：' + match.group()
        op = comparisons[match[1] or '']
        return 'brightness', (op, number), f'亮度{op}{number:g}'
    pattern(rf'亮度\s*({operator})?\s*(\d+(?:\.\d+)?)\s*%?', brightness)
    pattern(r'偏亮|明亮(?:的)?(?:图片|图)?|亮一些|亮一点', lambda m: ('brightness', ('>=', 65.), '偏亮（亮度≥65）'))
    pattern(r'偏暗|暗一些|暗一点|较暗(?:的)?(?:图片|图)?', lambda m: ('brightness', ('<=', 40.), '偏暗（亮度≤40）'))

    colors = {'红': '#e34242', '橙': '#f29940', '黄': '#f2cd40', '绿': '#43ad65',
              '青': '#40c5c0', '蓝': '#4385db', '紫': '#9563d9', '粉': '#ed91bb',
              '白': '#f5f5f5', '黑': '#151515', '灰': '#8d9298'}
    # Whole-image color is deliberately not used for "红色衣服/蓝色眼睛".
    for name, color in colors.items():
        pattern(name + r'色(?:调)?(?!衣|服|裙|眼|头发|鞋|帽|唇|人物)',
                lambda m, c=color, n=name: ('color', (c, 38., .08), n + '色（全图色彩占比≥8%）'))
    pattern(r'#[0-9a-f]{6}\b', lambda m: ('color', (m.group().lower(), 30., .08), m.group().lower() + '（全图色彩占比≥8%）'))

    pattern(r'全部图片|所有图片|全部图库|所有图|全部图', lambda m: ('all', True, '全部图片'))

    # Keep the spans: explicit OR applies only between adjacent conditions,
    # while subsequent orientation/size requirements remain conjunctive.
    residual = ''.join(' ' if occupied[i] else char for i, char in enumerate(text))
    filler = (r'当前|工作组|参考组|标签|关键词|关键字|筛选|搜索|'
              r'寻找|找出|找一下|帮我|给我|我想要|我想|请|再|只看|只留|保留|'
              r'不要|排除|不包含|不含|没有|不看|去掉|剔除|最好有|最好|需要|'
              r'重新|从头|重来|清空|新搜索|改成|换成|变成|里面|其中|里|的|找')
    for match in re.finditer(r'[^,，;；。\s、]+', residual):
        piece = match.group()
        # Quoted keywords retain literal punctuation and connector words.
        if len(piece) >= 2 and piece[0] in '"“「' and piece[-1] in '"”」':
            value = piece[1:-1]
            if value:
                add(match.start(), match.end(), 'keyword', value, '关键词：' + value)
            continue
        # Remove command words at the boundary only. Global replacement would
        # silently change genuine descriptions such as "中世纪构图".
        cleaned = piece.strip('「」“”"():：')
        cleaned = re.sub(r'^(?:' + filler + r')+', '', cleaned)
        cleaned = re.sub(r'(?:的)?(?:图片|图像|的图)$|的$', '', cleaned)
        cleaned = cleaned.strip('「」“”"():：')
        if cleaned in ('在', '中', '图', '图片', '图像'):
            continue
        if not cleaned or cleaned in ('和', '与', '并且', '而且', '或', '或者', '及', '并', '且', 'and', 'or'):
            continue
        # Split explicit connectors; do not invent Chinese word segmentation.
        offset = 0
        for part in re.split(r'(或者|或|并且|而且|和|与|及|且|\band\b|\bor\b)', cleaned):
            if part and part not in ('或者', '或', '并且', '而且', '和', '与', '及', '且', 'and', 'or'):
                found = text.find(part, match.start() + offset, match.end())
                if found >= 0:
                    add(found, found + len(part), 'keyword', part, '关键词：' + part)
                else:
                    add(match.start(), match.end(), 'keyword', part, '关键词：' + part)
            offset += len(part)

    matches.sort(key=lambda entry: entry[0])
    conditions = []
    index = 0
    while index < len(matches):
        start, end, condition = matches[index]
        children = [condition]
        while index + 1 < len(matches):
            next_start, next_end, other = matches[index + 1]
            between = text[end:next_start]
            if condition.exclude != other.exclude or not re.search(r'或者|或|\bor\b', between, re.I):
                break
            children.append(other)
            end = next_end
            index += 1
        if len(children) > 1:
            # Exclude (A OR B), rather than OR-ing the two negations.
            positive = tuple(replace(c, exclude=False, text=c.text.removeprefix('排除：')) for c in children)
            condition = _condition('any', positive, ' 或 '.join(c.text for c in positive), condition.exclude)
        conditions.append(condition)
        index += 1

    if re.search(r'改成|换成|变成', text):
        replacement_kinds = {c.kind for c in conditions if c.kind not in ('label', 'keyword', 'all')}
        base = tuple(c for c in base if c.kind not in replacement_kinds)
    combined = tuple({c.id: c for c in (*base, *conditions)}.values())
    def contains_keyword(condition):
        return condition.kind == 'keyword' or (condition.kind == 'any' and any(contains_keyword(c) for c in condition.value))
    if any(contains_keyword(c) for c in combined):
        warnings.append(METADATA_WARNING)
    if not combined:
        warnings.append('没有识别到筛选条件；请输入标签、关键词、方向、尺寸或颜色。')
    if len({c.value for c in combined if c.kind == 'orientation' and not c.exclude}) > 1:
        warnings.append('同时要求多种图片方向会得到空结果；可移除条件或使用“或”。')
    history = (() if previous is None or reset else previous.history) + (text,)
    return QueryPlan(text, combined, tuple(dict.fromkeys(warnings)), history[-20:])
