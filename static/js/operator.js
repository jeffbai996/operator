// operator.js — the entire cockpit client (extracted from operator.html, 1.0.14).
// Server endpoints arrive via window.OP_URLS, defined by a tiny inline config
// script in the template — the ONLY Jinja the client needs. Everything else in
// this file is plain JS; never put literal {{ }} here or in the template script
// (Jinja mangles it — see the 1.0.13 post-mortem in the module docs).
(function () {
  const op = document.getElementById('op');
  // Double-tap/click on the chat rail was selecting the last word of the nearest
  // message bubble (the owner: "double-tap highlights the last word in the chat box").
  // Swallow the native word-select EXCEPT inside a real input/textarea, where
  // double-click-to-select-word is expected. Drag-select (mousedown+drag) for
  // copying an agent reply is unaffected — this only cancels the dblclick gesture.
  if (op) op.addEventListener('dblclick', e => {
    if (e.target.closest('input, textarea, [contenteditable="true"]')) return;
    // #op-stage has its own dblclick handler (remote page owns the gesture) — don't double-handle
    if (e.target.closest('#op-stage')) return;
    e.preventDefault();
    try { window.getSelection().removeAllRanges(); } catch (_) {}
  });
  const view = document.getElementById('op-view');
  const stage = document.getElementById('op-stage');
  const urlEl = document.getElementById('op-url');
  const log = document.getElementById('op-log');
  const jumpBtn = document.getElementById('op-jump');
  // True when the user is parked at (or within a hair of) the bottom.
  function nearBottom(){ return (log.scrollHeight - log.scrollTop - log.clientHeight) < 60; }
  // Snapshot BEFORE appending whether the user was parked at the bottom — must be
  // read pre-append, since appending grows scrollHeight and would falsely read as
  // "scrolled away". scrollToBottom() then honors that (or force=true on send).
  let _wasAtBottom = true;
  function markScroll(){ _wasAtBottom = nearBottom(); }
  function scrollToBottom(force){ if (force || _wasAtBottom) log.scrollTop = log.scrollHeight; updateJump(); }
  // legacy name kept for the zoom handler etc. — reads live (no pre-snapshot needed there)
  function stickToBottom(){ if (nearBottom()) log.scrollTop = log.scrollHeight; updateJump(); }
  function updateJump(){
    // show the pill only after scrolling up a fair bit (not the 60px auto-scroll threshold)
    const up = (log.scrollHeight - log.scrollTop - log.clientHeight) > 900;
    if (jumpBtn) jumpBtn.classList.toggle('show', up);
  }
  if (jumpBtn) jumpBtn.addEventListener('click', ()=>{
    log.scrollTo({ top: log.scrollHeight, behavior: 'smooth' });
    // hide once we've arrived
    setTimeout(()=>{ jumpBtn.classList.remove('show'); }, 360);
  });
  log.addEventListener('scroll', updateJump, { passive: true });
  // status-card minimize: collapse to a slim pill (dot + caret), click to restore.
  (function(){
    const minBtn = document.getElementById('op-status-min');
    if (!minBtn) return;
    function toggleMin(e){ if(e){ e.stopPropagation(); e.preventDefault(); }
      const on = op.dataset.statusMin === '1';
      op.dataset.statusMin = on ? '0' : '1';
      minBtn.title = on ? 'minimize' : 'expand';
      try { localStorage.setItem('operator-statusmin-v1', op.dataset.statusMin); } catch {}
    }
    minBtn.addEventListener('click', toggleMin);
    minBtn.addEventListener('keydown', e => { if(e.key==='Enter'||e.key===' ') toggleMin(e); });
    try { if (localStorage.getItem('operator-statusmin-v1') === '1') { op.dataset.statusMin='1'; minBtn.title='expand'; } } catch {}
  })();

  // edit / retry the last user prompt — no branches; continues from that point.
  function _removeFromUserMsg(userEl, keepUser){
    // remove the user message (unless keepUser) + everything after it
    let el = keepUser ? userEl.nextSibling : userEl;
    while (el) { const next = el.nextSibling; el.remove(); el = next; }
    if (!keepUser) userEl.remove();
    _markLastUser(); saveSession();
  }
  log.addEventListener('click', e => {
    const act = e.target.closest('.op-msg-act');
    if (act) { e.stopPropagation();
      const userEl = act.closest('.op-msg.user'); if (!userEl) return;
      const txt = userEl.dataset.text || '';
      if (act.dataset.act === 'edit') {
        _removeFromUserMsg(userEl, false);     // drop the msg + its response
        input.value = txt; input.focus();      // put it back to edit; submit re-runs
        if (typeof autoGrow === 'function') autoGrow();
        if (typeof refreshSendButton === 'function') refreshSendButton();
      } else if (act.dataset.act === 'retry') {
        _removeFromUserMsg(userEl, false);     // drop msg + response, re-send same text
        if (MODE !== 'auto') { MODE='auto'; applyMode(); saveSession(); }
        logUser(txt);
        if (!_inFlight) dispatchTask(txt); else { _queue.push(txt); }
      }
      return;
    }
  });

  // delegated toggle: any click on a task header (current OR session-restored,
  // whose inline listeners don't survive innerHTML) collapses/expands its group.
  log.addEventListener('click', e => {
    const head = e.target.closest('.op-task-head');
    if (head && head.parentElement && !head.parentElement.classList.contains('op-no-steps'))
      head.parentElement.classList.toggle('collapsed');
  });

  // ── session persistence: chat + selections survive a refresh (localStorage) ──
  const LS_KEY = 'operator-session-v1';
  // ── one shared server-side session (2026-07-11): localStorage stays the
  // fast-path cache, but the source of truth lives on the server so the chat
  // survives across devices. _srev = the server revision this device last
  // saw; a differing server rev at boot means another device wrote — adopt.
  let _srev = 0;
  let _sessPushT = null;
  function _sessionPayload() {
    return {
      log: log.innerHTML,
      mode: (typeof MODE !== 'undefined' ? MODE : 'man'),
      bot: (document.getElementById('op-action-caret')||{}).value || '',
      model: (document.getElementById('op-model')||{}).value || '',
      effort: (document.getElementById('op-effort')||{}).value || '',
    };
  }
  // saveSession used to serialize the ENTIRE chat log's innerHTML + write
  // localStorage synchronously on every call — and it's called per trace step
  // while a run streams, so on a long session that was tens of KB of sync
  // main-thread work several times a second (a big slice of the iPhone typing
  // lag, 2026-07-12). Now the whole serialize+store is debounced; pagehide /
  // tab-hide flush immediately so nothing is lost on close or app-switch.
  let _sessDirty = false;
  function _sessionFlush() {
    if (!_sessDirty) return;
    _sessDirty = false;
    if (_sessPushT) { clearTimeout(_sessPushT); _sessPushT = null; }
    try {
      const d = _sessionPayload();
      localStorage.setItem(LS_KEY, JSON.stringify(Object.assign({_srev: _srev}, d)));
      // push to the server; fire-and-forget (a failed push just means this
      // device's copy wins on its NEXT successful save)
      (async () => { try {
        const r = await fetch(SESSION, {method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({data: d})});
        const j = await r.json();
        if (j && j.ok) {
          _srev = j.rev;
          try { const c = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
            if (c) { c._srev = _srev; localStorage.setItem(LS_KEY, JSON.stringify(c)); } } catch {}
        }
      } catch(_){} })();
    } catch {}
  }
  function saveSession() {
    _sessDirty = true;
    if (_sessPushT) clearTimeout(_sessPushT);
    _sessPushT = setTimeout(_sessionFlush, 600);
  }
  window.addEventListener('pagehide', _sessionFlush);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _sessionFlush();
  });
  function restoreSession() {
    try { const d = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
      if (d && typeof d._srev === 'number') _srev = d._srev;
      if (d && d.log) { log.innerHTML = d.log; log.scrollTop = log.scrollHeight; updateJump();
        // a restored handoff card has dead listeners (innerHTML loses them) — drop it;
        // if the agent still needs control, the next poll re-renders a live one.
        log.querySelectorAll('.op-handoff').forEach(c => c.remove());
        // copy buttons restored from innerHTML have dead listeners — strip + rebuild.
        log.querySelectorAll('.op-copy').forEach(c => c.remove());
        if (typeof _addCopyButtons === 'function') log.querySelectorAll('.op-msg.bot .bubble').forEach(_addCopyButtons);
        if (typeof _markLastUser === 'function') _markLastUser(); }
      return d || {};
    } catch { return {}; }
  }
  const input = document.getElementById('op-input');
  const send = document.getElementById('op-send');
  const actTxt = document.getElementById('op-action-txt');
  const actSub = document.getElementById('op-action-sub');
  const overlayText = document.getElementById('op-overlay-text');
  const overlaySub = document.getElementById('op-overlay-sub');

  const STREAM = OP_URLS.stream;
  const FRAME  = OP_URLS.frame;
  const SESSION = OP_URLS.session;
  const STATUS = OP_URLS.status;
  const STEER  = OP_URLS.steer;
  // hoisted: these are referenced by early init (applyMode→setFollowUp etc.) before
  // their original later declaration — a let/const there caused a TDZ crash that halted
  // ALL page JS (poll/agent/steer dead, feed stuck 'Connecting'). Declare them up top.
  let _inFlight = false;
  var _queue = [];
  // launchpad controller singleton — built once by wireLaunchpadControls().
  // Declared in this early-hoist zone so any early caller sees a defined
  // binding instead of a TDZ crash (the 2026-06-26 class).
  let _lpCtl = null;

  // ── smart viewport follow: report the stage's CSS size so the server can
  // match the remote viewport to it (frame fills the stage, no letterbox).
  // Server-side gated (demo always, prod via OPERATOR_VIEWPORT_FOLLOW) and
  // frozen during live runs, so this beacon is fire-and-forget. Only touches
  // STEER (declared above) — no forward references (TDZ rule). ──
  (() => {
    const st = document.getElementById('op-stage');
    if (!st) return;
    let _t = null, _last = '';
    const send = async () => {
      const r = st.getBoundingClientRect();
      const v = Math.round(r.width) + 'x' + Math.round(r.height);
      if (!r.width || !r.height || v === _last) return;
      try {
        const res = await fetch(STEER, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'stage_size', value: v }) });
        const j = await res.json();
        if (j && j.ok && Array.isArray(j.view)) {
          _last = v;
        }
      } catch (_) {}
    };
    const queue = () => { clearTimeout(_t); _t = setTimeout(send, 180); };
    window.addEventListener('resize', queue);
    if (window.ResizeObserver) new ResizeObserver(queue).observe(st);
    setTimeout(send, 1200);   // after first layout settles
  })();

  // ── full-window toggle (hides host-app header) ──
  // ── drag-to-resize the chat rail (desktop). Current width is the floor; drag
  //    right to grow the chat, capped so the browser keeps a usable width. ──
  (function(){
    const rez = document.getElementById('op-resizer'); if (!rez) return;
    const opEl = document.getElementById('op');
    const RW_KEY = 'operator-railwidth-v1';
    const MIN = 248;   /* matches .op-rail min-width floor — narrow chat for max browser */
    function maxW(){ return Math.round(opEl.getBoundingClientRect().width * 0.62); }
    function apply(px){ const w = Math.max(MIN, Math.min(maxW(), px));
      opEl.style.setProperty('--rail-w', w + 'px'); return w; }
    try { const saved = parseFloat(localStorage.getItem(RW_KEY));
      if (saved && saved > MIN) apply(saved); } catch {}
    let _rzStart = null;
    function down(clientX){ _rzStart = { x: clientX,
        w: document.querySelector('.op-rail').getBoundingClientRect().width };
      rez.classList.add('dragging'); document.body.classList.add('op-resizing');
      try { window.getSelection().removeAllRanges(); } catch(_){} }
    function move(clientX){ if (_rzStart == null) return;
      try { window.getSelection().removeAllRanges(); } catch(_){}
      apply(_rzStart.w + (clientX - _rzStart.x)); }
    function up(){ if (_rzStart == null) return; _rzStart = null;
      rez.classList.remove('dragging'); document.body.classList.remove('op-resizing');
      try { localStorage.setItem(RW_KEY,
        String(parseFloat(getComputedStyle(opEl).getPropertyValue('--rail-w'))||MIN)); } catch {} }
    rez.addEventListener('pointerdown', e => { e.preventDefault(); rez.setPointerCapture(e.pointerId); down(e.clientX); });
    rez.addEventListener('pointermove', e => move(e.clientX));
    rez.addEventListener('pointerup', e => { try{rez.releasePointerCapture(e.pointerId);}catch(_){} up(); });
    rez.addEventListener('pointercancel', up);
    // keep within bounds if the window resizes
    window.addEventListener('resize', () => { const cur = parseFloat(getComputedStyle(opEl).getPropertyValue('--rail-w')); if (cur) apply(cur); });
  })();

  // ── mobile bottom-sheet: drag the chat sheet between peek / half / full ──
  (function(){
    const handle = document.getElementById('op-sheet-handle');
    const opEl = document.getElementById('op');
    if (!handle || !opEl) return;
    const isMobile = () => window.matchMedia('(max-width: 820px)').matches;
    function vh(){ return window.innerHeight; }
    // Snap targets: peek / the FIT notch / full. The middle stop is computed,
    // not fixed (2026-07-12): it's the height where the sheet's top edge
    // sits exactly at the bottom of the full-width feed — .op-browser is
    // (100dvh - sheet - header) tall and the contain-fit frame fills the phone's
    // width when that equals vw × frame aspect. Release there = whole page
    // visible edge-to-edge with a message or two of chat below.
    function fitFrac(){
      const v = document.getElementById('op-view');
      const nW = v && v.naturalWidth, nH = v && v.naturalHeight;
      const aspect = (nW && nH) ? nH / nW : 0.5625;   // no frame yet → assume 16:9
      const f = (vh() - hdrH() - window.innerWidth * aspect) / vh();
      return Math.min(0.78, Math.max(0.3, f));        // clamp: odd frames stay usable
    }
    function SNAPSNOW(){ return [0.22, fitFrac(), 0.9]; }
    // header height (mobile, non-full) — the sheet must not grow past it (the owner: maximize
    // was colliding with the host-app header).
    function hdrH(){ const v = parseFloat(getComputedStyle(opEl).getPropertyValue('--op-hdr-h')); return v||0; }
    function setH(px){
      const maxH = vh() - hdrH() - 10;     // leave the header + a small gap clear
      const h = Math.max(vh()*0.12, Math.min(maxH, px));
      opEl.style.setProperty('--sheet-h', h + 'px');
      // tag nearest snap so CSS can switch the sheet into a compact 'peek' layout
      const frac = h / vh();
      const name = frac <= 0.30 ? 'peek' : (frac >= 0.72 ? 'full' : 'half');
      opEl.dataset.sheet = name;
      return h; }
    function snapTo(frac){ setH(vh()*frac); }
    let _dragH = null, _startY = 0, _startH = 0;
    function down(y){ if (!isMobile()) return;
      _dragH = true; _startY = y;
      _startH = document.querySelector('.op-rail').getBoundingClientRect().height;
      opEl.classList.add('op-sheet-dragging'); }
    function move(y){ if (_dragH == null) return;
      setH(_startH + (_startY - y)); }           // drag up = taller
    function up(y){ if (_dragH == null) return; _dragH = null;
      opEl.classList.remove('op-sheet-dragging');
      // snap to nearest target
      const cur = document.querySelector('.op-rail').getBoundingClientRect().height / vh();
      const S = SNAPSNOW();
      let best = S[0], bd = 9;
      S.forEach(f => { const d = Math.abs(f - cur); if (d < bd){ bd = d; best = f; } });
      // a quick flick (small move) toward a direction nudges one step
      snapTo(best); }
    handle.addEventListener('pointerdown', e => { e.preventDefault();
      handle.setPointerCapture(e.pointerId); down(e.clientY); });
    handle.addEventListener('pointermove', e => move(e.clientY));
    handle.addEventListener('pointerup', e => { try{handle.releasePointerCapture(e.pointerId);}catch(_){} up(e.clientY); });
    handle.addEventListener('pointercancel', () => up(0));
    // tap the handle = cycle peek → half → full → peek
    let _tapStart = 0;
    handle.addEventListener('pointerdown', () => { _tapStart = Date.now(); });
    handle.addEventListener('pointerup', () => {
      if (Date.now() - _tapStart < 200 && isMobile()) {
        const cur = document.querySelector('.op-rail').getBoundingClientRect().height / vh();
        const S = SNAPSNOW();
        let idx = 0; S.forEach((f,i)=>{ if (Math.abs(f-cur) < Math.abs(S[idx]-cur)) idx = i; });
        snapTo(S[(idx + 1) % S.length]);
      }
    });
    // start at the fit notch on mobile (frame not loaded yet → 16:9 estimate)
    if (isMobile()) snapTo(fitFrac());
    window.addEventListener('resize', () => { if (isMobile()) {
      const cur = parseFloat(getComputedStyle(opEl).getPropertyValue('--sheet-h')); if (cur) setH(cur);
    }});
    // ── iOS keyboard: pin the sheet ABOVE it (visualViewport tracking) ──
    // The fixed rail sits at LAYOUT-bottom, which is BEHIND the software
    // keyboard (iOS doesn't resize the layout viewport). Safari compensates
    // by panning the visual viewport and re-running scroll-caret-into-view
    // on EVERY keystroke against an input it considers occluded — that
    // per-key compensation was the iPhone typing lag that survived all the
    // main-thread fixes (2026-07-12; engine-side profiling showed 60fps and
    // 5ms key→paint, so the cost was in Safari's input pipeline, not JS).
    // Lifting the sheet by the keyboard's overlap makes the input genuinely
    // visible in layout terms, so Safari has nothing to compensate for.
    if (window.visualViewport) {
      const vv = window.visualViewport;
      let _kbLast = -1;
      const kbFix = () => {
        if (!isMobile()) { if (_kbLast !== 0) { opEl.style.removeProperty('--kb-off'); _kbLast = 0; } return; }
        const off = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
        if (off === _kbLast) return;   // vv fires scroll often — only write on change
        _kbLast = off;
        if (off > 0) opEl.style.setProperty('--kb-off', off + 'px');
        else opEl.style.removeProperty('--kb-off');
      };
      vv.addEventListener('resize', kbFix);
      vv.addEventListener('scroll', kbFix);
    }
  })();

  // ── kbhint timed visibility ──
  (function(){
    const hint = document.querySelector('.op-kbhint');
    if (!hint) return;
    let _t = null;
    function show(ms){ hint.classList.add('op-hint-show');
      clearTimeout(_t); if (ms) _t = setTimeout(()=>hint.classList.remove('op-hint-show'), ms); }
    function hide(ms){ clearTimeout(_t); _t = setTimeout(()=>hint.classList.remove('op-hint-show'), ms||0); }
    // on page load: show + persist 30s
    show(30000);
    // tapping/focusing the browser stage → fade after 1.5s (they're steering now)
    stage.addEventListener('focus', ()=>{ show(0); hide(1500); });
    // clicking away from the stage → re-show + persist 10s
    stage.addEventListener('blur', ()=>{ show(10000); });
  })();

  // lock page scroll while on the operator page; restore on leave
  document.documentElement.classList.add('op-locked');
  document.body.classList.add('op-locked');
  // enable disclaimer-info (and similar) reveal transitions only after first paint,
  // so a refresh doesn't flash the hover message open→closed.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    const opBoot = document.getElementById('op');
    if (!opBoot) return;
    opBoot.classList.remove('op-booting');
    opBoot.classList.add('op-ready');
  }));
  // keep the fixed mobile browser pane below the host-app header so its URL bar is
  // visible (not tucked under the header). Track header height live.
  (function(){
    function setHdr(){
      try {
        const mob = window.matchMedia('(max-width: 820px)').matches;
        const full = document.body.classList.contains('op-full');
        const hdr = document.querySelector('header.site');
        const h = (mob && !full) ? (hdr?.offsetHeight || 0) : 0;
        document.getElementById('op').style.setProperty('--op-hdr-h', h + 'px');
        // Mobile bug (recurring): if setHdr runs before the header has laid out,
        // offsetHeight is 0, the fixed browser stage docks at top:0 and paints
        // over the host-app nav → "the header disappeared". If we're mobile,
        // not fullscreen, the header exists but measured 0, retry until it's real.
        if (mob && !full && hdr && h === 0) {
          requestAnimationFrame(setHdr);
        }
      } catch(_){}
    }
    setHdr();
    // measure again after fonts/layout settle — the first synchronous read often
    // lands 0 on a cold mobile paint.
    requestAnimationFrame(() => requestAnimationFrame(setHdr));
    setTimeout(setHdr, 120); setTimeout(setHdr, 400);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(setHdr);
    window.addEventListener('load', setHdr);
    window.addEventListener('resize', setHdr);
    window.addEventListener('orientationchange', () => setTimeout(setHdr, 60));
    document.getElementById('op-full')?.addEventListener('click', () => setTimeout(setHdr, 50));
  })();
  // Keep the base viewport untouched. Temporarily installing a scalability lock while
  // the compact chat input was focused could strand iOS in the locked state when
  // focus changed during a gesture or navigation. Native page zoom is the higher-
  // priority contract; accepting Safari's small-input focus zoom is the tradeoff.
  // (2026-07-17) A pinch-zoom-out drift "snap to origin" listener lived here
  // for ~2 hours and was REVERTED: visualViewport scroll events also fire on
  // NORMAL page scrolling, so on the host-app mount (where window scroll is
  // legitimate) it force-reset every scroll and fought pinch gestures crossing
  // scale 1 — "can't zoom out of host-app or operator". A future fix needs
  // gesture-END detection (touchend + settle delay) gated to the truly-fixed
  // fullscreen cockpit only. Do not re-add the naive version.
  window.addEventListener('pagehide', () => {
    document.documentElement.classList.remove('op-locked');
    document.body.classList.remove('op-locked');
  });
  (function(){
    const FULL_KEY = 'operator-fullscreen-v1';
    const btn = document.getElementById('op-full');
    // restore: if the user was in fullscreen before a refresh, re-enter it
    try { if (localStorage.getItem(FULL_KEY) === '1') { document.body.classList.add('op-full'); if (typeof setHdr === 'function') setTimeout(setHdr, 50); } } catch {}
    btn && btn.addEventListener('click', () => {
      const on = document.body.classList.toggle('op-full');
      try { localStorage.setItem(FULL_KEY, on ? '1' : '0'); } catch {}
    });
  })();

  // ── flat / high-contrast theme toggle (strips gradients/glows/shadows) ──
  (function(){
    const FLAT_KEY = 'operator-flat-v1';
    const opEl = document.getElementById('op');
    const flatBtn = document.getElementById('op-flat');
    const apply = (on)=>{ if (opEl) opEl.classList.toggle('op-flat', on); };
    try { apply(localStorage.getItem(FLAT_KEY) === '1'); } catch {}
    if (flatBtn) flatBtn.addEventListener('click', ()=>{
      const on = !(opEl && opEl.classList.contains('op-flat'));
      apply(on);
      try { localStorage.setItem(FLAT_KEY, on ? '1' : '0'); } catch {}
    });
  })();

  // (overflow-dropdown removed — all controls now fit inline as a compact row)

  // ── live action status (Operator-style present-tense) ──
  const VERB = { goto:'Navigating', click:'Clicking', click_at:'Clicking', rclick_at:'Right-clicking',
    type:'Typing', key:'Pressing', scroll:'Scrolling', back:'Going back',
    forward:'Going forward', reload:'Reloading', tab_next:'Switching tab' };
  let settleT = null;
  // set status-card text and animate the change in (like the chat bubbles) — only
  // when the value actually changes, so the 1.5s polls don't strobe it.
  // subline as "gpt · browsing" (bot semibold) — animated like the verb.
  function setCardSub(bot, verb, emoji){
    const sub = document.getElementById('op-action-sub');
    if (!sub) return;
    const html = (bot ? '<span class="sub-bot">'+bot+'</span>' : '')
               + (bot && verb ? ' · ' : '') + (verb ? verb : '')
               + (emoji ? ' <span class="sub-emo">'+emoji+'</span>' : '');
    // mirror the live action emoji onto the minimized-pill glyph (shown by CSS
    // only when the card is collapsed) — same swap animation as the sub's emoji
    setMinEmoji(emoji);
    if (sub.innerHTML === html) return;
    sub.innerHTML = html;
    sub.classList.remove('op-card-swap'); void sub.offsetWidth; sub.classList.add('op-card-swap');
  }
  function setMinEmoji(emoji){
    const el = document.getElementById('op-min-emoji');
    if (!el || el.textContent === (emoji||'')) return;
    el.textContent = emoji || '';
    el.classList.remove('op-card-swap'); void el.offsetWidth; el.classList.add('op-card-swap');
  }
  function setCardText(el, text){
    if (!el || el.textContent === text) { if(el && text!==undefined) el.textContent = text==null?'':text; return; }
    el.textContent = text==null ? '' : text;
    el.classList.remove('op-card-swap');
    void el.offsetWidth;                 // reflow so the animation re-triggers
    el.classList.add('op-card-swap');
  }
  function setAction(kind, sub, busy) {
    if (settleT) { clearTimeout(settleT); settleT = null; }
    op.dataset.busy = busy ? '1' : '0';
    setCardText(actTxt, (VERB[kind] || 'Working'));
    if (sub !== undefined) setCardText(actSub, sub || '');
  }
  let _failRingT = null;
  // idle status-card label: NEVER "Manual" (the owner) — it reflects the BROWSER state.
  // live feed → "Ready"; otherwise (connecting / signal lost / not yet attached) →
  // "Connecting". Independent of MAN/AUTO mode.
  function idleCardText() {
    var st = op.dataset.state || '';
    if (st === 'live' && !op.classList.contains('op-signal-lost') &&
        !op.classList.contains('op-signal-stale')) return 'Ready';
    // ANY lost-signal state owns the word 'Reconnecting' — the frozen stale
    // frame AND the full overlay. signalLost writes the literal 'Reconnecting';
    // when this helper answered 'Connecting' for the same state, every other
    // card repaint (agent-poll idle, settle timers, mode re-apply) flipped the
    // word back and the card flapped at whatever cadence those run (2026-07-11).
    // Exception: the desktop "Not started" idle state keeps its own label.
    if (op.classList.contains('op-signal-stale') ||
        (op.classList.contains('op-signal-lost') &&
         !op.classList.contains('op-surface-idle'))) return 'Reconnecting';
    return 'Connecting';
  }
  function settleAction(ok) {
    // A manual browser action (click/drag/scroll/type on the live view) settles
    // here. But if an AGENT turn is in flight, the card belongs to that turn
    // ("Working") — a user click on the page must NOT stomp it to "Ready"/idle
    // (the flash the owner saw: Working -> Ready on every click mid-turn). Keep the
    // failure ring (useful "that manual action failed" feedback) but leave the
    // turn's busy/text alone; the agent poll owns them.
    if (typeof _inFlight !== 'undefined' && _inFlight) {
      if (ok === false) {
        op.classList.remove('op-act-failed'); void op.offsetWidth;
        op.classList.add('op-act-failed');
        if (_failRingT) clearTimeout(_failRingT);
        _failRingT = setTimeout(() => op.classList.remove('op-act-failed'), 1500);
      }
      return;
    }
    op.dataset.busy = '0';
    // idle label depends on mode: MAN -> "Manual", AUTO -> "Ready". never flash
    // "Ready" in manual (it's an auto-mode concept).
    setCardText(actTxt, ok === false ? 'Failed' : 'Ready');
    if (ok === false) {
      // flash the status-card ring red + pulse. Re-trigger the animation by removing
      // then re-adding on the next frame (so back-to-back failures each pulse).
      op.classList.remove('op-act-failed'); void op.offsetWidth;
      op.classList.add('op-act-failed');
      if (_failRingT) clearTimeout(_failRingT);
      _failRingT = setTimeout(() => op.classList.remove('op-act-failed'), 1500);
    } else { op.classList.remove('op-act-failed'); }
    settleT = setTimeout(() => { if (op.dataset.busy === '0' && !op.classList.contains('op-signal-lost'))
      setCardText(actTxt, idleCardText()); }, 1500);
  }

  // ── feed: self-clocking frame pump (replaces the MJPEG push stream) ──
  // The push stream had no backpressure: a client that decodes slower than the
  // feed produces (iPad Safari) buffered the excess and drifted PROGRESSIVELY
  // behind live — near-live after a reconnect, seconds behind a minute later
  // (2026-07-09). The pump fetches one frame, renders it, and only then asks
  // for the next: it can NEVER queue more than one frame, so latency is
  // bounded at ~1 frame in flight on any device or link. Fast clients pace at
  // PUMP_MS (~10fps); slow ones self-throttle to what they can actually drain.
  let backoff = 600;
  let _hasFrame = false;
  const PUMP_MS = 90;
  const _sleepMs = (ms) => new Promise(r => setTimeout(r, ms));
  // F1: lean frames on small screens / metered connections — tier=lo makes the
  // server downscale + compress harder (a full-res frame per pump tick rips
  // through mobile data). Read per fetch so rotation/Save-Data flips adapt live.
  const _mqNarrow = matchMedia('(max-width: 820px)');
  function _feedTier() {
    const c = navigator.connection || {};
    return (_mqNarrow.matches || c.saveData ||
            /(^|\b)(slow-)?2g|3g\b/.test(c.effectiveType || '')) ? 'lo' : 'hi';
  }
  let _pumpOn = false, _prevBlobUrl = null, _pumpFails = 0;
  // true while the frame on stage is the server's PLACEHOLDER (dark filler the
  // /frame route serves when the streamer has no real capture). Placeholder ≠
  // signal: letting its 'load' events call signalOk() had the pump clearing
  // SIGNAL LOST ~11×/s while the status poll re-asserted it every 1.5s — the
  // Connecting↔Reconnecting word flap + class strobing (2026-07-10).
  let _phFrame = false;
  async function _pump() {
    if (_pumpOn) return;   // one pump per page, ever
    _pumpOn = true;
    while (true) {
      if (document.visibilityState !== 'visible') { await _sleepMs(350); continue; }
      try {
        const r = await fetch(FRAME + "?t=" + Date.now() + "&tier=" + _feedTier(),
                              {cache: "no-store"});
        if (!r.ok) throw new Error("http " + r.status);
        const b = await r.blob();
        const u = URL.createObjectURL(b);
        _phFrame = (r.headers.get('X-Operator-Frame') === 'placeholder');
        view.src = u;                    // fires 'load' → signalOk (real frames only)
        if (_prevBlobUrl) URL.revokeObjectURL(_prevBlobUrl);
        _prevBlobUrl = u;
        _pumpFails = 0; backoff = 600;
        // typing on a phone: a ~10fps JPEG decode cycle on the main thread
        // competes with every keystroke's layout work — the biggest slice of
        // the iPhone input lag (2026-07-12). While the composer is focused on
        // a narrow screen, idle the feed to ~2.5fps; it snaps back on blur.
        const _typing = _mqNarrow.matches &&
          document.activeElement === document.getElementById('op-input');
        await _sleepMs(_typing ? 400 : PUMP_MS);
      } catch (_) {
        _pumpFails++;
        if (_pumpFails === 2) signalLost();   // one blip ≠ lost; two in a row is
        await _sleepMs(Math.min(backoff *= 1.6, 4000));
      }
    }
  }
  function connectStream() { _pump(); }          // legacy callers just nudge the pump
  function scheduleReconnect() { /* the pump retries itself — kept for old callers */ }
  let _lastImgLoad = 0;
  let _wasLost = false;
  let _desktopNoFrame = false;   // last poll said a desktop surface has no live frame
  // poll-authority cold flag: true while the status poll says the feed is cold
  // (has_frame:false past the grace window, or status:error). While set, a
  // pumped frame's 'load' must NOT clear the lost/stale state: /frame re-serves
  // the LAST frame marked "live" however old it is, so during a stall the pump
  // "healed" the overlay every 90ms and the next poll re-asserted it — the
  // dim↔undim strobe at poll cadence (the residual flicker, 2026-07-11). The
  // poll owns the exit; recovery costs at most one poll tick.
  let _pollCold = false;
  function signalLost(){
    // the desktop "Not started" rest state is poll-owned — a pump fetch blip
    // must not yank the calm idle card into SIGNAL LOST (it flapped
    // "Not started" ↔ "SIGNAL LOST" at retry cadence on flaky links). There
    // is no signal to lose while the desktop isn't started and nothing runs.
    if (op.classList.contains('op-surface-idle') && op.dataset.busy !== '1') return;
    _wasLost = true;
    op.classList.remove('op-surface-idle');
    if (_hasFrame && _lastImgLoad > 0) {
      // We HAVE a last good frame → freeze it (dimmed, small "reconnecting" chip)
      // instead of blanking to the SIGNAL LOST screen. Flapping between a live
      // frame and a full-screen overlay every few seconds read as the feed
      // "flickering in and out" (2026-07-10); a static stale frame is calm.
      op.classList.add('op-signal-stale');
      op.classList.remove('op-signal-lost');   // overlay stays hidden — frame owns the stage
    } else {
      // never had a frame this session — nothing to freeze, show the full overlay
      view.style.visibility='hidden';   // hide the broken-? glyph
      if (overlayText) overlayText.textContent = 'SIGNAL LOST';
      if (overlaySub) { overlaySub.hidden = true; overlaySub.textContent = ''; }
      op.classList.add('op-signal-lost');
    }
    // the card must agree with the stage — never show "Ready" over a dead feed.
    // Idempotent single label: idleCardText() also resolves to 'Reconnecting'
    // while stale, so the poll loop can't flap the word back to 'Connecting'.
    if (op.dataset.busy !== '1') { try { setCardText(actTxt, 'Reconnecting'); } catch(_){} } }
  // Desktop surface at rest: the virtual desktop hasn't been started yet. A
  // calm "here's how to start it" state, distinct from a lost signal.
  function desktopIdle(){ _wasLost = false;
    if (op.classList.contains('op-surface-idle')) return;   // idempotent — no per-poll churn
    view.style.visibility='hidden';
    setState('idle', '');
    if (overlayText) overlayText.textContent = 'Not started';
    if (overlaySub) { overlaySub.hidden = false;
      overlaySub.textContent = 'Send a task to start the desktop.'; }
    op.classList.add('op-signal-lost', 'op-surface-idle');
    if (op.dataset.busy !== '1') { try { setCardText(actTxt, 'Idle'); } catch(_){} } }
  function signalOk(){
    // The desktop stream always heartbeats a placeholder JPEG, so the <img>
    // fires 'load' even when the virtual desktop hasn't started. Don't let that
    // clear the deterministic "Not started" idle state (poll owns it via
    // has_frame); only a real frame (has_frame:true) exits idle. Return BEFORE
    // touching visibility — otherwise the placeholder load unhides the feed and
    // the next poll re-hides it, flickering at the ~1s heartbeat cadence.
    if (_desktopNoFrame && _isGameSurface() && op.dataset.busy !== '1') return;
    const wasLost = op.classList.contains('op-signal-lost') ||
                    op.classList.contains('op-signal-stale');
    op.classList.remove('op-signal-lost', 'op-signal-stale', 'op-surface-idle');
    view.style.visibility='visible';
    if (overlaySub) overlaySub.hidden = true;
    // feed came back while idle → settle the card to its proper idle label.
    if (wasLost && op.dataset.busy !== '1') {
      try { setCardText(actTxt, idleCardText()); } catch(_){} } }
  view.decoding = 'async';   // keep JPEG decode off the main thread — a 10fps sync decode stole time from every keystroke (iPhone lag)
  view.addEventListener('load', () => {
    if (_phFrame) return;   // placeholder ≠ signal — never claims a frame or clears SIGNAL LOST
    _hasFrame = true; _lastImgLoad = Date.now();
    if (_pollCold) return;  // stale re-served frame during a poll-declared cold spell — poll owns the exit
    signalOk(); backoff = 600; });   // signalOk owns visibility (skips it when desktop-idle)
  // a single bad blob isn't a lost signal — the pump's consecutive-failure
  // rule owns SIGNAL LOST; the next pumped frame simply replaces this one.
  view.addEventListener('error', () => {});
  connectStream();
  // tab was backgrounded (browser suspends the MJPEG + timers) → on return,
  // immediately reconnect the stream + re-poll so the feed snaps back fast.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      _coldMs = 0; connectStream();
      if (typeof poll === 'function') poll();
      if (typeof pollTabs === 'function') pollTabs();
    }
  });

  function setState(s, d) {
    op.dataset.state = s;  // connection dot/ring — never fights a live action label
    // When the feed comes up (or while connecting), the ONLY action text we own is the
    // stale connecting/reconnecting placeholder — clear it to Ready so the card doesn't
    // get stuck on "Connecting…" after the feed is actually live (esp. after a server
    // restart). We never touch a real working/done/error/interrupted label.
    const t = (actTxt.textContent || '').trim();
    const isPlaceholder = (t === 'Connecting…' || t === 'Connecting' || t === 'reconnecting…' || t === 'Reconnecting…' || t === '');
    if (s === 'live' && (!op.dataset.busy || op.dataset.busy === '0') && isPlaceholder) {
      actTxt.textContent = (MODE === 'man') ? 'Ready' : 'Ready';
      op.dataset.agent = op.dataset.agent || '';
    }
  }

  const lockEl = document.getElementById('op-lock');
  function showUrl(url) {
    if (lockEl) {
      const https = /^https:\/\//i.test(url||'');
      const http = /^http:\/\//i.test(url||'');
      const body = '<rect x="3" y="7" width="10" height="7" rx="1.4"></rect>';
      const closed = '<path d="M5 7V5a3 3 0 0 1 6 0v2"></path>';   // full shackle
      const open = '<path d="M5 7V5a3 3 0 0 1 5.2-2"></path>';      // open shackle
      const padlock = (https || http)
        ? '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">'
          + (https ? closed : open) + body + '</svg>'
        : '';
      // The URL-bar slot shows the live SITE FAVICON, falling back to the padlock
      // when the icon can't load. (The HTTPS lock proper lives on the page-status
      // dot — see setLockDot — so the favicon keeps its home here; 2026-07-02
      // reverting claude-f's v0.7.0 lock-in-favicon-slot swap.) Cache the host so
      // the img doesn't reflash every poll.
      let host = '';
      try { host = (https || http) ? new URL(url).hostname : ''; } catch {}
      if (host && lockEl.dataset.favHost !== host) {
        lockEl.dataset.favHost = host;
        lockEl.innerHTML = '';
        const fv = document.createElement('img');
        fv.width = 13; fv.height = 13; fv.alt = '';
        fv.style.borderRadius = '3px'; fv.style.display = 'block';
        fv.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=32';
        fv.addEventListener('error', () => { lockEl.innerHTML = padlock; });
        lockEl.appendChild(fv);
      } else if (!host) {
        lockEl.dataset.favHost = '';
        lockEl.innerHTML = padlock;
      }
      lockEl.className = 'op-lock' + (https ? ' secure' : (http ? ' insecure' : ''));
      lockEl.title = '';   // suppress native tooltip; we render a styled one
      lockEl.dataset.tip = (host ? host + ' — ' : '') + (https ? 'Secured with HTTPS' : (http ? 'Not secure' : ''));
      // The page-status dot doubles as the HTTPS lock (2026-07-02).
      setLockDot(https, http);
    }
    // urlEl is an editable input; don't clobber it while the user is typing in it
    if (document.activeElement === urlEl) return;
    urlEl.value = url || '';
  }
  // The page-status dot doubles as the HTTPS lock: render a padlock glyph into
  // it (closed shackle = https, open = http). Colour rides on the .loading/.err
  // classes act() toggles, so it still shows nav status. No scheme (blank/search)
  // → clear the glyph and the dot reverts to the plain filled status dot via
  // .op-dotstat:empty. (2026-07-02.)
  const dotEl = document.getElementById('op-dotstat');
  function setLockDot(https, http) {
    if (!dotEl) return;
    const kind = https ? 's' : (http ? 'i' : '');
    if (dotEl.dataset.lockKind === kind) return;   // avoid reflash every poll
    dotEl.dataset.lockKind = kind;
    if (!kind) { dotEl.innerHTML = ''; dotEl.title = ''; return; }
    const body = '<rect x="3" y="7" width="10" height="7" rx="1.4"></rect>';
    const shackle = https
      ? '<path d="M5 7V5a3 3 0 0 1 6 0v2"></path>'    // closed
      : '<path d="M5 7V5a3 3 0 0 1 5.2-2"></path>';   // open
    dotEl.innerHTML =
      '<svg viewBox="0 0 16 16" width="11" height="11" fill="none" '
      + 'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" '
      + 'stroke-linejoin="round">' + shackle + body + '</svg>';
    dotEl.title = https ? 'Secured with HTTPS' : 'Not secure';
  }
  // URL bar navigates on Enter
  // does this look like a URL/host the user means to navigate to (vs a search query)?
  function looksLikeUrl(v){
    if (/^https?:\/\//i.test(v)) return true;
    if (/\s/.test(v)) return false;                       // has spaces → it's a query
    if (/^localhost(:\d+)?(\/|$)/i.test(v)) return true;  // localhost[:port]
    if (/^[\w-]+(\.[\w-]+)+(:\d+)?(\/|$)/.test(v)) return true;  // has a dot (domain)
    if (/^[\d.]+(:\d+)?(\/|$)/.test(v)) return true;     // bare IP
    return false;                                          // single bare word → search it
  }
  urlEl.addEventListener('keydown', e => {
    if (e.key === 'Enter') { const v = urlEl.value.trim();
      if (v) {
        if (looksLikeUrl(v)) {
          act({kind:'goto', value: v}, null, true);        // navigate
        } else {
          act({kind:'goto', value: 'https://www.google.com/search?q=' + encodeURIComponent(v)}, null, true);  // search
        }
      }
      urlEl.blur(); }
  });

  // ── conversation log ──
  function logUser(text) {
    const m = document.createElement('div'); m.className = 'op-msg user';
    const b = document.createElement('span'); b.className='bubble'; b.textContent=text;
    m.appendChild(b); log.appendChild(m); _trimOnly(); scrollToBottom(true);
  }
  function _msgTime(){
    const d = new Date();
    let h = d.getHours(), m = d.getMinutes(); const ap = h < 12 ? 'AM' : 'PM';
    h = h % 12 || 12; m = (m < 10 ? '0' : '') + m;
    const t = document.createElement('span'); t.className = 'op-msg-time'; t.textContent = h + ':' + m + ' ' + ap;
    return t;
  }
  // only the LAST user message shows the edit/retry controls
  function _markLastUser(){
    const users = log.querySelectorAll('.op-msg.user');
    users.forEach((u,i)=> u.classList.toggle('op-last-user', i === users.length-1));
  }
  function _esc(t){ return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function _mdInline(t){
    // operate on already-escaped text; safe because no raw HTML survives _esc.
    // Order: inline-code first (so its contents aren't re-formatted), then bold,
    // italic, explicit [text](url) links, then BARE urls (so a plain https://… is
    // clickable too — the bot often pastes raw links).
    return t
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/!\[([^\]]*)\]\(((?:https?:\/\/|\/?operator\/shot\/)[^\s)]+)\)/g,
               '<img src="$2" alt="$1" class="op-md-img" loading="lazy">')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
               '<a href="$2" target="_blank" rel="noopener">$1</a>')
      // bare URL → link, but skip ones already inside an href="" we just made
      .replace(/(^|[^"\>=])(https?:\/\/[^\s<)]+)/g,
               '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  }
  function _mdToHtml(text){
    const src = _esc(text);
    // First peel off ``` fenced code blocks into <pre><code> — their contents are
    // verbatim (no bold/link processing). This is what was rendering as literal
    // ``` lines and swallowing bold/links the bot meant as prose.
    const parts = src.split(/```/);
    let html = '';
    for (let i = 0; i < parts.length; i++){
      if (i % 2 === 1){
        // inside a fence: drop an optional leading language tag line, keep the rest raw
        let code = parts[i].replace(/^[^\n]*\n/, m => /^[A-Za-z0-9_+-]*\s*$/.test(m.trim()) ? '' : m);
        html += '<pre><code>' + code.replace(/^\n/,'').replace(/\n$/,'') + '</code></pre>';
      } else {
        // trim ONE blank line adjoining the fence so it doesn't render an extra <br>
        let seg = parts[i];
        // strip ALL blank lines adjoining a fence (they'd each render a <br> and
        // balloon the gap above/below the code block) + any trailing/leading newline
        if (i > 0) seg = seg.replace(/^(?:[ \t]*\n)+/, '');          // after a fence
        if (i < parts.length - 1) seg = seg.replace(/(?:\n[ \t]*)+$/, '');  // before a fence
        html += _renderBlock(seg);
      }
    }
    return html;
  }
  function _renderBlock(src){
    const lines = src.split(/\n/);
    let html='', inList=false;
    for (let raw of lines){
      const h = raw.match(/^\s*(#{1,6})\s+(.*)/);
      if (h){ if(inList){html+='</ul>';inList=false;}
        const lvl=Math.min(h[1].length,6); html+='<div class="op-md-h op-md-h'+lvl+'">'+_mdInline(h[2])+'</div>'; continue; }
      const li = raw.match(/^\s*[-*]\s+(.*)/);
      if (li){ if(!inList){html+='<ul>';inList=true;} html+='<li>'+_mdInline(li[1])+'</li>'; }
      else { if(inList){html+='</ul>';inList=false;}
        if(raw.trim()==='') html+='<br>'; else html+='<div>'+_mdInline(raw)+'</div>'; }
    }
    if(inList) html+='</ul>';
    return html;
  }
  // add a hover-reveal copy button to the top-right of each code block in a bubble.
  // button is built via DOM (not innerHTML) → XSS-safe; copies the <code> text.
  function _addCopyButtons(bubble){
    bubble.querySelectorAll('pre').forEach(pre => {
      if (pre.querySelector('.op-copy')) return;
      pre.classList.add('op-haspre');
      const btn=document.createElement('button'); btn.type='button'; btn.className='op-copy';
      btn.title='copy'; btn.setAttribute('aria-label','copy code');
      const NS='http://www.w3.org/2000/svg';
      const mk=(d)=>{const s=document.createElementNS(NS,'svg');s.setAttribute('viewBox','0 0 24 24');
        s.setAttribute('width','12');s.setAttribute('height','12');s.setAttribute('fill','none');
        s.setAttribute('stroke','currentColor');s.setAttribute('stroke-width','2');
        s.setAttribute('stroke-linecap','round');s.setAttribute('stroke-linejoin','round');
        const p=document.createElementNS(NS,'path');p.setAttribute('d',d);s.appendChild(p);return s;};
      const COPY_D='M9 9h10v10H9zM5 15V5h10';      // two overlapping squares
      const CHECK_D='M5 13l4 4L19 7';                // SVG checkmark (confirm)
      btn.appendChild(mk(COPY_D));
      let _busy=false;
      async function doCopy(ev){
        if (ev) { ev.preventDefault(); ev.stopPropagation(); }   // iPad: stop the log/parent eating the tap
        if (_busy) return; _busy=true;
        const code=pre.querySelector('code'); const txt=code? code.textContent : pre.textContent;
        let ok=false;
        // 1) async Clipboard API — only available in a secure context (https/localhost)
        if (navigator.clipboard && window.isSecureContext) {
          try { await navigator.clipboard.writeText(txt); ok=true; } catch(_){}
        }
        // 2) execCommand fallback — works in non-secure contexts (e.g. plain-http LAN)
        if (!ok) {
          try { const ta=document.createElement('textarea'); ta.value=txt;
            ta.style.position='fixed'; ta.style.top='0'; ta.style.left='0'; ta.style.opacity='0';
            document.body.appendChild(ta); ta.focus(); ta.select();
            ok=document.execCommand('copy'); ta.remove(); } catch(__){}
        }
        btn.classList.add('copied'); btn.replaceChildren(mk(CHECK_D));   // always confirm (SVG check)
        setTimeout(()=>{ btn.classList.remove('copied'); btn.replaceChildren(mk(COPY_D)); _busy=false; }, 1200);
      }
      btn.addEventListener('click', doCopy);
      // iPad/touch: handle the tap directly so a parent touch handler can't swallow it
      btn.addEventListener('touchend', doCopy, { passive: false });
      pre.appendChild(btn);
    });
  }
  function logBotReply(text) {
    markScroll();
    const m = document.createElement('div'); m.className='op-msg bot';
    const b = document.createElement('span'); b.className='bubble';
    b.innerHTML = _mdToHtml(text);   // _mdToHtml escapes first → XSS-safe
    _addCopyButtons(b);
    m.appendChild(b); log.appendChild(m); _trimOnly(); scrollToBottom();
  }

  // ── #4 hand-off: render the "Take control" card; clicking it stops the agent,
  //    flips to MAN mode, and drops a "Took control ⌨️" notice. All textContent → XSS-safe. ──
  // the disclaimer's monitor icon, WITHOUT the diagonal slash (= you HAVE control).
  // built via DOM (SVG namespace) so it's XSS-safe and renders inline in the notice.
  function _monitorIcon() {
    const NS='http://www.w3.org/2000/svg';
    const svg=document.createElementNS(NS,'svg');
    svg.setAttribute('class','op-sys-ico'); svg.setAttribute('viewBox','0 0 24 24');
    svg.setAttribute('width','11'); svg.setAttribute('height','11'); svg.setAttribute('fill','none');
    svg.setAttribute('stroke','currentColor'); svg.setAttribute('stroke-width','1.7');
    svg.setAttribute('stroke-linecap','round'); svg.setAttribute('stroke-linejoin','round');
    svg.setAttribute('aria-hidden','true');
    const rect=document.createElementNS(NS,'rect');
    rect.setAttribute('x','3'); rect.setAttribute('y','4.5'); rect.setAttribute('width','18');
    rect.setAttribute('height','12'); rect.setAttribute('rx','1.6');
    const stand=document.createElementNS(NS,'path'); stand.setAttribute('d','M9 20h6M12 16.5V20');
    svg.appendChild(rect); svg.appendChild(stand);   // monitor + stand, NO slash
    return svg;
  }
  function logSys(text, icon) {
    markScroll();
    const m = document.createElement('div'); m.className='op-msg sys';
    const b = document.createElement('span'); b.className='body';
    if (icon === 'monitor') b.appendChild(_monitorIcon());   // SVG icon (spaced via CSS gap)
    else if (icon) { const e=document.createElement('span'); e.className='sys-emo'; e.textContent=icon; b.appendChild(e); }
    const t=document.createElement('span'); t.textContent = text; b.appendChild(t);
    m.appendChild(b); log.appendChild(m); _trimOnly(); scrollToBottom(); saveSession();
  }
  function takeControl(card){
    if (card && card.dataset.done === '1') return;
    if (card) card.dataset.done = '1';
    const btn = card && card.querySelector('.op-takeover-btn');
    if (btn) { btn.disabled = true; const l = btn.querySelector('.tk-lab'); if (l) l.textContent = 'You have control'; }
    try { fetch(STOP_URL, {method:'POST'}); } catch(_){}
    // close the running turn quietly + clear in-flight state
    _interrupting = true; _handledState = 'done'; _postSteerUntil = Date.now() + 1500;
    if (_task) { try { finishTask(false, 'Handed off'); } catch(_){} }
    op.dataset.busy='0'; op.dataset.agent=''; _inFlight = false;
    setTimeout(()=>{ _interrupting=false; }, 1500);
    if (card) card.remove();                       // the notice replaces the card
    logSys('Took control', 'monitor');   // monitor icon (no slash) = you have control
    _handedToUser = true;                // Operator kicked control to YOU → show Finish-up
    MODE = 'man'; applyMode(); saveSession();       // hand the wheel to the user
  }
  function renderHandoff(reason){
    // DEDUP: never stack hand-off cards. If one is already on screen (the agent can
    // re-emit the [[TAKE_CONTROL]] marker every step while it waits at a 2FA/captcha
    // gate, which otherwise spawns a card per step — the "million cards" storm), just
    // refresh the existing card's reason and bail instead of appending another.
    const _existing = log.querySelector('.op-handoff[data-handoff="1"]');
    if (_existing) {
      if (reason && String(reason).trim()) {
        let r = _existing.querySelector('.op-handoff-reason');
        if (!r) { r = document.createElement('div'); r.className='op-handoff-reason';
          _existing.appendChild(r); }
        r.textContent = String(reason).trim();
      }
      return _existing;
    }
    markScroll();
    const card = document.createElement('div'); card.className = 'op-handoff'; card.dataset.handoff='1';
    const head = document.createElement('div'); head.className='op-handoff-head';
    const hi = document.createElement('span'); hi.className='hi';   // CSS stop-square dot (no emoji)
    const ht = document.createElement('span'); ht.textContent='Operator needs your input';
    head.appendChild(hi); head.appendChild(ht); card.appendChild(head);
    if (reason && String(reason).trim()) {
      const r = document.createElement('div'); r.className='op-handoff-reason';
      r.textContent = String(reason).trim(); card.appendChild(r);
    }
    const btn = document.createElement('button'); btn.type='button'; btn.className='op-takeover-btn';
    const bl = document.createElement('span'); bl.className='tk-lab'; bl.textContent='Take control';
    btn.appendChild(bl);   // no emoji — clean text pill
    btn.addEventListener('click', ()=> takeControl(card));
    card.appendChild(btn);
    log.appendChild(card); _trimOnly(); scrollToBottom(); saveSession();
    return card;
  }

  // ── Operator-style task group ("Worked for Nm" + indented steps) ──
  let _task = null, _taskStart = 0, _stepCount = 0;
  const BOT_EMOJI = { 'claude-b':'🦆', 'claude-a':'💣', 'gpt':'🤖', 'gemma':'✨' };
  function botEmoji(b){ return BOT_EMOJI[b] || '🤖'; }
  // gemma rides on the agy runtime; its picker FACE shows the real Gemini logo
  // (gradient 4-point star) instead of a flat emoji. HTML <option> text can't
  // hold SVG, so the dropdown still uses the ✦ glyph above — the SVG is only for
  // the face span (innerHTML). Other bots keep their emoji face.
  const GEMINI_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true" style="display:block">'
    + '<defs><linearGradient id="op-gemini-grad" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">'
    + '<stop offset="0" stop-color="#4285F4"/><stop offset="0.45" stop-color="#9B72CB"/>'
    + '<stop offset="0.85" stop-color="#D96570"/><stop offset="1" stop-color="#F2A60C"/></linearGradient></defs>'
    + '<path fill="url(#op-gemini-grad)" d="M11.04 19.32Q12 21.51 12 24q0-2.49.93-4.68.96-2.19 2.58-3.81t3.81-2.55Q21.51 12 24 12q-2.49 0-4.68-.93a12.3 12.3 0 0 1-3.81-2.58 12.3 12.3 0 0 1-2.58-3.81Q12 2.49 12 0q0 2.49-.96 4.68-.93 2.19-2.55 3.81a12.3 12.3 0 0 1-3.81 2.58Q2.49 12 0 12q2.49 0 4.68.96 2.19.93 3.81 2.55t2.55 3.81"/></svg>';
  // Set the picker face to a bot's icon — SVG logo for gemma, emoji otherwise.
  function setPickFace(el, b){ if(!el) return; if(b==='gemma') el.innerHTML = GEMINI_SVG; else el.textContent = botEmoji(b); }
  // emoji per action label (matches the words from ACT_VERB / the action-tap)
  const ACT_EMOJI = {
    Browsing:'🌐', Navigating:'🌐', Navigated:'🌐', Clicking:'🖱️', Clicked:'🖱️',
    Dragging:'🖱️', Moving:'🖱️', Pressing:'🖱️', Releasing:'🖱️',
    Typing:'⌨️', Typed:'⌨️', Pressing:'⌨️', Pressed:'⌨️',
    Scrolling:'📜', Scrolled:'📜', Reading:'📖', 'Read page':'📖', Reading_page:'📖',
    'Took screenshot':'📸', Capturing:'📸', Captured:'📸', Waiting:'⏳', Waited:'⏳', Selecting:'☑️', Selected:'☑️',
    Hovering:'👆', Hovered:'👆', 'Going back':'↩️', 'Went back':'↩️',
    Uploading:'📎', Uploaded:'📎', 'Filling form':'📝', 'Filled form':'📝',
    'Ran JS':'⚙️', 'Run JS':'⚙️', 'Switched tab':'🗂️', 'Switching tab':'🗂️', 'Switch tab':'🗂️', Ran:'⚙️',
    Navigate:'🌐', Click:'🖱️', 'Double-click':'🖱️', Type:'⌨️', Press:'⌨️', Scroll:'📜', Read:'📖', Reading:'📖', Screenshot:'📸',
    Capture:'📸', Screenshot:'📸', Wait:'⏳', Select:'☑️', Hover:'👆', Back:'↩️', Forward:'↪️', Upload:'📎',
    'Fill form':'📝', Fill:'📝', Drag:'✊', 'New tab':'🗂️', 'Close tab':'🗂️', Close:'✖️',
    Resize:'📐', 'Handle dialog':'💬', 'Read console':'🖥️', 'Inspect network':'📡', 'Save PDF':'📄',
    Searching:'🔍', Fetching:'🔗', 'Running command':'⌨️', 'Reading file':'📄',
    'Searching files':'🔍', 'Finding files':'📁', 'Writing file':'✏️', 'Editing file':'✏️',
    'Checking quote':'📈', 'Checking portfolio':'📊',
    'Searching web':'🌐', 'Searching the web':'🌐', 'Searching memory':'🧠', 'Searching files':'🔍',
    Recalling:'🧠', 'Checking memory':'🧠', Fetching:'🔗', 'Fetching messages':'💬',
    Listing:'📋', 'Listing resources':'📋', 'Listing files':'📁', 'Reading resource':'📖',
    'Reading console':'🖥️', 'Reading docs':'📚', 'Reading file':'📄',
    Replying:'💬', 'Sending message':'💬', Reacting:'😀', Downloading:'📥', 'Setting presence':'🟢',
    Delegating:'🤝', 'Updating todos':'✅', 'Looking up library':'📚', 'Using tool':'🛠️',
    'Calling MCP tool':'🛠️', Getting:'📥', Creating:'✨', Making:'✨', Updating:'✏️',
    Deleting:'🗑️', Removing:'🗑️', Adding:'➕', Loading:'⏳', Saving:'💾',
    Querying:'🔎', Resolving:'🔗', Building:'🔨', Starting:'▶️', Stopping:'⏹️',
    Pulling:'⬇️', Pushing:'⬆️', Posting:'📮', Opening:'📂', Finding:'🔍',
    'took a screenshot':'📸', 'Took a screenshot':'📸',
    Thinking:'💭', Working:'⚙️'
  };
  function actEmoji(label){
    if (!label) return '⚙️';
    if (ACT_EMOJI[label]) return ACT_EMOJI[label];
    // fall back on the leading verb so unseen variants still get a sensible icon
    // ("Searching web/files/memory" -> Searching's 🔍-family; "Reading X" -> 📖, etc.)
    const first = label.split(' ')[0];
    return ACT_EMOJI[first] || '⚙️';
  }
  // imperative trace label -> present-continuous for the live spinner ("Run JS"→"Running JS")
  const ACT_CONT = {
    Navigate:'Navigating', Click:'Clicking', 'Double-click':'Double-clicking', Type:'Typing',
    Press:'Pressing', Scroll:'Scrolling', Read:'Reading', Capture:'Capturing', Wait:'Waiting',
    Select:'Selecting', Hover:'Hovering', Back:'Going back', Forward:'Going forward',
    Upload:'Uploading', 'Fill form':'Filling form', Fill:'Filling', Drag:'Dragging',
    'Run JS':'Running JS', 'Switch tab':'Switching tab', 'New tab':'Opening tab',
    'Close tab':'Closing tab', Close:'Closing', Resize:'Resizing',
    'Handle dialog':'Handling dialog', 'Read console':'Reading console',
    'Inspect network':'Inspecting network', 'Save PDF':'Saving PDF',
    'Took screenshot':'Taking screenshot', 'Screenshot':'Taking screenshot', 'Capture':'Taking screenshot'
  };
  function actCont(label){ return ACT_CONT[label] || label; }
  const ACT_VERB = { goto:'Navigating', click:'Clicking', click_at:'Clicking', rclick_at:'Right-clicking', type:'Typing',
    key:'Pressing', scroll:'Scrolling', back:'Going back', reload:'Reloading',
    browser_click:'Clicking', browser_type:'Typing', browser_navigate:'Navigating' };
  function _el(cls, txt) { const e=document.createElement('span'); e.className=cls; if(txt!=null) e.textContent=txt; return e; }
  // task-head collapse caret as an SVG chevron (the ⌄ glyph rendered invisibly small).
  function sweepOrphanTasks(){
    // Any task node still marked busy but not the active _task is a stuck orphan
    // (e.g. a prior turn whose finishTask never fired). Force-resolve them.
    log.querySelectorAll('.op-task[data-busy="1"]').forEach(t => {
      if (t === _task) return;
      t.dataset.busy = '0'; t.classList.add('collapsed');
      const car = t.querySelector('.car'); const v = t.querySelector('.verb');
      // only drop a TRULY empty orphan (no steps). never remove a task with any trace content.
      if (!t.querySelector('.op-task-step') && !t.querySelector('.op-act-step')) { t.remove(); return; }
      if (v && /working|thinking|reading|navigating|clicking|typing/i.test(v.textContent))
        v.textContent = 'Worked';
      if (car) car.style.display = '';
    });
  }
  function startTask() {
    if (_task) { try { finishTask(); } catch(_){} }   // never orphan a prior group
    sweepOrphanTasks();
    _stepCount = 0;
    _taskStart = Date.now();
    _task = document.createElement('div'); _task.className='op-task'; _task.dataset.busy='1';
    const head=document.createElement('div'); head.className='op-task-head';
    head.appendChild(_el('ico')); head.appendChild(_el('verb')); head.appendChild(_el('car'));   // .verb seeded below via taskVerb; .car = CSS-drawn chevron
    const steps=document.createElement('div'); steps.className='op-task-steps';
    const cnt=document.createElement('div'); cnt.className='op-step-count'; cnt.hidden=true;
    cnt.innerHTML='<span class="sc-n">0 steps</span>';
    steps.appendChild(cnt);
    const thisTask = _task;                       // capture — _task gets nulled on finish
    _task.appendChild(head); _task.appendChild(steps);
    // toggle handled by a delegated listener on `log` (survives session restore)
    log.appendChild(_task); trim();
    taskVerb('Working', true);   // seed the live verb through the animation path
    return _task;
  }
  // Swap the live verb with a modern vertical rise: the outgoing word animates
  // up and out, the incoming word rises in from below. `ellipsis` appends a
  // CSS-animated "…" (three pulsing dots) for live states; done labels omit it.
  function taskVerb(text, ellipsis) {
    if (!_task) return;
    const v = _task.querySelector('.verb'); if (!v) return;
    const words = v.querySelectorAll('.op-verb-word');
    const cur = words[words.length - 1];   // the currently-shown word is the last
    // no change (same word + same ellipsis state) → leave the running ellipsis be
    if (cur && words.length === 1 && cur.dataset.word === text
        && !!cur.querySelector('.op-ellip') === !!ellipsis) return;
    // Clean replace: remove EVERY existing word before the new one enters. A
    // rapid burst of taskVerb calls (the poll replays several actions in one
    // pass) was leaving stale words behind — the whole turn's verbs piled up in
    // the done header ("Typing…Clicking…Reading…Plotted for 5m"). Clearing all
    // of them guarantees exactly one word, animated in via op-msg-in.
    words.forEach(w => w.remove());
    const incoming = document.createElement('span');
    incoming.className = 'op-verb-word op-verb-in';
    incoming.dataset.word = text;
    incoming.textContent = text;
    if (ellipsis) {
      const e = document.createElement('span'); e.className = 'op-ellip';
      for (let d = 0; d < 3; d++) { const i = document.createElement('i'); i.textContent = '.'; e.appendChild(i); }
      incoming.appendChild(e);
    }
    v.appendChild(incoming);
  }
  function taskStep(text) {
    if (!_task) startTask();
    markScroll();
    const e=document.createElement('div'); e.className='op-task-step';
    e.innerHTML = _mdToHtml(text);   // _mdToHtml escapes first → XSS-safe
    _task.querySelector('.op-task-steps').appendChild(e);
    scrollToBottom(); saveSession();
  }
  function taskError(title, reason) {
    if (!_task) startTask();
    const e=document.createElement('div'); e.className='op-task-step op-err-step';
    const head=document.createElement('div'); head.className='op-err-head';
    const mk=document.createElement('span'); mk.className='op-err-mark'; mk.textContent='\u2715';
    const tl=document.createElement('span'); tl.textContent = title || 'Error';
    head.appendChild(mk); head.appendChild(tl); e.appendChild(head);
    if (reason && String(reason).trim()) {
      const r=document.createElement('div'); r.className='op-err-reason'; r.textContent=String(reason).trim();
      e.appendChild(r);
    }
    markScroll();
    _task.querySelector('.op-task-steps').appendChild(e);
    scrollToBottom(); saveSession();
  }
  function taskActionStep(label, detail) {
    if (!_task) startTask();
    const steps = _task.querySelector('.op-task-steps');
    // COALESCE consecutive identical actions: if the last step is an act-step with
    // the SAME label+detail, bump an animated ×N badge in place instead of spitting
    // out a new line (the owner — repeated clicks/screenshots shouldn't flood the trace).
    const _last = steps && steps.lastElementChild;
    const _sig = (label||'') + '' + (detail||'');
    const _noCoalesce = /^(Browsing|Navigating|Going back|Going forward)$/.test(label||'');   // navigations are milestones — never merge
    if (!_noCoalesce && _last && _last.classList.contains('op-act-step') && _last.dataset.sig === _sig) {
      const n = (parseInt(_last.dataset.n || '1', 10) || 1) + 1;
      _last.dataset.n = n;
      let badge = _last.querySelector('.op-act-x');
      if (!badge) { badge=document.createElement('span'); badge.className='op-act-x';
        _last.querySelector('.op-act-lab').appendChild(badge); }
      badge.textContent = '' + n;           // just the number: 2, 3, 4…
      badge.classList.remove('op-x-pop'); void badge.offsetWidth; badge.classList.add('op-x-pop');
      _stepCount++;
      const cnt = _task.querySelector('.op-step-count');
      if (cnt) { cnt.hidden = false; const sn = cnt.querySelector('.sc-n');
        if (sn) sn.textContent = _stepCount + ' steps'; }
      scrollToBottom(); saveSession();
      return;
    }
    _stepCount++;
    const cnt = _task.querySelector('.op-step-count');
    if (cnt) { cnt.hidden = false; const n = cnt.querySelector('.sc-n');
      if (n) n.textContent = _stepCount + (_stepCount===1 ? ' step' : ' steps'); }
    const e=document.createElement('div'); e.className='op-task-step op-act-step';
    e.dataset.sig = _sig; e.dataset.n = '1';
    const ico=document.createElement('span'); ico.className='op-act-ico2'; ico.textContent=actEmoji(label);
    const lab=document.createElement('span'); lab.className='op-act-lab';
    // labels are plain text, EXCEPT the code-block fallback ("Using `tool`") which
    // carries backticks → render markdown so it shows as a code chip. _mdToHtml escapes first.
    if ((label||'').indexOf('`') !== -1) lab.innerHTML=_mdToHtml(label); else lab.textContent=label||'';
    e.appendChild(ico); e.appendChild(lab);
    // a SEARCH query → show inline as Searching ("the terms") so the trace reveals
    // WHAT was searched (parity with Claude's WebSearch(query)). Covers web/code/file
    // search verbs; the query rides right after the label in muted quotes.
    const _isSearch = /search|searching|grep|finding|looking up/.test((label||'').toLowerCase());
    if (detail && _isSearch) {
      const c=document.createElement('span'); c.className='op-act-coord';
      c.textContent = '("' + detail.trim() + '")';
      lab.appendChild(c);
      markScroll(); steps.appendChild(e); scrollToBottom(); saveSession();
      return;
    }
    // coordinate-click detail e.g. "(420, 315)" or a drag "(120, 80) → (300, 240)":
    // show it INLINE after the label in lighter, smaller, muted text (the owner's preferred).
    const _isCoord = detail && /^\(\s*-?\d/.test(detail.trim());
    // a short duration like '2s' / '1m 3s' also goes INLINE (the owner: Waiting matches Clicking)
    const _isDur = detail && /^\d+(\.\d+)?\s*(ms|s|m|h)(\s+\d+\s*(s|m))?$/.test(detail.trim());
    // a short element label (e.g. "Button", "Submit") also goes inline — not a URL/path/command, not long.
    const _dt = (detail||'').trim();
    const _isShortLabel = detail && _dt.length <= 32 && !/^https?:\/\//.test(_dt)
        && !/^(\/|~\/|\.\/)/.test(_dt) && !/\s-{1,2}\w/.test(detail)
        && !/command|^ran|run js|running js|reading file|writing file|editing file/.test((label||'').toLowerCase());
    if (detail && (_isCoord || _isDur || _isShortLabel)) {
      const c=document.createElement('span'); c.className='op-act-coord'; c.textContent=detail;
      lab.appendChild(c);
    } else if (detail) {
      const lab2 = (label||'').toLowerCase();
      const isCmd = /command|^ran|run js|running js|reading file|writing file|editing file/.test(lab2);
      const t = detail.trim();
      const isHttp = /^https?:\/\//.test(t);
      const isUrlOrPath = isHttp || /^(\/|~\/|\.\/)/.test(t) || /\s-{1,2}\w/.test(detail);
      let d;
      if (isHttp) {                          // a real URL → clickable link (border animates on hover)
        d=document.createElement('a'); d.className='op-act-detail';
        d.href = t.split(/\s/)[0]; d.target='_blank'; d.rel='noopener';
        d.textContent=detail;
      } else {
        d=document.createElement('div');
        d.className = (isCmd || isUrlOrPath) ? 'op-act-detail' : 'op-act-detail-plain';
        d.textContent=detail;
      }
      e.appendChild(d);
    }
    markScroll();
    steps.appendChild(e);
    scrollToBottom(); saveSession();
  }
  const DONE_VERBS = ['Worked','Cogitated','Pondered','Investigated','Noodled',
                      'Ruminated','Churned','Mulled','Tinkered','Deliberated',
                      'Percolated','Brewed','Simmered','Marinated','Digested',
                      'Chewed','Puzzled','Wrangled','Untangled','Spelunked',
                      'Scoured','Sifted','Crunched','Hustled','Toiled','Labored',
                      'Schemed','Plotted','Computed','Reckoned','Wondered'];
  function _fmtDur(secs){
    if (secs <= 0) return 'a second';                       // sub-second turn — "0s" looks broken
    return secs >= 60 ? (Math.floor(secs/60)+'m '+(secs%60)+'s') : (secs+'s');
  }
  function finishTask(failed, labelOverride) {
    if (!_task) return;
    _task.dataset.busy='0';
    const secs=Math.round((Date.now()-_taskStart)/1000);
    let label;
    if (failed) {
      _task.classList.add('op-task-failed');
      const dur = _fmtDur(secs);
      if (labelOverride === 'Interrupted')      // user-stopped → show elapsed, like Steered
        label = 'Interrupted after ' + dur;
      else
        label = labelOverride || (secs < 2 ? 'Error' : 'Interrupted after ' + dur);
    } else if (labelOverride) {
      if (labelOverride === 'Steered' || labelOverride === 'Stopped') {
        _task.classList.add(labelOverride === 'Steered' ? 'op-task-steered' : 'op-task-stopped');
        const dur = _fmtDur(secs);
        label = labelOverride + ' after ' + dur;   // "Steered after Xs" / "Stopped after Xs"
      } else { label = labelOverride; }
    } else {
      const verb = DONE_VERBS[secs % DONE_VERBS.length];
      const dur = _fmtDur(secs);
      label = verb + ' for ' + dur;
    }
    // route through taskVerb so the finish animates the same rise-swap and the
    // live ellipsis drops (done labels like "Worked for 5s" carry no dots).
    taskVerb(label, false);
    const nsteps = _task.querySelectorAll('.op-task-step').length;
    if (nsteps === 0) {
      _task.classList.add('op-no-steps');         // hide the caret — nothing to expand
      const car = _task.querySelector('.car'); if (car) car.style.display = 'none';
    }
    _task.classList.add('collapsed');
    _task = null; saveSession();
    setFollowUp();   // agent done → revert placeholder to "Message Operator"
  }

  function logRes(text, ok) {
    const m = document.createElement('div'); m.className='op-msg step ' + (ok?'':'err');
    const b = document.createElement('span'); b.className='body'; b.textContent=text;
    m.appendChild(b); log.appendChild(m); trim();
  }
  function _trimOnly(){ while(log.children.length>60) log.removeChild(log.firstChild); saveSession(); }
  function trim(){ _trimOnly(); stickToBottom(); }

  // status panel toggles the granular event history (chat stays visible)
  // ── font-size steppers (A− / A+) — chat content only, Jakarta, persisted ──
  const SIZE_KEY = 'operator-chatscale-v1';
  const SIZE_MIN = 0.82, SIZE_MAX = 1.28, SIZE_STEP = 0.04;
  let _scale = 1.05;   // default bumped 0.96->1.05 to unify UI scale with the header bar
  try { const v = parseFloat(localStorage.getItem(SIZE_KEY)); if (v) _scale = v; } catch {}
  function applyScale(){ _scale = Math.min(SIZE_MAX, Math.max(SIZE_MIN, _scale));
    op.style.setProperty('--chat-scale', _scale.toFixed(2));
    try { localStorage.setItem(SIZE_KEY, _scale.toFixed(2)); } catch {}
    // re-fit the model picker to the new font scale so its dynamic-caret width
    // grows with the text (else the name clips at higher zoom). rAF: let the
    // --chat-scale change reflow first so the measurement is at the new size.
    try { requestAnimationFrame(() => { if (window._opFitModel) window._opFitModel(); }); } catch {} }
  applyScale();
  // Zoom without yanking the chat: if the user is pinned to the bottom, keep them
  // pinned; otherwise hold their distance-from-bottom constant across the reflow.
  function scaleKeepingView(delta){
    const atBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 24;
    const fromBottom = log.scrollHeight - log.scrollTop;
    _scale += delta; applyScale();
    // reflow has happened synchronously after the style change
    if (atBottom) log.scrollTop = log.scrollHeight;
    else log.scrollTop = log.scrollHeight - fromBottom;
  }
  const fontDec = document.getElementById('op-font-dec');
  const fontInc = document.getElementById('op-font-inc');
  if (fontDec) fontDec.addEventListener('click', ()=> scaleKeepingView(-SIZE_STEP));
  if (fontInc) fontInc.addEventListener('click', ()=> scaleKeepingView(+SIZE_STEP));



  const _clearBtn = document.getElementById('op-clear');
  if (_clearBtn) _clearBtn.addEventListener('click', () => {
    if (_clearBtn.dataset.busy === '1') return;          // ignore double-tap mid-animation
    const finishClear = () => {
      log.innerHTML=''; log.classList.remove('op-clearing');
      try{localStorage.removeItem(LS_KEY);}catch{}
      if (typeof _seenMsg!=='undefined') _seenMsg.clear();
      setFollowUp();
      _clearBtn.dataset.busy='0';
      // back to a fresh idle stage → bring the launchpad back as the SOLID
      // splash (2026-07-18, superseding the 07-17 over-the-feed blur;
      // the .op-lp-over CSS stays for now in case the presentation returns).
      try { initLaunchpad(); } catch(e){ console.error('operator: launchpad init failed', e); }
      try { const _lp = document.getElementById('op-lp');
        if (_lp) { _lp.classList.remove('op-lp-over'); _lp.hidden = false; } } catch(_){}
      // push the CLEARED state to the shared session (1.0.11) — without this
      // the server kept the old chat and the next boot-adopt resurrected it
      // ("trash not working", 2026-07-11).
      try { saveSession(); } catch(_){}
    };
    // wipe agent memory immediately (network); animate the UI out, then empty.
    try { fetch(OP_URLS.agent_reset, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({bot: selectedBot()})}); } catch {}
    _clearBtn.classList.add('op-clearing');              // shake the icon
    setTimeout(()=>_clearBtn.classList.remove('op-clearing'), 440);
    if (!log.children.length) { finishClear(); return; } // nothing to animate
    _clearBtn.dataset.busy='1';
    log.classList.add('op-clearing');                    // fade+collapse messages
    setTimeout(finishClear, 340);
  });
  const eventsEl = document.getElementById('op-events');
  const actionBtn = document.getElementById('op-action');
  actionBtn.addEventListener('click', () => {
    const exp = eventsEl.classList.toggle('expanded');
    actionBtn.setAttribute('aria-expanded', exp ? 'true' : 'false');
    eventsEl.scrollTop = eventsEl.scrollHeight;
  });
  function logEvent(text, ok) {
    const e = document.createElement('div'); e.className = 'op-ev' + (ok===false?' err':'');
    const t = document.createElement('span'); t.className='t'; t.textContent='';
    const b = document.createElement('span'); b.textContent = text;
    e.appendChild(t); e.appendChild(b); eventsEl.appendChild(e);
    while (eventsEl.children.length > 80) eventsEl.removeChild(eventsEl.firstChild);
    eventsEl.scrollTop = eventsEl.scrollHeight;
  }

  // ── MAN/AUTO mode + bot picker (in the caret slot) + agent dispatch ──
  const DRIVERS_URL = OP_URLS.drivers;
  const DISPATCH = OP_URLS.dispatch;
  const AGENT_URL = OP_URLS.agent_state;

  // ── surface switcher (Track C): hover/tap the Operator brand ────────────
  // browser / desktop-sandbox / desktop-real. The pick swaps the live feed
  // immediately and routes the next dispatch; desktop-real demands an inline
  // confirm every session and keeps a hard STOP over the feed while running.
  const SURFACES_URL = OP_URLS.surfaces;
  const SURFACE_SET_URL = OP_URLS.surface_set;
  const MAPS_URL = OP_URLS.maps;
  const SANDBOX_CTL_URL = OP_URLS.sandbox_ctl;
  const PANIC_STOP_URL = OP_URLS.agent_stop;
  const brandEl = document.getElementById('op-brand');
  const surfPop = document.getElementById('op-surface-pop');
  const surfChip = document.getElementById('op-surface-chip');
  const panicBtn = document.getElementById('op-panic');
  let _surfaces = [];                    // [{key,label,hint,available,gated}]
  let _surfaceActive = 'browser';
  let _realOk = false;                   // per-SESSION consent, never persisted
  let _maps = [];                        // shipped game maps (vision/maps/)
  let _activeMap = '';                   // '' = none; folded into dispatch text only
  const _isGameSurface = () => _surfaceActive !== 'browser';   // desktop = game-capable
  // The sandbox is already unmistakable from the desktop taskbar/feed. Keep
  // its brow chip empty so the Operator controls survive narrow/high-zoom rails.
  const _SURF_CHIP = { 'desktop-real': 'computer' };
  const _SURF_ICON = {
    browser: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"></circle><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"></path></svg>',
    'desktop-sandbox': '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"></path><path d="M4 7.5l8 4.5 8-4.5M12 12v9"></path></svg>',
    'desktop-real': '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><rect x="3" y="4" width="18" height="12" rx="1.6"></rect><path d="M9 20h6M12 16.5V20"></path></svg>'
  };
  function applySurfaceState(){
    op.dataset.surface = _surfaceActive;
    const chipTxt = _SURF_CHIP[_surfaceActive] || '';
    surfChip.hidden = !chipTxt;
    surfChip.textContent = chipTxt;
    surfChip.classList.toggle('real', _surfaceActive === 'desktop-real');
    // taskbar title follows the surface
    const tbn = document.getElementById('op-tb-name');
    if (tbn) tbn.textContent = _surfaceActive === 'desktop-real' ? 'Computer' : 'Sandbox';
    // manual steer works on every surface now — /operator/steer routes to the
    // desktop backends when a desktop surface is active (no forced AUTO).
    // saved-task launchpad is browser-only — hide on desktop, re-show on return
    // to an idle browser surface.
    const lp = document.getElementById('op-lp');
    if (lp) {
      if (_isGameSurface()) lp.hidden = true;
      else if (!log.children.length) { try { initLaunchpad(); } catch(e){ console.error('operator: launchpad init failed', e); } }
    }
    refreshPanic();
  }
  function refreshPanic(){
    // desktop-REAL only: the always-visible hard stop is for runs that drive
    // the actual machine. Sandbox is isolated — the composer's stop suffices.
    panicBtn.hidden = !(_surfaceActive === 'desktop-real' && op.dataset.busy === '1');
  }
  // busy is flipped centrally via op.dataset — observe it instead of patching
  // every setInFlight call site.
  new MutationObserver(refreshPanic).observe(op, { attributes: true,
    attributeFilter: ['data-busy', 'data-surface'] });
  panicBtn.addEventListener('click', async () => {
    panicBtn.disabled = true;
    try { await fetch(PANIC_STOP_URL, { method: 'POST' }); } catch(_){}
    panicBtn.disabled = false;
  });
  function renderSurfacePop(){
    surfPop.innerHTML = '';
    _surfaces.forEach(s => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'op-surf-item' + (s.key === _surfaceActive ? ' active' : '');
      row.disabled = s.available === false;
      row.innerHTML = '<span class="op-surf-ico">' + (_SURF_ICON[s.key]||'') + '</span>'
        + '<span class="op-surf-body"><span class="op-surf-name">' + s.label
        + (s.gated ? '<svg class="op-surf-warn" viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3.2 22 20H2L12 3.2z"></path><path d="M12 10v4"></path><circle cx="12" cy="17" r="0.6" fill="currentColor"></circle></svg>' : '')
        + '</span><span class="op-surf-hint">' + (s.available === false ? (s.unavailable_hint || 'not available on this host') : s.hint) + '</span></span>'
        + (s.key === _surfaceActive ? '<svg class="op-surf-check" viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.2 3L13 4.5"></path></svg>' : '');
      row.addEventListener('click', (e) => { e.stopPropagation(); pickSurface(s, row); });
      surfPop.appendChild(row);
    });
  }
  function pickSurface(s, row){
    if (s.key === _surfaceActive) { closeSurfPop(); return; }
    if (s.key === 'desktop-real' && !row.classList.contains('confirming')) {
      // two-step consent, inline in the row — every session, no shortcuts
      row.classList.add('confirming');
      row.innerHTML = '<span class="op-surf-ico">' + _SURF_ICON[s.key] + '</span>'
        + '<span class="op-surf-body"><span class="op-surf-name">Enter computer mode?</span>'
        + '<span class="op-surf-hint">Operator will have unrestricted control of your desktop.</span></span>'
        + '<span class="op-surf-confirm"><button type="button" class="op-surf-yes" aria-label="confirm" title="Confirm">'
        + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8.5l3.2 3L13 4.5"></path></svg>'
        + '</button><button type="button" class="op-surf-no" aria-label="cancel" title="Cancel">'
        + '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"></path></svg>'
        + '</button></span>';
      row.querySelector('.op-surf-yes').addEventListener('click', (e) => {
        e.stopPropagation(); setSurface('desktop-real', true); });
      row.querySelector('.op-surf-no').addEventListener('click', (e) => {
        e.stopPropagation(); renderSurfacePop(); });
      return;
    }
    setSurface(s.key, false);
  }
  async function setSurface(key, confirmed){
    try {
      const d = await (await fetch(SURFACE_SET_URL, { method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ surface: key, confirm: confirmed }) })).json();
      if (d.ok) {
        _surfaceActive = d.active;
        if (key === 'desktop-real' && confirmed) _realOk = true;
        applySurfaceState(); renderSurfacePop(); closeSurfPop();
      }
    } catch(_){}
  }
  // open/close: hover-intent on fine pointers, tap everywhere, Esc/outside close
  let _surfTimer = null;
  function openSurfPop(){
    if (!_surfaces.length) return;   // demo included — it gets browser + sandbox
    const r = brandEl.getBoundingClientRect();
    surfPop.style.left = Math.max(8, r.left - 2) + 'px';
    surfPop.style.top = (r.bottom + 6) + 'px';
    renderSurfacePop();
    surfPop.hidden = false;
    brandEl.setAttribute('aria-expanded', 'true');
  }
  function closeSurfPop(){
    surfPop.hidden = true;
    brandEl.setAttribute('aria-expanded', 'false');
  }
  brandEl.addEventListener('click', (e) => {
    e.stopPropagation();
    if (surfPop.hidden) openSurfPop(); else closeSurfPop();
  });
  if (window.matchMedia && matchMedia('(pointer:fine)').matches) {
    brandEl.addEventListener('mouseenter', () => {
      clearTimeout(_surfTimer);
      _surfTimer = setTimeout(openSurfPop, 140);
    });
    brandEl.addEventListener('mouseleave', () => {
      clearTimeout(_surfTimer);
      _surfTimer = setTimeout(() => { if (!surfPop.matches(':hover')) closeSurfPop(); }, 320);
    });
    surfPop.addEventListener('mouseleave', () => {
      clearTimeout(_surfTimer);
      _surfTimer = setTimeout(closeSurfPop, 320);
    });
    surfPop.addEventListener('mouseenter', () => clearTimeout(_surfTimer));
  }
  document.addEventListener('click', (e) => {
    if (!surfPop.hidden && !surfPop.contains(e.target) && !brandEl.contains(e.target)) closeSurfPop();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !surfPop.hidden) closeSurfPop();
  });
  (async () => {
    try {
      const d = await (await fetch(SURFACES_URL)).json();
      _surfaces = d.surfaces || [];
      _surfaceActive = d.active || 'browser';
      if (_surfaces.length < 2) brandEl.removeAttribute('role');  // nothing to switch
      applySurfaceState();
      try { _maps = (await (await fetch(MAPS_URL)).json()).maps || []; } catch(_){}
      renderTbGame();
    } catch(_){}
  })();

  // ── desktop taskbar: the game picker's home + sandbox controls ────────────
  const tbGame = document.getElementById('op-tb-game');
  const tbGameBtn = document.getElementById('op-tb-game-btn');
  const tbGameLbl = document.getElementById('op-tb-game-lbl');
  const tbGameMenu = document.getElementById('op-tb-game-menu');
  function renderTbGame(){
    if (!tbGame) return;
    tbGame.hidden = !_maps.length;
    tbGameLbl.textContent = _activeMap || 'Game';
    tbGameMenu.innerHTML = '';
    ['', ..._maps].forEach(m => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'op-tb-mi' + (m === _activeMap ? ' active' : '');
      b.textContent = m || 'None';
      b.addEventListener('click', (e) => {
        e.stopPropagation();
        _activeMap = m;
        tbGameMenu.hidden = true;
        renderTbGame();
      });
      tbGameMenu.appendChild(b);
    });
  }
  if (tbGameBtn) {
    tbGameBtn.addEventListener('click', (e) => {
      e.stopPropagation(); tbGameMenu.hidden = !tbGameMenu.hidden;
    });
    document.addEventListener('click', (e) => {
      if (!tbGameMenu.hidden && !tbGame.contains(e.target)) tbGameMenu.hidden = true;
    });
  }
  async function _tbPost(body){
    const r = await fetch(SANDBOX_CTL_URL, { method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
    return r.json();
  }
  document.querySelectorAll('#op-taskbar [data-app]').forEach(b => {
    b.addEventListener('click', async () => {
      if (b.disabled) return;
      b.disabled = true;
      try { await _tbPost({ action: 'launch', app: b.dataset.app }); } catch(_){}
      setTimeout(() => { b.disabled = false; }, 900);   // app needs a beat to map
    });
  });
  // taskbar auto-minimize (2026-07-11): after a few idle seconds the
  // button labels drop away (icons stay tappable); pointer over the bar
  // brings them back, leaving re-arms the timer.
  (function(){
    const tb = document.getElementById('op-taskbar');
    if (!tb) return;
    let t = null;
    const arm = (ms) => { clearTimeout(t);
      t = setTimeout(() => tb.classList.add('op-tb-min'), ms); };
    tb.addEventListener('pointerenter', () => { clearTimeout(t); tb.classList.remove('op-tb-min'); });
    tb.addEventListener('pointerleave', () => arm(2500));
    arm(5000);
  })();
  const tbRestart = document.getElementById('op-tb-restart');
  if (tbRestart) tbRestart.addEventListener('click', async () => {
    if (tbRestart.disabled) return;
    tbRestart.disabled = true; tbRestart.classList.add('busy');
    try { await _tbPost({ action: 'restart' }); } catch(_){}
    tbRestart.classList.remove('busy'); tbRestart.disabled = false;
  });
  const tbDelete = document.getElementById('op-tb-delete');
  const tbDelLbl = document.getElementById('op-tb-del-lbl');
  let _tbDelArm = null;
  if (tbDelete) tbDelete.addEventListener('click', async () => {
    if (tbDelete.disabled) return;
    if (!tbDelete.classList.contains('confirm')) {      // two-tap: arm, then fire
      tbDelete.classList.add('confirm');
      tbDelLbl.textContent = 'Confirm delete';
      clearTimeout(_tbDelArm);
      _tbDelArm = setTimeout(() => {
        tbDelete.classList.remove('confirm'); tbDelLbl.textContent = 'Delete';
      }, 2600);
      return;
    }
    clearTimeout(_tbDelArm);
    tbDelete.disabled = true; tbDelLbl.textContent = 'Deleting…';
    try { await _tbPost({ action: 'delete' }); } catch(_){}
    // the feed's next capture boots a factory-fresh desktop automatically
    tbDelete.classList.remove('confirm'); tbDelLbl.textContent = 'Delete';
    tbDelete.disabled = false;
  });

  // ── Transfer: files in / out of the sandbox home ──────────────────────────
  const XFER_FILES_URL = OP_URLS.sandbox_files;
  const XFER_UP_URL = OP_URLS.sandbox_upload;
  const XFER_FILE_BASE = OP_URLS.sandbox_file.replace('_R_', '');
  const xfer = document.getElementById('op-tb-xfer');
  const xferBtn = document.getElementById('op-tb-xfer-btn');
  const xferMenu = document.getElementById('op-tb-xfer-menu');
  const xferInput = document.getElementById('op-xfer-input');
  const fmtSize = (n) => n > 1048576 ? (n / 1048576).toFixed(1) + ' MB'
                       : n > 1024 ? Math.round(n / 1024) + ' KB' : n + ' B';
  async function renderXfer(){
    xferMenu.textContent = '';
    const up = document.createElement('button'); up.type = 'button';
    up.className = 'op-tb-mi op-xfer-up';
    up.textContent = '⇧ Send a file to the sandbox…';
    up.addEventListener('click', (e) => { e.stopPropagation(); xferInput.click(); });
    xferMenu.appendChild(up);
    let d = null;
    try { d = await (await fetch(XFER_FILES_URL)).json(); } catch(_){}
    let any = false;
    Object.entries((d && d.dirs) || {}).forEach(([dir, files]) => {
      if (!files.length) return;
      const h = document.createElement('div'); h.className = 'op-xfer-dir';
      h.textContent = dir; xferMenu.appendChild(h);
      files.sort((a, b) => b.mtime - a.mtime).forEach(f => {
        const a = document.createElement('a');
        a.className = 'op-tb-mi op-xfer-file';
        a.href = XFER_FILE_BASE + dir + '/' + encodeURIComponent(f.name);
        a.setAttribute('download', f.name);
        const nm = document.createElement('span'); nm.className = 'op-xfer-name'; nm.textContent = f.name;
        const sz = document.createElement('span'); sz.className = 'op-xfer-size'; sz.textContent = fmtSize(f.size);
        a.appendChild(nm); a.appendChild(sz);
        xferMenu.appendChild(a); any = true;
      });
    });
    if (!any) {
      const e = document.createElement('div'); e.className = 'op-xfer-empty';
      e.textContent = d && d.ok === false ? (d.error || 'unavailable')
        : 'Nothing yet — Downloads, Desktop and Documents show up here.';
      xferMenu.appendChild(e);
    }
  }
  if (xferBtn) {
    xferBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      xferMenu.hidden = !xferMenu.hidden;
      if (!xferMenu.hidden) renderXfer();
    });
    document.addEventListener('click', (e) => {
      if (!xferMenu.hidden && !xfer.contains(e.target)) xferMenu.hidden = true;
    });
    xferInput.addEventListener('change', async () => {
      const f = xferInput.files && xferInput.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append('file', f);
      xferBtn.disabled = true;
      try { await fetch(XFER_UP_URL, { method: 'POST', body: fd }); } catch(_){}
      xferBtn.disabled = false; xferInput.value = '';
      if (!xferMenu.hidden) renderXfer();
    });
  }

  // Code-block scroll trap fix (2026-07-21, round 2): scrolling STICKS
  // whenever the cursor/finger lands on a code block — the earlier delegate
  // (forward only when the <pre> lacks its own vertical scroll) missed cases,
  // and on iPad a touch that starts on the pre's selectable text initiates
  // selection instead of scroll. Robust fix: attach directly to each <pre>
  // (capture phase, so we run before the pre consumes anything) and unless the
  // pre TRULY has its own scrollable overflow, forward every vertical wheel/
  // touch delta straight to the chat log. Runs on existing + future bubbles via
  // a light MutationObserver.
  function _preScrollsV(pre){ return pre.scrollHeight > pre.clientHeight + 2; }
  function _wirePre(pre){
    if (pre._opScrollWired) return; pre._opScrollWired = true;
    pre.addEventListener('wheel', (e) => {
      if (_preScrollsV(pre) || !e.deltaY) return;   // pre owns a real scroll → leave it
      log.scrollTop += e.deltaY; e.preventDefault();
    }, { passive: false, capture: true });
    // touch: pan the log by the finger delta when the gesture starts on a
    // non-scrolling pre (otherwise iOS grabs it for text selection and sticks)
    let _ty = 0;
    pre.addEventListener('touchstart', (e) => { _ty = e.touches[0]?.clientY || 0; },
                         { passive: true });
    pre.addEventListener('touchmove', (e) => {
      if (_preScrollsV(pre)) return;
      const y = e.touches[0]?.clientY || 0; const dy = _ty - y; _ty = y;
      if (dy) { log.scrollTop += dy; e.preventDefault(); }
    }, { passive: false });
  }
  function _wireAllPres(){ log.querySelectorAll('pre').forEach(_wirePre); }
  _wireAllPres();
  new MutationObserver(_wireAllPres).observe(log, { childList: true, subtree: true });
  // screenshot lightbox: tap an inline agent screenshot to view it full-size;
  // tap anywhere (or Esc) to dismiss.
  log.addEventListener('click', (e) => {
    const im = e.target.closest && e.target.closest('.op-md-img');
    if (!im) return;
    const ov = document.createElement('div'); ov.className = 'op-lightbox';
    const big = document.createElement('img'); big.src = im.src; big.alt = im.alt || '';
    ov.appendChild(big);
    const close = () => { ov.remove(); document.removeEventListener('keydown', onKey); };
    const onKey = (ev) => { if (ev.key === 'Escape') close(); };
    ov.addEventListener('click', close);
    document.addEventListener('keydown', onKey);
    document.body.appendChild(ov);
  });
  const caretSel = document.getElementById('op-action-caret');
  const pickWrap = document.getElementById('op-pick-wrap');
  const modeBox = document.getElementById('op-mode');
  // seed MODE from the saved session immediately so the first applyMode() doesn't
  // run as 'man' and flash the Manual-mode card on refresh (the later _sess restore
  // would only fix it a beat later). Read localStorage directly — _sess is declared
  // later (TDZ). Default 'man' only if nothing saved.
  let MODE = (function(){ try { const d = JSON.parse(localStorage.getItem('operator-session-v1')||'null');
    const _demoDefault = document.body.classList.contains('op-demo') ? 'auto' : 'man';
    return (d && (d.mode === 'auto' || d.mode === 'man')) ? d.mode : _demoDefault; } catch { return document.body.classList.contains('op-demo') ? 'auto' : 'man'; } })();
  // true ONLY in the live moment after Operator hands control to the user (Take control).
  // SESSION-ONLY by design — never seeded from storage, always false on load. A refresh
  // or a manual MAN flip must NOT resurrect Finish-up (it's not a real pending hand-off).
  let _handedToUser = false;
  // The welcome surface must be interactive even if driver/model discovery is
  // slow or unavailable. Defer only until this script's constants initialize;
  // never put its event wiring behind a backend request — and never swallow an
  // init failure silently (an inert painted splash is a total lockout in AUTO).
  setTimeout(() => { try { initLaunchpad(); } catch(e){ console.error('operator: launchpad init failed', e); } }, 0);
  (async () => {
    try { const d = await (await fetch(DRIVERS_URL)).json();
      (d.drivers||[]).forEach(b => { const o=document.createElement('option');
        o.value=b.key; o.textContent=botEmoji(b.key)+' '+b.label; o.title=b.label; caretSel.appendChild(o); });
    } catch {}
    // seed the saved bot BEFORE the first model load so we don't flash the default (claude-a/sonnet) on refresh
    try { const _sv = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
      if (_sv && _sv.bot && [].some.call(caretSel.options, o=>o.value===_sv.bot)) caretSel.value = _sv.bot;
    } catch {}
    await loadModels(selectedBot());
    initSlashTasks();
    initNewTaskModal();
  })();
  // ── Launchpad (#1): saved-task cards on the idle stage, OpenAI-Operator
  // homepage style. Fresh sessions only — the overlay vanishes for good the
  // moment the conversation starts (any log entry), and never renders in MAN
  // mode or the demo (CSS-gated too).
  // Clean display names for common connector sites (YouTube, not youtube.com).
  // Anything not listed falls back to the bare domain — fine for random links.
  const _SITE_LABELS = {
    'ubereats.com':'Uber Eats', 'doordash.com':'DoorDash', 'instacart.com':'Instacart',
    'opentable.com':'OpenTable', 'resy.com':'Resy', 'amazon.com':'Amazon',
    'reuters.com':'Reuters', 'bloomberg.com':'Bloomberg', 'youtube.com':'YouTube',
    'google.com':'Google', 'expedia.com':'Expedia', 'booking.com':'Booking.com',
    'airbnb.com':'Airbnb', 'yelp.com':'Yelp', 'linkedin.com':'LinkedIn',
    'github.com':'GitHub', 'zillow.com':'Zillow', 'spotify.com':'Spotify',
    'lichess.org':'Lichess', 'alltrails.com':'AllTrails',
    'flights.google.com':'Google Flights', 'target.com':'Target', 'costco.com':'Costco',
    'wikipedia.org':'Wikipedia', 'imdb.com':'IMDb', 'goodreads.com':'Goodreads',
    'redfin.com':'Redfin', 'kayak.com':'Kayak', 'seatgeek.com':'SeatGeek',
    'craigslist.org':'Craigslist', 'etsy.com':'Etsy', 'weather.gov':'Weather.gov',
    'espn.com':'ESPN', 'notion.so':'Notion', 'trello.com':'Trello',
    'nytimes.com':'NYTimes', 'arxiv.org':'arXiv', 'stackoverflow.com':'Stack Overflow',
    'reddit.com':'Reddit', 'wolframalpha.com':'WolframAlpha', 'archive.org':'Archive.org',
    'producthunt.com':'Product Hunt', 'coursera.org':'Coursera', 'ikea.com':'IKEA',
    'wikihow.com':'wikiHow', 'yellowpages.com':'Yellow Pages', 'nih.gov':'NIH',
    'sfmoma.org':'SFMOMA', 'tripadvisor.com':'Tripadvisor', 'homedepot.com':'Home Depot',
    'grubhub.com':'Grubhub', 'chewy.com':'Chewy', 'petco.com':'Petco',
    'bestbuy.com':'Best Buy', 'ticketmaster.com':'Ticketmaster',
    'investopedia.com':'Investopedia', 'khanacademy.org':'Khan Academy',
    'news.ycombinator.com':'Hacker News', 'duolingo.com':'Duolingo',
    'rottentomatoes.com':'Rotten Tomatoes', 'bandcamp.com':'Bandcamp',
    'vivino.com':'Vivino', 'strava.com':'Strava', 'fandango.com':'Fandango',
    'offerup.com':'OfferUp', 'bookshop.org':'Bookshop.org',
    'amazon.ca':'Amazon', 'ebay.com':'eBay', 'walmart.ca':'Walmart',
    'bestbuy.ca':'Best Buy', 'ibkr.com':'Interactive Brokers', 'gmail.com':'Gmail',
    'docs.google.com':'Google Docs', 'expedia.ca':'Expedia', 'x.com':'X',
    'netflix.com':'Netflix', 'weather.com':'Weather.com',
    'dominos.com':'Domino’s', 'toasttab.com':'Toast', 'gopuff.com':'Gopuff',
    'freshdirect.com':'FreshDirect', 'seamless.com':'Seamless', 'goldbelly.com':'Goldbelly',
    'taskrabbit.com':'Taskrabbit', 'thumbtack.com':'Thumbtack', 'zocdoc.com':'Zocdoc',
    'classpass.com':'ClassPass', 'eventbrite.com':'Eventbrite', 'recreation.gov':'Recreation.gov',
    'mindbodyonline.com':'Mindbody', 'rover.com':'Rover', 'rei.com':'REI',
    'wayfair.com':'Wayfair', 'newegg.com':'Newegg', 'bhphotovideo.com':'B&H Photo',
    'sephora.com':'Sephora', 'nordstrom.com':'Nordstrom', 'backmarket.com':'Back Market',
    'lowes.com':'Lowe’s', 'vrbo.com':'Vrbo', 'hotels.com':'Hotels.com',
    'rome2rio.com':'Rome2Rio', 'thetrainline.com':'Trainline', 'hostelworld.com':'Hostelworld',
    'skyscanner.com':'Skyscanner', 'lonelyplanet.com':'Lonely Planet', 'wanderlog.com':'Wanderlog',
    'pubmed.ncbi.nlm.nih.gov':'PubMed', 'docs.python.org':'Python Docs',
    'consumerreports.org':'Consumer Reports', 'nasa.gov':'NASA', 'loc.gov':'Library of Congress',
    'justwatch.com':'JustWatch', 'glassdoor.com':'Glassdoor',
  };

  // Deterministic rotating pick of N examples from the pool. The "which N"
  // changes every 6h on its own (a time-bucket seeds the shuffle) and the ↻
  // button advances the bucket by one — no storage, same for everyone, stable
  // within a window. A tiny seeded PRNG (mulberry32) + Fisher–Yates gives a
  // clean shuffle from an integer seed.
  const _LP_ROTATE_HOURS = 6, _LP_SHOW = 6;
  function _mulberry32(a){ return function(){ a|=0; a=a+0x6D2B79F5|0; let t=Math.imul(a^a>>>15,1|a); t=t+Math.imul(t^t>>>7,61|t)^t; return ((t^t>>>14)>>>0)/4294967296; }; }
  function _pickExamples(offset){
    const bucket = Math.floor(Date.now()/(_LP_ROTATE_HOURS*3600*1000)) + (offset|0);
    const rng = _mulberry32(bucket >>> 0);
    const a = _LP_EXAMPLE_POOL.slice();
    for (let i=a.length-1; i>0; i--){ const j=Math.floor(rng()*(i+1)); [a[i],a[j]]=[a[j],a[i]]; }
    return a.slice(0, _LP_SHOW);
  }
  const _siteLabel = (dom) => _SITE_LABELS[dom.replace(/^www\./,'')] || dom;

  // Example-task pool — 86 varied tasks, each on a unique site. A rotating six
  // are surfaced at a time; category views draw from the full pool.
  // Specific, descriptive titles; clicking one loads it into the composer to
  // tweak (and connect the site) rather than running blind. isExample → "Go".
  const _LP_EXAMPLE_POOL = [
    { name: 'Book a table for two at Mott 32', prompt: 'On OpenTable, find a table for two at Mott 32 this Friday around 7pm and show me what times are available.', sites: ['opentable.com'], isExample: true },
    { name: 'Order sushi from a top-rated spot', prompt: 'Open Uber Eats, find a top-rated sushi restaurant near me that can deliver now, and build a cart for two — stop before placing the order so I can review it.', sites: ['ubereats.com'], isExample: true },
    { name: 'Restock grocery staples', prompt: 'On Instacart, fill my cart with this week’s staples: milk, eggs, bread, coffee, bananas, and chicken. Stop at checkout for me to confirm.', sites: ['instacart.com'], isExample: true },
    { name: 'Find cheap flights to Hawaii', prompt: 'Search Google Flights for round-trip fares from my city to Hawaii next month, 7–10 nights, and list the three cheapest reasonable options with times.', sites: ['flights.google.com'], isExample: true },
    { name: 'Track my Amazon orders', prompt: 'Check my recent Amazon orders and tell me which items are still in transit and when each is expected to arrive.', sites: ['amazon.com'], isExample: true },
    { name: 'Brief me on today’s top stories', prompt: 'Open Reuters and give me a short brief of today’s top business and world stories, grouped by theme.', sites: ['reuters.com'], isExample: true },
    { name: 'Compare Barcelona hotels', prompt: 'On Booking.com, find three well-reviewed hotels in central Barcelona for a weekend next month under $200/night and summarize the trade-offs.', sites: ['booking.com'], isExample: true },
    { name: 'Plan a Saturday hike nearby', prompt: 'On AllTrails, find three well-rated day hikes near me for this Saturday, and summarize distance, elevation, and difficulty for each.', sites: ['alltrails.com'], isExample: true },
    { name: 'Find a dinner spot tonight', prompt: 'On Yelp, find three highly-rated restaurants near me open for dinner tonight in the $$ range, and summarize what each is known for.', sites: ['yelp.com'], isExample: true },
    { name: 'Build a dinner-party playlist', prompt: 'On Spotify, build me a 2-hour dinner-party playlist — warm, low-key, mostly instrumental — and show the tracklist before saving it.', sites: ['spotify.com'], isExample: true },
    { name: 'Check the weekend forecast', prompt: 'On Weather.gov, pull the detailed forecast for my area Friday through Sunday and tell me the best window for an outdoor plan.', sites: ['weather.gov'], isExample: true },
    { name: 'Shortlist condos under $600k', prompt: 'On Redfin, find three 2-bed condos near me listed under $600k, and summarize square footage, HOA, and days on market for each.', sites: ['redfin.com'], isExample: true },
    { name: 'Price a trip to Mexico City', prompt: 'On Kayak, price a 5-night trip to Mexico City next month — flights plus a central hotel — and give me two options at different budgets.', sites: ['kayak.com'], isExample: true },
    { name: 'Find tickets to an upcoming game', prompt: 'On SeatGeek, find pairs of tickets to the next home game for my team, under $150 each, and list the three best value seats.', sites: ['seatgeek.com'], isExample: true },
    { name: 'How mRNA vaccines work', prompt: 'On Wikipedia, give me a clear plain-English summary of how mRNA vaccines work, with the key milestones in their development.', sites: ['wikipedia.org'], isExample: true },
    { name: 'Pick a sci-fi movie for tonight', prompt: 'On IMDb, find three well-reviewed sci-fi films from the last five years I could stream tonight, with a one-line hook for each.', sites: ['imdb.com'], isExample: true },
    { name: 'Build a literary-fiction reading list', prompt: 'On Goodreads, put together a five-book summer reading list of literary fiction with high ratings, and tell me what each is about.', sites: ['goodreads.com'], isExample: true },
    { name: 'Compare 65-inch 4K TVs under $800', prompt: 'On Best Buy, compare three 65-inch 4K TVs under $800 and summarize the trade-offs in picture, ports, and reviews.', sites: ['bestbuy.com'], isExample: true },
    { name: 'Price a bulk grocery run', prompt: 'On Costco, build a cart for a month of household staples for two people and total it up — stop before checkout so I can review.', sites: ['costco.com'], isExample: true },
    { name: 'Find men’s running shoes under $80', prompt: 'On Target, find well-reviewed men’s running shoes in size 10 under $80, and list the three best options with prices.', sites: ['target.com'], isExample: true },
    { name: 'Handmade gift ideas for a home cook', prompt: 'On Etsy, find five unique handmade gift ideas under $50 for someone who loves cooking, and summarize each.', sites: ['etsy.com'], isExample: true },
    { name: 'Plan an IKEA trip for a home office', prompt: 'On IKEA, put together a home-office setup — desk, chair, shelving, lighting — under $600 and give me the shopping list with prices.', sites: ['ikea.com'], isExample: true },
    { name: 'Source a weekend DIY project', prompt: 'On Home Depot, price out the materials for building a raised garden bed and give me the parts list with a total.', sites: ['homedepot.com'], isExample: true },
    { name: 'Catch up on the sports scores', prompt: 'On ESPN, give me last night’s scores for the NBA and the standings for my team’s division.', sites: ['espn.com'], isExample: true },
    { name: 'Read the day’s deep-dive story', prompt: 'On the New York Times, find today’s most-read long-form feature and give me a tight summary plus why it matters.', sites: ['nytimes.com'], isExample: true },
    { name: 'Survey research on retrieval-augmented generation', prompt: 'On arXiv, find three recent papers on retrieval-augmented generation and summarize each abstract in plain English.', sites: ['arxiv.org'], isExample: true },
    { name: 'Fix a Python “list index out of range”', prompt: 'On Stack Overflow, find the top answer for a Python “list index out of range” in a loop and explain the fix simply.', sites: ['stackoverflow.com'], isExample: true },
    { name: 'Gauge the mood on r/personalfinance', prompt: 'On Reddit, skim r/personalfinance’s top posts this week and give me the three most common questions people are asking.', sites: ['reddit.com'], isExample: true },
    { name: 'Work out a $30k loan’s monthly payment', prompt: 'On WolframAlpha, compute the monthly payment on a $30,000 loan at 6% over 5 years and show the total interest.', sites: ['wolframalpha.com'], isExample: true },
    { name: 'Find a free data-analysis course', prompt: 'On Coursera, find three highly-rated free courses on data analysis for beginners and summarize what each covers.', sites: ['coursera.org'], isExample: true },
    { name: 'See what’s launching on Product Hunt today', prompt: 'On Product Hunt, tell me the top five products launching today and what problem each one solves.', sites: ['producthunt.com'], isExample: true },
    { name: 'Look up how to descale a coffee machine', prompt: 'On wikiHow, find a clear step-by-step guide to descaling a coffee machine and summarize the steps for me.', sites: ['wikihow.com'], isExample: true },
    { name: 'Plan an afternoon at SFMOMA', prompt: 'On the SFMOMA site, check the current exhibitions and hours, and suggest a two-hour visit plan for this weekend.', sites: ['sfmoma.org'], isExample: true },
    { name: 'Research the top things to do in Lisbon', prompt: 'On Tripadvisor, find the top five things to do in Lisbon and group them into a rough two-day itinerary.', sites: ['tripadvisor.com'], isExample: true },
    { name: 'Compare grain-free dog foods for a medium breed', prompt: 'On Chewy, compare three well-reviewed grain-free dog foods for a medium breed, and summarize ingredients and price per pound.', sites: ['chewy.com'], isExample: true },
    { name: 'Look up healthy sleep habits for adults', prompt: 'On the NIH site, find plain-language guidance on healthy sleep habits for adults and give me the key takeaways.', sites: ['nih.gov'], isExample: true },
    { name: 'Find concert tickets for this month', prompt: 'On Ticketmaster, find shows near me this month for the kind of music I like, and list three with dates and starting prices under $120.', sites: ['ticketmaster.com'], isExample: true },
    { name: 'Understand what an ETF expense ratio is', prompt: 'On Investopedia, explain what an ETF expense ratio is in plain English, why it matters, and what counts as high vs. low.', sites: ['investopedia.com'], isExample: true },
    { name: 'Start learning linear algebra', prompt: 'On Khan Academy, find the intro linear-algebra track and lay out the first few lessons I should do in order.', sites: ['khanacademy.org'], isExample: true },
    { name: 'See what’s trending in tech today', prompt: 'On Hacker News, give me the five top stories on the front page right now and a one-line take on why each is interesting.', sites: ['news.ycombinator.com'], isExample: true },
    { name: 'Plan a 10-minute daily Spanish habit', prompt: 'On Duolingo, look at the Spanish course structure and suggest a realistic 10-minute-a-day plan for a beginner.', sites: ['duolingo.com'], isExample: true },
    { name: 'Find a well-reviewed comedy to stream', prompt: 'On Rotten Tomatoes, find three comedies from the last few years with strong critic and audience scores, with a one-line hook for each.', sites: ['rottentomatoes.com'], isExample: true },
    { name: 'Discover new indie music', prompt: 'On Bandcamp, find three under-the-radar indie albums released recently that fit a mellow, atmospheric vibe, and tell me about each.', sites: ['bandcamp.com'], isExample: true },
    { name: 'Pick a crowd-pleasing wine under $25', prompt: 'On Vivino, find three highly-rated red wines under $25 that pair well with steak, and summarize the tasting notes.', sites: ['vivino.com'], isExample: true },
    { name: 'Find a beginner 5K training plan', prompt: 'On Strava, look up popular beginner 5K running routes near me and outline a simple couch-to-5K style weekly plan.', sites: ['strava.com'], isExample: true },
    { name: 'Check movie showtimes for tonight', prompt: 'On Fandango, find what’s playing at theaters near me tonight after 7pm, and list three options with showtimes.', sites: ['fandango.com'], isExample: true },
    { name: 'Hunt for a used desk locally', prompt: 'On OfferUp, find three well-priced used standing or writing desks near me under $150, and summarize condition and pickup location.', sites: ['offerup.com'], isExample: true },
    { name: 'Order books from an indie shop', prompt: 'On Bookshop.org, build a cart of three acclaimed recent novels that support local bookstores — stop before checkout so I can review.', sites: ['bookshop.org'], isExample: true },
    { name: 'Set up pizza pickup for game night', prompt: 'On Domino’s, build a pickup order with two large pizzas, one vegetarian and one with pepperoni, plus garlic bread. Stop before placing it so I can review.', sites: ['dominos.com'], category: 'delivery', isExample: true },
    { name: 'Order lunch from a nearby cafe', prompt: 'On Toast, find a nearby cafe offering online ordering, choose a well-reviewed sandwich and side under $20, and stop before submitting the order.', sites: ['toasttab.com'], category: 'delivery', isExample: true },
    { name: 'Build a late-night essentials basket', prompt: 'On Gopuff, add sparkling water, popcorn, ice cream, pain reliever, and phone charging cables to a basket, then show me the total before checkout.', sites: ['gopuff.com'], category: 'delivery', isExample: true },
    { name: 'Make a seasonal produce order', prompt: 'On FreshDirect, build a one-week produce order for two people using seasonal fruit, salad greens, and roasting vegetables. Keep it under $55 and stop before checkout.', sites: ['freshdirect.com'], category: 'delivery', isExample: true },
    { name: 'Compare delivery costs for pad thai', prompt: 'On Seamless, compare the delivered price and arrival estimate for pad thai from three well-rated nearby restaurants, including fees before tip.', sites: ['seamless.com'], category: 'delivery', isExample: true },
    { name: 'Send a regional food gift', prompt: 'On Goldbelly, find three regional food gifts that serve at least four people, ship within a week, and cost under $90. Summarize what makes each distinctive.', sites: ['goldbelly.com'], category: 'delivery', isExample: true },
    { name: 'Find help assembling a bookcase', prompt: 'On Taskrabbit, find three available furniture assemblers near me for a two-hour bookcase job this weekend, and compare rates, ratings, and earliest availability.', sites: ['taskrabbit.com'], category: 'local', isExample: true },
    { name: 'Get quotes for a deep clean', prompt: 'On Thumbtack, find three highly-rated home cleaners near me for a one-time deep clean, and summarize estimated price, availability, and what is included.', sites: ['thumbtack.com'], category: 'local', isExample: true },
    { name: 'Find a dentist accepting new patients', prompt: 'On Zocdoc, find three well-reviewed dentists near me who accept new patients and have an appointment next week. Show times and ratings without booking.', sites: ['zocdoc.com'], category: 'local', isExample: true },
    { name: 'Choose a beginner yoga class', prompt: 'On ClassPass, find three beginner-friendly yoga classes near me on Saturday morning and compare distance, start time, and class style.', sites: ['classpass.com'], category: 'local', isExample: true },
    { name: 'Find an interesting workshop this weekend', prompt: 'On Eventbrite, find three in-person workshops near me this weekend covering cooking, art, or practical skills, and summarize price and schedule.', sites: ['eventbrite.com'], category: 'local', isExample: true },
    { name: 'Reserve a campsite for a quiet weekend', prompt: 'On Recreation.gov, find three available campsites within a three-hour drive for a two-night weekend next month, prioritizing shade, water access, and quiet.', sites: ['recreation.gov'], category: 'local', isExample: true },
    { name: 'Book a recovery massage', prompt: 'On Mindbody, find three 60-minute massage appointments near me after 5pm this week and compare price, therapist rating, and cancellation policy.', sites: ['mindbodyonline.com'], category: 'local', isExample: true },
    { name: 'Shortlist a dog sitter for the weekend', prompt: 'On Rover, find three highly-rated sitters near me available this weekend for one medium dog, and compare price, repeat-client count, and home setup.', sites: ['rover.com'], category: 'local', isExample: true },
    { name: 'Build a lightweight hiking kit', prompt: 'On REI, choose a daypack, rain shell, headlamp, and water filter for beginner weekend hikes, keeping the total under $350 and explaining each pick.', sites: ['rei.com'], category: 'shopping', isExample: true },
    { name: 'Furnish a compact guest room', prompt: 'On Wayfair, assemble a guest-room set with a full bed frame, two nightstands, and a lamp for under $700, using items rated at least four stars.', sites: ['wayfair.com'], category: 'shopping', isExample: true },
    { name: 'Spec a quiet home-office computer', prompt: 'On Newegg, choose compatible parts for a quiet productivity PC with 32GB of memory and 2TB of storage under $1,100, then show the parts and total.', sites: ['newegg.com'], category: 'shopping', isExample: true },
    { name: 'Compare lenses for indoor portraits', prompt: 'On B&H Photo, compare three lenses suitable for indoor portraits on a full-frame mirrorless camera, focusing on price, aperture, weight, and autofocus reviews.', sites: ['bhphotovideo.com'], category: 'shopping', isExample: true },
    { name: 'Create a simple sensitive-skin routine', prompt: 'On Sephora, build a fragrance-free cleanser, moisturizer, and sunscreen routine for sensitive skin under $100, using products with strong reviews.', sites: ['sephora.com'], category: 'shopping', isExample: true },
    { name: 'Put together a summer wedding outfit', prompt: 'On Nordstrom, assemble a semi-formal summer wedding outfit with shoes for under $450, prioritizing breathable fabrics and pieces available in common sizes.', sites: ['nordstrom.com'], category: 'shopping', isExample: true },
    { name: 'Compare refurbished phones under $500', prompt: 'On Back Market, compare three unlocked refurbished phones under $500 with at least 128GB storage, including condition, warranty, battery notes, and seller rating.', sites: ['backmarket.com'], category: 'shopping', isExample: true },
    { name: 'Choose a quiet dishwasher', prompt: 'On Lowe’s, compare three stainless dishwashers under $900 with strong drying performance and noise ratings below 48 dB, including installation cost if shown.', sites: ['lowes.com'], category: 'shopping', isExample: true },
    { name: 'Find a lake cabin for six', prompt: 'On Vrbo, find three lakefront cabins for six people for a long weekend next month, with a dock or beach access and a total stay under $1,800.', sites: ['vrbo.com'], category: 'travel', isExample: true },
    { name: 'Find an accessible downtown hotel', prompt: 'On Hotels.com, find three central hotels for two nights next month with step-free access, roll-in showers, and recent accessibility reviews. Compare full stay prices.', sites: ['hotels.com'], category: 'travel', isExample: true },
    { name: 'Plan the easiest route between two cities', prompt: 'On Rome2Rio, compare train, bus, flight, and driving options between two cities I specify, including total time, transfers, and estimated cost.', sites: ['rome2rio.com'], category: 'travel', isExample: true },
    { name: 'Price a scenic rail weekend', prompt: 'On Trainline, find round-trip rail options for a scenic weekend destination next month, leaving Friday evening and returning Sunday, with fare and transfer details.', sites: ['thetrainline.com'], category: 'travel', isExample: true },
    { name: 'Choose a social hostel for a solo trip', prompt: 'On Hostelworld, find three highly-rated hostels in a city I choose with privacy curtains, secure lockers, and lively common spaces, then compare location and price.', sites: ['hostelworld.com'], category: 'travel', isExample: true },
    { name: 'Find a cheap surprise weekend away', prompt: 'On Skyscanner, search everywhere for the cheapest nonstop weekend trips next month from my nearest airport and show five appealing options with fare times.', sites: ['skyscanner.com'], category: 'travel', isExample: true },
    { name: 'Pick the best neighborhood to stay in', prompt: 'On Lonely Planet, compare the main neighborhoods in a city I choose for food, nightlife, transit, and quiet, then recommend the best fit for a first visit.', sites: ['lonelyplanet.com'], category: 'travel', isExample: true },
    { name: 'Draft a seven-day road trip', prompt: 'On Wanderlog, build a seven-day road-trip outline between two cities I specify, balancing scenic stops, short hikes, food, and no more than four hours driving per day.', sites: ['wanderlog.com'], category: 'travel', isExample: true },
    { name: 'Review evidence on magnesium and sleep', prompt: 'On PubMed, find three recent human studies or reviews on magnesium supplements and sleep, and summarize the population, design, and findings without overstating conclusions.', sites: ['pubmed.ncbi.nlm.nih.gov'], isExample: true },
    { name: 'Understand changes in a software release', prompt: 'On GitHub, open the latest release notes for a repository I specify and summarize breaking changes, important fixes, and any migration steps.', sites: ['github.com'], isExample: true },
    { name: 'Learn the Python argparse basics', prompt: 'In the official Python documentation, find the argparse tutorial and turn it into a minimal example with positional arguments, flags, help text, and common mistakes.', sites: ['docs.python.org'], isExample: true },
    { name: 'Compare appliance reliability', prompt: 'On Consumer Reports, compare reliability and owner satisfaction for three major dishwasher brands, noting what information is available without a subscription.', sites: ['consumerreports.org'], isExample: true },
    { name: 'Check the upcoming launch calendar', prompt: 'On NASA, list the next five scheduled launches or major mission events, with dates, mission goals, and where to watch when available.', sites: ['nasa.gov'], isExample: true },
    { name: 'Find historic photos of a neighborhood', prompt: 'In the Library of Congress digital collections, find five historic photographs related to a city or neighborhood I specify and summarize their dates and context.', sites: ['loc.gov'], isExample: true },
    { name: 'Find where a film is streaming', prompt: 'On JustWatch, check where a movie I name is currently available to stream, rent, or buy, and compare prices and subscription options.', sites: ['justwatch.com'], isExample: true },
    { name: 'Prepare for an interview at a company', prompt: 'On Glassdoor, review recent interview reports for a company and role I specify, then summarize the process, recurring question types, and reported difficulty.', sites: ['glassdoor.com'], isExample: true },
  ];

  // ── launchpad lifecycle: three separate jobs (2026-07-18) ─────────────────
  // The old initializer conflated them and bailed on `if (log.children.length)
  // return` BEFORE any wiring — a device restoring a nonempty session painted
  // the splash with ZERO live listeners (cards, pills, X, HOME, theme, composer
  // all dead) and never fetched /operator/tasks. Desktop escaped only because
  // a cached MAN mode CSS-hides the splash; a cached AUTO mode (the iPad) sat
  // on a fully-drawn, fully-inert welcome screen with the cockpit CSS-hidden
  // underneath. The split:
  //   wireLaunchpadControls()   one-time controller build + event wiring —
  //                             always runs when the DOM exists, regardless of
  //                             log/session state or backend availability
  //   ctl.showDefault()         reset to the examples view (Browse, page 0)
  //   ctl.syncVisibility()      the ONLY layer that lets log/surface state
  //                             decide whether the splash is on screen
  //   ctl.refreshTasks()        saved-task hydration (the fetch that marks a
  //                             COMPLETED init — keep it last)
  // initLaunchpad() orchestrates all four and stays the entry point for boot,
  // trash-clear, and the return-to-browser surface switch.
  function _lpBuild(){
    const lp = document.getElementById('op-lp');
    const grid = document.getElementById('op-lp-grid');
    if (!lp || !grid) return null;
    const TASKS_URL = OP_URLS.tasks;
    const refreshBtn = document.getElementById('op-lp-refresh');
    const searchBtn  = document.getElementById('op-lp-search');
    const searchRow  = document.getElementById('op-lp-searchrow');
    const searchIn   = document.getElementById('op-lp-searchinput');
    const searchClr  = document.getElementById('op-lp-searchclear');
    const emptyEl    = document.getElementById('op-lp-empty');
    const heroInput  = document.getElementById('op-lp-input');
    const heroSend   = document.getElementById('op-lp-send');
    const lpTitle    = document.getElementById('op-lp-title');
    const addBtn     = document.getElementById('op-lp-add');
    const tasksTgl   = document.getElementById('op-lp-tasks-toggle');
    const themeBtn   = document.getElementById('op-lp-theme');
    const catBtns    = Array.from(document.querySelectorAll('.op-lp-cat'))
      .filter(b => b.id !== 'op-lp-tasks-toggle');   // Saved tasks switches sources; the others filter examples
    let exOffset = 0;   // ↻ advances the example rotation bucket
    let searchQ  = '';  // live task filter (empty = default view)
    let activeCat = 'all';
    let _lpExamples = true; // examples are the welcome default; Saved tasks is an explicit source switch
    let savedPage = 0;  // which window of 6 saved tasks is showing (auto-cycle pages through)
    // true only while the splash was deliberately reopened (HOME) over a live
    // conversation — the log-growth watchdog must not yank THAT splash away.
    let _userOpened = false;
    const _LP_PAGE = 6; // three desktop columns × two rows

    // Match a task against the query across name + prompt + site connectors, so
    // "amazon" finds a task whose only amazon signal is a site pill.
    function _taskMatches(t, q){
      if (!q) return true;
      const hay = [t.name, t.prompt, (t.sites || []).join(' ')].join(' ').toLowerCase();
      return q.split(/\s+/).every(w => hay.includes(w));   // AND across words
    }

    // The old Operator homepage grouped its starters by intent. Keep the data
    // model lean: connectors already tell us enough to classify cards without
    // persisting another field in every saved task.
    function _taskCategory(t){
      if (t.category) return t.category;
      const sites = (t.sites || []).join(' ').toLowerCase();
      if (/(ubereats|doordash|instacart|grubhub)/.test(sites)) return 'delivery';
      if (/(kayak|booking|airbnb|expedia|tripadvisor|flights\.google)/.test(sites)) return 'travel';
      if (/(wikipedia|arxiv|stackoverflow|wolframalpha|coursera|wikihow|nih\.gov|investopedia|khanacademy|pubmed|docs\.python|consumerreports|nasa\.gov|loc\.gov|glassdoor)/.test(sites)) return 'research';
      if (/(spotify|imdb|goodreads|espn|nytimes|reddit|rottentomatoes|bandcamp|fandango|bookshop|justwatch|seatgeek|ticketmaster)/.test(sites)) return 'media';
      if (/(amazon|bestbuy|target|costco|etsy|ikea|homedepot|chewy|petco|bookshop)/.test(sites)) return 'shopping';
      if (/(yelp|opentable|resy|alltrails|yellowpages|sfmoma|fandango|offerup)/.test(sites)) return 'local';
      return 'all';
    }

    // (Re)render the grid. 🔍 search works in BOTH states: it filters the
    // rotating examples and saved tasks. Every view is capped at six so the
    // desktop grid remains exactly two rows. ↻ shuffles and is
    // examples-only; 🔍 is always available whenever the launchpad is open.
    function renderGrid(showExamples){
      _lpExamples = showExamples;
      let source = showExamples ? _pickExamples(exOffset) : window._opTasks;
      if (activeCat !== 'all') {
        // Category views search the full example pool, not only the current
        // rotation bucket, or a perfectly valid category could appear empty.
        source = (showExamples ? _LP_EXAMPLE_POOL : window._opTasks)
          .filter(t => _taskCategory(t) === activeCat).slice(0, _LP_SHOW);
      }
      let items, filtering = false;
      if (searchQ) {
        filtering = true;
        items = source.filter(t => _taskMatches(t, searchQ)).slice(0, _LP_PAGE);
      } else if (showExamples) {
        items = source;                                          // examples: _pickExamples already caps at 6
      } else {
        // saved tasks: show one two-row page; auto-cycle pages through the rest.
        const pages = Math.max(1, Math.ceil(source.length / _LP_PAGE));
        savedPage = ((savedPage % pages) + pages) % pages;       // wrap + guard against shrink
        items = source.slice(savedPage * _LP_PAGE, savedPage * _LP_PAGE + _LP_PAGE);
      }
      grid.textContent = '';
      grid.classList.toggle('op-lp-examples', showExamples);
      items.forEach(t => grid.appendChild(buildLpCard(t, lp)));
      // Heading follows the active category (2026-07-19) — expanded copy,
      // not the pill's terse label; Browse keeps the classic line.
      const _CAT_TITLES = {
        delivery: 'Order food and groceries',
        local:    'Explore places nearby',
        shopping: 'Shop for anything',
        travel:   'Plan trips and getaways',
        research: 'Research and learn',
        media:    'Music, movies, and more',
      };
      if (lpTitle) lpTitle.textContent = !showExamples ? 'Saved tasks'
        : (_CAT_TITLES[activeCat] || 'Things to do with Operator');
      if (emptyEl) {
        const noHits = (filtering || activeCat !== 'all') && !items.length;
        const noSaved = !showExamples && !items.length && !noHits;   // empty Saved view, no filter
        emptyEl.hidden = !(noHits || noSaved);
        if (noSaved) {
          emptyEl.textContent = 'No saved tasks';
        } else if (noHits) {
          emptyEl.textContent = '';
          const q = document.createElement('span');
          q.id = 'op-lp-empty-q';
          q.textContent = searchQ || activeCat;
          emptyEl.append('No tasks match “', q, '”');
        }
      }
      // ↻ shuffles examples (examples-state only); 🔍 is always shown so search is
      // reachable whether you're looking at your saved tasks or the examples.
      if (refreshBtn) refreshBtn.hidden = !showExamples;
      if (searchBtn)  searchBtn.hidden  = false;
    }

    // One transition path for every discrete splash-state change. Search stays
    // immediate while typing; categories, shuffle, saved/examples, and cycling
    // cross-fade the fixed grid without moving its geometry.
    let _gridSwapTimer = null, _gridSwapSeq = 0;
    function swapGrid(showExamples){
      const seq = ++_gridSwapSeq;
      clearTimeout(_gridSwapTimer);
      grid.classList.add('op-lp-fading');
      _gridSwapTimer = setTimeout(() => {
        if (seq !== _gridSwapSeq) return;
        renderGrid(showExamples);
        requestAnimationFrame(() => grid.classList.remove('op-lp-fading'));
      }, 210);
    }

    function showBrowseSource(){
      activeCat = 'all'; savedPage = 0;
      catBtns.forEach((b, i) => {
        b.classList.toggle('active', i === 0);
        b.setAttribute('aria-pressed', i === 0 ? 'true' : 'false');
      });
      if (tasksTgl) {
        tasksTgl.classList.remove('active');
        tasksTgl.setAttribute('aria-pressed', 'false');
      }
    }

    function syncSavedToggle(){
      // Saved is a PERMANENT category (2026-07-19) — an empty list shows
      // a minimal "No saved tasks" state instead of hiding the tab. If tasks
      // vanish while the saved view is open, repaint in place (no jarring
      // bounce back to Browse).
      if (tasksTgl) tasksTgl.hidden = false;
      if (!(window._opTasks || []).length && !_lpExamples) {
        renderGrid(false);
        grid.classList.remove('op-lp-fading');
      }
    }

    async function refreshLaunchpadTasks(){
      try {
        const d = await (await fetch(TASKS_URL)).json();
        window._opTasks = d.ok ? (d.tasks || []) : [];
      } catch {
        window._opTasks = [];
      }
      syncSavedToggle();
      if (!_lpExamples && !log.children.length && window._opTasks.length) swapGrid(false);
    }
    window._opRefreshLaunchpadTasks = refreshLaunchpadTasks;

    // Examples are local data: paint them immediately instead of leaving the
    // welcome screen blank while the saved-tasks request is in flight.
    window._opTasks = window._opTasks || [];
    syncSavedToggle();
    renderGrid(true);
    // factory scope (not inside the searchBtn wiring): showDefault() also
    // needs to close the search row when the splash resets.
    const setSearchOpen = (open) => {
      if (!searchRow || !searchIn) return;
      searchRow.hidden = !open;
      if (searchBtn) {
        searchBtn.setAttribute('aria-pressed', open ? 'true' : 'false');
        searchBtn.classList.toggle('op-lp-search-on', open);
      }
      searchIn.placeholder = _lpExamples ? 'Search examples…' : 'Search saved tasks…';
      if (open) { searchIn.focus(); searchIn.select(); }
      else { searchQ = ''; searchIn.value = ''; if (searchClr) searchClr.hidden = true; renderGrid(_lpExamples); }
    };
    // ── control wiring: unconditional, once (this factory runs once) ──
    {
      if (refreshBtn && !refreshBtn._wired) { refreshBtn._wired = true;
        refreshBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          exOffset++;                       // next rotation bucket
          swapGrid(true);                   // ↻ only exists in the examples state
          refreshBtn.classList.remove('op-spin'); void refreshBtn.offsetWidth;  // restart the spin
          refreshBtn.classList.add('op-spin');
        });
      }
      if (searchBtn && !searchBtn._wired) { searchBtn._wired = true;
        searchBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          setSearchOpen(searchRow.hidden);   // toggle
        });
        searchIn.addEventListener('input', () => {
          searchQ = searchIn.value.trim().toLowerCase();
          if (searchClr) searchClr.hidden = !searchIn.value;
          renderGrid(_lpExamples);
        });
        searchIn.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); setSearchOpen(false); searchBtn.focus(); }
        });
        if (searchClr) searchClr.addEventListener('click', (e) => {
          e.stopPropagation(); searchIn.value = ''; searchQ = ''; searchClr.hidden = true;
          renderGrid(_lpExamples); searchIn.focus();
        });
      }
      if (heroInput && !heroInput._wired) { heroInput._wired = true;
        const heroComposer = heroInput.closest('.op-lp-composer');
        if (heroComposer && !heroComposer._focusWired) {
          heroComposer._focusWired = true;
          heroComposer.addEventListener('click', (e) => {
            if (e.target.closest('button')) return;
            heroInput.focus();
          });
        }
        // Scale-aware measure-and-set. On coarse-pointer WebKit the input is
        // computed-16px painted at 0.7x (see the .op-lp-input @supports note),
        // so the LAYOUT box runs 1/0.7 tall/wide of what's painted: cap and
        // pill growth are figured in PAINTED pixels, and the phantom 30% of
        // layout height is trimmed with a negative bottom margin — clean in
        // the composer's block flow (it fought flex centering, 2026-07-18).
        // Everywhere else paintScale is 1 and this is the rail's plain grow.
        let heroGrowFrame = 0;
        const HERO_VISUAL_CAP = 140;   // ~9 painted lines, rail parity — then scroll
        const autoGrowHero = () => {
          cancelAnimationFrame(heroGrowFrame);
          heroGrowFrame = requestAnimationFrame(() => {
            if (!heroInput.offsetWidth) return;   // splash display:none — nothing to measure
            heroInput.style.height = 'auto';
            heroInput.style.marginBottom = '0px';
            const paintScale = Math.max(0.1,
              heroInput.getBoundingClientRect().width / heroInput.offsetWidth);
            const layoutCap = HERO_VISUAL_CAP / paintScale;
            const fullHeight = heroInput.scrollHeight;
            const layoutHeight = Math.min(fullHeight, layoutCap);
            heroInput.style.height = layoutHeight + 'px';
            heroInput.style.marginBottom = -(layoutHeight * (1 - paintScale)) + 'px';
            heroInput.style.overflowY = fullHeight > layoutCap + 1 ? 'auto' : 'hidden';
          });
        };
        heroInput.addEventListener('input', autoGrowHero);
        autoGrowHero();
        const runHero = () => {
          const txt = heroInput.value.trim();
          if (!txt) { heroInput.focus(); return; }
          input.value = txt;
          heroInput.value = '';
          autoGrowHero();
          lp.hidden = true; _userOpened = false;
          submit();
        };
        heroInput.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); runHero(); }
        });
        if (heroSend) heroSend.addEventListener('click', runHero);
      }
      catBtns.forEach(btn => { if (!btn._wired) { btn._wired = true;
        btn.addEventListener('click', (e) => {
          // stopPropagation (same as the Saved toggle): expanding REFLOWS the
          // splash (collapsed assembly is centered, expanded is top-anchored),
          // so if this click bubbles, the click-away handler measures the
          // MOVED hero against the tap's old coordinates → judges "outside" →
          // instantly re-collapses what we just expanded (mobile, 2026-07-20).
          e.stopPropagation();
          lp.classList.remove('op-lp-collapsed');
          activeCat = btn.dataset.category || 'all';
          catBtns.forEach(b => {
            const on = b === btn;
            b.classList.toggle('active', on);
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
          });
          if (tasksTgl) { tasksTgl.classList.remove('active'); tasksTgl.setAttribute('aria-pressed', 'false'); }
          swapGrid(true);
        });
      } });
      const xBtn = document.getElementById('op-lp-x');
      if (xBtn && !xBtn._wired) { xBtn._wired = true;
        xBtn.addEventListener('click', (e) => { e.stopPropagation();
          lp.hidden = true; _userOpened = false;
          lp.classList.remove('op-lp-over');   // next open starts from the clean splash
        }); }
      // return-to-splash: the header HOME button reopens the solid splash at
      // any point, mid-conversation included (tasks minimized, no blur —
      // the trash's over-the-feed presentation is separate). Forerunner of
      // the sessions sidebar's "new task" entry.
      const openBtn = document.getElementById('op-lp-open');
      if (openBtn && !openBtn._wired) { openBtn._wired = true;
        openBtn.addEventListener('click', (e) => { e.stopPropagation();
          lp.classList.remove('op-lp-over');
          lp.classList.remove('op-lp-collapsed');
          showBrowseSource();
          renderGrid(true);
          _userOpened = true;              // deliberate reopen — watchdog hands off
          lp.hidden = false; }); }
      // Saved tasks is a data-source pill, not a visibility prerequisite.
      if (tasksTgl && !tasksTgl._wired) { tasksTgl._wired = true;
        tasksTgl.addEventListener('click', (e) => { e.stopPropagation();
          lp.classList.remove('op-lp-collapsed');
          activeCat = 'all'; savedPage = 0;
          catBtns.forEach(b => { b.classList.remove('active'); b.setAttribute('aria-pressed', 'false'); });
          tasksTgl.classList.add('active'); tasksTgl.setAttribute('aria-pressed', 'true');
          swapGrid(false); }); }
      if (addBtn && !addBtn._wired) { addBtn._wired = true;
        addBtn.addEventListener('click', (e) => { e.stopPropagation();
          if (window._opOpenSaveModal) window._opOpenSaveModal(); }); }
      if (themeBtn && !themeBtn._wired) { themeBtn._wired = true;
        const syncThemeButton = () => {
          const light = document.documentElement.getAttribute('data-theme') === 'light';
          const label = light ? 'use dark mode' : 'use light mode';
          themeBtn.setAttribute('aria-label', label); themeBtn.title = label;
        };
        themeBtn.addEventListener('click', (e) => {
          e.stopPropagation();
          const light = document.documentElement.getAttribute('data-theme') === 'light';
          const next = light ? 'dark' : 'light';
          document.documentElement.setAttribute('data-theme', next);
          try { localStorage.setItem('op_theme', next); } catch (_) {}
          syncThemeButton();
        });
        new MutationObserver(syncThemeButton).observe(document.documentElement,
          {attributes: true, attributeFilter: ['data-theme']});
        syncThemeButton();
      }
      if (!lp._collapseWired) { lp._collapseWired = true;
        lp.addEventListener('click', (e) => {
          // The central assembly owns a compact 24px interaction halo. Using
          // e.target alone made the viewport-wide results wrapper swallow huge
          // empty regions; requiring the literal backdrop made collapse feel
          // arbitrarily far away. Geometry keeps the hitbox stable regardless
          // of which layout wrapper happens to receive the click.
          // cats included explicitly: on mobile the pill row sits just past the
          // 24px halo below the hero, so a category tap collapsed the splash it
          // was trying to expand (remove-then-re-add race via bubbling).
          const hit = [lp.querySelector('.op-lp-hero'), lp.querySelector('.op-lp-results'),
                       lp.querySelector('.op-lp-cats')]
            .filter(Boolean).map(el => el.getBoundingClientRect());
          const pad = 24;
          const inside = hit.length && e.clientX >= Math.min(...hit.map(r => r.left)) - pad
            && e.clientX <= Math.max(...hit.map(r => r.right)) + pad
            && e.clientY >= Math.min(...hit.map(r => r.top)) - pad
            && e.clientY <= Math.max(...hit.map(r => r.bottom)) + pad;
          if (inside) return;
          lp.classList.add('op-lp-collapsed');
          activeCat = 'all';
          catBtns.forEach(b => {
            b.classList.remove('active');
            b.setAttribute('aria-pressed', 'false');
          });
          if (tasksTgl) {
            tasksTgl.classList.remove('active');
            tasksTgl.setAttribute('aria-pressed', 'false');
          }
        }); }

      // ── Auto-cycle (#1): rotate the visible cards every 15s with a smooth
      // cross-fade. Examples advance the shuffle bucket; saved tasks page through
      // in windows of 6. Frozen while the user is searching or hovering a card, or
      // when the tab is backgrounded — never yank a card out from under a click. ──
      const CYCLE_MS = 15000;
      let _hovered = false;
      grid.addEventListener('pointerenter', () => { _hovered = true; });
      grid.addEventListener('pointerleave', () => { _hovered = false; });
      function _cycleTick(){
        if (lp.hidden) return;                                   // launchpad dismissed
        if (searchQ || !searchRow.hidden) return;                // mid-search — leave it be
        if (_hovered) return;                                    // about to click a card
        if (document.hidden) return;                             // backgrounded tab
        // Only cycle when there's more than one page/bucket to show.
        if (!_lpExamples) {
          const pages = Math.ceil((window._opTasks || []).length / _LP_PAGE);
          if (pages <= 1) return;                                // ≤6 saved tasks: nothing to rotate
          savedPage++;
        } else {
          exOffset++;                                            // next example bucket
        }
        swapGrid(_lpExamples);
      }
      if (!grid._cycle) grid._cycle = setInterval(_cycleTick, CYCLE_MS);
    }

    // Conversation-starts watchdog: a log that gains children while the splash
    // sits in its boot presentation hides it — that's how the fresh-device
    // server-session adoption swap yields to the restored chat. A splash the
    // user deliberately reopened (HOME) stays put; _cycleTick no-ops while
    // hidden, so the 15s timer idling on is harmless (not a leak).
    new MutationObserver(() => {
      if (log.children.length && !_userOpened) lp.hidden = true;
    }).observe(log, { childList: true });

    return {
      // reset to the examples default: Browse source, page 0, search closed —
      // the presentation boot/trash/surface-return expect.
      showDefault(){
        _userOpened = false;
        activeCat = 'all'; savedPage = 0;
        if (searchRow && !searchRow.hidden) setSearchOpen(false);
        else { searchQ = ''; if (searchIn) searchIn.value = ''; if (searchClr) searchClr.hidden = true; }
        showBrowseSource();
        renderGrid(true);
        grid.classList.remove('op-lp-fading');
        // Default is the COLLAPSED assembly — wordmark + composer + pills, no
        // example grid (the same state click-away produces; 2026-07-19).
        // Any pill/search interaction expands it, as already wired.
        lp.classList.add('op-lp-collapsed');
      },
      // the ONLY place session/surface state decides splash visibility
      syncVisibility(){
        lp.hidden = (typeof _isGameSurface === 'function' && _isGameSurface())
          || !!log.children.length;
        if (lp.hidden) _userOpened = false;
      },
      refreshTasks: refreshLaunchpadTasks,
    };
  }
  function wireLaunchpadControls(){
    if (!_lpCtl) _lpCtl = _lpBuild();
    return _lpCtl;
  }
  function initLaunchpad(){
    const ctl = wireLaunchpadControls();
    if (!ctl) return;
    ctl.showDefault();
    ctl.syncVisibility();
    // Saved tasks hydrate in the background, LAST — local examples and every
    // control are usable immediately, even on a slow request. This fetch is
    // also the completed-init marker the production probe watches for.
    ctl.refreshTasks();
  }

  // one launchpad card — shared by saved tasks and the example set.
  const _PLAY_SVG = '<svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor" aria-hidden="true"><path d="M4.5 3.2v9.6a.6.6 0 0 0 .93.5l7.2-4.8a.6.6 0 0 0 0-1l-7.2-4.8a.6.6 0 0 0-.93.5z"/></svg>';
  const _PENCIL_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 20h4L18.5 9.5a2 2 0 0 0-2.8-2.8L5 17v3z"/><path d="M13.5 6.5l4 4"/></svg>';
  function buildLpCard(t, lp){
    const c = document.createElement('div');
    c.className = 'op-lp-card' + (t.isExample ? ' op-lp-example' : '');
    const nm = document.createElement('div'); nm.className = 'op-lp-name'; nm.textContent = t.name; c.appendChild(nm);
    const pr = document.createElement('div'); pr.className = 'op-lp-prompt'; pr.textContent = t.prompt; c.appendChild(pr);
    const meta = document.createElement('div'); meta.className = 'op-lp-meta';
    (t.sites || []).slice(0, 2).forEach(s => {
      const chip = document.createElement('span'); chip.className = 'op-lp-site';
      const dom = s.split('/')[0];
      if (/^[a-z0-9.-]+\.[a-z]{2,}$/i.test(dom)) {   // looks like a domain → favicon
        const fv = document.createElement('img'); fv.alt = '';
        fv.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(dom) + '&sz=32';
        fv.addEventListener('error', () => fv.remove());
        chip.appendChild(fv);
      }
      const tx = document.createElement('span'); tx.textContent = _siteLabel(dom); chip.appendChild(tx);
      meta.appendChild(chip);
    });
    if (t.schedule) {
      const sc = document.createElement('span'); sc.className = 'op-lp-site op-lp-sched';
      sc.textContent = '⏱ ' + cronToHuman(t.schedule); sc.title = t.schedule; meta.appendChild(sc);
    }
    if (t.vars && t.vars.length) {
      const vc = document.createElement('span'); vc.className = 'op-lp-site op-lp-vars';
      vc.textContent = '{…} ' + t.vars.join(', ');
      vc.title = 'fill-in variables — Go loads the prompt to complete'; meta.appendChild(vc);
    }
    if (!t.isExample) {
      const who = document.createElement('span'); who.className = 'op-lp-bot';
      who.textContent = [t.bot, t.model].filter(Boolean).join(' · '); meta.appendChild(who);
    }
    c.appendChild(meta);
    const activate = () => {
      lp.hidden = true;
      if (t.isExample) {   // Go sends the example straight to the agent (like typing it + Enter)
        if (MODE !== 'auto') { MODE = 'auto'; try { applyMode(); } catch(_){} }
        try { logUser(t.prompt); } catch(_){}
        dispatchTask(t.prompt);
      } else {
        if (window._opRunSavedTask) window._opRunSavedTask(t);   // runs the stored bundle
      }
    };
    // Go button — the primary action; revealed on card hover (always shown on touch).
    const go = document.createElement('button'); go.type = 'button'; go.className = 'op-lp-go';
    go.innerHTML = _PLAY_SVG + '<span>Go</span>';
    go.addEventListener('click', (e) => { e.stopPropagation(); activate(); });
    c.appendChild(go);
    // Edit (user tasks only) — flat pencil, appears on hover.
    if (!t.isExample && t.slug && window._opEditSavedTask) {
      const ed = document.createElement('button'); ed.type = 'button'; ed.className = 'op-lp-edit';
      ed.title = 'Edit'; ed.setAttribute('aria-label','edit task'); ed.innerHTML = _PENCIL_SVG;
      ed.addEventListener('click', (e) => { e.stopPropagation(); lp.hidden = true; window._opEditSavedTask(t); });
      c.appendChild(ed);
    }
    // Go launches; a tap anywhere else on the card ONLY pastes the prompt into
    // the composer — the launchpad stays open and keeps browsing, tapping
    // another card just swaps the draft (2026-07-11; supersedes the
    // 2026-07-09 close-on-tap). Never auto-fires. Go/Edit stopPropagation.
    c.addEventListener('click', () => {
      input.value = t.prompt || '';
      const heroInput = document.getElementById('op-lp-input');
      if (heroInput) { heroInput.value = t.prompt || ''; heroInput.focus(); }
      if (typeof autoGrow === 'function') autoGrow();
      if (typeof refreshSendButton === 'function') refreshSendButton();
    });
    return c;
  }
  // cron → short human label (v0.7.0): reverses the shapes the scheduler UI
  // compiles (hourly/daily/weekdays/weekly/monthly); anything else shows raw.
  function cronToHuman(expr){
    const f = (expr || '').trim().split(/\s+/);
    if (f.length !== 5) return expr || '';
    const [mi, hr, dom, mon, dow] = f;
    const t = () => { const h = parseInt(hr,10), m = parseInt(mi,10);
      if (isNaN(h) || isNaN(m)) return hr + ':' + mi;
      const ap = h < 12 ? 'AM' : 'PM'; return (h%12||12) + ':' + String(m).padStart(2,'0') + ' ' + ap; };
    const NAMES = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    if (mon !== '*') return expr;
    if (hr === '*' && dom === '*' && dow === '*') return 'hourly at :' + String(parseInt(mi,10)||0).padStart(2,'0');
    if (dom === '*' && dow === '*') return 'daily ' + t();
    if (dom === '*' && dow === '1-5') return 'weekdays ' + t();
    if (dom === '*' && /^[0-7](,[0-7])*$/.test(dow))
      return dow.split(',').map(d => NAMES[parseInt(d,10)%7]).join(', ') + ' ' + t();
    if (dow === '*' && /^\d+$/.test(dom)) return 'monthly · day ' + dom + ' ' + t();
    return expr;
  }
  // ── New-task modal (#30 v2b) — the 💾 urlbar button opens an OpenAI-Operator-
  // style save dialog: name / prompt / websites+apps+MCPs. Prefill order: the
  // composer draft (if it isn't a /command), else the last dispatched task.
  // Saves through the same POST as the /save palette row; current bot/model/
  // effort are captured silently, exactly like a dispatch would use them.
  function initNewTaskModal(){
    const veil = document.getElementById('op-nt-veil');
    const btn  = document.getElementById('op-save-task');
    if (!veil || !btn) return;
    const name = document.getElementById('op-nt-name');
    const prompt = document.getElementById('op-nt-prompt');
    const sitesIn = document.getElementById('op-nt-sites');
    const pillsEl = document.getElementById('op-nt-pills');
    const sugEl = document.getElementById('op-nt-sug');
    const _delBtn = document.getElementById('op-nt-delete');
    const _titleEl = document.getElementById('op-nt-title');
    const TASKS_URL = OP_URLS.tasks;

    // ── sites/tools pill input (#6): chips + icon autocomplete ──
    // Common picks get real favicons (Google's s2 endpoint — this page has
    // network); MCP/tool entries get a letter-avatar placeholder (first letter
    // of the tool name — MCPs have no favicon) and save WITHOUT a label suffix.
    // MCPs are pinned to the TOP of the pick list (v0.7.0, the owner). Typing +
    // Enter pills anything; Backspace on empty pops the last.
    const COMMON = [
      // MCPs / tools first
      {v:'playwright', tool:true}, {v:'github-mcp', tool:true}, {v:'notion-mcp', tool:true},
      {v:'search', tool:true}, {v:'discord', tool:true},
      // finance / work
      {v:'bloomberg.com'}, {v:'reuters.com'}, {v:'finviz.com'},
      {v:'ibkr.com', ico:'interactivebrokers.com'}, {v:'github.com'},
      {v:'gmail.com', ico:'mail.google.com'}, {v:'docs.google.com'},
      // shopping / food
      {v:'amazon.ca'}, {v:'ebay.com'}, {v:'walmart.ca'}, {v:'bestbuy.ca'},
      {v:'doordash.com'}, {v:'ubereats.com'}, {v:'instacart.com'}, {v:'opentable.com'},
      // travel / local
      {v:'google.com/maps', ico:'maps.google.com'}, {v:'booking.com'}, {v:'airbnb.com'},
      {v:'expedia.ca', ico:'expedia.com'}, {v:'yelp.com'}, {v:'tripadvisor.com'},
      // media / reference / play
      {v:'youtube.com'}, {v:'x.com'}, {v:'reddit.com'}, {v:'netflix.com'},
      {v:'spotify.com'}, {v:'wikipedia.org'}, {v:'weather.com'}, {v:'lichess.org'},
      // learning / reference
      {v:'khanacademy.org'}, {v:'duolingo.com'}, {v:'investopedia.com'},
      {v:'news.ycombinator.com', ico:'ycombinator.com'},
      // events / tickets / local
      {v:'ticketmaster.com'}, {v:'fandango.com'}, {v:'offerup.com'},
      // media / taste / fitness
      {v:'rottentomatoes.com'}, {v:'bandcamp.com'}, {v:'vivino.com'},
      {v:'strava.com'}, {v:'bookshop.org'},
    ];
    let _pills = [];
    let _editSlug = null;   // set while editing an existing task → save() updates in place
    function fav(d){ return 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(d) + '&sz=32'; }
    function letterIcon(name){
      const s = document.createElement('span'); s.className = 'op-nt-letter';
      s.textContent = ((name || '?').trim()[0] || '?');
      return s;
    }
    function pillIcon(entry){
      if (entry.tool) return letterIcon(entry.v);
      const im = document.createElement('img'); im.alt = '';
      im.src = fav(entry.ico || entry.v);
      im.addEventListener('error', () => im.remove());   // no favicon → text-only pill
      return im;
    }
    function addPill(val, entry){
      val = (val || '').trim().replace(/,+$/, '');
      if (!val || _pills.some(p => p.v === val)) return;
      const meta = entry || COMMON.find(c => c.v === val) || {v: val, guess: true};
      _pills.push({ v: val });
      const pill = document.createElement('span'); pill.className = 'op-nt-pill'; pill.dataset.v = val;
      if (meta.tool || /^[a-z0-9.-]+\.[a-z]{2,}$/i.test(val) || meta.ico)
        pill.appendChild(pillIcon(meta.tool ? meta : {v: val, ico: meta.ico}));
      const tx = document.createElement('span');
      const dom = val.split('/')[0];
      tx.textContent = meta.tool ? val : _siteLabel(dom);
      if (!meta.tool && tx.textContent !== val) pill.title = val;
      pill.appendChild(tx);
      const x = document.createElement('span'); x.className = 'op-nt-pill-x';
      x.innerHTML = '<svg viewBox="0 0 12 12" width="8" height="8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"></path></svg>';
      x.addEventListener('click', () => { _pills = _pills.filter(p => p.v !== val); pill.remove(); renderSug(); });
      pill.appendChild(x);
      pillsEl.insertBefore(pill, sitesIn);
      sitesIn.value = ''; renderSug();
    }
    function renderSug(){
      const q = sitesIn.value.trim().toLowerCase();
      const rows = COMMON.filter(c => !_pills.some(p => p.v === c.v)
                                   && (!q || c.v.toLowerCase().includes(q)));
      sugEl.textContent = '';
      if (!rows.length) { sugEl.hidden = true; return; }
      rows.forEach(c => {
        const r = document.createElement('div'); r.className = 'op-nt-sug-row';
        r.appendChild(pillIcon(c));
        const t = document.createElement('span');
        t.textContent = c.tool ? c.v : _siteLabel(c.v.split('/')[0]);
        if (!c.tool && t.textContent !== c.v) r.title = c.v;
        r.appendChild(t);
        if (c.tool) { const k = document.createElement('span'); k.className = 'op-nt-sug-kind'; k.textContent = 'MCP'; r.appendChild(k); }
        const plus = document.createElement('span'); plus.className = 'op-nt-sug-add'; plus.textContent = '＋';
        r.appendChild(plus);
        r.addEventListener('mousedown', e => { e.preventDefault(); addPill(c.v, c); sitesIn.focus(); });
        sugEl.appendChild(r);
      });
      sugEl.hidden = document.activeElement !== sitesIn;
    }
    sitesIn.addEventListener('focus', renderSug);
    sitesIn.addEventListener('input', renderSug);
    sitesIn.addEventListener('blur', () => setTimeout(() => { sugEl.hidden = true; }, 120));
    sitesIn.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); addPill(sitesIn.value); }
      else if (e.key === 'Backspace' && !sitesIn.value && _pills.length) {
        const last = _pills[_pills.length - 1];
        _pills.pop();
        const el = pillsEl.querySelector('.op-nt-pill[data-v="' + (window.CSS && CSS.escape ? CSS.escape(last.v) : last.v) + '"]');
        if (el) el.remove(); renderSug();
      }
    });
    pillsEl.addEventListener('mousedown', e => { if (e.target === pillsEl) { e.preventDefault(); sitesIn.focus(); } });

    // ── real scheduler (v0.7.0): repeat/time/day pickers → 5-field cron. The
    // backend contract is unchanged (cron string in `schedule`); only the UI
    // stopped being a cron textbox. "Custom (cron)" keeps the raw escape hatch.
    const repSel = document.getElementById('op-nt-rep');
    const timeIn = document.getElementById('op-nt-time');
    const dowEl  = document.getElementById('op-nt-dow');
    const domRow = document.getElementById('op-nt-dom-row');
    const domIn  = document.getElementById('op-nt-dom');
    const cronIn = document.getElementById('op-nt-sched');
    const sumEl  = document.getElementById('op-nt-sched-sum');
    // chips Mon..Sun → cron dow 1..6,0
    ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].forEach((d, i) => {
      const c = document.createElement('span'); c.className = 'op-nt-dowchip';
      c.textContent = d; c.dataset.d = String((i + 1) % 7);
      c.addEventListener('click', () => { c.classList.toggle('active'); schedSync(); });
      dowEl.appendChild(c);
    });
    function _timeParts(){ const p = (timeIn.value || '09:00').split(':');
      return { h: parseInt(p[0], 10) || 0, m: parseInt(p[1], 10) || 0 }; }
    function _fmtTime(){ const {h, m} = _timeParts(); const ap = h < 12 ? 'AM' : 'PM';
      return (h % 12 || 12) + ':' + String(m).padStart(2, '0') + ' ' + ap; }
    function _pickedDays(){ return [...dowEl.querySelectorAll('.active')]; }
    function schedCron(){
      const rep = repSel.value, {h, m} = _timeParts();
      if (!rep) return '';
      if (rep === 'cron')     return cronIn.value.trim();
      if (rep === 'hourly')   return m + ' * * * *';
      if (rep === 'daily')    return m + ' ' + h + ' * * *';
      if (rep === 'weekdays') return m + ' ' + h + ' * * 1-5';
      if (rep === 'weekly') { const ds = _pickedDays().map(c => c.dataset.d);
        return ds.length ? m + ' ' + h + ' * * ' + ds.join(',') : ''; }
      if (rep === 'monthly')  return m + ' ' + h + ' ' + (parseInt(domIn.value, 10) || 1) + ' * *';
      return '';
    }
    function schedHuman(){
      const rep = repSel.value, {m} = _timeParts();
      if (!rep) return '';   // "None" is self-explanatory — no helper line
      if (rep === 'hourly')   return 'Runs every hour at :' + String(m).padStart(2, '0') + '.';
      if (rep === 'daily')    return 'Runs every day at ' + _fmtTime() + '.';
      if (rep === 'weekdays') return 'Runs Mon–Fri at ' + _fmtTime() + '.';
      if (rep === 'weekly') { const names = _pickedDays().map(c => c.textContent);
        return names.length ? 'Runs every ' + names.join(', ') + ' at ' + _fmtTime() + '.'
                            : 'Pick at least one day.'; }
      if (rep === 'monthly')  return 'Runs monthly on day ' + (parseInt(domIn.value, 10) || 1)
                                     + ' at ' + _fmtTime() + '.';
      if (rep === 'cron') { const c = cronIn.value.trim();
        if (!c) return 'Enter a 5-field cron: min hour dom mon dow.';
        return (c.split(/\s+/).length === 5 ? '' : '⚠ needs 5 fields — ') + cronToHuman(c); }
      return '';
    }
    function schedSync(){
      const rep = repSel.value;
      timeIn.hidden = !rep || rep === 'cron';
      dowEl.hidden  = rep !== 'weekly';
      domRow.hidden = rep !== 'monthly';
      cronIn.hidden = rep !== 'cron';
      sumEl.innerHTML = '';
      const span = document.createElement('span');
      if (rep === 'cron') span.className = 'cronlit';
      span.textContent = schedHuman();
      sumEl.appendChild(span);
    }
    function schedReset(){
      repSel.value = ''; timeIn.value = '09:00'; domIn.value = '1'; cronIn.value = '';
      _pickedDays().forEach(c => c.classList.remove('active'));
      schedSync();
    }
    repSel.addEventListener('change', schedSync);
    [timeIn, domIn, cronIn].forEach(el => el.addEventListener('input', schedSync));
    schedSync();

    function openModal(){
      const draft = input.value.trim();
      prompt.value = (draft && !draft.startsWith('/')) ? draft
        : ((window._opLastDispatch || {}).task || '');
      name.value = ''; sitesIn.value = '';
      schedReset();
      _pills = []; pillsEl.querySelectorAll('.op-nt-pill').forEach(p => p.remove());
      _editSlug = null;
      _titleEl.textContent = 'Save task';
      document.getElementById('op-nt-save').textContent = 'Save task';
      _delBtn.hidden = true; _delBtn.textContent = 'Delete';
      veil.hidden = false;
      setTimeout(() => name.focus(), 0);
    }
    // Edit an existing saved task: prefill every field + its site pills, remember
    // the slug so save() updates in place (backend keeps `created`).
    function openModalWith(t){
      name.value = t.name || '';
      prompt.value = t.prompt || '';
      sitesIn.value = '';
      schedReset();
      _pills = []; pillsEl.querySelectorAll('.op-nt-pill').forEach(p => p.remove());
      (t.sites || (t.site ? [t.site] : [])).forEach(s => {
        (String(s).split(',')).map(x => x.trim()).filter(Boolean).forEach(v => addPill(v));
      });
      _editSlug = t.slug || null;
      _titleEl.textContent = 'Edit task';
      document.getElementById('op-nt-save').textContent = 'Save changes';
      _delBtn.hidden = !_editSlug; _delBtn.textContent = 'Delete';   // delete lives in the edit menu
      veil.hidden = false;
      setTimeout(() => name.focus(), 0);
    }
    function closeModal(){ veil.hidden = true; _editSlug = null; _delBtn.textContent = 'Delete'; }
    async function delTask(){
      if (!_editSlug) return;
      // two-tap confirm inline on the button (no separate dialog)
      if (_delBtn.textContent !== 'Confirm delete') { _delBtn.textContent = 'Confirm delete'; return; }
      try {
        const r = await fetch(TASKS_URL + '/' + encodeURIComponent(_editSlug), { method: 'DELETE' });
        const d = await r.json().catch(() => ({}));
        if (d.ok) {
          logRes('deleted', true); closeModal();
          if (window._opRefreshLaunchpadTasks) await window._opRefreshLaunchpadTasks();
        }
        else logRes(d.error || 'delete failed', false);
      } catch { logRes('delete failed', false); }
    }
    async function save(){
      const n = name.value.trim(), p = prompt.value.trim();
      if (!n) { name.focus(); return; }
      if (!p) { prompt.focus(); return; }
      if (sitesIn.value.trim()) addPill(sitesIn.value);   // pending text counts
      const sc = schedCron();
      if (repSel.value && !sc) { schedSync(); return; }   // weekly with 0 days / empty custom
      const body = { name: n, task: p, sites: _pills.map(x => x.v).join(', '),
        schedule: sc,
        bot: (typeof selectedBot === 'function') ? selectedBot() : '',
        model: (document.getElementById('op-model')||{}).value || '',
        effort: (document.getElementById('op-effort')||{}).value || '' };
      if (_editSlug) body.slug = _editSlug;   // update in place
      try { const d = await (await fetch(TASKS_URL, { method:'POST',
          headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })).json();
        if (d.ok) {
          const launchpad = document.getElementById('op-lp');
          // Saving from the homepage is launchpad state, not a conversation
          // event. Logging it would add the first chat row and dismiss the
          // entire splash just as its new Saved pill becomes available.
          if (!launchpad || launchpad.hidden) logRes('saved “' + n + '”', true);
          closeModal();
          if (window._opRefreshLaunchpadTasks) await window._opRefreshLaunchpadTasks();
        }
        else logRes(d.error || 'save failed', false); }
      catch { logRes('save failed', false); }
    }
    window._opOpenSaveModal = openModal;   // launchpad's ＋ card opens the dialog too
    window._opEditSavedTask = openModalWith;   // launchpad card's pencil opens prefilled
    btn.addEventListener('click', openModal);
    _delBtn.addEventListener('click', (e) => { e.preventDefault(); delTask(); });
    // reset the two-tap delete confirm if the user touches anything else
    [name, prompt, sitesIn].forEach(el => el && el.addEventListener('input', () => {
      if (_delBtn.textContent === 'Confirm delete') _delBtn.textContent = 'Delete'; }));
    document.getElementById('op-nt-close').addEventListener('click', closeModal);
    document.getElementById('op-nt-cancel').addEventListener('click', closeModal);
    document.getElementById('op-nt-save').addEventListener('click', save);
    veil.addEventListener('mousedown', e => { if (e.target === veil) closeModal(); });
    veil.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.preventDefault(); closeModal(); }
      // Enter saves from the name field; the textarea keeps Enter for newlines
      // and the pill input consumes Enter itself (adds a pill).
      if (e.key === 'Enter' && e.target !== prompt && e.target !== sitesIn) { e.preventDefault(); save(); }
    });
  }
  // ── Slash palette — saved tasks (#30 v2) ──────────────────────────────────
  // Summoned by typing "/" in the composer; no standing chrome. Filter mode
  // lists tasks (↑↓ pick, ↵ run-as-stored, Tab load-and-edit, hover 🗑 delete
  // with click-again-to-confirm); "/save" switches to save mode, storing the
  // LAST DISPATCHED bundle (prompt+bot+model+effort — the draft is gone by
  // save time) under an inline name + optional sites. All state lives here;
  // the composer only consults window._opPalKeydown while a draft starts "/".
  function initSlashTasks(){
    const pal   = document.getElementById('op-pal');
    const list  = document.getElementById('op-pal-list');
    const foot  = document.getElementById('op-pal-foot');
    const saveRow   = document.getElementById('op-pal-save');
    const saveName  = document.getElementById('op-pal-save-name');
    const saveSites = document.getElementById('op-pal-save-sites');
    const title = document.getElementById('op-pal-title');
    if (!pal || !list) return;
    const TASKS_URL = OP_URLS.tasks;
    const RUN_URL   = OP_URLS.task_run;
    const DEL_URL   = OP_URLS.task_delete;
    // shared saved-task runner — the palette (↵) and the launchpad cards both
    // dispatch through here; the server applies the task's stored bundle.
    // Defined BEFORE the demo gate: the demo keeps the launchpad + save modal
    // (which need this); only the slash palette below stays cockpit-only.
    window._opRunSavedTask = async function(t){
      if (_inFlight) { logRes('agent is busy — stop it or steer first, then re-run the saved task', false); return; }
      if (t.vars && t.vars.length) {
        // variable-placeholder task (1.0.13): values are needed — load the
        // prompt into the composer and select the first placeholder; never
        // auto-fire (the server would 400 it anyway). NB the brace pairs are
        // built at runtime: literal double braces in this template are Jinja
        // syntax and mangle the rendered script.
        const OB = '{' + '{', CB = '}' + '}';
        if (MODE !== 'auto') { MODE = 'auto'; try { applyMode(); } catch(_){} }
        input.value = t.prompt; autoGrow(); refreshSendButton();
        const i = input.value.indexOf(OB);
        if (i >= 0) { const j = input.value.indexOf(CB, i);
          input.focus(); input.setSelectionRange(i, j >= 0 ? j + 2 : i + 2); }
        logEvent('Fill the ' + OB + 'variables' + CB + ', then Enter', true);
        return;
      }
      logUser('▶ ' + t.name);
      op.dataset.busy = '1'; setCardText(actTxt, 'Starting up'); setCardSub(t.bot || selectedBot(), '');
      try { const d = await (await fetch(RUN_URL.replace('__S__', encodeURIComponent(t.slug)), {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).json();
        // fresh watchdog anchor per turn — same reason as dispatchTask's reset
        if (d.ok) { _lastAssistant = ''; _runProgressTs = 0; startTask(); _agentSince = Date.now()/1000; setInFlight(true); }
        else { logRes(d.error || 'failed to run task', false); settleAction(false); } }
      catch { logRes('failed to run task', false); settleAction(false); }
    };
    if (op.classList.contains('op-demo')) return;   // demo: no slash palette (CSS hides it too)
    let _tasks = [];        // fetched list (public shape)
    let _sel = 0;           // selected index into the FILTERED view
    let _view = [];         // current filtered slugs
    let _mode = '';         // '' closed | 'list' | 'save'
    let _armedDel = '';     // slug whose 🗑 is armed for confirm

    async function refresh(){
      try { const d = await (await fetch(TASKS_URL)).json();
        _tasks = d.ok ? (d.tasks || []) : []; } catch { _tasks = []; }
    }
    function open(){ pal.hidden = false; }
    function close(){ pal.hidden = true; _mode=''; _armedDel=''; saveRow.hidden = true; foot.hidden = false; }
    function filterText(){ return input.value.trim().slice(1).toLowerCase(); }

    function render(){
      const q = filterText();
      _view = _tasks.filter(t => !q || (t.name||'').toLowerCase().includes(q)
                                    || (t.prompt||'').toLowerCase().includes(q));
      if (_sel >= _view.length) _sel = Math.max(0, _view.length - 1);
      list.textContent = '';
      if (!_view.length) {
        const e = document.createElement('div'); e.className = 'op-pal-empty';
        e.textContent = _tasks.length ? 'no saved task matches — keep typing or Esc'
                                      : 'no saved tasks yet — run something, then type /save';
        list.appendChild(e); return;
      }
      _view.forEach((t, i) => {
        const row = document.createElement('div');
        row.className = 'op-pal-item' + (i === _sel ? ' sel' : '');
        const nm = document.createElement('span'); nm.className = 'op-pal-name'; nm.textContent = t.name;
        const pr = document.createElement('span'); pr.className = 'op-pal-prompt'; pr.textContent = t.prompt;
        row.appendChild(nm); row.appendChild(pr);
        if (i === _sel) { const k = document.createElement('span'); k.className = 'op-pal-key'; k.textContent = '↵ run'; row.appendChild(k); }
        const del = document.createElement('button'); del.type = 'button';
        del.className = 'op-pal-del' + (_armedDel === t.slug ? ' arm' : '');
        del.title = _armedDel === t.slug ? 'click again to delete' : 'delete task';
        del.textContent = _armedDel === t.slug ? 'sure?' : '🗑';
        del.addEventListener('mousedown', ev => { ev.preventDefault(); ev.stopPropagation(); tapDelete(t.slug); });
        row.appendChild(del);
        row.addEventListener('mousedown', ev => { ev.preventDefault(); _sel = i; runSelected(); });
        row.addEventListener('mousemove', () => { if (_sel !== i) { _sel = i; render(); } });
        list.appendChild(row);
      });
      const s = list.querySelector('.sel'); if (s) s.scrollIntoView({ block: 'nearest' });
    }

    async function tapDelete(slug){
      if (_armedDel !== slug) { _armedDel = slug; render(); return; }
      _armedDel = '';
      try { const d = await (await fetch(DEL_URL.replace('__S__', encodeURIComponent(slug)), { method: 'DELETE' })).json();
        if (d.ok) {
          await refresh();
          if (window._opRefreshLaunchpadTasks) await window._opRefreshLaunchpadTasks();
          logRes('deleted', true);
        } } catch {}
      render();
    }

    async function runSelected(){
      const t = _view[_sel]; if (!t) return;
      input.value = ''; autoGrow(); refreshSendButton(); close();
      window._opRunSavedTask(t);
    }

    function loadSelected(){
      const t = _view[_sel]; if (!t) return;
      input.value = t.prompt || ''; autoGrow(); refreshSendButton();
      (async () => {
        if (t.bot && [].some.call(caretSel.options, o => o.value === t.bot)) {
          caretSel.value = t.bot; await loadModels(t.bot);
        }
        const ms = document.getElementById('op-model'), es = document.getElementById('op-effort');
        if (ms && t.model) { ms.value = t.model; if (typeof syncEffort === 'function') syncEffort(); }
        if (es && t.effort) es.value = t.effort;
      })();
      close(); input.focus();
    }

    function enterSaveMode(){
      const last = window._opLastDispatch;
      _mode = 'save'; open(); list.textContent = ''; foot.hidden = true;
      if (!last || !last.task) {
        const e = document.createElement('div'); e.className = 'op-pal-empty';
        e.textContent = 'nothing to save yet — dispatch a task first, then /save';
        list.appendChild(e); saveRow.hidden = true;
        title.textContent = 'SAVE TASK'; return;
      }
      const e = document.createElement('div'); e.className = 'op-pal-empty';
      e.textContent = '💾 “' + (last.task.length > 90 ? last.task.slice(0, 90) + '…' : last.task)
        + '”  ·  ' + (last.bot || '') + (last.model ? ' · ' + last.model : '');
      list.appendChild(e);
      title.textContent = 'SAVE TASK';
      saveRow.hidden = false; saveName.value = ''; saveSites.value = '';
      setTimeout(() => saveName.focus(), 0);
    }

    async function doSave(){
      const last = window._opLastDispatch; if (!last || !last.task) return;
      const name = saveName.value.trim();
      if (!name) { saveName.focus(); return; }
      const body = { name, task: last.task, sites: saveSites.value,
                     bot: last.bot || '', model: last.model || '', effort: last.effort || '' };
      try { const d = await (await fetch(TASKS_URL, { method: 'POST',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
        if (d.ok) {
          await refresh();
          if (window._opRefreshLaunchpadTasks) await window._opRefreshLaunchpadTasks();
          logRes('saved “' + name + '”', true);
        }
        else logRes(d.error || 'save failed', false); }
      catch { logRes('save failed', false); }
      input.value = ''; autoGrow(); refreshSendButton(); close(); input.focus();
    }
    [saveName, saveSites].forEach(el => el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); doSave(); }
      if (e.key === 'Escape') { e.preventDefault(); input.value=''; autoGrow(); refreshSendButton(); close(); input.focus(); }
    }));

    // Composer input drives open/filter/close. Runs alongside autoGrow's listener.
    input.addEventListener('input', async () => {
      const v = input.value;
      if (!v.startsWith('/')) { if (_mode) close(); return; }
      if (/^\/save(\s|$)/i.test(v)) { if (_mode !== 'save') enterSaveMode(); return; }
      if (_mode !== 'list') { _mode = 'list'; _sel = 0; title.textContent = 'SAVED TASKS';
        saveRow.hidden = true; foot.hidden = false; await refresh(); open(); }
      _armedDel = '';
      render();
    });
    // Key routing while the palette is up — called FIRST by the composer's
    // keydown handler; return true = the palette consumed the key.
    window._opPalKeydown = function(e){
      if (!_mode) return false;
      if (e.key === 'Escape') { e.preventDefault(); input.value=''; autoGrow(); refreshSendButton(); close(); return true; }
      if (_mode !== 'list') return false;   // save mode: fields have their own handlers
      if (e.key === 'ArrowDown') { e.preventDefault(); _sel = Math.min(_sel + 1, Math.max(0, _view.length - 1)); render(); return true; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); _sel = Math.max(_sel - 1, 0); render(); return true; }
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); runSelected(); return true; }
      if (e.key === 'Tab')       { e.preventDefault(); loadSelected(); return true; }
      return false;
    };
    // Click-away closes (mousedown inside the palette is preventDefault'ed above).
    document.addEventListener('mousedown', e => {
      if (_mode && !pal.contains(e.target) && e.target !== input) close();
    });
  }
  // (re)load the model list for a driver — Claude models for claude-a/claude-b, GPT
  // models for gpt. Defaults: Sonnet for Claude bots, GPT-5.6 Sol (low) for gpt.
  async function loadModels(driver){
    try {
      const m = await (await fetch(OP_URLS.models + "?driver=" + encodeURIComponent(driver||''))).json();
      const sel = document.getElementById('op-model');
      sel.textContent = '';
      (m.models||[]).forEach(x => { const o=document.createElement('option');
        o.value=x.value; o.textContent=x.label; sel.appendChild(o); });
      let savedModel = (typeof _sess!=='undefined' && _sess) ? _sess.model : '';
      if (!savedModel) { try { const _sv = JSON.parse(localStorage.getItem(LS_KEY) || 'null'); if (_sv) savedModel = _sv.model || ''; } catch {} }
      const want = (driver === 'gpt') ? 'gpt-5.6-sol' : (driver === 'gemma') ? 'Gemini 3.5 Flash' : 'claude-sonnet-5';
      if (savedModel && [].some.call(sel.options, o=>o.value===savedModel)) sel.value = savedModel;
      else if ([].some.call(sel.options, o=>o.value===want)) sel.value = want;
      if (typeof syncEffort === 'function') syncEffort();
      fitMini(sel);   // model select only — effort is right-aligned + auto-sizes
      // enable the smooth gray transition only AFTER this first programmatic build paints,
      // so the disabled state lands instantly on load (no light->gray flash) but animates on change.
      const _r=document.getElementById('op-modelrow'); if(_r) _r.classList.add('op-ready');
      requestAnimationFrame(() => { if(_r) _r.classList.add('op-anim-ready'); });
    } catch {}
  }

  // effort tiers per model (authoritative, claude docs): Opus and Sonnet 5 both
  // get the full 5-tier scale (xhigh added 2026-06-30 — Sonnet 5 supports it,
  // this list was stale from when it was written for Sonnet 4.6). Haiku = no
  // effort support. Picker reacts to the chosen model.
  const EFFORT_BY_MODEL = {
    opus:   ["low", "medium", "high", "xhigh", "max"],
    "claude-sonnet-5": ["low", "medium", "high", "xhigh", "max"],
    haiku:  [],   // Haiku 4.5 has no effort support
    // GPT-5.6 family: per-tier ladders (OpenAI, 2026-06). Sol flagship adds max+ultra
    // above the base low/medium/high; Terra is the standard ladder; Luna (fast/cheap)
    // caps at minimal/low. No xhigh on the 5.6 line — that ladder is Claude-side only.
    "gpt-5.6-sol":   ["low", "medium", "high", "max", "ultra"],
    "gpt-5.6-terra": ["low", "medium", "high"],
    "gpt-5.6-luna":  ["minimal", "low"],
    "gpt-5.5": ["low", "medium", "high", "xhigh"],
    // gemma/agy: pick the Gemini family in the model picker, the tier in the effort picker;
    // start() combines them into agy's "Gemini X (Tier)" --model string.
    "Gemini 3.5 Flash": ["low", "medium", "high"],
    "Gemini 3.1 Pro": ["low", "high"],
    "Claude Sonnet 4.6 (Thinking)": [], "Claude Opus 4.6 (Thinking)": [], "GPT-OSS 120B (Medium)": [],
  };
  // a width:auto <select> sizes to its WIDEST option, so a short selection (e.g. '3.5 Flash')
  // leaves the caret floating right. fitMini measures the SELECTED option's text and sets the
  // select width to it so the caret stays snug. Uses a shared hidden measuring span.
  let _measSpan = null;
  function fitMini(sel){ try {
    if (!sel) return; const opt = sel.options[sel.selectedIndex]; if (!opt) return;
    if (!_measSpan) { _measSpan=document.createElement('span'); _measSpan.style.cssText='position:absolute;visibility:hidden;white-space:nowrap;left:-9999px'; document.body.appendChild(_measSpan); }
    const cs=getComputedStyle(sel); _measSpan.style.font=cs.font; _measSpan.textContent=opt.textContent;
    const w=_measSpan.getBoundingClientRect().width;
    // DYNAMIC CARET: size the picker to the SELECTED name (text + pads + caret
    // gutter) AND pin that width as min-width with flex:0 0 auto — otherwise
    // #op-model's flex-shrink collapsed it below the measured width and the row
    // ellipsized the name ("Sonne…", 2026-07-21). The name now can't be
    // clipped; the effort picker (margin-left:auto) absorbs any row squeeze.
    const px = (w + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) + 1) + 'px';
    sel.style.width = px;
    sel.style.minWidth = px;
    sel.style.flex = '0 0 auto';
  } catch {} }
  // expose so the +/- zoom (applyScale, defined earlier) can RE-measure the
  // model picker after a font-scale change — otherwise the width pinned at the
  // old scale stayed fixed while the bigger text needed more room, clipping the
  // name at higher zooms (2026-07-21).
  window._opFitModel = () => { const m = document.getElementById('op-model'); if (m) fitMini(m); };
  const _modelSel = document.getElementById('op-model');
  const _effortSel = document.getElementById('op-effort');
  function syncEffort() {
    _effortSel.style.width = '';   // clear any prior fitMini width — effort auto-sizes + right-aligns
    const m = _modelSel.value || 'opus';
    const opts = EFFORT_BY_MODEL[m] || ["low", "medium", "high"];
    const prev = _effortSel.value;
    _effortSel.textContent = '';
    if (!opts.length) {
      // fixed-tier models: if the model name bakes in a tier e.g. 'Claude Opus 4.6 (Thinking)'
      // or 'GPT-OSS 120B (Medium)', SHOW that word in the effort picker but GRAYED OUT (disabled).
      const _mm = (m.match(/\(([^)]+)\)\s*$/)||[])[1];
      if (_mm) {
        _effortSel.hidden = false; _effortSel.disabled = true;
        const o=document.createElement('option'); o.value=_mm.toLowerCase(); o.textContent=_mm.toLowerCase(); _effortSel.appendChild(o); _effortSel.value=o.value;
        return;
      }
      _effortSel.hidden = true; return;   // truly no effort (e.g. Haiku): hide
    }
    _effortSel.disabled = false;
    _effortSel.hidden = false;
    opts.forEach(v => { const o=document.createElement('option');
      o.value=v; o.textContent = v; _effortSel.appendChild(o); });
    // default when nothing meaningful was chosen (prev blank/unavailable): GPT
    // models default to 'low' (the owner — GPT default is 5.6 Sol low), everything
    // else to 'medium'.
    if (prev && opts.includes(prev)) _effortSel.value = prev;
    else if (m.startsWith('gpt-') && opts.includes('low')) _effortSel.value = 'low';
    else if (opts.includes('medium')) _effortSel.value = 'medium';
  }
  function _flashMini(el){ if(!el || el.disabled) return; el.classList.remove('op-mini-swap'); void el.offsetWidth; el.classList.add('op-mini-swap'); }
  _modelSel.addEventListener('change', () => { syncEffort(); fitMini(_modelSel); _flashMini(_modelSel); _flashMini(_effortSel); saveSession(); });
  _effortSel.addEventListener('change', () => { _flashMini(_effortSel); saveSession(); });

  // ⌘L / Ctrl+L → focus the URL bar; ⌘K / Ctrl+K → focus the composer (v0.7.0,
  // browser idiom). Both swallow the browser default (⌘L would hijack OUR urlbar).
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.shiftKey || e.altKey) return;
    if (e.key === 'l' || e.key === 'L') { e.preventDefault();
      urlEl.focus(); urlEl.select(); }
    else if (e.key === 'k' || e.key === 'K') { e.preventDefault();
      const inp = document.getElementById('op-input'); if (inp) inp.focus(); }
  });

  // ⌘⇧E (mac) / Ctrl+Shift+E (win) → cycle the model picker
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'E' || e.key === 'e')) {
      e.preventDefault();
      const sel = document.getElementById('op-model');
      if (sel && sel.options.length) {
        sel.selectedIndex = (sel.selectedIndex + 1) % sel.options.length;
        sel.dispatchEvent(new Event('change'));
        // brief flash of the chosen model in the status sub
        if (typeof actSub !== 'undefined') {
          const lbl = sel.options[sel.selectedIndex].textContent;
          const prev = actSub.textContent; actSub.textContent = 'model → ' + lbl;
          setTimeout(() => { actSub.textContent = prev; }, 1200);
        }
      }
    }
  });

  // run once the model options have loaded
  setTimeout(syncEffort, 300);

  function selectedBot() { return caretSel.value || (caretSel.options[0] && caretSel.options[0].value) || 'claude-a'; }
  function setFollowUp(){
    if (MODE !== 'auto') { input.placeholder = 'You have control'; return; }
    // While an AUTO turn is live, the composer STEERS it (1.0.12) — the
    // message reaches the running agent without killing the run.
    input.placeholder = _inFlight ? 'Follow up' : 'Message Operator';
  }
  function applyMode() {
    // keep the chat fixed across AUTO⇄MAN: toggling the "Manual mode" banner changes
    // the rail height and reflows the log, shoving it up (the owner). Capture the log's
    // position before the change, restore it after the synchronous reflow.
    const _atBottom = (log.scrollHeight - log.scrollTop - log.clientHeight) < 24;
    const _fromBottom = log.scrollHeight - log.scrollTop;
    op.dataset.mode = MODE;   // drives the sliding thumb (orange MAN ⇄ green AUTO)
    modeBox.setAttribute('data-mode', MODE);   // drives the sliding thumb
    modeBox.querySelectorAll('.op-mode-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.mode === MODE));
    // re-trigger the subline fade/slide on every mode switch
    actSub.classList.remove('op-sub-swap'); void actSub.offsetWidth; actSub.classList.add('op-sub-swap');
    const inputbox = input.closest('.op-inputbox');
    const manNote = document.getElementById('op-man-note');
    if (MODE === 'auto') { if(pickWrap) pickWrap.hidden = false;
      if(caretSel) caretSel.style.pointerEvents = '';
      input.disabled = false; if(inputbox) inputbox.classList.remove('disabled');
      if(manNote) manNote.hidden = true;
      setFollowUp();
      { const f=document.getElementById('op-pick-face'); setPickFace(f, selectedBot()); }
      if (actTxt.textContent === 'Manual') actTxt.textContent = idleCardText();
      setCardSub(selectedBot()||'', 'idle'); }
    else { if(pickWrap) pickWrap.hidden = false;
      { const f=document.getElementById('op-pick-face'); if(f) f.textContent='🖥️'; }
      if(caretSel) caretSel.style.pointerEvents = 'none';   // no bot pick in manual
      input.disabled = true; input.value=''; input.placeholder = 'You have control';
      if(inputbox) inputbox.classList.add('disabled');
      if(manNote) manNote.hidden = false;
      // Finish-up hand-back: only when Operator kicked control to the user.
      // Preserve an OPEN expand across re-applies — the old blind reset
      // re-showed the trigger while the expand was open, so tapping Finish up
      // left two "Finish up" buttons on screen (2026-07-11). Trigger and
      // expand are mutually exclusive by construction now.
      { const fin=document.getElementById('op-finish'), exp=document.getElementById('op-finish-expand'),
            fbtn=document.getElementById('op-finish-btn');
        const open = !!(exp && !exp.hidden && _handedToUser);
        if (fin) fin.hidden = !_handedToUser;
        if (exp) exp.hidden = !open;
        if (fbtn) fbtn.hidden = open; }
      // clean manual state: no leftover done-checkmark / agent badge, just the
      // orange ring + "Manual". (a real page-load will set busy=1 → spinner.)
      op.dataset.agent = ''; op.dataset.busy = '0';
      actTxt.textContent = 'Ready'; setCardSub('', ''); }
    // restore the log position after the banner toggle reflows the rail
    void log.offsetHeight;   // force synchronous reflow so scrollHeight is current
    if (_atBottom) log.scrollTop = log.scrollHeight;
    else log.scrollTop = Math.max(0, log.scrollHeight - _fromBottom);
  }
  modeBox.addEventListener('click', e => { const b=e.target.closest('.op-mode-btn');
    if (b) { MODE = b.dataset.mode; _handedToUser = false;   // a MANUAL mode toggle (either dir) is never a hand-off → no Finish-up (only Take control sets it)
      applyMode(); saveSession(); } });
  // ── Finish-up hand-back flow (only present after Operator handed control to you) ──
  (function(){
    const finBtn=document.getElementById('op-finish-btn');
    const finExp=document.getElementById('op-finish-expand');
    const finGo=document.getElementById('op-finish-go');
    const finInput=document.getElementById('op-finish-input');
    if (finBtn) finBtn.addEventListener('click', ()=>{
      if (finExp) {
        // grow from collapsed → natural height (transition, not a snap):
        // unhide in the collapsed state, then release it next frame.
        finExp.classList.add('collapsed'); finExp.hidden = false;
        requestAnimationFrame(()=>requestAnimationFrame(()=>finExp.classList.remove('collapsed')));
      }
      finBtn.hidden = true;                       // hide the trigger so "Finish up" isn't duplicated
      if (finInput) setTimeout(()=>finInput.focus(), 200);   // after the ease, so focus doesn't jank it
    });
    // collapse the expand smoothly (ease height/opacity down, THEN hide) and
    // bring the trigger back — used by the outside-click dismiss below.
    function collapseFinish(){
      if (!finExp || finExp.hidden || finExp.classList.contains('collapsed')) return;
      finExp.classList.add('collapsed');
      setTimeout(()=>{ finExp.hidden = true; finExp.classList.remove('collapsed');
                       if (finBtn) finBtn.hidden = false; }, 330);
    }
    // tap/click anywhere outside the Finish-up block → minimize it back to the
    // trigger (2026-07-11). Capture-phase so popover handlers can't eat it.
    document.addEventListener('click', (e)=>{
      const fin = document.getElementById('op-finish');
      if (!fin || fin.hidden || !finExp || finExp.hidden) return;
      if (!fin.contains(e.target)) collapseFinish();
    }, true);
    function handBack(){
      const msg = (finInput && finInput.value.trim()) || '';
      _handedToUser = false;
      MODE = 'auto'; applyMode();
      logSys('Handed control to Operator', 'monitor');
      if (finInput) finInput.value='';
      saveSession();
      if (msg) { logUser(msg); dispatchTask(msg); }   // resume with the user's note
      else { dispatchTask('I\'m done — please continue from where you left off.'); }  // resume, no note
    }
    if (finGo) finGo.addEventListener('click', handBack);
    if (finInput) finInput.addEventListener('keydown', e=>{
      if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); handBack(); } });
  })();
  caretSel.addEventListener('change', () => { applyMode(); loadModels(selectedBot()); });
  applyMode();
  // hard-reset the Finish-up UI at load: hidden + collapsed, regardless of any cached/
  // restored state. It only appears after a live Take control (sets _handedToUser).
  { _handedToUser = false;
    const fin=document.getElementById('op-finish'), exp=document.getElementById('op-finish-expand'), fbtn=document.getElementById('op-finish-btn');
    if (fin) fin.hidden = true; if (exp) exp.hidden = true; if (fbtn) fbtn.hidden = false; }

  // poll the running agent → stream its reasoning into the chat
  let _agentSince = Date.now()/1000;
  const _seenMsg = new Set();
  let _agentPolling = false;
  let _lastAssistant = '';
  let _lastActionEmoji = '💭', _lastActionVerb = 'Thinking';
  let _runProgressTs = 0;
  let _runSawProgress = false;   // has THIS run emitted at least one progress msg yet?
  let _handledState = '';
  let _steerQueued = false;  // a soft steer is queued but not yet consumed by a delivery seam
  let _postSteerUntil = 0;
  let _steering = false;     // true from a steer until the NEW run is confirmed 'running' —
  // suppresses ALL terminal-state handling so the killed run's stale done/error can't
  // finish the fresh turn as "Worked for 0s" (the time-window guard alone raced and lost).
  let _handoffTs = 0;        // #4: ts of the handoff request we've already surfaced
  let _handoffActive = false; // a hand-off concluded THIS turn → don't also emit done/error
  let _sawRunning = false;   // did we observe this turn go 'running' in THIS page load?
  // ^ guards re-emitting a turn that COMPLETED before the page loaded: on refresh the
  // server still reports the last turn's terminal state, which would otherwise re-append
  // its reply (the "last 2 messages duplicate on every refresh" bug, the owner).
  let _errShown = false;     // one error card per turn — suppress the stacking (the owner)
  // show at most ONE error card per turn. A failing turn otherwise stacks 3-4:
  // the stderr 'error' message + the 120s watchdog + the 'error' state handler.
  // Prefer a specific reason; ignore generic follow-ups once one is shown.
  function turnError(title, reason){
    if (_errShown) return;
    _errShown = true;
    taskError(title, reason);
  }
  async function pollAgent() {
    if (MODE !== 'auto' || _agentPolling) return;   // guard re-entrancy
    _agentPolling = true;
    try {
      const d = await (await fetch(AGENT_URL + '?since=' + _agentSince)).json();
      let maxTs = _agentSince;
      const msgs = (d.messages||[]);
      msgs.forEach((m, i) => {
        const key = (m.role==='action' ? 'a:'+m.ts+':' : '') + (m.text||'').trim();
        maxTs = Math.max(maxTs, m.ts);
        if (_seenMsg.has(key)) return;
        _seenMsg.add(key);
        if (m.role === 'action') {
          taskActionStep(m.text, m.detail);
          taskVerb(actCont(m.text), true);
          _lastActionEmoji = actEmoji(m.text); _lastActionVerb = actCont(m.text);
        } else if (m.role === 'assistant') {
          // a genuine final-answer step; remember it — the LAST one becomes the reply bubble
          _lastAssistant = m.text;
          taskStep(m.text);
          taskVerb('Thinking', true);
        } else if (m.role === 'thinking') {
          // scratch reasoning (agy/gemma's per-step "thinking" field) -- show it live in
          // the trace same as 'assistant', but NEVER let it become the reply bubble: a
          // turn that ends (or is cut off mid-loop) without a real answer should fall
          // through to the "no summary" card below, not leak a raw work-summary/checklist
          // (2026-06-30, #37/#40).
          taskStep(m.text);
          taskVerb('Thinking', true);
        } else if (m.role === 'error') {
          const et = (m.text||'').trim();
          turnError('Turn failed', et || 'The agent ended the turn with an error.');   // one card/turn, specific msg preferred
        }
      });
      _agentSince = maxTs;
      if (msgs.length) { _runProgressTs = Date.now(); _runSawProgress = true; }   // any new msg = progress
      // 1.0.12 steer delivery notice: pending → 0 while still running means a
      // delivery seam consumed the queue (the hook injected it mid-loop, or
      // the exit seam started the follow-up turn).
      if (_steerQueued && d.steer_pending === 0) {
        _steerQueued = false;
        if (d.state === 'running') logEvent('Steer delivered to ' + (d.bot || 'the agent'), true);
      }
      // terminal states must be handled ONCE — not re-fired every poll (that
      // looped "agent stopped" after a stop). Reset when a fresh run starts.
      // While interrupting (user hit Stop), do NOT let a lingering 'running' poll from
      // the dying run re-arm the turn — that reset cleared the swallow guards and let the
      // killed run's trailing 'done' spawn a spurious extra card ("Cogitated for 1s"
      // after the Interrupted card). Ignore running polls until _interrupting clears.
      if (d.state === 'running' && _interrupting) { return; }
      if (d.state === 'running') { _handledState = ''; _handoffTs = 0; _errShown = false; _sawRunning = true; _steering = false; }
      // post-steer: until the NEW run is confirmed 'running' (above), swallow every
      // non-running poll — the killed run's stale done/error must not finish the fresh
      // turn as "Worked for 0s". Cleared the instant the new run reports running.
      if (_steering && d.state !== 'running') { return; }
      // A turn that finished BEFORE this page load (refresh): we never saw it run, so
      // don't re-emit its reply/handoff/error — the restored log already shows it.
      // Just mark the state handled and bail.
      if (d.state !== 'running' && !_sawRunning) { _handledState = d.state; return; }
      // #4 HAND-OFF: the agent emitted [[TAKE_CONTROL]] → surface the takeover card
      // (once per request) instead of the normal done/no-summary path.
      if (d.handoff && d.handoff.ts && d.handoff.ts !== _handoffTs) {
        _handoffTs = d.handoff.ts;
        _handoffActive = true;     // this turn ended in a hand-off — suppress its done/error tail
        if (_task) { try { finishTask(false, 'Stopped'); } catch(_){} }   // trace: "Stopped after Xs"
        renderHandoff(d.handoff.reason || '');
        op.dataset.busy='0'; op.dataset.agent='stopped'; setInFlight(false);   // stop-sign status icon
        setCardText(actTxt, 'Stopped'); setCardSub(d.bot||'', 'waiting for human');
        _handledState = d.state;   // swallow the done/error tail of this run
        return;
      }
      // a hand-off already concluded this turn → the agent ending as done/error is its
      // tail; don't also emit a reply / "no summary" / error card (the bug: the
      // "(no summary)" line appearing right under "Operator needs human input").
      if (_handoffActive && d.state !== 'running') { _handledState = d.state; return; }
      if (d.state !== 'running' && _handledState === d.state) { return; }
      // Swallow the STEERED (killed) run's trailing done/error until the NEW run
      // reaches 'running' (which clears _steering, line ~3237). The old guard
      // used only a 1500ms window (_postSteerUntil) which raced: if the killed
      // run's `done` landed after the window but before the new run started, it
      // fired finishTask() with the default "Worked for 1s" — an orphan card
      // alongside the "Steered after Xs" one (2026-07-21). _steering is the
      // real signal; the timer is only a belt-and-suspenders backstop now.
      if ((d.state === 'done' || d.state === 'error')
          && (_steering || Date.now() < _postSteerUntil)) { _handledState = d.state; return; }  // swallow the killed run's tail after a steer
      if (_interrupting && d.state !== 'running') { _handledState = d.state; return; }  // user hit Stop → swallow the killed run's stale done/error (no spurious "Worked for 0s")
      if (d.state !== 'running') _handledState = d.state;
      if (d.state === 'running') { op.dataset.busy='1'; setCardText(actTxt, 'Working');
        // soft stall (server signal, v1.1): the run's process is alive but the server
        // saw no progress heartbeat past the soft budget — surface it, don't kill it.
        // The HARD stall is handled server-side (auto-stop → state flips to error).
        if (d.stalled) { setCardSub(d.bot||'', 'quiet ' + Math.round(d.stalled_for||0) + 's — stalled? ■ stops it', '⏱'); }
        else {
          // 1.0.15 live token meter: the ledger's burn number, while it burns
          const _tk = (d.cum_in_tokens >= 1e6) ? (d.cum_in_tokens/1e6).toFixed(1) + 'M tok'
                    : (d.cum_in_tokens >= 1000) ? Math.round(d.cum_in_tokens/1000) + 'k tok' : '';
          setCardSub(d.bot||'', _lastActionVerb.toLowerCase() + (_tk ? ' · ' + _tk : ''), _lastActionEmoji);
        }
        op.dataset.agent='running'; setInFlight(true);
        if (!_runProgressTs) { _runProgressTs = Date.now(); _runSawProgress = false; }
        // watchdog: only declare a turn STALLED when the agent subprocess is actually
        // DEAD/wedged — never just because it's been silent. The old version tripped
        // on 120s of no new *message*, but a healthy agent legitimately goes quiet for
        // >2min: a long reasoning step, a slow page load, a cold start spinning up the
        // subprocess+MCP, or a natural pause mid-conversation. Those were all getting
        // false-killed with "the agent stalled" (the owner: happens mid-flight, not just at
        // start). The server now reports `alive` (subprocess poll()==None); we gate the
        // watchdog on it. A long timeout (8min) stays as a backstop for a process that's
        // alive but truly hung, so we never spin forever — but a working agent is never
        // killed for being quiet.
        const _dead = d.alive === false;            // subprocess gone but state stuck "running" → real crash
        const _hung = Date.now() - _runProgressTs > 480000;  // 8min backstop: alive but wedged
        if ((_dead && Date.now() - _runProgressTs > 15000) || _hung) {
          op.dataset.busy='0'; setCardText(actTxt, 'Failed');
          setCardSub(d.bot||'', 'stalled'); op.dataset.agent='errored';
          turnError('Turn failed', 'The agent stopped responding and was ended.'); finishTask(true); setInFlight(false);
          try { fetch(STOP_URL, {method:'POST'}); } catch {}
          _runProgressTs = 0;
        }
      }
      else if (d.state === 'done') { op.dataset.busy='0'; setCardText(actTxt, 'Done');
        _lastActionEmoji='💭'; _lastActionVerb='Thinking';   // clear stale live verb
        setCardSub(selectedBot()||'', 'idle'); op.dataset.agent='done';   // idle shows the SELECTED bot, not whoever last ran
        // promote the final reply into a markdown bubble. Prefer the incrementally
        // captured one; fall back to the server's authoritative `final` (covers the
        // case where the message flushed together with turn-end — gpt/codex does
        // this, which is why a "Spelunked 19s" turn could end with no reply shown).
        const reply = _lastAssistant || (d.final || '').trim();
        // don't re-append a reply that's already the last bot bubble (e.g. after a
        // refresh restores the log, or repeated 'done' polls) — that caused the
        // message to duplicate on every refresh.
        const _lastBubble = log.querySelector('.op-msg.bot:last-of-type .bubble');
        const _alreadyShown = _lastBubble && _lastBubble.textContent.trim() === reply.trim();
        if (reply && !_alreadyShown) {
          if (_task) { const steps=_task.querySelectorAll('.op-task-step');
            const lastStep = steps[steps.length-1];
            if (lastStep && !lastStep.classList.contains('op-act-step')
                && lastStep.textContent.trim() === reply.trim()) lastStep.remove(); }
          finishTask();
          logBotReply(reply);
          _lastAssistant = '';
        } else {
          // genuinely no final answer from the agent — say so rather than going silent
          finishTask();
          logBotReply('_(done — the agent acted but returned no summary. ask it to recap, or try again.)_');
        }
        _runProgressTs = 0;   // like interrupted/error: never leak this run's stamp into the next turn's dead-run watchdog
        setInFlight(false); }
      else if (d.state === 'interrupted') {           // USER hit stop → clean interrupt, NOT an error
        // server SIGTERM'd the agent (exit -15). Finalize the turn as "Interrupted
        // after Xs" — no red error card, no extra done-verb. (Unless a steer is mid-
        // flight, in which case submit() already closed it as "Steered".)
        if (!_interrupting && !_steering) {
          op.dataset.busy='0'; setCardText(actTxt, 'Ready');
          _lastActionEmoji='💭'; _lastActionVerb='Thinking';
          setCardSub(selectedBot()||'', 'idle'); op.dataset.agent='';
          if (_task) { try { finishTask(false, 'Interrupted'); } catch(_){} }
        }
        _runProgressTs = 0; setInFlight(false); }
      else if (d.state === 'error') {                 // agent died/gave up → don't hang
        if (!_interrupting) {
          op.dataset.busy='0'; setCardText(actTxt, 'Failed');
          setCardSub(d.bot||'', 'failed'); op.dataset.agent='errored';
          turnError('Turn failed', (d && d.detail) ? d.detail : 'The agent ended the turn with an error.'); finishTask(true);
        }
        _runProgressTs = 0; setInFlight(false); }
      else if (d.state === 'idle') { setInFlight(false); }
    } catch {} finally { _agentPolling = false; }
  }
  setInterval(pollAgent, 800);   // snappier trace (esp. gemma/agy, whose steps surface via polling not streaming)
  // orphan-spinner watchdog: nothing in flight but a task still spinning → finish it.
  setInterval(() => {
    if (!_inFlight && _task) { try { finishTask(); } catch(_){} }
    if (!_inFlight) { const z=log.querySelector('.op-task[data-busy="1"]');
      if (z) { z.dataset.busy='0'; } }
  }, 2000);

  // poll who's actually driving (from the action-tap) → live status + event trail
  let _lastEvTs = Date.now()/1000;


  // urlbar reload + hamburger items → act(); hamburger open/close; tabs toggle
  document.querySelectorAll('.op-ubtn[data-kind], .op-ham-item[data-kind]').forEach(b =>
    b.addEventListener('click', () => act({kind:b.dataset.kind, value:b.dataset.value||''}, null, true)));  // silent: no status flash
  const ham = document.getElementById('op-ham-menu');
  const hamWrap = document.getElementById('op-ham');
  const hamBtn = document.getElementById('op-ham-btn');
  function closeHam() { ham.hidden = true; }
  function openHam() {
    const r = hamBtn.getBoundingClientRect();
    // anchor the fixed menu just under the button, right-aligned to it
    const top = r.bottom + 4;
    ham.style.top = top + 'px';
    ham.style.left = 'auto';
    ham.style.right = (window.innerWidth - r.right) + 'px';
    // cap to the space actually below the button so a tall menu (high zoom /
    // short viewport) scrolls inside itself instead of running off the bottom
    ham.style.maxHeight = Math.max(160, (window.innerHeight - top - 10)) + 'px';
    ham.hidden = false;
  }
  hamBtn.addEventListener('click', e => {
    e.stopPropagation(); e.preventDefault();
    if (ham.hidden) openHam(); else closeHam(); });
  // outside click (capture phase so it always fires first) closes it
  document.addEventListener('click', (e) => {
    if (!ham.hidden && !hamWrap.contains(e.target)) closeHam(); }, true);
  // picking a menu item runs its action then closes
  ham.querySelectorAll('.op-ham-item').forEach(it =>
    it.addEventListener('click', () => {
      if (it.dataset.kind === 'copy_url') {
        const u = (document.getElementById('op-url')||{}).value || '';
        if (u && navigator.clipboard) navigator.clipboard.writeText(u);
        logEvent('Copied URL', true);
      }
      if (it.dataset.kind === 'reset_view') logEvent('Cleared stuck zoom', true);
      closeHam();
    }));

  // ── settings popover (v0.7.0): ham → Settings. Each row applies live and
  //    persists to localStorage; restored at init below. ──
  (function(){
    const pop = document.getElementById('op-settings');
    const item = document.getElementById('op-ham-settings');
    if (!pop || !item) return;
    const fontSel = document.getElementById('op-set-font');
    const hintCb  = document.getElementById('op-set-hint');
    const glideCb = document.getElementById('op-set-glide');
    const K_FONT = 'operator-chatfont-v1', K_HINT = 'operator-kbhint-v1', K_GLIDE = 'operator-glide-v1';
    function applyAll(){
      op.style.setProperty('--chat-font', fontSel.value);
      op.classList.toggle('op-no-hint', !hintCb.checked);
      op.classList.toggle('op-no-glide', !glideCb.checked);
    }
    function persist(){
      try { localStorage.setItem(K_FONT, fontSel.value);
        localStorage.setItem(K_HINT, hintCb.checked ? '1' : '0');
        localStorage.setItem(K_GLIDE, glideCb.checked ? '1' : '0'); } catch {}
    }
    try {
      const f = localStorage.getItem(K_FONT);
      if (f && [].some.call(fontSel.options, o => o.value === f)) fontSel.value = f;
      hintCb.checked  = localStorage.getItem(K_HINT)  !== '0';
      glideCb.checked = localStorage.getItem(K_GLIDE) !== '0';
    } catch {}
    applyAll();
    [fontSel, hintCb, glideCb].forEach(el =>
      el.addEventListener('change', () => { applyAll(); persist(); }));
    item.addEventListener('click', e => {
      e.stopPropagation();
      const r = hamBtn.getBoundingClientRect();
      pop.style.top = (r.bottom + 4) + 'px';
      pop.style.left = 'auto';
      pop.style.right = (window.innerWidth - r.right) + 'px';
      pop.hidden = false;
    });
    document.addEventListener('click', e => {
      if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true; }, true);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !pop.hidden) pop.hidden = true; });
  })();

  // ── history popover (1.0.11): ham → History. The flight recorder's ledger —
  //    every finished run with who/where/how-it-ended/token spend. ──
  (function(){
    const HISTORY = OP_URLS.history_list;
    const pop = document.getElementById('op-history');
    const item = document.getElementById('op-ham-history');
    if (!pop || !item) return;
    const listEl = document.getElementById('op-hist-list');
    function fmtWhen(ts){ if (!ts) return '';
      const d = new Date(ts * 1000);
      return d.toLocaleDateString(undefined, {month:'short', day:'numeric'})
        + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'}); }
    function fmtDur(s){ if (s == null) return '';
      return s < 90 ? Math.round(s) + 's' : Math.round(s / 60) + 'm'; }
    function fmtTok(n){ if (!n) return '';
      return n >= 1e6 ? (n / 1e6).toFixed(1) + 'M tok'
           : n >= 1000 ? Math.round(n / 1000) + 'k tok' : n + ' tok'; }
    async function loadHistory(){
      listEl.textContent = 'loading…';
      try {
        const j = await (await fetch(HISTORY + '?limit=40', {cache:'no-store'})).json();
        if (!j || !j.ok) throw 0;
        listEl.textContent = '';
        if (!j.runs.length) { listEl.textContent = 'No runs recorded yet.'; return; }
        j.runs.forEach(run => {
          const row = document.createElement('div'); row.className = 'op-hist-row';
          const top = document.createElement('div'); top.className = 'top';
          const task = document.createElement('span'); task.className = 'task';
          task.textContent = run.task || '(no task)'; task.title = run.task || '';
          const st = document.createElement('span');
          st.className = 'st st-' + (run.state || 'done');
          st.textContent = run.reason || run.state || '';
          top.appendChild(task); top.appendChild(st);
          // ↻ run again (1.0.13): re-dispatch with the ROW's bundle (bot/
          // model/effort/surface), not the current pickers. desktop-real is
          // never auto-fired — it keeps its explicit consent flow.
          if (run.task) {
            const re = document.createElement('button'); re.type = 'button';
            re.className = 'op-hist-rerun'; re.title = 'Run again';
            re.setAttribute('aria-label', 'run again'); re.textContent = '↻';
            re.addEventListener('click', (ev) => {
              ev.stopPropagation();
              if (_inFlight) { logEvent('agent is busy — stop or steer first', false); return; }
              pop.hidden = true;
              if (MODE !== 'auto') { MODE = 'auto'; try { applyMode(); } catch(_){} }
              if (run.surface === 'desktop-real') {
                input.value = run.task; autoGrow(); refreshSendButton(); input.focus();
                logEvent('desktop-real needs explicit confirm — review and send', false);
                return;
              }
              logUser('↻ ' + run.task);
              dispatchTask(run.task, {bot: run.bot || '', model: run.model || '',
                                      effort: run.effort || '', surface: run.surface || ''});
            });
            top.appendChild(re);
          }
          const meta = document.createElement('div'); meta.className = 'meta';
          meta.textContent = [fmtWhen(run.started_ts), run.bot, run.surface,
                              fmtDur(run.duration_s), fmtTok(run.cum_in_tokens)]
                             .filter(Boolean).join(' · ');
          row.appendChild(top); row.appendChild(meta);
          // 1.0.15 detail view: click the row → inline trace panel (lazy,
          // fetched once). The ↻ button stopPropagation()s past this.
          row.addEventListener('click', async () => {
            const open = row.querySelector('.op-hist-trace');
            if (open) { open.remove(); return; }
            const tr = document.createElement('div'); tr.className = 'op-hist-trace';
            tr.textContent = 'loading…'; row.appendChild(tr);
            try {
              const dj = await (await fetch(OP_URLS.history_get.replace('/0', '/' + run.id),
                                            {cache:'no-store'})).json();
              if (!dj || !dj.ok) throw 0;
              tr.textContent = '';
              (dj.run.trace || []).forEach(m => {
                const ln = document.createElement('div');
                ln.className = 'ln ln-' + (m.role || 'assistant');
                const tag = m.role === 'action' ? '▸' : m.role === 'user' ? '›'
                          : m.role === 'error' ? '⚠' : '·';
                ln.textContent = tag + ' ' + (m.text || '') +
                                 (m.detail ? ' — ' + m.detail : '');
                tr.appendChild(ln);
              });
              if (!tr.children.length) tr.textContent = '(no trace recorded)';
            } catch(_) { tr.textContent = 'trace unavailable'; }
          });
          listEl.appendChild(row);
        });
      } catch(_) { listEl.textContent = 'History unavailable.'; }
    }
    item.addEventListener('click', e => {
      e.stopPropagation();
      const r = hamBtn.getBoundingClientRect();
      pop.style.top = (r.bottom + 4) + 'px';
      pop.style.left = 'auto';
      pop.style.right = (window.innerWidth - r.right) + 'px';
      pop.style.maxHeight = Math.max(200, window.innerHeight - r.bottom - 16) + 'px';
      pop.hidden = false;
      loadHistory();
    });
    document.addEventListener('click', e => {
      if (!pop.hidden && !pop.contains(e.target)) pop.hidden = true; }, true);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !pop.hidden) pop.hidden = true; });
  })();
  const tabsToggle = document.getElementById('op-tabs-toggle');
  tabsToggle.addEventListener('click', () => {
    const t = document.getElementById('op-tabs'); t.hidden = !t.hidden;
    tabsToggle.classList.toggle('active', !t.hidden); });

  // ── browser tab strip (mirror the Chrome's open tabs) ──
  const TABS_URL = OP_URLS.tabs;
  const tabsEl = document.getElementById('op-tabs');
  let _dragTab = null;
  // ── favicon hover popover (v0.7.0): one floating card per cockpit, repositioned
  //    under whichever tab-favicon is hovered. Pointer-events:none so it can never
  //    steal the hover; a poll rebuild while shown just leaves it to fade on leave.
  let _favPop = null, _favPopHide = null;
  function _ensureFavPop(){
    if (_favPop) return _favPop;
    _favPop = document.createElement('div'); _favPop.className = 'op-favpop';
    _favPop.innerHTML = '<img alt=""><span class="fp-txt"><span class="fp-t"></span><span class="fp-h"></span></span>';
    document.querySelector('.op-browser').appendChild(_favPop);
    return _favPop;
  }
  function showFavPop(anchor, host, title){
    const pop = _ensureFavPop();
    if (_favPopHide) { clearTimeout(_favPopHide); _favPopHide = null; }
    pop.querySelector('img').src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=64';
    pop.querySelector('.fp-t').textContent = title;
    pop.querySelector('.fp-h').textContent = host;
    const br = document.querySelector('.op-browser').getBoundingClientRect();
    const ar = anchor.getBoundingClientRect();
    pop.style.top = (ar.bottom - br.top + 7) + 'px';
    // clamp so the card never spills past the browser's right edge
    const left = Math.max(6, Math.min(ar.left - br.left - 8, br.width - 330));
    pop.style.left = left + 'px';
    pop.classList.add('show');
  }
  function hideFavPop(){
    if (!_favPop) return;
    _favPop.classList.remove('show');
    _favPopHide = setTimeout(() => { if (_favPop) _favPop.classList.remove('show'); }, 180);
  }
  async function pollTabs() {
    try {
      const d = await (await fetch(TABS_URL)).json();
      const tabs = d.tabs || [];
      // rebuild only if changed (avoid clobbering on every poll)
      if (_dragTab) return;   // don't rebuild mid-drag
      const sig = tabs.map(t => t.i + (t.active?'*':'') + t.title).join('|');
      if (tabsEl._sig === sig) return; tabsEl._sig = sig;
      tabsEl.textContent = '';
      tabs.forEach(t => {
        const el = document.createElement('div');
        el.className = 'op-tab' + (t.active ? ' active' : '');
        // drag-to-reorder, POINTER-based (the old HTML5 draggable never fired on
        // touch — iPad is a primary driver). Pointer capture engages AT pointer-
        // down (not after the slop threshold): un-captured pointermove events
        // fire on whatever is under the pointer, so a fast drag would escape the
        // tab and never reach this listener. With capture, tap-vs-drag is decided
        // here too — the tab SWITCH happens on a no-drag pointerup instead of a
        // label click (capture retargets the composed click unreliably across
        // browsers). Reorder is the strip's own (visual) order; Chrome's real
        // tab order is untouched. The ✕ keeps its own click path (no capture).
        el.addEventListener('pointerdown', e => {
          if (e.target.closest('.op-tab-x')) return;      // the ✕ is not a handle
          if (e.button !== undefined && e.button !== 0) return;
          try { el.setPointerCapture(e.pointerId); } catch {}
          const startX = e.clientX, startY = e.clientY;
          let dragging = false;
          const move = ev => {
            if (!dragging) {
              if (Math.abs(ev.clientX - startX) < 6 && Math.abs(ev.clientY - startY) < 6) return;
              dragging = true; _dragTab = el; el.classList.add('dragging');
            }
            // classic list-reorder: insert before the first sibling whose midpoint
            // is right of the pointer; past every midpoint → land at the end
            // (before the + button). Covers exact-center drops and drops beyond
            // the last tab — the earlier inside-a-rect-only version missed both.
            let placed = false;
            for (const sib of el.parentNode.querySelectorAll('.op-tab')) {
              if (sib === el) continue;
              const r = sib.getBoundingClientRect();
              if (ev.clientX < r.left + r.width/2) {
                if (el.nextSibling !== sib) el.parentNode.insertBefore(el, sib);
                placed = true; break;
              }
            }
            if (!placed) {
              const add = el.parentNode.querySelector('.op-tab-add');
              if (add && el.nextSibling !== add) el.parentNode.insertBefore(el, add);
            }
          };
          const done = async ev => {
            el.removeEventListener('pointermove', move);
            el.removeEventListener('pointerup', done);
            el.removeEventListener('pointercancel', cancel);
            try { el.releasePointerCapture(e.pointerId); } catch {}
            if (dragging) { el.classList.remove('dragging'); _dragTab = null; return; }
            // plain tap → switch to this tab and collapse the strip
            try { await fetch(OP_URLS.tab_switch.replace('/0','/'+t.i), {method:'POST'}); pollTabs(); } catch {}
            const tb=document.getElementById('op-tabs'); tb.hidden=true;
            tabsToggle.classList.remove('active');
          };
          const cancel = () => {
            el.removeEventListener('pointermove', move);
            el.removeEventListener('pointerup', done);
            el.removeEventListener('pointercancel', cancel);
            try { el.releasePointerCapture(e.pointerId); } catch {}
            if (dragging) { el.classList.remove('dragging'); _dragTab = null; }
          };
          el.addEventListener('pointermove', move);
          el.addEventListener('pointerup', done);
          el.addEventListener('pointercancel', cancel);
        });
        // favicon (#5): derived from the tab's url via Google's s2 endpoint —
        // no backend change; a failed load just removes the img (text-only tab).
        if (/^https?:\/\//i.test(t.url || '')) {
          try {
            const host = new URL(t.url).hostname;
            const fv = document.createElement('img'); fv.className = 'op-tab-fav'; fv.alt = '';
            fv.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=32';
            fv.addEventListener('error', () => fv.remove());
            // hover the favicon → big-icon + page-title popover (v0.7.0)
            fv.addEventListener('mouseenter', () => showFavPop(fv, host, t.title || host));
            fv.addEventListener('mouseleave', hideFavPop);
            el.appendChild(fv);
          } catch {}
        }
        const l = document.createElement('span'); l.className='lbl'; l.textContent = t.title || 'tab';
        const x = document.createElement('span'); x.className='op-tab-x';
        x.innerHTML = '<svg viewBox="0 0 14 14" width="9" height="9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7"></path></svg>';
        x.addEventListener('click', async (e) => { e.stopPropagation();
          el.classList.add('op-tab-closing');   // play the close-out animation first
          setTimeout(async () => {
            try { await fetch(OP_URLS.tab_close.replace('/0','/'+t.i), {method:'POST'}); pollTabs(); } catch {}
          }, 190);
        });
        el.appendChild(l); el.appendChild(x);
        tabsEl.appendChild(el);
      });
      const add = document.createElement('button'); add.className='op-tab-add';
      add.innerHTML='<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M8 3.5v9M3.5 8h9"></path></svg>';
      add.title='new tab';
      add.addEventListener('click', async () => {
        try { await fetch(OP_URLS.tab_new, {method:'POST'}); pollTabs(); } catch {}
      });
      tabsEl.appendChild(add);
    } catch {}
  }
  pollTabs(); setInterval(pollTabs, 2500);

  // ── agent cursor: map a normalized click onto the displayed (letterboxed) frame ──
  const _agentCursor = document.getElementById('op-agent-cursor');
  let _lastClickT = 0;
  function showAgentClick(c){
    if (!_agentCursor || !c) return;
    if (c.t === _lastClickT) return;   // de-dupe (server uses age, not id) — guard by pos+age
    const r = stage.getBoundingClientRect();
    const nW = view.naturalWidth, nH = view.naturalHeight;
    if (!nW || !nH) return;
    // invert object-fit:contain — the frame is centered + scaled inside the stage
    const sc = Math.min(r.width / nW, r.height / nH);
    const dW = nW * sc, dH = nH * sc;
    const ox = (r.width - dW) / 2, oy = (r.height - dH) / 2;
    const px = ox + c.x * dW, py = oy + c.y * dH;
    _agentCursor.classList.add('show');
    // glide to the new point (CSS transition animates left/top smoothly)
    _agentCursor.style.left = px + 'px';
    _agentCursor.style.top = py + 'px';
    // the press-dot fires AFTER the glide finishes, so it marks where it landed
    clearTimeout(_agentCursor._press);
    _agentCursor._press = setTimeout(() => {
      _agentCursor.classList.remove('press'); void _agentCursor.offsetWidth; _agentCursor.classList.add('press');
      setTimeout(()=>_agentCursor.classList.remove('press'), 460);
    }, 520);   // ≈ the glide duration
    // keep the cursor visible while the agent is active; fade only after idle
    clearTimeout(_agentCursor._fade);
    _agentCursor._fade = setTimeout(()=>_agentCursor.classList.remove('show'), 5000);
  }
  // MANUAL-steer local cursor: snap a client-drawn pointer to normalized coords
  // the INSTANT you act, so input feels immediate even while the video feed (the
  // ground-truth ffmpeg-drawn pointer) is still catching up over the wire. Only
  // meaningful on a desktop surface in MAN mode; CSS hides it otherwise.
  const _steerCursor = document.getElementById('op-steer-cursor');
  function steerCursorAt(nx, ny){
    if (!_steerCursor || _surfaceActive === 'browser' || MODE !== 'man') return;
    if (typeof nx !== 'number' || typeof ny !== 'number') return;
    const r = stage.getBoundingClientRect();
    const nW = view.naturalWidth, nH = view.naturalHeight;
    if (!nW || !nH) return;
    // invert object-fit:contain — same mapping as the agent cursor
    const sc = Math.min(r.width / nW, r.height / nH);
    const dW = nW * sc, dH = nH * sc;
    const ox = (r.width - dW) / 2, oy = (r.height - dH) / 2;
    _steerCursor.style.left = (ox + nx * dW) + 'px';
    _steerCursor.style.top  = (oy + ny * dH) + 'px';
    _steerCursor.classList.add('show');
    clearTimeout(_steerCursor._fade);
    // hold it while you're interacting; fade after a short idle so it doesn't
    // linger as a false pointer once the feed has caught up
    _steerCursor._fade = setTimeout(() => _steerCursor.classList.remove('show'), 2500);
  }
  let _coldSince = 0;
  async function poll() {
    try { const d = await (await fetch(STATUS)).json();
      // another tab (or a dispatch) may have switched the surface — stay in sync
      if (d.surface && d.surface !== _surfaceActive) {
        _surfaceActive = d.surface; applySurfaceState();
        if (!surfPop.hidden) renderSurfacePop();
      }
      if (d.url) showUrl(d.url);   // reflect the live page URL (incl. agent navigation)
      if (d.click) showAgentClick(d.click);   // draw the agent cursor where it clicked
      if (d.has_frame) { setState('live', ''); _coldSince = 0; _desktopNoFrame = false;
        _pollCold = false;   // poll authority: feed is healthy — loads may clear again
        // server healthy → if we were showing SIGNAL LOST, reconnect ONCE to
        // resume real frames, then trust the open MJPEG connection (no per-poll
        // reconnect — that caused the ~1/sec flicker).
        if (_wasLost) { _wasLost = false; connectStream(); }
        signalOk();
      }
      else if (d.status === 'error') { _desktopNoFrame = false;
        setState('error', 'disconnected');
        if (!_pollCold) { _pollCold = true; signalLost(); }   // transition-gated: assert once per cold spell
        _coldSince = _coldSince||Date.now(); }
      else if (_isGameSurface() && op.dataset.busy !== '1') {
        _desktopNoFrame = true;
        _pollCold = false;   // rest state, not a cold spell — desktopIdle owns the stage
        // Desktop surface, idle, no frame yet = the virtual desktop hasn't
        // started (the agent's first task boots it). This is a normal resting
        // state, NOT a lost signal — show it immediately and deterministically
        // off the server's has_frame, no 6s cold wait, no alarming glyph.
        desktopIdle();
      }
      else {
        _desktopNoFrame = false;
        if (!_coldSince) _coldSince = Date.now();
        const coldMs = Date.now() - _coldSince;
        // brief gaps: the server heartbeats the last frame, so stay as-is (no
        // hide/blink). Only a sustained 6s drop is a real SIGNAL LOST.
        if (coldMs >= 6000) {
          if (!_pollCold) {   // transition-gated: one assert per cold spell, not per poll
            _pollCold = true;
            signalLost();
            setState('connecting', 'reconnecting…');
          }
          connectStream();   // idempotent nudge — the pump self-retries
        }
      }
    } catch { if (!_coldSince) _coldSince = Date.now();
      if (Date.now()-_coldSince >= 6000) setState('error','disconnected'); }
  }
  const _sess = restoreSession();
  // restore the mode IMMEDIATELY (not in the 400ms timeout) so an early saveSession
  // can't overwrite the stored mode with the default before it's read.
  if (_sess && (_sess.mode === 'auto' || _sess.mode === 'man')) {
    MODE = _sess.mode;
    // paint the restored mode WITHOUT the slide animation (it's a restore, not a toggle)
    if (modeBox) { modeBox.classList.add('op-mode-noanim'); modeBox.setAttribute('data-mode', MODE); }
    requestAnimationFrame(() => { if (modeBox) modeBox.classList.remove('op-mode-noanim'); });
  }
  // ── adopt the server session when it's ahead of this device's cache: a
  // fresh device (_srev 0) adopts any server copy; a returning device adopts
  // only revisions other devices wrote. Local paint above stays instant —
  // this swaps it a beat later only when the server actually knows more. ──
  (async () => { try {
    const r = await fetch(SESSION, {cache: 'no-store'});
    const j = await r.json();
    if (!j || !j.ok || !j.data || j.rev === _srev) return;
    const d = j.data;
    _srev = j.rev;
    if (d.log) {
      log.innerHTML = d.log; log.scrollTop = log.scrollHeight; updateJump();
      // same dead-listener hygiene as restoreSession (innerHTML loses handlers)
      log.querySelectorAll('.op-handoff').forEach(c => c.remove());
      log.querySelectorAll('.op-copy').forEach(c => c.remove());
      if (typeof _addCopyButtons === 'function') log.querySelectorAll('.op-msg.bot .bubble').forEach(_addCopyButtons);
      if (typeof _markLastUser === 'function') _markLastUser();
    }
    if (d.mode === 'auto' || d.mode === 'man') {
      MODE = d.mode;
      if (modeBox) { modeBox.classList.add('op-mode-noanim'); modeBox.setAttribute('data-mode', MODE); }
      requestAnimationFrame(() => { if (modeBox) modeBox.classList.remove('op-mode-noanim'); });
      if (typeof applyMode === 'function') applyMode();
    }
    if (d.bot) { const c = document.getElementById('op-action-caret');
      if (c) { c.value = d.bot; await loadModels(selectedBot()); } }
    if (d.model) { const c = document.getElementById('op-model');
      if (c && [].some.call(c.options, o => o.value === d.model)) c.value = d.model;
      if (typeof syncEffort === 'function') syncEffort(); }
    if (d.effort) { const c = document.getElementById('op-effort');
      if (c && [].some.call(c.options, o => o.value === d.effort)) c.value = d.effort; }
    // cache the adopted copy locally WITH its rev — and no re-push (nothing new)
    try { localStorage.setItem(LS_KEY, JSON.stringify(Object.assign({_srev: _srev}, _sessionPayload()))); } catch {}
  } catch(_){} })();
  setTimeout(async () => { try {
    // Restore the bot FIRST, then reload the model list for THAT bot — otherwise the
    // picker keeps the default (Claude) models even after the bot is set to gpt, so
    // it goes blank (the saved gpt model isn't in the Claude option list). Reloading
    // repopulates with gpt-5.x and re-applies _sess.model inside loadModels.
    if (_sess.bot) {
      const c=document.getElementById('op-action-caret');
      if (c) { c.value=_sess.bot; await loadModels(selectedBot()); }
    }
    if (_sess.model) { const c=document.getElementById('op-model');
      if (c && [].some.call(c.options,o=>o.value===_sess.model)) c.value=_sess.model;
      if (typeof syncEffort==='function') syncEffort(); }
    if (_sess.effort) { const c=document.getElementById('op-effort');
      if (c && [].some.call(c.options,o=>o.value===_sess.effort)) c.value=_sess.effort; }
    if (typeof applyMode === 'function') { MODE = (_sess.mode === 'auto' ? 'auto' : (_sess.mode === 'man' ? 'man' : MODE)); applyMode(); }
  } catch {} }, 400);
  poll(); setInterval(poll, 1500);

  // ── send an action ──
  async function act(action, userLabel, silent) {
    if (userLabel) logUser(userLabel);
    if (!silent) setAction(action.kind, action.value || '', true);
    // draw the local steer cursor at this action's target the moment it fires —
    // instant feedback that doesn't wait on the video feed's round-trip. Prefer
    // the endpoint of a drag (x1,y1), else the point (x,y).
    if (action && (action.kind === 'click_at' || action.kind === 'rclick_at'
        || action.kind === 'dblclick_at' || action.kind === 'move'
        || action.kind === 'mousedown_at' || action.kind === 'mouseup_at'
        || action.kind === 'drag')) {
      if (action.kind === 'drag') steerCursorAt(action.x1, action.y1);
      else steerCursorAt(action.x, action.y);
    }
    const _rl = document.getElementById('op-reload');
    const _dot = document.getElementById('op-dotstat');
    const _hl = document.getElementById('op-nav-hairline');
    const _isNav = action && (action.kind === 'goto' || action.kind === 'reload' || action.kind === 'hard_reload' || action.kind === 'back' || action.kind === 'forward');
    if (_isNav) {
      if (_rl) _rl.classList.add('loading');
      if (_dot) { _dot.classList.remove('err'); _dot.classList.add('loading'); }
      if (_hl) _hl.classList.add('on');
    }
    const _navDone = ok => {
      if (!_isNav) { if (!ok && _dot) _dot.classList.add('err'); return; }
      if (_rl) _rl.classList.remove('loading');
      if (_hl) _hl.classList.remove('on');
      if (_dot) { _dot.classList.remove('loading'); _dot.classList.toggle('err', !ok); }
    };
    try {
      const d = await (await fetch(STEER, { method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify(action) })).json();
      if (d.ok) { showUrl(d.url);
        settleAction(true); }
      else { if (!silent) logEvent((d.error||'failed').slice(0,80), false); settleAction(false); }
      _navDone(!!d.ok);
      return d;
    } catch { logRes('Action failed — browser disconnected', false); settleAction(false);
      _navDone(false); }
  }

  function parse(t){ t=t.trim(); if(!t) return null;
    if(/^https?:\/\//i.test(t)||/^[\w-]+(\.[\w-]+)+/.test(t)) return {kind:'goto',value:t};
    let m=t.match(/^(navigate to|navigate|go to|goto|visit|open|click|type|press)\s+(.+)/i);
    if(m){ const v=m[1].toLowerCase(), rest=m[2].trim();
      if(v==='click') return {kind:'click',value:rest};
      if(v==='type')  return {kind:'type', value:rest};
      if(v==='press') return {kind:'key',  value:rest};
      const url=/\.[a-z]{2,}/i.test(rest)?rest:rest.replace(/\s+/g,'')+'.com';
      return {kind:'goto',value:url}; }
    if(/^[\w-]+$/.test(t)) return {kind:'goto',value:t+'.com'};
    // a bare multi-word phrase/question isn't a browser command — don't type it
    // blindly into the page; let submit() hint the user toward AUTO mode.
    if(/\s/.test(t)) return {kind:'_ambiguous', value:t};
    return {kind:'type',value:t};
  }
  function drainQueue(){
    if (_inFlight || !_queue.length || MODE!=='auto') return;
    const next = _queue.shift();
    dispatchTask(next);
  }
  async function dispatchTask(txt, opts){
    // opts (1.0.13): explicit bot/model/effort/surface overrides — the History
    // "run again" path re-dispatches with the ROW's bundle, not the pickers'.
    const o = opts || {};
    const bot = o.bot || selectedBot();
    // Game (Track C): fold the picked game into the task so the agent passes
    // map:"<name>" to perceive/game_macro. Works on any surface. No host state.
    if (_activeMap) {
      txt = '[Active game map: "' + _activeMap + '" — pass map:"' + _activeMap
          + '" to your perceive and game_macro tool calls so perception is scoped '
          + 'to this game.]\n\n' + txt;
    }
    // slash palette (#30 v2): remember the last dispatched bundle so "/save"
    // can store it as a re-runnable task (the draft is gone by save time).
    window._opLastDispatch = { task: txt, bot,
      model: (document.getElementById('op-model')||{}).value || '',
      effort: (document.getElementById('op-effort')||{}).value || '' };
    // if the user declined a hand-off and just sent a message, dismiss the lingering
    // "Operator needs human input" card and let the agent carry on (its session
    // resumes, so the new message continues where it left off).
    log.querySelectorAll('.op-handoff').forEach(c => c.remove());
    _handoffTs = 0; _handoffActive = false; _errShown = false; op.dataset.agent='';
    op.dataset.busy='1'; setCardText(actTxt, 'Starting up');
    setCardSub(bot, '');
    try { const d = await (await fetch(DISPATCH, { method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({bot, task:txt, model: o.model || (document.getElementById('op-model')||{}).value||'', effort: o.effort || (document.getElementById('op-effort')||{}).value||'',
        surface: o.surface || _surfaceActive, real_ok: (_surfaceActive==='desktop-real' && _realOk)}) })).json();
      // _runProgressTs is the dead-run watchdog's anchor and MUST start fresh
      // each turn: the 'done' path used to leave it at the previous run's last
      // progress time, so the next dispatch compared "dead for 15s?" against an
      // hour-old stamp — one alive:false poll during the new run's pre-spawn
      // window and the watchdog killed the newborn turn as a bare "Error"
      // (ledger run #10, 2026-07-11: dispatch → auto-stop 1s later).
      if (d.ok) { _lastAssistant=''; _runProgressTs = 0; startTask(); _agentSince = Date.now()/1000; setInFlight(true); }   // don't clear _handledState here — a post-steer dispatch needs the '__interrupted__' guard to swallow the killed run's stale 'done'
      else { logRes(d.error || 'failed to start ' + bot, false); settleAction(false); }
    } catch { logRes('failed to start agent', false); settleAction(false); }
  }
  async function submit(){
    const txt = input.value.trim(); if(!txt) return; input.value=''; autoGrow(); if(typeof refreshSendButton==='function') refreshSendButton();
    if (MODE === 'auto') {
      logUser(txt);
      if (_inFlight) {
        // INTERRUPT-STEER (restored 2026-07-12, the owner): a mid-run message stops
        // the current turn and immediately redirects the bot — barge-in, not a
        // polite queue. The 1.0.12 soft-steer ("lands at the agent's next step",
        // delivered via SAY_URL) was never asked for and read as sluggish; the
        // old stop+re-dispatch "worked well enough." This is intentional
        // steering, NOT an error — close the current turn quietly as "Steered"
        // (no Interrupted/error UI, no extra message). Stop (■) is unchanged.
        _interrupting = true; _steering = true;   // hard-suppress terminal handling until the new run is RUNNING
        try { await fetch(STOP_URL, {method:'POST'}); } catch(_){}
        if (_task) { try { finishTask(false, 'Steered'); } catch(_){} }
        op.dataset.busy='0'; op.dataset.agent='';
        _inFlight = false;
        await new Promise(r => setTimeout(r, 350));   // let the backend register the stop
        _postSteerUntil = Date.now() + 1500;
        _interrupting = false; _handledState = 'done';
        setTimeout(()=>{ _steering = false; }, 8000);   // backstop: never leave terminal handling suppressed forever
        dispatchTask(txt);
        return;
      }
      dispatchTask(txt);
      return;
    }
    {
      const a = parse(txt);
      if (a && a.kind === '_ambiguous') {
        logUser(txt);
        logRes('That looks like a question, not a browser command. Flip to AUTO to ask the agent — or type a URL / "click X" / "type X" to drive the page manually.', false);
      } else if (a) act(a, txt);
    }
  }
  // ── send ⇄ stop toggle ──
  const STOP_URL = OP_URLS.agent_stop;
  const SAY_URL = OP_URLS.agent_say;
  const _sendSVG = send.innerHTML;
  const _stopSVG = '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor">'
    + '<rect x="5" y="5" width="14" height="14" rx="2.5"></rect></svg>';
  let _sendShowStop = null;   // change-guard: this runs per keystroke — don't re-parse the SVG innerHTML when nothing flipped
  function refreshSendButton(){
    // stop only while in-flight AND the box is empty; typing a follow-up flips it
    // back to the send arrow.
    const showStop = _inFlight && !input.value.trim();
    if (showStop === _sendShowStop) return;
    _sendShowStop = showStop;
    send.classList.toggle('stopping', showStop);
    send.innerHTML = showStop ? _stopSVG : _sendSVG;
    send.title = showStop ? 'stop' : 'send (Enter)';
  }
  function setInFlight(on){
    if (on === _inFlight) return;
    _inFlight = on;
    if (!on) {                              // turn over → kill the spinner, no orphans
      op.dataset.busy = '0';
      if (typeof _task !== 'undefined' && _task) { try { finishTask(); } catch(_){} }
      if (actTxt.textContent === 'Working') actTxt.textContent = (MODE==='auto'?'Done':idleCardText());
    }
    refreshSendButton();
    setFollowUp();
    if (!on) drainQueue();   // turn finished → run the next queued message
  }
  let _interrupting = false;
  async function stopAgent(){
    _interrupting = true;
    try { await fetch(STOP_URL, {method:'POST'}); } catch {}
    if (_task) { try { taskError('Interrupted', 'Stopped by user.'); finishTask(true, 'Interrupted'); } catch(_){} }
    op.dataset.busy='0'; op.dataset.agent='errored'; setCardText(actTxt, 'Interrupted');
    setCardSub(selectedBot()||'', '');
    setCardSub(selectedBot()||'', '');
    _inFlight=false; refreshSendButton(); setFollowUp();
    _handledState='__interrupted__';   // ignore the backend 'error' that follows
    setTimeout(()=>{ _interrupting=false; }, 2000);
  }
  send.addEventListener('click', () => {
    if (_inFlight && !input.value.trim()) stopAgent(); else submit();
  });
  // Batch textarea measurement into one animation frame. The former 84px hard
  // cap made ordinary multiline drafts scroll while they still had ample rail
  // room, and its clean-layout shortcut could miss wrapped-line growth.
  let _growFrame = 0;
  function autoGrow(){
    cancelAnimationFrame(_growFrame);
    _growFrame = requestAnimationFrame(() => {
      input.style.height = 'auto';
      const fullHeight = input.scrollHeight;
      input.style.height = Math.min(fullHeight, 140) + 'px';
      input.style.overflowY = fullHeight > 140 ? 'auto' : 'hidden';
    });
  }
  input.addEventListener('input', () => { autoGrow(); refreshSendButton(); });
  input.addEventListener('keydown', e => {
    // slash palette (#30 v2) owns the keys while open — it's initialized later
    // in the async boot, so consult it dynamically rather than by listener order.
    if (window._opPalKeydown && window._opPalKeydown(e)) return;
    if (e.key==='Enter' && !e.shiftKey) { e.preventDefault(); submit(); }   // Shift+Enter = newline
  });

  const PRETTY={back:'Back',forward:'Forward',reload:'Reload',tab_next:'Next tab'};
  document.querySelectorAll('.op-chip').forEach(c => c.addEventListener('click', () =>
    act({kind:c.dataset.kind, value:c.dataset.value||''})));  // direct action: result-only, no 'Operator :' line

  // ── click on the live view → real page click (object-fit:contain mapping) ──
  function viewToNorm(cx, cy){ const r=stage.getBoundingClientRect();
    const nW=view.naturalWidth, nH=view.naturalHeight; if(!nW||!nH) return null;
    const sc=Math.min(r.width/nW, r.height/nH); const dW=nW*sc, dH=nH*sc;
    const ox=(r.width-dW)/2, oy=(r.height-dH)/2; const px=cx-r.left-ox, py=cy-r.top-oy;
    if(px<0||py<0||px>dW||py>dH) return null; return {x:px/dW, y:py/dH}; }
  function ripple(cx,cy){ const r=stage.getBoundingClientRect(); const d=document.createElement('span');
    d.className='op-ripple'; d.style.left=(cx-r.left)+'px'; d.style.top=(cy-r.top)+'px';
    stage.appendChild(d); setTimeout(()=>d.remove(),500); }
  // press-and-hold: pointerdown starts a real hold on the page (for "hold to
  // verify" captchas etc.); pointerup releases it. A quick tap still falls through
  // to the normal click handler below.
  // mouse interaction: a quick tap = click, a press-hold (>220ms, no move) = a real
  // held press (captchas), and a press-move-release = a DRAG (one atomic action).
  let _holdTimer = null, _holding = false, _down = null, _dragging = false;
  stage.addEventListener('pointerdown', e => {
    if (e.pointerType === 'touch') return;       // touch handled by touch* events below
    if (e.button !== 0) return;
    if (_overlayTarget(e)) return;               // overlay (launchpad) owns its own press
    const n = viewToNorm(e.clientX, e.clientY); if (!n) return;
    try { stage.setPointerCapture(e.pointerId); } catch(_){}
    _down = { n, cx: e.clientX, cy: e.clientY }; _dragging = false; _holding = false;
    _holdTimer = setTimeout(() => {           // stationary press → HOLD
      if (_dragging) return;
      _holding = true; ripple(e.clientX, e.clientY);
      act({kind:'mousedown_at', x:n.x, y:n.y}, null, true);
    }, 220);
  });
  stage.addEventListener('pointermove', e => {
    if (!_down || _dragging) return;
    if (Math.abs(e.clientX-_down.cx) > 6 || Math.abs(e.clientY-_down.cy) > 6) {
      _dragging = true;                          // moved enough → it's a drag
      if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
    }
  });
  stage.addEventListener('pointerup', e => {
    if (e.pointerType === 'touch') return;
    if (_holdTimer) { clearTimeout(_holdTimer); _holdTimer = null; }
    try { stage.releasePointerCapture(e.pointerId); } catch(_){}
    const end = viewToNorm(e.clientX, e.clientY);
    if (_dragging && _down && end) {             // DRAG: down at start → up at end
      ripple(e.clientX, e.clientY);
      act({kind:'drag', x0:_down.n.x, y0:_down.n.y, x1:end.x, y1:end.y}, null, true);
      e.preventDefault(); e.stopPropagation();
    } else if (_holding) {                        // release a held press
      const n = end || _down && _down.n;
      act({kind:'mouseup_at', x:(n?n.x:0.5), y:(n?n.y:0.5)}, null, true);
      e.preventDefault(); e.stopPropagation();
    }
    _down = null; _holding = false; _dragging = false;
  });
  stage.addEventListener('pointercancel', e => { try{stage.releasePointerCapture(e.pointerId);}catch(_){} if(_holdTimer){clearTimeout(_holdTimer);_holdTimer=null;} _holding=false; _dragging=false; _down=null; });

  // ── native TOUCH handlers (iPad/iOS): Pointer Events for touch are unreliable on
  //    a non-button <div>, so drive touch directly. CSS touch-action:pinch-zoom
  //    blocks one-finger page panning while leaving two-finger page zoom native.
  //    Do not cancel touchstart: doing so also cancels a pinch whose second finger
  //    lands after the first. A >220ms stationary
  //    press becomes a real page mousedown→…→mouseup (captcha "hold to verify"); a
  //    quick tap fires a single click. We fully own touch here — the synthetic click
  //    that iOS emits after touchend is swallowed by _touchHandled. ──
  let _tHoldTimer=null, _tHolding=false, _tN=null, _tStart=null, _tDragging=false,
      _touchHandled=false;
  function _endTouch(){ _tN=null; _tStart=null; _tHolding=false; _tDragging=false;
    setTimeout(()=>{ _touchHandled=false; }, 400); }

  // Interactive overlays that sit ON TOP of the feed (launchpad cards, the panic
  // STOP) own their own taps — the feed steer handler must NOT intercept them, or
  // iOS's preventDefault kills the tap and "none of the buttons work" (iPad).
  function _overlayTarget(e){
    const t = e.target;
    return t && t.closest && t.closest('.op-lp, .op-panic, .op-handoff, .op-takeover-btn');
  }
  stage.addEventListener('touchstart', e => {
    if (_overlayTarget(e)) return;   // let the overlay handle its own tap
    if (!e.touches.length) return;
    if (e.touches.length >= 2) {   // 2-finger pinch → let Safari zoom the page natively
      if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
      _endTouch(); return;          // don't preventDefault, don't steer
    }
    const t = e.touches[0];
    const n = viewToNorm(t.clientX, t.clientY); if (!n) return;
    _tN = n; _tStart = {n, cx:t.clientX, cy:t.clientY};
    _tHolding = false; _tDragging = false; _touchHandled = true;
    stage.focus();
    _tHoldTimer = setTimeout(() => {            // stationary press → HOLD
      if (_tDragging) return;
      _tHolding = true; ripple(t.clientX, t.clientY);
      act({kind:'mousedown_at', x:_tN.x, y:_tN.y}, null, true);
    }, 220);
  }, {passive:false});
  let _tScroll = false, _tLastY = 0, _tLastX = 0, _tScrollDy = 0, _tScrollDx = 0, _tScrollT = null;
  stage.addEventListener('touchmove', e => {
    if (_overlayTarget(e)) return;   // let the overlay scroll/handle its own move
    if (!e.touches.length || !_tStart) return;
    if (e.touches.length >= 2) {   // pinch in progress → release steering, let native zoom run
      if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
      _endTouch(); return;
    }
    e.preventDefault();
    const t = e.touches[0];
    const n = viewToNorm(t.clientX, t.clientY);
    if (n) _tN = n;
    const dxTot = t.clientX - _tStart.cx, dyTot = t.clientY - _tStart.cy;
    // first decisive move: vertical-dominant + not a held press → it's a SCROLL gesture.
    if (!_tScroll && !_tDragging && !_tHolding &&
        (Math.abs(dxTot) > 8 || Math.abs(dyTot) > 8)) {
      if (Math.abs(dyTot) >= Math.abs(dxTot)) {
        _tScroll = true; _tLastY = t.clientY; _tLastX = t.clientX;
        if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
      } else {
        _tDragging = true;                       // horizontal-dominant → drag (existing)
        if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
      }
    }
    if (_tScroll) {
      // Forward the finger's direction directly to the remote wheel gesture.
      // The viewport-origin fix prevents the old white-void capture; scroll
      // direction is independent and should follow the Operator control's
      // established drag convention. Wheel/trackpad remains native passthrough.
      _tScrollDy += (t.clientY - _tLastY);
      _tScrollDx += (t.clientX - _tLastX);
      _tLastY = t.clientY; _tLastX = t.clientX;
      if (!_tScrollT) _tScrollT = setTimeout(() => {
        const dy = _tScrollDy, dx = _tScrollDx; _tScrollDy = 0; _tScrollDx = 0; _tScrollT = null;
        if (dy || dx) act({kind:'scroll', dx: Math.round(dx), dy: Math.round(dy)}, null, true);
      }, 50);
    }
  }, {passive:false});
  stage.addEventListener('touchend', e => {
    if (_overlayTarget(e)) return;   // overlay tap — don't preventDefault its click
    if (_tScroll) { _tScroll = false; e.preventDefault(); _endTouch(); return; }
    if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
    e.preventDefault();
    if (_tDragging && _tStart && _tN) {          // DRAG (one atomic action)
      act({kind:'drag', x0:_tStart.n.x, y0:_tStart.n.y, x1:_tN.x, y1:_tN.y}, null, true);
    } else if (_tHolding) {                       // release the held press
      act({kind:'mouseup_at', x:(_tN?_tN.x:0.5), y:(_tN?_tN.y:0.5)}, null, true);
    } else if (_tN) {                            // quick tap → click
      const ct = (e.changedTouches && e.changedTouches[0]) || null;
      if (ct) ripple(ct.clientX, ct.clientY);
      else if (_tStart) ripple(_tStart.cx, _tStart.cy);
      // rapid multi-tap → select all (3+ taps within 600ms, like triple/quad-click a page)
      const _now = performance.now();
      if (!window.__tapN || (_now - (window.__tapT||0)) > 600) window.__tapN = 0;
      window.__tapN++; window.__tapT = _now;
      if (window.__tapN >= 3) { window.__tapN = 0; act({kind:'key', value:'Control+a'}, null, true); }
      else act({kind:'click_at', x:_tN.x, y:_tN.y}, null, true);
    }
    _endTouch();
  }, {passive:false});
  stage.addEventListener('touchcancel', () => {
    if (_tHoldTimer) { clearTimeout(_tHoldTimer); _tHoldTimer = null; }
    // if a press was already down when iOS cancels, RELEASE it so the button
    // doesn't get stuck down on the page.
    if (_tHolding) act({kind:'mouseup_at', x:(_tN?_tN.x:0.5), y:(_tN?_tN.y:0.5)}, null, true);
    _endTouch();
  });
  // hover passthrough (MAN mode only — in AUTO your mouse would fight the
  // agent's cursor): stream throttled pointer moves so menus, tooltips and
  // hover states react on the remote side like a real mouse. Desktop surfaces
  // get real X11/win motion; the browser gets CDP mouseMoved.
  let _mvTs = 0, _mvLast = null;
  stage.addEventListener('mousemove', e => {
    if (MODE !== 'man') return;
    const now = performance.now();
    if (now - _mvTs < 90) return;
    const n = viewToNorm(e.clientX, e.clientY);
    if (!n) return;
    if (_mvLast && Math.abs(n.x - _mvLast.x) < 0.004
                && Math.abs(n.y - _mvLast.y) < 0.004) return;
    _mvTs = now; _mvLast = n;
    act({kind:'move', x:n.x, y:n.y}, null, true);
  });
  // dblclick: swallow the native event only — the click handler below already
  // forwarded each physical click with its native detail count (1,2,3…), so the
  // remote page has the real double/triple-click. Dispatching dblclick_at here
  // too would replay EXTRA presses and break word/paragraph selection.
  stage.addEventListener('dblclick' , e => { e.preventDefault(); });
  // right-click → forward to the remote page (its native context menu, captured in the frame);
  // preventDefault stops OUR browser's menu from popping over the stage.
  stage.addEventListener('contextmenu', e => { e.preventDefault(); const n=viewToNorm(e.clientX,e.clientY); if(!n) return;
    ripple(e.clientX,e.clientY); act({kind:'rclick_at',x:n.x,y:n.y}, null, true); });
  stage.addEventListener('click', e => { if (_touchHandled) return;   // touch already handled it
    if (_overlayTarget(e)) return;   // click originated on an overlay (launchpad etc.) — don't steer the page
    stage.focus(); const n=viewToNorm(e.clientX,e.clientY); if(!n) return;
    ripple(e.clientX,e.clientY);
    // e.detail = native multi-click count (2=word select, 3=paragraph select) —
    // forwarded so the remote page sees a REAL double/triple-click, not three singles.
    act({kind:'click_at',x:n.x,y:n.y,count:Math.min(e.detail||1,4)}, null, true); });

  // ── keyboard passthrough when stage focused ──
  const PASS={Enter:'Enter',Backspace:'Backspace',Tab:'Tab',Escape:'Escape',ArrowUp:'ArrowUp',
    ArrowDown:'ArrowDown',ArrowLeft:'ArrowLeft',ArrowRight:'ArrowRight',Delete:'Delete',
    Home:'Home',End:'End',PageUp:'PageUp',PageDown:'PageDown'};
  // mouse wheel / trackpad scroll → scroll the bot browser by the same delta.
  let _wheelDx = 0, _wheelDy = 0, _wheelT = null;
  stage.addEventListener('wheel', e => {
    if (_overlayTarget(e)) return;   // wheel over the launchpad scrolls IT, not the page
    e.preventDefault();
    _wheelDx += e.deltaX; _wheelDy += e.deltaY;
    // coalesce rapid wheel events into one steer (~50ms) so we don't flood the server
    if (_wheelT) return;
    _wheelT = setTimeout(() => {
      const dx = _wheelDx, dy = _wheelDy; _wheelDx = 0; _wheelDy = 0; _wheelT = null;
      if (dx || dy) act({kind:'scroll', dx: Math.round(dx), dy: Math.round(dy)}, null, true);
    }, 50);
  }, {passive:false});

  // typed chars are sent per keydown; navigation/special keys are HELD (key_down on press,
  // key_up on release) so holding an arrow gives smooth continuous movement in the bot
  // browser (native key-repeat) instead of laggy per-repeat HTTP round-trips.
  const _heldKeys = new Set();
  stage.addEventListener('keydown', e => { if(e.target!==stage || e.metaKey||e.ctrlKey||e.altKey) return;
    if(e.key.length===1){ e.preventDefault(); act({kind:'type',value:e.key}, null, true); }
    else if(PASS[e.key]){ e.preventDefault();
      if(e.repeat) return;                       // ignore OS auto-repeat — bot Chrome repeats the held key
      _heldKeys.add(PASS[e.key]);
      act({kind:'key_down',value:PASS[e.key]}, null, true); } });
  stage.addEventListener('keyup', e => {
    if(e.target!==stage) return;
    if(PASS[e.key] && _heldKeys.has(PASS[e.key])){ e.preventDefault();
      _heldKeys.delete(PASS[e.key]);
      act({kind:'key_up',value:PASS[e.key]}, null, true); } });
  // release any stuck held keys if focus leaves the stage
  stage.addEventListener('blur', () => { _heldKeys.forEach(k => act({kind:'key_up',value:k}, null, true)); _heldKeys.clear(); });
})();
