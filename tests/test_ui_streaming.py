import json
import pytest
from xclaw.ui_streaming import PlacementStream

REPLY='结论。\n\n行情。\n\n限制。'
SOURCE='q = Query("stock_quote", {symbol:"600519"})\nroot = Col([QuoteCard("样本😀",q)])'
def run(parts):
 parser=PlacementStream(REPLY);events=[]
 for part in parts:events+=parser.feed(part)
 return parser,events

def test_every_single_character_boundary_and_unicode_escape():
 text=json.dumps({'placements':[{'after':1,'source':SOURCE}]},ensure_ascii=True)
 parser,events=run(list(text));assert parser.finish(text)
 assert events[0]=={'event':'placement_start','data':{'id':'p1u0','after':1}}
 assert ''.join(e['data']['text'] for e in events if e['event']=='placement_delta')==SOURCE
 assert events[-1]['event']=='placement_end'
 assert len([e for e in events if e['event']=='placement_delta'])>10

def test_partial_source_is_delivered_before_string_or_object_closes():
 text=json.dumps({'placements':[{'after':0,'source':SOURCE}]},ensure_ascii=False)
 split=text.index('root')
 parser=PlacementStream(REPLY);early=parser.feed(text[:split])
 assert any(e['event']=='placement_delta' and 'Query' in e['data']['text'] for e in early)
 assert not any(e['event']=='placement_end' for e in early)
 with pytest.raises(ValueError):parser.finish(text[:split])
 parser.feed(text[split:]);assert parser.finish(text)

def test_anchor_ids_match_final_grouping_even_for_unordered_anchors():
 text=json.dumps({'placements':[{'after':2,'source':SOURCE},{'after':0,'source':SOURCE},{'after':2,'source':SOURCE}]})
 p,e=run([text]);out=json.loads(p.finish(text))
 assert [x['data']['id'] for x in e if x['event']=='placement_start']==['p2u0','p0u0','p2u1']
 assert [x['id'] for x in out['blocks'] if x['type']=='openui']==['p0u0','p2u0','p2u1']

@pytest.mark.parametrize('text', ['{"placements":[{"after":true,"source":"root=Col([])"}]}','{"placements":[{"after":99,"source":"root=Col([])"}]}','{"placements":[],"rewrite":"x"}','{"placements":[{"after":0,"source":"root=Col([])","x":1}]}','{"placements":[],}','{"placements":[]} trailing','{"placements":[{"source":"root=Col([])","after":0}]}','{"placements":[{"after":0,"source":"\\ud800oops"}]}'])
def test_malformed_stream_is_not_repaired(text):
 with pytest.raises(ValueError):
  p,_=run(list(text));p.finish(text)

@pytest.mark.parametrize('suffix',['\\','\\u','\\u12','\\ud83d','\\ud83d\\u'])
def test_incomplete_escape_emits_no_fabricated_character(suffix):
 prefix='{"placements":[{"after":0,"source":"root=Col([Text(\\"'
 p,e=run([prefix,suffix]);assert not any('\ufffd' in x['data'].get('text','') for x in e)
 with pytest.raises(ValueError):p.finish(prefix+suffix)

def test_empty_and_legacy_empty_final_only():
 for text in ['{"placements":[]}','root = Col([])']:
  p,e=run(list(text));assert not e;assert p.finish(text)=='root = Col([])'

def test_rewritten_prose_is_rejected_at_end_of_placement():
 with pytest.raises(ValueError):run([json.dumps({'placements':[{'after':0,'source':'root=Col([Text("rewrite")])'}]})])
