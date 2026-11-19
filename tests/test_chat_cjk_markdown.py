"""Synthetic Chinese punctuation fixtures for Markdown rendering."""
from test_chat_ui import render_markdown


def test_cjk_label_next_to_body_and_full_conclusion():
    doc = render_markdown('**数据说明：**查看样本。 **样例：第二天较高。**继续测试。')
    assert [s.text for s in doc.find_all('strong')] == ['数据说明：', '样例：第二天较高。']
    assert '**' not in doc.text


def test_code_escapes_and_unclosed_delimiters_remain_literal():
    doc = render_markdown('`**数据说明：**查询`\n\n```text\n**结论：**如你\n```\n\n\\*\\*数据说明：\\*\\*查询\n\n**尚未结束：')
    assert not doc.find('strong')
    assert '**数据说明：**查询' in doc.code.text
    assert '**尚未结束：' in doc.text


def test_plain_cjk_rule_does_not_expand_html_or_unsafe_links():
    doc = render_markdown('**数据说明：**查询<script>alert(1)</script> [入口](javascript:alert(1))')
    assert not doc.find('script')
    assert not doc.find('a')
    assert doc.strong.text == '数据说明：'


def test_cjk_labels_adjacent_to_numbers():
    doc = render_markdown('**字段甲，字段乙：**11和12为样本编号。 **批次号：**16号样本。')
    assert len(doc.find_all('strong')) == 2
    assert '**' not in doc.text


def test_synthetic_status_does_not_leak_numeric_entities():
    source = '**样本记录已生成。**请查看示例表格。'
    doc = render_markdown(source)
    assert doc.strong.text == '样本记录已生成。'
    assert doc.p.text == '样本记录已生成。请查看示例表格。'
    assert '&#x' not in doc.text and '**' not in doc.text


def test_history_narrative_preserves_original_chinese_and_html_boundary():
    import json
    from xclaw.ui_narrative import compile_narrative
    source = '**样本记录已生成。**请查看示例表格。\n\n<script>alert(1)</script> [入口](jav&#x61;script:alert(1))\n\n`&#x5DF2;`'
    narrative = json.loads(compile_narrative(json.dumps({'placements': [{'after': 0, 'source': 'root = Col([WatchlistCard("自选股", q)])'}]}), source))
    texts = [b['source'] for b in narrative['blocks'] if b['type'] == 'text']
    assert '\n\n'.join(texts) == source
    rendered = [render_markdown(text) for text in texts]
    assert rendered[0].p.text == '样本记录已生成。请查看示例表格。'
    assert not rendered[1].find('script') and not rendered[1].find('a')
    # Literal entities in code must remain literal, not be globally unescaped.
    assert rendered[2].code.text == '&#x5DF2;'
