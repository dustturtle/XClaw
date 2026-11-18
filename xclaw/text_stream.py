"""Ephemeral text observation only; never admission or business execution."""
from contextvars import ContextVar
from collections import OrderedDict
current_text_sink = ContextVar('receipt_text_sink', default=None)

class TextStreams:
    def __init__(self, capacity=256, max_chars=262144):
        self.states=OrderedDict();self.capacity=capacity;self.max_chars=max_chars
    async def publish(self, identity, event):
        kind=event.get('type',event.get('event'))
        if kind not in {'reset','delta','terminal','ui_snapshot','ui_commit','draft_failed'}:return
        state=self.states.setdefault(identity,{'seq':0,'round':None,'text':'','terminal':False,'truncated':False})
        if state['terminal']:return
        old_round, new_round = state.get('round'), event.get('round')
        if isinstance(old_round, int) and isinstance(new_round, int) and new_round < old_round:return
        if kind in {'ui_snapshot','ui_commit','draft_failed'} and event.get('round') != state['round']:return
        if state.get('draft_failed') and kind in {'ui_snapshot','ui_commit'}:return
        state['seq']+=1
        if kind=='reset':state.update(text='',round=event.get('round'),truncated=False,presentation=None,draft_failed=False,reply_hash=None,presentation_id=None,message_id=None)
        elif kind=='delta':
            if event.get('round')!=state['round']:state.update(text='',round=event.get('round'),truncated=False,presentation=None,draft_failed=False,reply_hash=None,presentation_id=None,message_id=None)
            text=state['text']+str(event.get('text',''))
            state['truncated']=len(text)>self.max_chars
            state['text']=text[:self.max_chars]
        elif kind in {'ui_snapshot','ui_commit'}:
            import json
            from xclaw.inline_ui_stream import MAX_SNAPSHOT_BYTES
            if len(json.dumps(event['presentation'], ensure_ascii=False).encode('utf-8')) > MAX_SNAPSHOT_BYTES:
                state.update(presentation=None,draft_failed=True)
                return
            from copy import deepcopy
            state['text']=event['text'][:self.max_chars]
            state['presentation']=deepcopy(event['presentation'])
            state['truncated']=len(event['text'])>self.max_chars
            if event.get('reply_hash'):state['reply_hash']=event['reply_hash']
            for key in ('presentation_id','message_id'):
                if key in event:state[key]=event[key]
        elif kind=='draft_failed':
            prefix = event.get('text')
            if isinstance(prefix, str):
                prefix.encode('utf-8', errors='strict')
                state['text'] = prefix[:self.max_chars]
                state['truncated'] = len(prefix) > self.max_chars
            state.update(presentation=None,draft_failed=True)
        else:
            state['terminal']=True
            if state.get('presentation') and not state['presentation'].get('validated'):
                state.update(presentation=None,draft_failed=True)
        if (kind in {"delta", "ui_snapshot", "ui_commit"}
                or kind == "draft_failed" and isinstance(event.get("text"), str)):
            from xclaw.stage_timing import visible
            visible(identity, state["round"], kind, state["text"])
        self.states.move_to_end(identity)
        while len(self.states)>self.capacity:self.states.popitem(last=False)
    def snapshot(self, identity):
        from copy import deepcopy
        return deepcopy(self.states.get(identity,{'seq':0,'round':None,'text':'','terminal':False,'truncated':False}))
