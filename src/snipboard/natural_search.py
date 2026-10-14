"""Cancelable, metadata-only execution of natural-language query plans."""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from .query_plan import Condition, QueryPlan


@dataclass(frozen=True)
class SearchResult:
    rows: list[dict]
    matched_count: int
    truncated: bool = False
    cancelled: bool = False
    warnings: tuple[str, ...] = ()


def _compare(actual, operator, expected):
    return {'>=': actual >= expected, '<=': actual <= expected,
            '>': actual > expected, '<': actual < expected, '=': actual == expected}.get(operator, False)


def search_catalog(catalog, plan: QueryPlan, cancel=None, limit=200, *, scope_digests=None, archived=False):
    """Read saved metadata and features; never load or analyze image files.

    Cancellation does not modify the catalog. ``None`` means the full library;
    an empty scope means no images. Work-group/label scopes retain local copies
    just like Catalog.query_filtered. Results are capped, but the exact matching
    count is returned. Use a separate Catalog connection inside the Qt job.
    """
    limit = max(1, min(int(limit), 2000))
    cancelled = cancel or (lambda: False)
    warnings = list(plan.warnings)

    def stopped():
        return bool(cancelled())

    def cancellation_result():
        return SearchResult([], 0, cancelled=True, warnings=tuple(warnings))

    if stopped():
        return cancellation_result()
    if not plan.conditions or scope_digests is not None and not scope_digests:
        return SearchResult([], 0, warnings=tuple(warnings))

    leaf_conditions = []
    def flatten(condition):
        if condition.kind == 'any':
            for child in condition.value:
                flatten(child)
        else:
            leaf_conditions.append(condition)
    for condition in plan.conditions:
        flatten(condition)
    memberships = {}
    db = catalog.db
    # SQLite checks cancellation while it reads a large metadata catalog.
    db.set_progress_handler(lambda: 1 if stopped() else 0, 2000)
    try:
        for condition in leaf_conditions:
            if stopped():
                return cancellation_result()
            if condition.kind in ('label', 'work'):
                reference = f'work:{condition.value}' if condition.kind == 'work' else condition.value
                memberships[condition.id] = catalog._members(reference)

        allowed = None if scope_digests is None else set(scope_digests)
        def membership_scope(condition):
            if condition.kind in ('label', 'work'):
                return memberships[condition.id]
            if condition.kind == 'any':
                scopes = [membership_scope(child) for child in condition.value]
                if all(scope is not None for scope in scopes):
                    return set().union(*scopes)
            return None
        for condition in plan.conditions:
            members = membership_scope(condition)
            if members is not None:
                if condition.exclude:
                    if allowed is None:
                        allowed = {r[0] for r in db.execute('SELECT DISTINCT digest FROM items')}
                    allowed -= members
                else:
                    allowed = set(members) if allowed is None else allowed & members
        # Push saved numeric features into SQLite before JSON hydration. This
        # makes a narrow query cheap even when the full library is large.
        for condition in plan.conditions:
            kind, value = condition.kind, condition.value
            expression, parameters = None, ()
            if kind == 'dimension':
                field, operator, expected = value
                if field in ('width', 'height') and operator in ('>', '<', '>=', '<=', '='):
                    expression, parameters = f'{field} {operator} ?', (expected,)
            elif kind == 'orientation':
                expression = {'landscape': 'width > height * 1.05',
                              'portrait': 'width < height * .95',
                              'square': 'width >= height * .95 AND width <= height * 1.05'}.get(value)
            elif kind == 'ratio':
                target, tolerance = value
                expression = 'width >= height * ? AND width <= height * ?'
                parameters = (target * (1 - tolerance), target * (1 + tolerance))
            elif kind == 'brightness':
                operator, expected = value
                if operator in ('>', '<', '>=', '<=', '='):
                    expression, parameters = f"json_extract(data,'$.brightness') {operator} ?", (expected,)
            if expression is not None:
                if condition.exclude:
                    expression = f'NOT ({expression})'
                members = {r[0] for r in db.execute('SELECT digest FROM features WHERE height>0 AND (' + expression + ')', parameters)}
                allowed = members if allowed is None else allowed & members
            if stopped():
                return cancellation_result()
        retained = scope_digests is not None or any(not c.exclude and membership_scope(c) is not None for c in plan.conditions)
        keyword = next((c.value for c in plan.conditions if c.kind == 'keyword' and not c.exclude), '')
        rows = catalog.query(text=keyword, archived=None if archived is False and retained else archived, digests=allowed)

        properties = {'orientation', 'dimension', 'ratio', 'brightness', 'color'}
        if any(c.kind in properties for c in leaf_conditions) and any(not r.get('feature') for r in rows):
            warnings.append('部分图片缺少已保存的分析数据；会跳过无法确认属性的图片，不重新分析原图。')

        result = []
        count = 0
        color_conditions = {c.id: c for c in leaf_conditions if c.kind == 'color'}
        for offset in range(0, len(rows), 128):
            if stopped():
                return cancellation_result()
            batch = rows[offset:offset + 128]
            color_matches = {}
            for ident, condition in color_conditions.items():
                if stopped():
                    return cancellation_result()
                color, tolerance, coverage = condition.value
                color_matches[ident] = {r['digest'] for r in catalog.filter_colors(batch, [color], tolerance, coverage)}

            def match(condition, row):
                kind, value = condition.kind, condition.value
                if kind == 'unsupported':
                    return False
                if kind in properties and row.get('feature') is None:
                    return False
                if kind == 'any':
                    accepted = any(match(child, row) for child in value)
                elif kind in ('label', 'work'):
                    accepted = row['digest'] in memberships[condition.id]
                elif kind == 'keyword':
                    metadata = ' '.join([row.get('source_name', ''), row.get('note', ''),
                                         *row.get('manual_tags', []),
                                         *(str(name) for _, name in row.get('memberships', []))]).casefold()
                    accepted = str(value).casefold() in metadata
                elif kind == 'orientation':
                    ratio = row['width'] / max(row['height'], 1)
                    accepted = {'landscape': ratio > 1.05, 'portrait': ratio < .95,
                                'square': .95 <= ratio <= 1.05}.get(value, False)
                elif kind == 'dimension':
                    field, operator, expected = value
                    accepted = _compare(row[field], operator, expected)
                elif kind == 'ratio':
                    target, tolerance = value
                    accepted = abs(row['width'] / max(row['height'], 1) / target - 1) <= tolerance
                elif kind == 'brightness':
                    operator, expected = value
                    brightness = (row.get('feature') or {}).get('brightness')
                    if brightness is None:
                        return False
                    accepted = _compare(brightness, operator, expected)
                elif kind == 'color':
                    if not (row.get('feature') or {}).get('palette'):
                        return False
                    accepted = row['digest'] in color_matches[condition.id]
                elif kind == 'all':
                    accepted = True
                else:
                    accepted = False
                # Missing color/brightness information is unknown, not evidence
                # for an exclusion. Keep it out of both positive/negative sets.
                if condition.exclude and kind in ('color', 'brightness') and not row.get('feature'):
                    return False
                return not accepted if condition.exclude else accepted

            for row in batch:
                if stopped():
                    return cancellation_result()
                if all(match(condition, row) for condition in plan.conditions):
                    count += 1
                    if len(result) < limit:
                        result.append(row)
        if stopped():
            return cancellation_result()
        if count > limit:
            warnings.append(f'共匹配 {count} 张，显示前 {limit} 张；可增加条件缩小范围。')
        return SearchResult(result, count, count > limit, warnings=tuple(dict.fromkeys(warnings)))
    except sqlite3.OperationalError:
        if stopped():
            return cancellation_result()
        raise
    finally:
        db.set_progress_handler(None, 0)
