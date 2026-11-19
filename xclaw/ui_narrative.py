"""Validated placement of UI between immutable answer paragraphs, never model-rewritten prose."""
import json
import re


def paragraph_blocks(reply):
    # Reference definitions may resolve links in any paragraph, so retain their
    # parser scope. Fences, unlike references, only bind their own contents.
    if re.search(r'(?m)^\s*\[[^\]]+\]:', reply):
        return [reply] if reply.strip() else []
    fenced_ranges = []
    fence = None
    offset = 0
    for line in reply.splitlines(keepends=True):
        body = line.rstrip('\r\n')
        if fence is not None:
            marker, length, begin = fence
            if re.fullmatch(r'[ \t]{0,3}' + re.escape(marker) + '{' + str(length) + r',}[ \t]*', body):
                # The newline after the closer belongs to the paragraph separator,
                # not the fenced scope; allow a normal split immediately after it.
                fenced_ranges.append((begin, offset + len(body)))
                fence = None
        else:
            opening = re.fullmatch(r'[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)', body)
            if opening and not (opening[1][0] == '`' and '`' in opening[2]):
                fence = (opening[1][0], len(opening[1]), offset)
        offset += len(line)
    if fence is not None:
        fenced_ranges.append((fence[2], len(reply)))
    parts = []
    start = 0
    for separator in re.finditer(r'\n[ \t]*\n', reply):
        if any(begin <= separator.start() < end for begin, end in fenced_ranges):
            continue
        part = reply[start:separator.start()]
        if part.strip():
            parts.append(part)
        start = separator.end()
    if reply[start:].strip():
        parts.append(reply[start:])
    result = []
    is_list = lambda text: bool(re.match(r'^\s*(?:[-+*]|\d+[.)])\s', text))
    for part in parts:
        if result and is_list(result[-1]) and (is_list(part) or part.startswith(('  ', '\t'))):
            result[-1] += '\n\n' + part
        else:
            result.append(part)
    return result


def compile_narrative(plan_text, reply):
    # Empty legacy output is safe and simply preserves the original answer.
    if re.fullmatch(r'\s*root\s*=\s*Col\(\s*\[\s*\]\s*\)\s*;?\s*', plan_text):
        return 'root = Col([])'
    plan = json.loads(plan_text)
    if not isinstance(plan, dict) or set(plan) != {'placements'}:
        raise ValueError('invalid narrative plan')
    placements = plan['placements']
    paragraphs = paragraph_blocks(reply)
    if not isinstance(placements, list) or len(placements) > 6:
        raise ValueError('too many placements')
    grouped = {}
    for item in placements:
        if not isinstance(item, dict) or set(item) != {'after', 'source'}:
            raise ValueError('invalid placement')
        anchor, source = item['after'], item['source']
        if type(anchor) is not int or not 0 <= anchor < len(paragraphs):
            raise ValueError('invalid paragraph anchor')
        if not isinstance(source, str) or not source.strip() or len(source) > 40000:
            raise ValueError('invalid component source')
        # Prose comes exclusively from the original answer, never a second model rewrite.
        code = re.sub(r'"(?:\\.|[^"\\])*"', '""', source)
        if re.search(r'\b(?:Text|Heading)\s*\(', code):
            raise ValueError('narrative prose must use original paragraphs')
        if not re.search(r'\broot\s*=', source):
            raise ValueError('component block must be self contained')
        grouped.setdefault(anchor, []).append(source)
    if not placements:
        return 'root = Col([])'
    blocks = []
    for index, paragraph in enumerate(paragraphs):
        blocks.append({'type': 'text', 'source': paragraph, 'id': f'p{index}'})
        for offset, source in enumerate(grouped.get(index, [])):
            blocks.append({'type': 'openui', 'source': source, 'id': f'p{index}u{offset}'})
    return json.dumps({'version': 'xclaw-narrative-1', 'blocks': blocks}, ensure_ascii=False)
