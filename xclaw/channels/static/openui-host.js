/* Progressive OpenUI enhancement. Original assistant prose always remains readable. */
(function () {
  const mounted = new Map();
  const liveMounted = new Map();
  const encoder = new TextEncoder();
  async function hash(text) {
    const bytes = await crypto.subtle.digest('SHA-256', encoder.encode(text));
    return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
  }
  function dispose(item) {
    const entry = mounted.get(item);
    if (!entry) return;
    entry.controller?.abort();
    entry.restore?.();
    entry.targets.forEach(el => window.XClawOpenUI?.unmount(el));
    mounted.delete(item);
  }
  function emptyOutcome(source) {
    try { const p = JSON.parse(source); if (p.version === 'xclaw-empty-1') return p.outcome; } catch (_) {}
    return '';
  }
  function emptyPresentation(source) {
    if (emptyOutcome(source)) return true;
    const bare = source.trim().replace(/^```openui\s*\n/, '').replace(/\n```\s*$/, '').trim();
    return /^root\s*=\s*Col\(\s*\[\s*\]\s*\)\s*;?\s*$/.test(bare);
  }
  // Match ui_narrative.paragraph_blocks: compare exact immutable paragraphs,
  // not a whitespace-stripped digest. Code/reference/list parser scope matters.
  function paragraphBlocks(reply) {
    if (/^\s*\[[^\]]+\]:/m.test(reply)) return reply.trim() ? [reply] : [];
    const ranges = []; let fence = null, offset = 0;
    for (const line of reply.match(/[^\n]*\n|[^\n]+$/g) || []) {
      const body = line.replace(/[\r\n]+$/, '');
      if (fence) {
        if (new RegExp('^[ \t]{0,3}' + fence.marker + '{' + fence.length + ',}[ \t]*$').test(body)) {
          ranges.push([fence.begin, offset + body.length]); fence = null;
        }
      } else {
        const opening = body.match(/^[ \t]{0,3}(`{3,}|~{3,})([^\r\n]*)$/);
        if (opening && !(opening[1][0] === '`' && opening[2].includes('`')))
          fence = {marker:opening[1][0], length:opening[1].length, begin:offset};
      }
      offset += line.length;
    }
    if (fence) ranges.push([fence.begin, reply.length]);
    const parts = []; let start = 0;
    for (const separator of reply.matchAll(/\n[ \t]*\n/g)) {
      if (ranges.some(([begin,end]) => begin <= separator.index && separator.index < end)) continue;
      const part = reply.slice(start, separator.index);
      if (part.trim()) parts.push(part);
      start = separator.index + separator[0].length;
    }
    if (reply.slice(start).trim()) parts.push(reply.slice(start));
    const result = [], isList = text => /^\s*(?:[-+*]|\d+[.)])\s/.test(text);
    for (const part of parts) {
      if (result.length && isList(result.at(-1)) && (isList(part) || /^(?:  |\t)/.test(part))) result[result.length - 1] += '\n\n' + part;
      else result.push(part);
    }
    return result;
  }
  function clear() { [...mounted.keys()].forEach(dispose); [...liveMounted.keys()].forEach(disposeLive); }
  async function attach(item, reply, options) {
    if (options.presentationId) return attachPresentation(item, reply, options);
    if (!window.XClawOpenUI || !crypto.subtle) return;
    dispose(item);
    const entry = {targets: [], controller: null, slots: new Map()};
    mounted.set(item, entry);
    const valid = () => item.isConnected && mounted.get(item) === entry && options.isCurrent();
    const replyHash = await hash(reply);
    const scope = await hash(JSON.stringify([options.chatId, options.getToken()]));
    const key = 'xclaw.openui.source.v1.' + scope + '.' + replyHash;
    if (!valid()) { dispose(item); return; }
    const original = item.querySelector(':scope > .markdown');
    const panel = document.createElement('section');
    panel.className = 'openui-presentation'; panel.hidden = true;
    const status = document.createElement('span');
    status.setAttribute('role', 'status');
    const retry = document.createElement('button');
    retry.type = 'button'; retry.textContent = '生成交互卡片';
    const stop = document.createElement('button');
    stop.type = 'button'; stop.textContent = '停止生成'; stop.hidden = true;
    const content = document.createElement('div');
    panel.append(status, retry, stop, content); item.append(panel);
    function showEmpty(source, outcome) {
      outcome = outcome || emptyOutcome(source);
      content.replaceChildren();
      if (outcome === 'no_data' || outcome === 'empty') {
        panel.hidden = false; status.hidden = false;
        status.textContent = outcome === 'no_data' ? '本轮没有可用于图表的数据，已保留文字答复。' : '本轮未生成合适的图表，文字答复已保留。';
        retry.hidden = outcome === 'no_data';
      } else { panel.hidden = true; }
    }
    let progressive = null;
    function presentationStatus(state) {
      if (!entry.presentationAttempt || options.history || !valid() || entry.presentationStatus === state) return;
      entry.presentationStatus = state;
      options.onPresentationStatus?.(state);
    }
    function rollback() {
      entry.targets.forEach(el => window.XClawOpenUI.unmount(el));
      entry.targets = []; entry.slots.clear(); content.replaceChildren();
      if (original) original.hidden = false;
      progressive = null;
    }
    entry.restore = () => { rollback(); panel.hidden = true; };
    function failure() {
      entry.failed = true; presentationStatus('failed'); rollback(); entry.controller?.abort(); panel.hidden = false;
      status.hidden = false; status.textContent = '图表暂时无法显示，文字内容不受影响。'; retry.hidden = false;
    }
    function renderBlocks(blocks, streaming) {
      panel.hidden = false; content.hidden = false;
      const keys = new Set();
      for (const [index, part] of blocks.entries()) {
        const id = part.id || String(index);
        if (keys.has(id) || !['text', 'openui'].includes(part.type) || typeof part.source !== 'string') throw new Error('invalid_block');
        keys.add(id);
        let slot = entry.slots.get(id);
        if (slot && slot.type !== part.type) throw new Error('changed_block_type');
        if (!slot) {
          slot = {el:document.createElement('div'), type:part.type}; entry.slots.set(id, slot);
          if (part.type === 'openui') entry.targets.push(slot.el);
        }
        if (content.children[index] !== slot.el) content.insertBefore(slot.el, content.children[index] || null);
        if (slot.source === part.source && slot.streaming === streaming) continue;
        slot.source = part.source; slot.streaming = streaming;
        const el = slot.el;
        const slotCurrent = () => valid() && entry.slots.get(id) === slot && slot.source === part.source && slot.streaming === streaming;
        if (part.type === 'text') { el.className = 'markdown'; el.innerHTML = window.renderChatMarkdown(part.source); continue; }
        window.XClawOpenUI.update(el, {source:part.source, messageId:replyHash + ':' + id,
          chatId:options.chatId, getToken:options.getToken, isStreaming:streaming,
          evidence: progressive?.evidence || [],
          onOperationComplete: () => { if (!streaming && slotCurrent() && !item.querySelector('.operation-summary')) { const note = document.createElement('p'); note.className = 'operation-summary'; note.textContent = '操作已完成，下面保留的是操作前的确认说明；当前结果见已更新的卡片。'; item.insertBefore(note, item.firstChild); } },
          onRenderError: () => { if (slotCurrent()) failure(); },
          onContinue: message => { if (!streaming && slotCurrent()) options.onContinue(message); }});
      }
      if (entry.failed) throw new Error("render_failed");
      for (const [id, slot] of entry.slots) if (!keys.has(id)) {
        if (slot.type === 'openui') { window.XClawOpenUI.unmount(slot.el); entry.targets = entry.targets.filter(el => el !== slot.el); }
        slot.el.remove(); entry.slots.delete(id);
      }
    }
    function progressiveBlocks() {
      return progressive.paragraphs.flatMap((source, index) => [
        {type:'text', source, id:'p' + index},
        ...[...progressive.placements.values()].filter(p => p.after === index).map(p => ({type:'openui', id:p.id, source:p.source}))]);
    }
    function render(source, streaming) {
      let narrative = null;
      let envelope; try { envelope = JSON.parse(source); } catch (_) {}
      if (envelope?.presentation_id || envelope?.blocks?.some(b => b.type === 'action')) throw new Error('identity_required');
      try { const parsed = JSON.parse(source); if (parsed.presentation_id || parsed.blocks?.some(b => b.type === 'action')) throw new Error('identity_required'); if (parsed.version === 'xclaw-narrative-1' && Array.isArray(parsed.blocks)) narrative = parsed.blocks; } catch (_) {}
      if (progressive?.placements.size) {
        if (!narrative || [...progressive.placements.values()].some(p => !p.ended)
            || JSON.stringify(narrative.map(p => [p.id,p.type,p.source])) !== JSON.stringify(progressiveBlocks().map(p => [p.id,p.type,p.source]))) throw new Error('final_plan_mismatch');
      }
      const parts = narrative || window.XClawOpenUI.parseMixed(source);
      const blocks = (parts.some(p => p.type === 'openui') ? parts : [{type:'openui', source}]).filter(p => p.source.trim());
      renderBlocks(blocks, streaming);
      if (!streaming) { if (original) original.hidden = Boolean(narrative); if (narrative) status.hidden = true; }
    }
    function progressiveEvent(event, payload) {
      if (event === 'narrative_start') {
        if (payload.reply !== reply || JSON.stringify(payload.paragraphs) !== JSON.stringify(paragraphBlocks(reply))) throw new Error('changed_original_prose');
        if ((payload.reply_hash !== undefined && payload.reply_hash !== replyHash) || progressive || !Array.isArray(payload.paragraphs) || !payload.paragraphs.length || payload.paragraphs.some(p => typeof p !== 'string')) throw new Error('invalid_narrative');
        progressive = {paragraphs:payload.paragraphs, evidence:payload.evidence || [], placements:new Map()};
        renderBlocks(progressiveBlocks(), true);
        if (original) original.hidden = true;
        return;
      }
      if (!progressive) throw new Error('missing_narrative_start');
      if (event === 'placement_start') {
        const after = payload.after, offset = [...progressive.placements.values()].filter(p => p.after === after).length;
        if (!Number.isInteger(after) || after < 0 || after >= progressive.paragraphs.length || payload.id !== 'p'+after+'u'+offset || progressive.placements.size >= 6 || progressive.placements.has(payload.id)) throw new Error('invalid_placement');
        progressive.placements.set(payload.id, {id:payload.id,after,source:'',ended:false});
      } else {
        const placement = progressive.placements.get(payload.id);
        if (!placement || placement.ended) throw new Error('invalid_placement_order');
        if (event === 'placement_delta') {
          if (typeof payload.text !== 'string') throw new Error('invalid_delta');
          placement.source += payload.text;
          if (placement.source.length > 40000) throw new Error('placement_too_large');
        } else if (event === 'placement_end') placement.ended = true;
      }
      renderBlocks(progressiveBlocks(), true);
    }
    async function generate() {
      if (!valid() || entry.controller) return;
      panel.hidden = true;
      const controller = new AbortController(); entry.controller = controller;
      const timeout = setTimeout(() => controller.abort(), 120000);
      retry.hidden = true; stop.hidden = true; status.hidden = true; status.textContent = '';
      let source = '', outcome = '', done = false, buffer = '';
      rollback(); entry.failed = false;
      entry.presentationAttempt = !options.history; entry.presentationStatus = null;
      try {
        const token = options.getToken();
        const response = await fetch('/api/openui/generate', {method:'POST',
          headers:{'Content-Type':'application/json', ...(token ? {Authorization:'Bearer ' + token}: {})},
          body:JSON.stringify({chat_id:options.chatId, reply_hash:replyHash}), signal:controller.signal});
        if (!response.ok || !response.body) throw new Error('generation_failed');
        const reader = response.body.getReader(), decoder = new TextDecoder();
        while (true) {
          const next = await reader.read();
          buffer += decoder.decode(next.value || new Uint8Array(), {stream:!next.done});
          buffer = buffer.replace(/\r\n/g, '\n');
          let boundary;
          while ((boundary = buffer.indexOf('\n\n')) >= 0) {
            const block = buffer.slice(0,boundary); buffer = buffer.slice(boundary+2);
            const event = block.split('\n').find(l => l.startsWith('event:'))?.slice(6).trim();
            const data = block.split('\n').filter(l => l.startsWith('data:')).map(l => l.slice(5).trimStart()).join('\n');
            if (!data) continue;
            const payload = JSON.parse(data);
            if (!valid()) { controller.abort(); return; }
            if (done) throw new Error('event_after_done');
            if (['status','narrative_start','placement_start','placement_delta','placement_end'].includes(event)) presentationStatus('generating');
            if (event === 'status') { panel.hidden = false; stop.hidden = false; status.hidden = false; status.textContent = '正在整理图表…'; }
            if (event === 'error') throw new Error('generation_failed');
            if (['narrative_start','placement_start','placement_delta','placement_end'].includes(event)) progressiveEvent(event, payload);
            // Legacy delta is buffered only: partial JSON is never rendered as DSL.
            if (event === 'delta') { source += payload.text || ''; }
            if (source.length > 100000) throw new Error('generation_too_large');
            if (event === 'done') { source = payload.source ?? source; outcome = payload.outcome || ''; done = true; }
          }
          if (next.done) break;
        }
        if (!done || buffer.trim()) throw new Error('incomplete_stream');
        if (!valid()) return;
        if (emptyPresentation(source)) { if (progressive?.placements.size) throw new Error('discarded_placements'); rollback(); showEmpty(source, outcome); }
        else if (source.trim()) { render(source, false); status.textContent = ''; status.hidden = true; }
        else { status.textContent = '此回复使用文字展示。'; }
        try { sessionStorage.setItem(key, source); } catch (_) {}
        presentationStatus('completed');
      } catch (_) {
        if (valid()) failure();
      } finally { clearTimeout(timeout); entry.controller = null; stop.hidden = true; }
    }
    retry.addEventListener('click', generate);
    stop.addEventListener('click', () => entry.controller?.abort());
    let cached = null;
    try { cached = sessionStorage.getItem(key); } catch (_) {}
    if (cached !== null) { try { if (emptyPresentation(cached)) showEmpty(cached); else { if (cached.trim()) render(cached, false); status.textContent = ''; status.hidden = true; retry.hidden = true; } } catch (_) { if (valid()) failure(); } }
    else if (options.history) {
      status.textContent = '正在恢复内容…';
      try {
        const token = options.getToken();
        const response = await fetch('/api/openui/presentation', {method:'POST',
          headers:{'Content-Type':'application/json', ...(token ? {Authorization:'Bearer '+token} : {})},
          body:JSON.stringify({chat_id:options.chatId,reply_hash:replyHash})});
        if (!response.ok) throw new Error('history_unavailable');
        const saved = await response.json();
        if (!valid()) return;
        if (saved.source && !emptyPresentation(saved.source)) { render(saved.source,false); status.textContent = ''; status.hidden = true; retry.hidden = true; }
        else { showEmpty(saved.source || 'root = Col([])'); }
      } catch (_) { if (valid()) { panel.hidden = false; status.hidden = false; status.textContent = '图表暂时无法显示，文字内容不受影响。'; retry.hidden = false; } }
    }
    else await generate();
  }
  async function attachPresentation(item, reply, options) {
    if (!/^[a-f0-9]{32}$/.test(options.presentationId) || !options.isCurrent()) return false;
    const valid = () => item.isConnected && options.isCurrent();
    try {
      const replyHash = await hash(reply), token = options.getToken();
      const response = await fetch('/api/openui/presentation', {method:'POST',
        headers:{'Content-Type':'application/json',...(token?{Authorization:'Bearer '+token}:{})},
        body:JSON.stringify({chat_id:options.chatId,reply_hash:replyHash,presentation_id:options.presentationId,
          ...(Number.isInteger(options.messageId)?{message_id:options.messageId}:{})})});
      if (!response.ok) throw new Error('presentation_unavailable');
      const saved = await response.json(); if (!valid()) return false;
      const p = JSON.parse(saved.source);
      if (saved.presentation_id !== options.presentationId || p.presentation_id !== options.presentationId
          || p.version !== 'xclaw-narrative-1' || !Array.isArray(p.blocks) || !Array.isArray(saved.evidence)
          || Number.isInteger(options.messageId) && saved.message_id !== options.messageId
          || p.blocks.filter(b=>b.type==='text').map(b=>b.source).join('\n\n') !== paragraphBlocks(reply).join('\n\n')) throw new Error('presentation_identity_mismatch');
      const accepted = updateLive(item,{version:'xclaw-live-1',round:0,blocks:p.blocks,evidence:saved.evidence,
        provisional:false,validated:true,presentation_id:saved.presentation_id,message_id:saved.message_id,reply_hash:replyHash},
        {...options,runId:options.presentationId});
      if (!accepted) throw new Error('invalid_saved_presentation');
      return true;
    } catch (_) {
      if (valid()) {
        const note = document.createElement('p'); note.textContent='图表暂时无法恢复，文字内容不受影响。';item.append(note);
      }
      return false;
    }
  }
  function disposeLive(item) {
    const entry = liveMounted.get(item);
    if (!entry) return;
    entry.slots.forEach(slot => { if (slot.type !== 'text') window.XClawOpenUI?.unmount(slot.el); });
    entry.container.remove();
    entry.operationNotice?.remove();
    if (entry.original) entry.original.hidden = false;
    liveMounted.delete(item);
  }
  function failLive(item, verifiedText) {
    const entry = liveMounted.get(item);
    if (!entry || entry.final) return false;
    const priorText = entry.failureText ?? entry.blocks.filter(b => b.type === 'text').map(b => b.source).join('\n\n');
    const acceptedText = verifiedText === undefined || typeof verifiedText === 'string' && verifiedText.startsWith(priorText);
    if (acceptedText && typeof verifiedText === 'string' && verifiedText !== priorText) {
      entry.failureText = verifiedText;
      if (!entry.draft) {
        entry.draft = document.createElement('div'); entry.draft.className = 'markdown';
        entry.container.append(entry.draft);
      }
      entry.draft.innerHTML = window.renderChatMarkdown(verifiedText);
      entry.slots.forEach(slot => { if (slot.type === 'text') slot.el.hidden = true; });
    }
    entry.failed = true;
    for (const [id, slot] of entry.slots) if (slot.type !== 'text') {
      window.XClawOpenUI?.unmount(slot.el); slot.el.remove(); entry.slots.delete(id);
    }
    entry.container.setAttribute('data-state', 'incomplete');
    if (!entry.notice) {
      entry.notice = document.createElement('p'); entry.notice.className = 'live-incomplete';
      entry.notice.textContent = '本次回答未完成，以下文字仅为已收到的部分；图表已撤下。';
      entry.container.insertBefore(entry.notice, entry.container.children[0] || null);
    }
    return acceptedText;
  }
  function updateLive(item, presentation, options) {
    if (!window.XClawOpenUI || !item.isConnected || !options.isCurrent()) return false;
    let entry = liveMounted.get(item);
    const scope = JSON.stringify([options.chatId, options.runId, options.getToken()]);
    if (entry && entry.scope !== scope) { disposeLive(item); entry = null; }
    try {
      const p = presentation;
      if (!p || p.version !== 'xclaw-live-1' || !Number.isInteger(p.round) || p.round < 0
          || !Array.isArray(p.blocks) || p.blocks.length > 200 || !Array.isArray(p.evidence)
          || !(p.provisional === true || p.provisional === false && p.validated === true)) throw new Error('invalid_live_snapshot');
      const final = p.provisional === false && p.validated === true;
      if (p.presentation_id !== undefined && !/^[a-f0-9]{32}$/.test(p.presentation_id)) throw new Error('invalid_presentation_id');
      if (entry?.presentationId && p.presentation_id !== entry.presentationId) throw new Error('changed_presentation_id');
      if (entry?.failureText && final && !p.blocks.filter(b => b.type === 'text').map(b => b.source).join('\n\n').startsWith(entry.failureText)) throw new Error('lost_failed_prefix');
      if (entry && (p.round < entry.round || entry.final)) return true;
      if (entry?.failed && !final) return false;
      const seen = new Set();
      for (const block of p.blocks) {
        if (!block || typeof block.id !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(block.id) || seen.has(block.id)
            || !['text','openui','action'].includes(block.type) || typeof block.source !== 'string' || block.source.length > 262144) throw new Error('invalid_live_block');
        if (block.type === 'action' && (!final || !p.presentation_id || typeof block.summary !== 'string'
            || !new RegExp('^'+p.presentation_id+'_a[0-9]+$').test(block.action_plan_id)
            || block.id !== 'action-'+block.action_plan_id)) throw new Error('invalid_action_identity');
        seen.add(block.id);
      }
      const readBlocks = p.blocks.filter(b => b.type !== 'action');
      if (entry && p.round === entry.round) {
        // Out-of-order snapshots are harmless no-ops; conflicting edits are not.
        if (readBlocks.length < entry.blocks.length && !final) return true;
        for (let i = 0; i < entry.blocks.length; i++) {
          const old = entry.blocks[i], next = readBlocks[i];
          if (!next || next.id !== old.id || next.type !== old.type) throw new Error('changed_live_order');
          if (old.type === 'openui' && next.source !== old.source) throw new Error('changed_live_card');
          if (old.type === 'text' && !next.source.startsWith(old.source)) {
            if (!final && old.source.startsWith(next.source)) return true;
            throw new Error('changed_live_text');
          }
        }
        if (JSON.stringify(p.evidence) !== entry.evidenceKey) throw new Error('changed_live_evidence');
      }
      if (entry && p.round > entry.round) { disposeLive(item); entry = null; }
      if (!entry) {
        dispose(item);
        const container = document.createElement('div'); container.className = 'live-narrative';
        const original = item.querySelector(':scope > .markdown'); if (original) original.hidden = true;
        item.append(container);
        entry = {scope,round:p.round,container,original,slots:new Map(),blocks:[],evidenceKey:JSON.stringify(p.evidence),final:false,failed:false};
        liveMounted.set(item, entry);
      }
      const live = entry;
      if (p.presentation_id) live.presentationId = p.presentation_id;
      if (live.failed && final) {
        live.failed = false;
        live.notice?.remove(); live.notice = null;
        live.draft?.remove(); live.draft = null;
        live.slots.forEach(slot => { if (slot.type === 'text') slot.el.hidden = false; });
      }
      for (const [index, block] of p.blocks.entries()) {
        let slot = live.slots.get(block.id);
        if (!slot) {
          slot = {type:block.type,el:document.createElement('div')}; live.slots.set(block.id, slot);
        }
        if (live.container.children[index] !== slot.el) live.container.insertBefore(slot.el, live.container.children[index] || null);
        if (slot.source === block.source && slot.final === final) continue;
        slot.source = block.source; slot.final = final;
        if (block.type === 'text') { slot.el.className = 'markdown'; slot.el.innerHTML = window.renderChatMarkdown(block.source); }
        else if (block.type === 'action') window.XClawOpenUI.update(slot.el, {
          source:block.source,messageId:p.presentation_id+':'+block.action_plan_id,
          chatId:options.chatId,getToken:options.getToken,isStreaming:false,
          presentationId:p.presentation_id,actionPlanId:block.action_plan_id,replyHash:p.reply_hash,
          onOperationComplete:result=>{
            if (!options.isCurrent() || liveMounted.get(item) !== live || !item.isConnected
                || result?.status !== 'completed' || result.presentationId !== p.presentation_id
                || result.actionPlanId !== block.action_plan_id || live.operationNotice) return;
            live.operationNotice = document.createElement('p'); live.operationNotice.className = 'operation-summary';
            live.operationNotice.textContent = '本条确认操作已完成，正文保留操作前的说明；当前业务结果见已更新的卡片。';
            item.insertBefore(live.operationNotice, item.children[0] || null);
          },
          onRenderError:()=>{slot.el.setAttribute('data-state','unavailable');}
        });
        else window.XClawOpenUI.update(slot.el, {
          source:block.source, messageId:options.runId + ':' + block.id,
          chatId:options.chatId,getToken:options.getToken,isStreaming:!final,
          evidence:p.evidence,inlineEvidence:p.evidence,presentationId:p.presentation_id,readonly:true,replyHash:final?p.reply_hash:undefined,
          onRenderError:() => { if (options.isCurrent() && liveMounted.get(item) === live) failLive(item); }
        });
        if (live.failed) return false;
      }
      live.blocks = readBlocks.map(b => ({id:b.id,type:b.type,source:b.source})); live.final = final;
      live.container.setAttribute('data-state', final ? 'complete' : 'streaming');
      return true;
    } catch (_) { if (entry) failLive(item); return false; }
  }
  window.XClawOpenUIHost = {attach, clear, updateLive, failLive, disposeLive};
  window.addEventListener('pagehide', clear);
})();
