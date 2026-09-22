/* Durable jobs/files/results. Rich text uses the cockpit's escaping Markdown renderer. */
(() => {
  'use strict';
  const bridge = window.OperatorWorkbenchBridge, op = document.querySelector('.op');
  if (!bridge || !op || bridge.context().demo) return;
  const $ = id => document.getElementById(id);
  const node = (tag, text, cls) => { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; };
  const button = (text, action, cls) => { const e = node('button', text, cls); e.type = 'button'; e.addEventListener('click', action); return e; };
  const trim = x => x.toFixed(1).replace(/\.0$/, '');
  const bytes = n => n >= 1073741824 ? trim(n / 1073741824) + ' GB' : n >= 1048576 ? trim(n / 1048576) + ' MB' : Math.ceil(n / 1024) + ' KB';
  let cid = '', state = null, agent = {}, requested = 0, loading = false, expanded = false;
  let steerId = '', lastRender = '', filesRender = '', diagnosticsOn = false, lastFocus = null;
  const panel = node('section', null, 'op-workbench op-job-panel'); panel.id = 'op-job-panel'; panel.hidden = true;
  $('op-action').after(panel);
  const notice = node('div', null, 'op-workbench op-wb-notice'); notice.id = 'op-steering-notice'; notice.hidden = true;
  notice.setAttribute('role', 'status'); notice.setAttribute('aria-live', 'polite');
  document.querySelector('.op-inputbox').before(notice);
  function status(text, id = '') { notice.textContent = text; notice.hidden = !text; if (id) steerId = id; }
  function context() { return bridge.context(); }
  async function api(path = '', options = {}) {
    const c = context(), expected = c.conversation_id || 'legacy';
    const url = path.startsWith('!') ? path.slice(1) : OP_URLS.workspace + path;
    const init = Object.assign({}, options);
    let target = url;
    if (init.body instanceof FormData) {
      for (const [k, v] of Object.entries(c)) if (v != null) init.body.set(k, String(v));
    } else if (init.method && init.method !== 'GET') {
      init.headers = {'Content-Type': 'application/json'};
      init.body = JSON.stringify(Object.assign({}, c, init.body || {}));
    } else target += (target.includes('?') ? '&' : '?') + 'conversation_id=' + encodeURIComponent(expected);
    const response = await fetch(target, init);
    const data = await response.json();
    if ((context().conversation_id || 'legacy') !== expected) throw new Error('Chat changed');
    if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed');
    return data;
  }
  function dialog(title, id) {
    const d = node('dialog', null, 'op-workbench op-wb-dialog'); d.id = id;
    const head = node('div', null, 'op-wb-head'), h = node('h2', title); h.id = id + '-title';
    d.setAttribute('aria-labelledby', h.id);
    const close = button('\u00d7', () => d.close(), 'op-wb-close'); close.setAttribute('aria-label', 'Close');
    head.append(h, close); d.append(head); op.append(d); d.tabIndex = -1;
    d.addEventListener('close', () => { if (lastFocus?.isConnected) lastFocus.focus({preventScroll: true}); });
    return d;
  }
  function show(d) { lastFocus = document.activeElement; if (!d.open) { d.showModal(); d.focus({preventScroll: true}); } }
  const filesDialog = dialog('Chat files', 'op-files-dialog');
  const fileInput = node('input'); fileInput.type = 'file'; fileInput.multiple = true; fileInput.hidden = true;
  const uploadButton = button('Add files', () => fileInput.click(), 'op-wb-zone');
  const filesList = node('div', null, 'op-wb-files'), storage = node('p', '', 'op-wb-storage'), fileError = node('p', '', 'op-wb-status'); fileError.setAttribute('role', 'status');
  filesDialog.append(filesList, uploadButton, fileInput, fileError, storage);
  $('op-files-open').hidden = false;
  $('op-files-open').addEventListener('click', () => { show(filesDialog); refresh(true); });
  fileInput.addEventListener('change', () => { upload(fileInput.files); fileInput.value = ''; });
  async function upload(files) {
    if (!context().can_control) { fileError.textContent = 'Take over this chat to add files.'; return; }
    uploadButton.disabled = true;
    try {
      for (const file of files) {
        if (state && file.size > state.storage.file_limit) throw new Error(file.name + ' exceeds the per-file limit.');
        fileError.textContent = 'Adding ' + file.name + '…';
        const form = new FormData(); form.append('file', file);
        await api('/files', {method: 'POST', body: form});
      }
      fileError.textContent = '';
      await refresh(true);
    } catch (e) { fileError.textContent = e.message; }
    finally { uploadButton.disabled = !context().can_control; }
  }
  const composer = document.querySelector('.op-inputbox');
  composer.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); composer.classList.add('op-wb-drop'); } });
  composer.addEventListener('dragleave', () => composer.classList.remove('op-wb-drop'));
  composer.addEventListener('drop', e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); composer.classList.remove('op-wb-drop'); show(filesDialog); upload(e.dataTransfer.files); });
  function fileUrl(id) { return OP_URLS.workspace + '/files/' + encodeURIComponent(id) + '?conversation_id=' + encodeURIComponent(cid); }
  function renderFiles() {
    if (!state) return;
    const signature = JSON.stringify([cid, state.files, state.transfers, state.storage, context().can_control, !!agent.alive]);
    if (signature === filesRender) return;
    filesRender = signature;
    filesList.replaceChildren(); uploadButton.disabled = !context().can_control;
    for (const file of state.files) {
      const row = node('div', null, 'op-wb-file'), link = node('a', file.name);
      link.href = fileUrl(file.id); link.download = file.name;
      const remove = button('Remove', async () => {
        try { await api('/files/' + file.id, {method: 'DELETE'}); await refresh(true); }
        catch (e) { fileError.textContent = e.message; }
      });
      remove.disabled = !context().can_control || !!agent.alive;
      row.append(link, node('small', bytes(file.size)), remove); filesList.append(row);
    }
    for (const transfer of state.transfers || []) {
      filesList.append(node('p', transfer.name + ' · ' + (transfer.status === 'downloading' ? 'Downloading…' : transfer.error), 'op-wb-storage'));
    }
    uploadButton.classList.toggle('op-wb-zone-empty', !state.files.length);
    storage.textContent = bytes(state.storage.chat) + ' / ' + bytes(state.storage.chat_limit) + ' \u00b7 max ' + bytes(state.storage.file_limit) + ' per file';
    $('op-files-open').title = 'Chat files' + (state.files.length ? ' (' + state.files.length + ')' : '');
  }
  function renderJob() {
    const job = state?.job;
    panel.hidden = !job || !expanded;
    if (!job) return;
    panel.replaceChildren(node('strong', job.goal || 'Current job'));
    if (job.checkpoints.length) {
      const list = node('ol');
      job.checkpoints.forEach(step => { const li = node('li', step.step); li.dataset.state = step.status; list.append(li); });
      panel.append(list);
    }
    for (const [key, title] of [['constraints', 'Keep in mind'], ['decisions', 'Decisions']]) {
      if (!job[key].length) continue;
      panel.append(node('h3', title)); const list = node('ul');
      job[key].forEach(text => list.append(node('li', text))); panel.append(list);
    }
    const actions = node('div', null, 'op-wb-actions');
    actions.append(button('Activity', () => $('op-events').classList.toggle('expanded')));
    const fresh = button('New job', async () => {
      try { await api('/job', {method: 'POST', body: {goal: ''}}); await refresh(true); }
      catch (e) { status(e.message); }
    }); fresh.disabled = !!agent.alive || !context().can_control; actions.append(fresh);
    if (diagnosticsOn) actions.append(button('Run details', () => openHealth(true)));
    panel.append(actions);
    // Connection and human-handoff messages always outrank checkpoints.
    const title = $('op-action-txt').textContent;
    const step = job.checkpoints.find(s => s.status === 'inProgress');
    const pending = job.approvals.some(a => a.status === 'pending');
    if (pending && !agent.handoff && op.dataset.mode !== 'man' && op.dataset.threadControl !== 'observer'
        && !/connect|starting|stalled|manual|control/i.test(title)) {
      $('op-action-txt').textContent = 'Approval';
      $('op-action-sub').textContent = 'Waiting for your approval';
    } else if (step && agent.alive && !agent.handoff && op.dataset.threadControl !== 'observer'
        && !/connect|starting|stalled|manual|input|control/i.test(title)) {
      $('op-action-sub').textContent = step.step;
      $('op-action-sub').title = step.step;
    }
  }
  function renderCards() {
    const log = $('op-log');
    const signature = JSON.stringify([state.jobs.map(j => [j.id, j.revision]), context().can_control, !!agent.alive, agent.run_id, agent.started_ts, state.files.map(f => f.id)]);
    if (signature === lastRender && log.querySelector('[data-workbench-card]')) return;
    lastRender = signature;
    const previous = new Map([...log.querySelectorAll('[data-workbench-card]')].map(e => [e.dataset.workbenchCard, e]));
    function mount(card) {
      const old = previous.get(card.dataset.workbenchCard); previous.delete(card.dataset.workbenchCard);
      if (!old) { log.append(card); return; }
      if (old.tagName === 'DETAILS') card.open = old.open;
      if (old.outerHTML === card.outerHTML) return;
      const focused = old.contains(document.activeElement) ? document.activeElement.textContent : '';
      old.replaceWith(card);
      if (focused) [...card.querySelectorAll('button,a')].find(e => e.textContent === focused)?.focus({preventScroll:true});
    }
    for (const job of [...state.jobs].reverse()) {
      if (job.id !== state.job?.id && !job.results.length) {
        const summary = node('details', null, 'op-workbench op-wb-card'); summary.dataset.workbenchCard = job.id;
        summary.append(node('summary', job.goal || 'Previous job'));
        summary.append(node('p', job.checkpoints.map(s => (s.status === 'completed' ? '✓ ' : '– ') + s.step).join('\n') || job.state));
        mount(summary);
      }
      for (const result of job.results) {
        const card = node('article', null, 'op-workbench op-wb-card'); card.dataset.workbenchCard = result.id;
        card.dataset.workbenchJob = job.id;
        const currentRun = result.run_id ? result.run_id === agent.run_id
          : result.created >= agent.started_ts && (!agent.ended_ts || result.created <= agent.ended_ts);
        card.dataset.workbenchTask = result.task || (currentRun && agent.task) || job.goal;
        card.dataset.workbenchRun = result.run_id || card.dataset.workbenchTask;
        const body = node('div', result.summary, 'op-wb-rich');
        bridge.renderMarkdown(body);
        card.append(node('span', result.status === 'found' ? 'Result' : result.status, 'op-wb-tag'), node('h3', result.title), body);
        if (result.confirmation) {
          const confirmation = node('div', result.confirmation, 'op-wb-rich');
          bridge.renderMarkdown(confirmation); card.append(confirmation);
        }
        for (const id of result.evidence_ids) {
          const evidence = job.evidence.find(e => e.id === id); if (!evidence) continue;
          const stamp = new Date(evidence.observed * 1000).toLocaleString();
          for (const raw of evidence.urls || []) {
            try {
              const url = new URL(raw); if (!['https:', 'http:'].includes(url.protocol)) continue;
              const link = node('a', url.hostname + ' · checked ' + stamp, 'op-wb-source');
              link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(link);
            } catch (_) {}
          }
        }
        for (const fid of result.file_ids) {
          const file = state.files.find(f => f.id === fid); if (!file) continue;
          const link = node('a', file.name, 'op-wb-source'); link.href = fileUrl(fid); link.download = file.name; card.append(link);
        }
        mount(card);
      }
      for (const approval of job.approvals.filter(a => a.status === 'pending' || (a.source === 'user' && a.status === 'approved' && !a.claimed))) {
        const card = node('article', null, 'op-workbench op-wb-card op-wb-approval'); card.dataset.workbenchCard = approval.id;
        card.append(node('span', approval.status === 'pending' ? 'Your approval' : 'Approved', 'op-wb-tag'),
          node('h3', approval.action.destination), node('p', approval.action.description));
        if (approval.action.amount) card.append(node('p', approval.action.currency + ' ' + approval.action.amount.toFixed(2)));
        const actions = node('div', null, 'op-wb-actions');
        const decide = async approved => {
          try { await api('/approval', {method: 'POST', body: {id: approval.id, fingerprint: approval.fingerprint, approved}}); await refresh(true); }
          catch (e) { status(e.message); }
        };
        if (approval.status === 'pending') {
          const yes = button('Approve this action', () => decide(true), 'op-wb-primary');
          const no = button('Decline', () => decide(false));
          yes.disabled = no.disabled = !context().can_control; actions.append(yes, no);
        } else {
          const resume = button('Continue', () => {
            if (!bridge.send('Continue with the approved action ' + approval.id + '. Recheck its exact details before committing.')) status('Switch to Auto and wait for the current run to finish before continuing.');
          }, 'op-wb-primary'); resume.disabled = !context().can_control || !!agent.alive; actions.append(resume);
        }
        card.append(actions); mount(card);
      }
    }
    previous.forEach(e => e.remove());
    orderResults();
  }
  function orderResults() {
    if (!state) return;
    const log = $('op-log'), norm = text => String(text || '').replace(/\s+/g, ' ').trim();
    const used = new Set();
    // Workspace results and the transcript arrive on separate requests. Keep a
    // result after its own turn even when a delayed trace/final arrives later.
    const groups = new Map();
    for (const card of log.querySelectorAll('[data-workbench-job]')) {
      const key = card.dataset.workbenchRun;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(card);
    }
    for (const cards of groups.values()) {
      const start = [...log.querySelectorAll('.op-msg.user')].find(e => !used.has(e) && norm(e.querySelector('.bubble')?.textContent) === norm(cards[0].dataset.workbenchTask));
      if (!start) continue; // Never guess another turn when an old prompt was trimmed.
      used.add(start);
      let next = start.nextElementSibling, anchor = start;
      while (next && !next.matches('.op-msg.user')) {
        if (!next.hasAttribute('data-workbench-card')) anchor = next;
        next = next.nextElementSibling;
      }
      for (const card of cards) {
        if (anchor.nextElementSibling !== card) anchor.after(card);
        anchor = card;
      }
    }
  }
  new MutationObserver(orderResults).observe($('op-log'), {childList: true});
  async function refresh(force = false) {
    const expected = context().conversation_id || 'legacy';
    if (loading || (!force && Date.now() - requested < 1800) || document.hidden) return;
    loading = true; requested = Date.now();
    try {
      const data = await api();
      if ((context().conversation_id || 'legacy') !== expected) return;
      if (cid !== expected) { cid = expected; expanded = false; lastRender = ''; steerId = ''; status(''); }
      state = data; renderJob(); renderFiles(); renderCards();
      // correction status strip retired 2026-09-12: the cockpit parks the
      // steer as a held bubble and promotes it at the delivery seam instead
    } catch (_) { /* connection status is already owned by the cockpit */ }
    finally { loading = false; }
  }
  const health = dialog('Operator health', 'op-health-dialog');
  const metrics = node('dl', null, 'op-wb-metrics'), healthNote = node('p', 'Last 24 hours · metadata only. No prompts, page text, screenshots or URLs.');
  const debug = button('Record 15 minutes of debug metrics', async () => {
    try { await api('!' + OP_URLS.diagnostics, {method: 'POST', body: {enabled: true}}); await openHealth(); }
    catch (e) { healthNote.textContent = e.message; }
  });
  health.append(healthNote, metrics, debug);
  async function openHealth(runOnly = false) {
    if (!diagnosticsOn) return;
    show(health);
    try {
      const filter = runOnly && agent.run_id ? '?run_id=' + encodeURIComponent(agent.run_id) : '';
      const data = await api('!' + OP_URLS.diagnostics + filter); metrics.replaceChildren();
      healthNote.textContent = (filter ? 'This run' : 'Last 24 hours') + ' · metadata only. Capture + encode timing is measured together by Chrome.';
      for (const metric of data.metrics) {
        const label = metric.name.replaceAll('_', ' ') + (metric.model ? ' · ' + metric.model : '');
        const value = metric.name.endsWith('_ms') ? (metric.total / metric.count).toFixed(1) + ' ms avg · ' + metric.maximum.toFixed(1) + ' max' : metric.name === 'frame_bytes' ? bytes(metric.total) : Math.round(metric.total).toLocaleString();
        metrics.append(node('dt', label), node('dd', value));
      }
      if (!data.metrics.length) metrics.append(node('dt', 'No measurements yet'));
      debug.textContent = data.debug_until * 1000 > Date.now() ? 'Recording until ' + new Date(data.debug_until * 1000).toLocaleTimeString() : 'Record 15 minutes of debug metrics';
    } catch (e) { healthNote.textContent = e.message; }
  }
  $('op-diagnostics-setting').hidden = false;
  try { diagnosticsOn = localStorage.getItem('operator-diagnostics-v1') === '1'; } catch (_) {}
  function toggleDiagnostics() { $('op-diagnostics-toggle').checked = diagnosticsOn; $('op-health-open').hidden = !diagnosticsOn; renderJob(); }
  $('op-diagnostics-toggle').addEventListener('change', e => { diagnosticsOn = e.target.checked; try { localStorage.setItem('operator-diagnostics-v1', diagnosticsOn ? '1' : '0'); } catch (_) {} toggleDiagnostics(); });
  $('op-health-open').addEventListener('click', () => openHealth(false)); toggleDiagnostics();
  $('op-recipe-options').hidden = false;
  $('op-nt-authorization').addEventListener('change', () => { $('op-nt-limits').hidden = $('op-nt-authorization').value !== 'bounded'; });
  window.OperatorWorkbench = {
    runId: () => agent.run_id || '', steering: status,
    update: (data, thread) => {
      if ((context().conversation_id || 'legacy') !== thread) return;
      agent = data;
      if (cid !== thread) { panel.hidden = true; state = null; lastRender = ''; $('op-log').querySelectorAll('[data-workbench-card]').forEach(e => e.remove()); }
      renderJob(); refresh(cid !== thread);
    },
    toggleJob: () => { if (!state?.job) return false; expanded = !expanded; $('op-action').setAttribute('aria-expanded', String(expanded)); renderJob(); return true; },
    recipeFill: task => {
      const a = task.authorization || {mode: 'confirm'};
      $('op-nt-success').value = task.success_criteria || '';
      $('op-nt-authorization').value = a.mode; $('op-nt-limits').hidden = a.mode !== 'bounded';
      $('op-nt-actions').querySelectorAll('input').forEach(e => { e.checked = (a.actions || []).includes(e.value); });
      $('op-nt-destinations').value = (a.destinations || []).join('\n');
      $('op-nt-amount').value = a.max_amount || 0; $('op-nt-currency').value = a.currency || '';
    },
    recipeFields: () => ({success_criteria: $('op-nt-success').value.trim(), authorization: $('op-nt-authorization').value === 'bounded'
      ? {mode: 'bounded', actions: [...$('op-nt-actions').querySelectorAll('input:checked')].map(e => e.value), destinations: $('op-nt-destinations').value.split('\n').map(s => s.trim()).filter(Boolean), max_amount: Number($('op-nt-amount').value), currency: $('op-nt-currency').value.trim().toUpperCase()}
      : {mode: 'confirm'}})
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(true); });
  refresh(true);
})();
