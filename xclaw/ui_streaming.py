"""Strict incremental JSON placement reader; never repairs incomplete JSON/DSL.

Only source-string characters already decoded by JSON rules are published. Final
JSON and narrative validation remain authoritative; provisional UI is revocable.
"""
import json
import re
from collections import defaultdict
from xclaw.ui_narrative import compile_narrative

class PlacementStream:
    def __init__(self, reply):
        self.reply=reply; self.buf=''; self.pos=0; self.state='open'; self.events=[]
        self.anchor=None; self.ident=None; self.source=''; self.count=0
        self.offsets=defaultdict(int); self.legacy=False; self.total=0

    def _space(self):
        while self.pos<len(self.buf) and self.buf[self.pos] in ' \t\r\n':self.pos+=1

    def _literal(self, value, next_state):
        self._space(); remaining=self.buf[self.pos:]
        if len(remaining)<len(value):
            if not value.startswith(remaining):raise ValueError('invalid placement JSON token')
            return False
        if not remaining.startswith(value):raise ValueError('invalid placement JSON token')
        self.pos+=len(value);self.state=next_state;return True

    def feed(self, text):
        if not isinstance(text,str):raise ValueError('nontext placement delta')
        self.total+=len(text)
        if self.total>100000:raise ValueError('placement stream too large')
        self.buf+=text; self.events=[]
        while True:
            self._space() if self.state!='source' else None
            if self.pos>=len(self.buf):break
            if self.state=='open':
                # Backwards-compatible empty DSL is final-only, never provisional.
                if self.buf[self.pos]=='r':self.legacy=True;break
                if not self._literal('{','key'):break
            elif self.state=='key':
                if not self._literal('"placements"','colon'):break
            elif self.state=='colon':
                if not self._literal(':','array'):break
            elif self.state=='array':
                if not self._literal('[','item'):break
            elif self.state=='item':
                if self.buf[self.pos]==']':self.pos+=1;self.state='close';continue
                if self.count>=6:raise ValueError('too many placements')
                if not self._literal('{','afterkey'):break
            elif self.state=='afterkey':
                if not self._literal('"after"','aftercolon'):break
            elif self.state=='aftercolon':
                if not self._literal(':','anchor'):break
            elif self.state=='anchor':
                match=re.match(r'(?:0|[1-9][0-9]*)',self.buf[self.pos:])
                if not match:raise ValueError('invalid anchor')
                end=self.pos+match.end()
                if end==len(self.buf):break
                if self.buf[end] not in ' \t\r\n,':raise ValueError('invalid anchor delimiter')
                self.anchor=int(match[0]);self.pos=end;self.state='comma'
            elif self.state=='comma':
                if not self._literal(',','sourcekey'):break
            elif self.state=='sourcekey':
                if not self._literal('"source"','sourcecolon'):break
            elif self.state=='sourcecolon':
                if not self._literal(':','stringopen'):break
            elif self.state=='stringopen':
                from xclaw.ui_narrative import paragraph_blocks
                if not 0<=self.anchor<len(paragraph_blocks(self.reply)):raise ValueError('anchor outside original answer')
                if not self._literal('"','source'):break
                self.ident=f'p{self.anchor}u{self.offsets[self.anchor]}'
                self.offsets[self.anchor]+=1;self.source=''
                self.events.append({'event':'placement_start','data':{'id':self.ident,'after':self.anchor}})
            elif self.state=='source':
                decoded=[]
                while self.pos<len(self.buf):
                    char=self.buf[self.pos]
                    if char=='"':self.pos+=1;self.state='itemclose';break
                    if char=='\\':
                        remaining=self.buf[self.pos:]
                        if len(remaining)<2:break
                        n=6 if remaining[1]=='u' else 2
                        if len(remaining)<n:break
                        token=remaining[:n]
                        if n==6:
                            if not re.fullmatch(r'\\u[0-9a-fA-F]{4}',token):raise ValueError('invalid unicode escape')
                            point=int(token[2:],16)
                            if 0xD800<=point<=0xDBFF:
                                if len(remaining)<12:break
                                token=remaining[:12];n=12
                                if not re.fullmatch(r'\\u[0-9a-fA-F]{4}\\u[dD][c-fC-F][0-9a-fA-F]{2}',token):raise ValueError('unpaired high surrogate')
                            elif 0xDC00<=point<=0xDFFF:raise ValueError('unpaired low surrogate')
                        decoded.append(json.loads('"'+token+'"'));self.pos+=n
                    else:
                        if ord(char)<32 or 0xD800<=ord(char)<=0xDFFF:raise ValueError('invalid JSON character')
                        decoded.append(char);self.pos+=1
                if decoded:
                    delta=''.join(decoded);self.source+=delta
                    if len(self.source)>40000:raise ValueError('component too large')
                    self.events.append({'event':'placement_delta','data':{'id':self.ident,'text':delta}})
                if self.state=='source':break
            elif self.state=='itemclose':
                if not self._literal('}','separator'):break
                compile_narrative(json.dumps({'placements':[{'after':self.anchor,'source':self.source}]}),self.reply)
                self.count+=1
                self.events.append({'event':'placement_end','data':{'id':self.ident}})
            elif self.state=='separator':
                char=self.buf[self.pos];self.pos+=1
                if char==',':self.state='nextitem'
                elif char==']':self.state='close'
                else:raise ValueError('invalid placement separator')
            elif self.state=='nextitem':
                if self.buf[self.pos]==']':raise ValueError('trailing comma')
                self.state='item'
            elif self.state=='close':
                if not self._literal('}','finished'):break
            elif self.state=='finished':raise ValueError('trailing content')
        return self.events

    def finish(self, full_source):
        if full_source != self.buf:raise ValueError('final source differs from observed stream')
        if self.legacy:
            if not re.fullmatch(r'\s*root\s*=\s*Col\(\s*\[\s*\]\s*\)\s*;?\s*',full_source):raise ValueError('invalid legacy empty source')
        elif self.state!='finished' or self.buf[self.pos:].strip():raise ValueError('incomplete placement JSON')
        return compile_narrative(full_source,self.reply)
