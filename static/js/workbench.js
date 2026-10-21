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
  function fitApprovalHeading(heading) {
    if (!heading.isConnected || !heading.clientWidth) return;
    heading.style.removeProperty('font-size');
    const label = heading.lastElementChild;
    const icon = heading.firstElementChild;
    for (let pass = 0; pass < 4; pass++) {
      const style = getComputedStyle(heading);
      const needed = label.getBoundingClientRect().width + icon.getBoundingClientRect().width + (parseFloat(style.columnGap) || 0);
      if (needed <= heading.clientWidth) break;
      heading.style.fontSize = (parseFloat(style.fontSize) * (heading.clientWidth - 2) / needed) + 'px';
    }
  }
  const approvalHeadingResize = new ResizeObserver(entries => entries.forEach(({target}) => {
    if (target.isConnected) fitApprovalHeading(target);
    else approvalHeadingResize.unobserve(target);
  }));
  function money(amount, currency) {
    const code = String(currency || '').toUpperCase();
    try {
      const format = new Intl.NumberFormat('en-US', {style: 'currency', currency: code,
        currencyDisplay: 'narrowSymbol'});
      const parts = format.formatToParts(amount);
      // Some currencies have no narrow symbol: Intl falls back to the ISO code.
      // The code already sits beside the amount, so do not print it twice.
      const symbol = parts.find(p => p.type === 'currency')?.value || '';
      const value = parts.filter(p => p.type !== 'currency').map(p => p.value).join('').trim();
      return {value, code, symbol: symbol === code ? '' : symbol};
    } catch (_) {
      return {value: Number(amount).toLocaleString('en-US', {minimumFractionDigits: 2,
        maximumFractionDigits: 2}), code: /^[A-Z]{3}$/.test(code) ? code : ''};
    }
  }
  function approvalDestination(destination, tag, cls = '') {
    const raw = String(destination || '').trim();
    const match = /^(.*?)\s*(https?:\/\/[^\s]+)\s*$/.exec(raw);
    let merchant = match ? match[1].replace(/\s*[—–-]\s*$/, '').trim() : raw;
    let checkout = null;
    if (match) {
      try { checkout = new URL(match[2]); } catch (_) {}
    }
    const host = checkout?.hostname.toLowerCase().replace(/^www\./, '') || '';
    // Older action records combine provider, merchant and address in one
    // destination string. Preserve the address, but give each fact its own row.
    const addressParts = merchant.split(/\s+[—–]\s+/);
    merchant = addressParts.shift();
    const address = addressParts.join(' — ').replace(/(^|[\s,])([a-z])/g,
      (_, gap, letter) => gap + letter.toUpperCase());
    const brands = [
      [/^uber\s*eats$/i, 'Uber Eats', 'ubereats.com'],
      [/^doordash$/i, 'DoorDash', 'doordash.com'],
      [/^instacart$/i, 'Instacart', 'instacart.com'],
      [/^mcdonald[’']?s/i, "McDonald's", 'mcdonalds.com'],
      [/^best buy(?: canada)?/i, 'Best Buy Canada', 'bestbuy.ca'],
      [/^dyson(?: canada)?/i, 'Dyson Canada', 'dysoncanada.ca'],
      [/^amazon/i, 'Amazon', 'amazon.com'],
      [/^walmart/i, 'Walmart', 'walmart.com'],
      [/^costco/i, 'Costco', 'costco.com'],
      [/^target/i, 'Target', 'target.com'],
      [/^starbucks/i, 'Starbucks', 'starbucks.com']
    ];
    const lookup = text => brands.find(([pattern]) => pattern.test(text));
    let provider = null;
    const providerParts = merchant.split(/\s+\/\s+/);
    if (providerParts.length > 1 && lookup(providerParts[0])) {
      provider = lookup(providerParts.shift()); merchant = providerParts.join(' / ');
    }
    const bestBuy = /^(?:best buy canada|(?:www\.)?bestbuy\.ca)$/i.test(merchant) || host === 'bestbuy.ca';
    const dyson = /^(?:dyson(?: canada)?|(?:www\.)?dysoncanada\.ca)$/i.test(merchant) || host === 'dysoncanada.ca';
    merchant = dyson ? 'Dyson Canada' : bestBuy ? 'Best Buy Canada' : merchant || host || raw;
    const brand = lookup(merchant);
    if (brand) {
      const suffix = merchant.replace(brand[0], '');
      merchant = brand[1] + suffix.replace(/(^|[\s(])([a-z])/g, (_, gap, letter) => gap + letter.toUpperCase());
    }
    const el = node(tag, null, ['op-wb-merchant', cls].filter(Boolean).join(' '));
    const row = node('span', null, 'op-wb-merchant-row');
    const domain = dyson ? 'dysoncanada.ca' : bestBuy ? 'bestbuy.ca' : brand?.[2] || host;
    function logo(domain, parent){
      if (!domain || !/^(?:[a-z0-9-]+\.)+[a-z]{2,}$/i.test(domain)) return;
      const favicon = node('img', null, 'op-wb-merchant-icon');
      favicon.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(domain) + '&sz=32';
      favicon.alt = ''; favicon.width = favicon.height = 20;
      favicon.addEventListener('error', () => favicon.remove()); parent.append(favicon);
    }
    logo(domain, row);
    row.append(node('span', merchant)); el.append(row);
    if (provider) {
      const via = node('span', null, 'op-wb-provider'); logo(provider[2], via);
      via.append(node('span', provider[1])); el.append(via);
    }
    if (address) el.append(node('span', address, 'op-wb-destination-detail'));
    if (checkout && ['https:', 'http:'].includes(checkout.protocol)) {
      const link = node('a', checkout.href, 'op-wb-checkout-link');
      link.href = checkout.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      el.append(link);
    }
    return el;
  }
  function resultSources(job, result) {
    const seen = new Set(), sources = [];
    for (const id of result.evidence_ids || []) {
      const evidence = job.evidence.find(e => e.id === id);
      if (!evidence) continue;
      for (const raw of evidence.urls || []) {
        try {
          const url = new URL(raw);
          if (!['https:', 'http:'].includes(url.protocol) || seen.has(url.href)) continue;
          seen.add(url.href);
          sources.push({url, observed: evidence.observed});
        } catch (_) { /* An invalid observed URL is not a link. */ }
      }
    }
    return sources;
  }
  function sourceName(host) {
    if (/(^|\.)amazon\.ca$/i.test(host)) return 'Amazon.ca';
    if (host === 'mail.google.com') return 'Gmail';
    return host.replace(/^www\./, '');
  }
  function foldedResults() {
    try { return new Set(JSON.parse(localStorage.getItem('operator-result-folded-v1') || '[]')); } catch (_) { return new Set(); }
  }
  function setFolded(card, id, folded) {
    card.classList.toggle('collapsed', folded);
    const fold = card.querySelector('.op-wb-fold');
    if (fold) { fold.setAttribute('aria-expanded', folded ? 'false' : 'true'); fold.setAttribute('aria-label', folded ? 'Expand result' : 'Collapse result'); }
    const ids = foldedResults();
    if (folded) ids.add(id); else ids.delete(id);
    try { localStorage.setItem('operator-result-folded-v1', JSON.stringify([...ids].slice(-200))); } catch (_) {}
  }

  function resultTag(status, pendingApproval = true) {
    const states = {
      found: ['Result', '<circle cx="8.5" cy="8.5" r="5.2"/><path d="m12.4 12.4 4.1 4.1"/>'],
      prepared: [pendingApproval ? 'Ready for approval' : 'Prepared', '<circle cx="10" cy="10" r="7.5"/><path d="M10 5.8v4.6l3.1 1.9"/>'],
      confirmed: ['Confirmed', '<circle cx="10" cy="10" r="7.5"/><path d="m6.2 10 2.6 2.6 5-5.1"/>'],
    };
    const kind = Object.hasOwn(states, status) ? status : 'found';
    const tag = node('span', null, 'op-wb-tag op-wb-tag-' + kind);
    const icon = node('span', null, 'op-wb-tag-icon');
    icon.setAttribute('aria-hidden', 'true');
    icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none">' + states[kind][1] + '</svg>';
    tag.append(icon, node('span', states[kind][0]));
    return tag;
  }
  function renderSources(card, sources) {
    if (!sources.length) return;
    const group = node('details', null, 'op-wb-sources');
    const heading = node('summary');
    heading.append(node('span', 'Sources'), node('span', String(sources.length), 'op-wb-source-count'));
    group.append(heading);
    const list = node('div', null, 'op-wb-source-list');
    const perHost = new Map(), index = new Map();
    sources.forEach(({url}) => perHost.set(url.hostname, (perHost.get(url.hostname) || 0) + 1));
    for (const {url, observed} of sources) {
      const host = url.hostname, number = (index.get(host) || 0) + 1;
      index.set(host, number);
      const link = node('a', null, 'op-wb-source');
      link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      const icon = node('img', null, 'op-wb-source-icon');
      icon.src = 'https://www.google.com/s2/favicons?domain=' + encodeURIComponent(host) + '&sz=32';
      icon.alt = ''; icon.width = icon.height = 18;
      icon.addEventListener('error', () => icon.remove());
      const label = node('span', sourceName(host));
      const detail = /(?:order.details|order.history|your.orders)/i.test(url.pathname) ? 'Order page'
        : host === 'mail.google.com' ? 'Message'
          : perHost.get(host) > 1 ? 'Page ' + number : 'Page';
      link.append(icon, label, node('small', detail), node('span', '↗', 'op-wb-source-arrow'));
      if (observed) link.title = 'Checked ' + new Date(observed * 1000).toLocaleString();
      list.append(link);
    }
    group.append(list); card.append(group);
  }
  function renderConfirmation(card, result, sources) {
    if (!result.confirmation) return;
    const match = result.status === 'confirmed' && /^Amazon\.ca order (\d{3}-\d{7}-\d{7})$/i.exec(result.confirmation.trim());
    if (!match) {
      const confirmation = node('div', result.confirmation, 'op-wb-rich');
      bridge.renderMarkdown(confirmation); card.append(confirmation);
      return;
    }
    const id = match[1], row = node('div', null, 'op-wb-order');
    row.append(node('span', 'Order #', 'op-wb-order-label'));
    const exact = sources.find(({url}) => url.protocol === 'https:' && /(^|\.)amazon\.ca$/i.test(url.hostname)
      && /order|confirmation/i.test(url.pathname) && url.href.includes(id));
    if (exact) {
      const link = node('a', id, 'op-wb-order-control');
      link.href = exact.url.href; link.target = '_blank'; link.rel = 'noopener noreferrer';
      link.title = 'View order on Amazon.ca'; row.append(link);
    } else {
      const feedback = node('span', '', 'op-wb-copy-status');
      feedback.setAttribute('role', 'status');
      const copy = button(id, async () => {
        try {
          await navigator.clipboard.writeText(id);
          feedback.textContent = 'Copied';
        } catch (_) {
          feedback.textContent = 'Copy failed';
          status('Could not copy the order number.');
        }
      }, 'op-wb-order-control');
      copy.setAttribute('aria-label', 'Copy order number ' + id);
      copy.title = 'Copy order number'; row.append(copy, feedback);
    }
    card.append(row);
  }
  let cid = '', state = null, agent = {}, requested = 0, loading = false, expanded = false;
  let steerId = '', lastRender = '', filesRender = '', diagnosticsOn = false, lastFocus = null;
  let confirmingApprovalId = '', revisingApprovalId = '', revisionDraft = '';
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
  const composer = document.querySelector('.op-inputbox');
  const fileInput = node('input'); fileInput.type = 'file'; fileInput.multiple = true; fileInput.hidden = true;
  fileInput.id = 'op-file-input';
  const filesList = node('div', null, 'op-wb-files'); filesList.hidden = true;
  filesList.setAttribute('role', 'list');
  filesList.setAttribute('aria-label', 'Attached files');
  const filesDialog = dialog('Chat files', 'op-files-dialog');
  const addFiles = button('Add files', () => fileInput.click(), 'op-wb-zone');
  const filesSummary = node('p', '', 'op-wb-storage');
  const filesHelp = node('p', 'All chat files are available as context on future turns.', 'op-wb-files-help');
  const fileTools = node('div', null, 'op-wb-file-tools');
  const fileSearch = node('input'); fileSearch.type = 'search'; fileSearch.placeholder = 'Search files';
  fileSearch.setAttribute('aria-label', 'Search files');
  const fileSort = node('select'); fileSort.setAttribute('aria-label', 'Sort files');
  for (const [value, label] of [['newest', 'Newest first'], ['name', 'Name'], ['size', 'Largest first']]) {
    const option = node('option', label); option.value = value; fileSort.append(option);
  }
  fileTools.append(fileSearch, fileSort);
  const filesFeedback = node('p', '', 'op-wb-storage'); filesFeedback.setAttribute('role', 'status'); filesFeedback.hidden = true;
  function fileStatus(text) { filesFeedback.textContent = text; filesFeedback.hidden = !text; }
  filesDialog.append(filesSummary, filesHelp, fileTools, filesFeedback, filesList, addFiles, fileInput);
  for (const control of [fileSearch, fileSort]) control.addEventListener('input', () => { filesRender = ''; renderFiles(); });
  const filePills = node('div', null, 'op-file-pills'); filePills.id = 'op-file-pills'; filePills.hidden = true;
  filePills.setAttribute('aria-label', 'Chat files'); composer.before(filePills);
  let pillSignature = '';
  function renderFilePills() {
    const files = state?.files || [];
    const capacity = Math.max(1, Math.min(3, Math.floor(composer.clientWidth / 145)));
    const signature = JSON.stringify([cid, files.map(f => [f.id, f.name]), capacity, context().can_control, !!agent.alive]);
    if (signature === pillSignature) return;
    pillSignature = signature; filePills.replaceChildren(); filePills.hidden = !files.length;
    files.slice(0, capacity).forEach(file => {
      const shell = node('span', null, 'op-file-pill-shell');
      const pill = button('', () => openFile(file), 'op-file-pill');
      const icon = node('span', null, 'op-file-pill-icon'); icon.setAttribute('aria-hidden', 'true');
      const isImage = /\.(png|jpe?g|webp|gif|avif|svg)$/i.test(file.name);
      icon.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
        (isImage ? '<rect x="3" y="3" width="14" height="14" rx="3"/><circle cx="7" cy="7" r="1"/><path d="m3 13 4-4 4 4 2-2 4 4"/>' : '<path d="M11 2H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M11 2v6h6M6 12h8M6 15h5"/>') + '</svg>';
      pill.append(icon, node('span', file.name, 'op-file-pill-name'));
      pill.title = file.name; pill.setAttribute('aria-label', 'Preview ' + file.name); const remove = button('×', async () => {
        remove.disabled = true;
        try { await api('/files/' + file.id, {method: 'DELETE'}); await refresh(true); }
        catch (e) { status(e.message); remove.disabled = !context().can_control || !!agent.alive; }
      }, 'op-file-pill-remove');
      remove.setAttribute('aria-label', 'Remove ' + file.name); remove.title = 'Remove ' + file.name;
      remove.disabled = !context().can_control || !!agent.alive;
      shell.append(pill, remove); filePills.append(shell);
    });
    if (files.length > capacity) {
      const more = button('+' + (files.length - capacity), openFiles, 'op-file-pill op-file-more');
      more.setAttribute('aria-label', 'Open Chat files, ' + (files.length - capacity) + ' more files'); filePills.append(more);
    }
  }
  // Only updates local pills; it never sends a remote viewport resize.
  new ResizeObserver(renderFilePills).observe(composer);
  function openFiles() { show(filesDialog); refresh(true); }


  const preview = dialog('File preview', 'op-file-preview');
  const previewBody = node('div', null, 'op-wb-preview-body');
  const previewDownload = node('a', 'Download', 'op-wb-preview-download');
  const previewBack = button('Chat files', () => { preview.close(); openFiles(); });
  const previewActions = node('div', null, 'op-wb-preview-actions');
  previewActions.append(previewBack, previewDownload); preview.append(previewBody, previewActions);
  let previewRequest = 0, previewFileId = '';
  async function openFile(file) {
    const owner = cid, requestId = ++previewRequest; previewFileId = file.id;
    $('op-file-preview-title').textContent = file.name;
    previewDownload.href = fileUrl(file.id); previewDownload.download = file.name;
    previewBody.textContent = 'Loading…'; filesDialog.close(); show(preview);
    try {
      const data = await api('/files/' + encodeURIComponent(file.id) + '/preview');
      if (owner !== cid || requestId !== previewRequest || !preview.open) return;
      if (data.image) {
        previewBody.className = 'op-wb-preview-body op-wb-image-preview';
        const img = node('img'); img.alt = file.name; img.src = fileUrl(file.id);
        img.addEventListener('error', () => { if (owner === cid && requestId === previewRequest) previewBody.textContent = 'This image could not be loaded. Download it to open it.'; });
        previewBody.replaceChildren(img); return;
      }
      previewBody.textContent = data.text;
      previewBody.className = data.markdown ? 'op-wb-preview-body op-wb-rich' : 'op-wb-preview-body op-wb-plain';
      if (data.markdown) bridge.renderMarkdown(previewBody);
    } catch (e) {
      if (owner === cid && requestId === previewRequest && preview.open) previewBody.textContent = e.message;
    }
  }
  op.addEventListener('click', e => {
    const link = e.target.closest('[data-operator-file]');
    if (!link) return;
    const file = state?.files.find(f => f.id === link.dataset.operatorFile);
    if (!file) return;
    e.preventDefault(); openFile(file);
  });
  const uploadButton = $('op-files-open');
  uploadButton.hidden = false;
  uploadButton.setAttribute('aria-label', 'Chat files');
  uploadButton.addEventListener('click', openFiles);
  fileInput.addEventListener('change', () => { upload(fileInput.files); fileInput.value = ''; });
  async function upload(files) {
    if (!context().can_control) { status('Take over this chat to add files.'); return; }
    uploadButton.disabled = true; addFiles.disabled = true;
    try {
      for (const file of files) {
        if (state && file.size > state.storage.file_limit) throw new Error(file.name + ' exceeds the per-file limit.');
        fileStatus('Adding ' + file.name + '…');
        const form = new FormData(); form.append('file', file);
        await api('/files', {method: 'POST', body: form});
      }
      fileStatus('');
      await refresh(true);
    } catch (e) { fileStatus(e.message); await refresh(true); }
    finally { uploadButton.disabled = false; addFiles.disabled = !context().can_control; }
  }
  const chat = document.querySelector('.op-rail');
  chat.addEventListener('dragover', e => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); chat.classList.add('op-wb-drop'); } });
  chat.addEventListener('dragleave', e => { if (!chat.contains(e.relatedTarget)) chat.classList.remove('op-wb-drop'); });
  chat.addEventListener('drop', e => { if (!e.dataTransfer.files.length) return; e.preventDefault(); chat.classList.remove('op-wb-drop'); show(filesDialog); upload(e.dataTransfer.files); });
  function fileUrl(id) { return OP_URLS.workspace + '/files/' + encodeURIComponent(id) + '?conversation_id=' + encodeURIComponent(cid); }
  function renderFiles() {
    if (!state) return;
    const signature = JSON.stringify([cid, state.files, state.transfers, state.storage, context().can_control, !!agent.alive]);
    if (signature === filesRender) return;
    filesRender = signature;
    renderFilePills();
    filesList.replaceChildren(); addFiles.disabled = !context().can_control;
    filesSummary.textContent = state.files.length + (state.files.length === 1 ? ' file · ' : ' files · ') + bytes(state.storage.chat) + ' of ' + bytes(state.storage.chat_limit);
    const query = fileSearch.value.trim().toLocaleLowerCase();
    const visible = state.files.filter(f => !query || f.name.toLocaleLowerCase().includes(query));
    visible.sort((a, b) => fileSort.value === 'name' ? a.name.localeCompare(b.name) :
      fileSort.value === 'size' ? b.size - a.size : (b.created || 0) - (a.created || 0));
    for (const file of visible) {
      const row = node('div', null, 'op-wb-file'), link = node('a', null, 'op-wb-file-link');
      row.setAttribute('role', 'listitem');
      link.title = file.name;
      link.setAttribute('aria-label', file.name);
      const icon = node('span', null, 'op-wb-file-icon');
      icon.setAttribute('aria-hidden', 'true');
      icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></svg>';
      const copy = node('span', null, 'op-wb-file-copy');
      const extension = /\.([a-z0-9]{1,8})$/i.exec(file.name)?.[1].toUpperCase();
      copy.append(node('span', file.name, 'op-wb-file-name'),
        node('small', [extension, bytes(file.size)].filter(Boolean).join(' · ')));
      link.append(icon, copy);
      link.href = fileUrl(file.id); link.download = file.name;
      const remove = button('×', async () => {
        try { await api('/files/' + file.id, {method: 'DELETE'}); await refresh(true); }
        catch (e) { fileStatus(e.message); }
      });
      remove.className = 'op-wb-file-remove';
      remove.setAttribute('aria-label', 'Remove ' + file.name);
      remove.title = 'Remove ' + file.name;
      remove.disabled = !context().can_control || !!agent.alive;
      const controls = node('div', null, 'op-wb-file-controls');
      const rename = button('Rename', () => {
        const input = node('input'); input.value = file.name; input.maxLength = 240;
        input.setAttribute('aria-label', 'Filename');
        const save = button('Save', async () => {
          save.disabled = true;
          try { await api('/files/' + file.id, {method: 'PATCH', body: {name: input.value}}); fileStatus(''); filesRender = ''; await refresh(true); }
          catch (e) { fileStatus(e.message); save.disabled = false; }
        });
        const cancel = button('Cancel', () => { filesRender = ''; renderFiles(); });
        controls.replaceChildren(input, save, cancel); input.focus(); input.select();
        input.addEventListener('keydown', e => { if (e.key === 'Enter') save.click(); if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancel.click(); } });
      });
      rename.disabled = remove.disabled;
      controls.append(rename);
      row.append(link, remove, controls);
      {
        link.addEventListener('click', e => { e.preventDefault(); openFile(file); });
        const download = node('a', 'Download', 'op-wb-file-download');
        download.href = fileUrl(file.id); download.download = file.name;
        download.setAttribute('aria-label', 'Download ' + file.name);
        controls.append(download); filesList.append(row);
      }
    }
    for (const transfer of state.transfers || []) {
      filesList.append(node('p', transfer.name + ' · ' + (transfer.status === 'downloading' ? 'Downloading…' : transfer.error), 'op-wb-storage'));
    }
    filesList.hidden = !filesList.childElementCount;
    if (!filesList.childElementCount) { filesList.append(node('p', state.files.length ? 'No matching files.' : 'No files in this chat.', 'op-wb-storage')); filesList.hidden = false; }
    if (preview.open && !state.files.some(f => f.id === previewFileId)) preview.close();
    uploadButton.title = 'Chat files' + (state.files.length ? ' (' + state.files.length + ')' : '');
  }
  function setJobExpanded(value) {
    expanded = value;
    try { localStorage.setItem('operator-job-expanded-v1:' + (cid || 'legacy'), value ? '1' : '0'); } catch (_) {}
    $('op-action').setAttribute('aria-expanded', String(value));
    $('op-action').setAttribute('aria-controls', 'op-job-panel');
    renderJob();
  }
  function renderJob() {
    const job = state?.job;
    window._opApprovalPending?.(!!job?.approvals?.some(a => a.status === 'pending'));
    const hasDetails = job && (job.goal?.trim() || job.checkpoints.length || job.constraints.length || job.decisions.length);
    panel.hidden = !hasDetails || !expanded;
    if (!job) return;
    panel.replaceChildren();
    const heading = node('div', null, 'op-job-heading');
    heading.append(node('span', job.goal || 'Progress', 'op-job-goal'));
    if (job.checkpoints.length) heading.append(node('span',
      job.checkpoints.filter(s => s.status === 'completed').length + '/' + job.checkpoints.length,
      'op-job-progress'));
    const hide = button('', () => setJobExpanded(false), 'op-job-hide');
    hide.setAttribute('aria-label', 'Hide checklist'); hide.title = 'Hide checklist';
    hide.innerHTML = '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m5 12 5-5 5 5"/></svg>';
    heading.append(hide);
    panel.append(heading);
    if (job.checkpoints.length) {
      const completed = job.checkpoints.filter(s => s.status === 'completed').length;
      const meter = node('div', null, 'op-job-meter');
      meter.setAttribute('role', 'progressbar'); meter.setAttribute('aria-label', 'Checklist progress');
      meter.setAttribute('aria-valuemin', '0'); meter.setAttribute('aria-valuemax', String(job.checkpoints.length));
      meter.setAttribute('aria-valuenow', String(completed));
      const fill = node('span'); fill.style.width = (100 * completed / job.checkpoints.length) + '%'; meter.append(fill); panel.append(meter);
      const list = node('ol', null, 'op-job-checklist');
      job.checkpoints.forEach(step => {
        const li = node('li'); li.dataset.state = step.status;
        li.setAttribute('aria-label', step.step + ', ' + ({completed: 'completed', inProgress: 'in progress'}[step.status] || 'pending'));
        const mark = node('span', null, 'op-job-mark'); mark.setAttribute('aria-hidden', 'true');
        if (step.status === 'completed') mark.innerHTML = '<svg viewBox="0 0 16 16" fill="none"><path d="m4 8 2.5 2.5L12 5"/></svg>';
        else if (step.status === 'inProgress') mark.append(node('span'));
        li.append(mark, node('span', step.step, 'op-job-step')); list.append(li);
      });
      panel.append(list);
    }
    for (const [key, title] of [['constraints', 'Keep in mind'], ['decisions', 'Decisions']]) {
      if (!job[key].length) continue;
      panel.append(node('h3', title)); const list = node('ul');
      job[key].forEach(text => list.append(node('li', text))); panel.append(list);
    }
    const actions = node('div', null, 'op-wb-actions');

    const fresh = button('Start new job', async () => {
      try { await api('/job', {method: 'POST', body: {goal: ''}}); await refresh(true); }
      catch (e) { status(e.message); }
    }); fresh.disabled = !!agent.alive || !context().can_control; actions.append(fresh);
    if (diagnosticsOn) actions.append(button('Run details', () => openHealth(true)));
    panel.append(actions);

  }
  function renderCards() {
    const log = $('op-log');
    const signature = JSON.stringify([state.jobs.map(j => [j.id, j.revision]), context().can_control, !!agent.alive, agent.run_id, agent.started_ts, state.files.map(f => f.id), confirmingApprovalId, revisingApprovalId]);
    if (signature === lastRender && log.querySelector('[data-workbench-card]')) return;
    lastRender = signature;
    const previous = new Map([...log.querySelectorAll('[data-workbench-card]')].map(e => [e.dataset.workbenchCard, e]));
    function mount(card) {
      const old = previous.get(card.dataset.workbenchCard); previous.delete(card.dataset.workbenchCard);
      if (!old) { log.append(card); return; }
      if (old.tagName === 'DETAILS') card.open = old.open;
      if (old.querySelector('.op-wb-sources')?.open) card.querySelector('.op-wb-sources')?.setAttribute('open', '');
      if (old.outerHTML === card.outerHTML) return;
      const focused = old.contains(document.activeElement) ? document.activeElement.textContent : '';
      const morph = old.classList.contains('op-wb-approval') && !old.classList.contains('is-approved') &&
        card.classList.contains('is-approved') && !matchMedia('(prefers-reduced-motion: reduce)').matches;
      const from = morph ? {height: old.getBoundingClientRect().height,
        border: getComputedStyle(old).borderColor,
        heading: getComputedStyle(old.querySelector('.op-wb-approval-heading')).color,
        button: old.querySelector('.op-wb-actions > button:first-child')?.getBoundingClientRect()} : null;
      old.replaceWith(card);
      if (from) {
        const height = card.getBoundingClientRect().height;
        const heading = card.querySelector('.op-wb-approval-heading');
        const button = card.querySelector('.op-wb-actions > button:first-child');
        card.classList.add('op-wb-approval-morph');
        card.style.height = from.height + 'px';
        card.style.overflow = 'hidden';
        card.style.borderColor = from.border;
        heading.style.color = from.heading;
        if (button && from.button) {
          const to = button.getBoundingClientRect();
          button.style.transform = 'translate(' + (from.button.x - to.x) + 'px,' +
            (from.button.y - to.y) + 'px)';
        }
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (!card.isConnected) return;
          card.style.height = height + 'px';
          card.style.borderColor = '';
          heading.style.color = '';
          if (button) button.style.transform = '';
        }));
        setTimeout(() => {
          card.classList.remove('op-wb-approval-morph');
          card.style.height = card.style.overflow = '';
        }, 380);
      }
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
        const pendingApproval = job.approvals.find(a => a.source === 'user' && a.status === 'pending');
        // The card folds to its title (the owner 2026-10-19). The fold is remembered
        // per result so a re-render or reload keeps a long result out of the way.
        const head = node('div', null, 'op-wb-head');
        const fold = button('', () => setFolded(card, result.id, !card.classList.contains('collapsed')), 'op-wb-fold');
        fold.setAttribute('aria-label', 'Collapse result'); fold.setAttribute('aria-expanded', 'true');
        head.append(resultTag(result.status, !!pendingApproval), node('h3', result.title), fold);
        card.append(head, body);
        if (foldedResults().has(result.id)) setFolded(card, result.id, true);
        if (result.status === 'prepared' && pendingApproval) {
          const review = button('Review approval', () => {
            const target = [...log.querySelectorAll('[data-workbench-card]')].find(c => c.dataset.workbenchCard === pendingApproval.id);
            if (target) { target.scrollIntoView({block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'}); target.querySelector('button')?.focus({preventScroll: true}); }
          }, 'op-wb-review-approval');
          card.append(review);
        }
        const sources = resultSources(job, result);
        renderConfirmation(card, result, sources);
        renderSources(card, sources);
        for (const fid of result.file_ids) {
          const file = state.files.find(f => f.id === fid); if (!file) continue;
          const link = node('a', file.name, 'op-wb-source'); link.href = fileUrl(fid); link.download = file.name; card.append(link);
        }
        mount(card);
      }
      for (const approval of job.approvals.filter(a => a.source === 'user')) {
        const card = node('article', null, 'op-workbench op-wb-card op-wb-approval'); card.dataset.workbenchCard = approval.id;
        const approved = approval.status === 'approved', expired = approval.status === 'expired';
        const pending = approval.status === 'pending';
        const replaced = approval.status === 'superseded', declined = approval.status === 'declined';
        card.classList.toggle('is-approved', approved);
        card.classList.toggle('is-expired', expired);
        card.classList.toggle('is-replaced', replaced);
        card.classList.toggle('is-declined', declined);
        card.classList.toggle('is-closed', !pending && !approved && !expired);
        const heading = node('div', null, 'op-wb-approval-heading');
        approvalHeadingResize.observe(heading);
        const icon = node('span', null, 'op-wb-approval-icon'); icon.setAttribute('aria-hidden', 'true');
        icon.innerHTML = approved
          ? '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="8"/><path d="m6 10 2.7 2.7L14 7.5"/></svg>'
          : expired
            ? '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="8"/><path d="M10 5.5v4.8l3 1.7"/></svg>'
            : pending
              ? '<svg viewBox="0 0 20 20" fill="none"><path d="M10 2.5 18 17H2L10 2.5Z"/><path d="M10 7.3v4.7m0 2.5v.2"/></svg>'
              : replaced
                ? '<svg viewBox="0 0 20 20" fill="none"><path d="M4.5 7h10m0 0-2.6-2.6M14.5 7l-2.6 2.6M15.5 13h-10m0 0 2.6-2.6M5.5 13l2.6 2.6"/></svg>'
              : '<svg viewBox="0 0 20 20" fill="none"><circle cx="10" cy="10" r="8"/><path d="m7 7 6 6m0-6-6 6"/></svg>';
        const headingText = approved ? 'Approved' : expired ? 'Approval expired'
          : pending ? 'Operator needs your permission'
            : approval.status === 'changes_requested' ? 'Changes requested'
              : approval.status === 'superseded' ? 'Replaced' : 'Declined';
        heading.append(icon, node('span', headingText));
        card.append(heading);
        if (!pending) {
          card.append(approvalDestination(approval.action.destination, 'p', 'op-wb-approved-destination'));
          if (!approved) card.append(node('p', approval.action.description, 'op-wb-approval-description'));
        }
        else card.append(approvalDestination(approval.action.destination, 'h3'), node('p', approval.action.description));
        if (approval.action.amount) {
          const price = money(approval.action.amount, approval.action.currency);
          const amount = node('div', null, 'op-wb-amount');
          const value = node('span', null, 'op-wb-amount-value');
          if (price.symbol) value.append(node('span', price.symbol, 'op-wb-currency-symbol'));
          const decimal = price.value.lastIndexOf('.');
          if (decimal === -1) value.append(document.createTextNode(price.value));
          else value.append(document.createTextNode(price.value.slice(0, decimal)),
            node('span', '.', 'op-wb-decimal'), document.createTextNode(price.value.slice(decimal + 1)));
          amount.append(value, node('span', price.code, 'op-wb-amount-code'));
          card.append(amount);
        }
        const actions = node('div', null, 'op-wb-actions');
        const decide = async approved => {
          actions.querySelectorAll('button').forEach(b => { b.disabled = true; });
          try {
            await api('/approval', {method: 'POST', body: {id: approval.id, fingerprint: approval.fingerprint, approved}});
            confirmingApprovalId = '';
            lastRender = '';
            await refresh(true);
            if (approved && !agent.alive) resumeApproved(approval);
          }
          catch (e) {
            actions.querySelectorAll('button').forEach(b => { b.disabled = !context().can_control; });
            status(e.message);
          }
        };
        const resumeApproved = a => {
          if (!bridge.sendInternal('Continue with the approved action ' + a.id + '. Recheck its exact details before committing.'))
            status('Switch to Auto and wait for the current run to finish before continuing.');
        };
        let changeEditor = null;
        if (pending) {
          changeEditor = node('div', null, 'op-wb-change-editor');
          const changeField = node('textarea', null, 'op-wb-change-field');
          changeField.placeholder = 'What should Operator change?';
          changeField.setAttribute('aria-label', 'What should Operator change?');
          changeField.maxLength = 1000;
          changeField.rows = 3;
          changeField.value = revisingApprovalId === approval.id ? revisionDraft : '';
          changeField.addEventListener('input', () => { revisionDraft = changeField.value; });
          const cancelChanges = button('Cancel', () => {
            revisingApprovalId = ''; revisionDraft = '';
            changeEditor.hidden = true;
            actions.querySelectorAll('button').forEach(b => { b.disabled = !context().can_control; });
          });
          const sendChanges = button('Send changes', async () => {
            const note = changeField.value.trim();
            if (!note) { changeField.focus(); return; }
            if (!bridge.canSendUser()) { status('Switch to Auto to request changes.'); return; }
            sendChanges.disabled = cancelChanges.disabled = true;
            try {
              await api('/approval', {method: 'POST', body: {id: approval.id,
                fingerprint: approval.fingerprint, approved: false, change_request: note}});
              const destination = approvalDestination(approval.action.destination, 'span').textContent;
              const message = 'Change the pending approval for ' + destination + ': ' + note +
                '\nRequest approval again with the updated details before taking the action.';
              if (!bridge.sendUser(message)) status('Switch to Auto and send the requested changes to Operator.');
              revisingApprovalId = ''; revisionDraft = '';
              lastRender = '';
              await refresh(true);
            } catch (e) {
              sendChanges.disabled = cancelChanges.disabled = false;
              status(e.message);
            }
          }, 'op-wb-primary');
          changeEditor.append(changeField, sendChanges, cancelChanges);
          changeEditor.hidden = revisingApprovalId !== approval.id;
          // Keep the same button boxes across review stages. Replacing the row
          // shifted the remaining buttons under the pointer on every click.
          const primary = button('Approve', () => {
            if (confirmingApprovalId === approval.id) { decide(true); return; }
            confirmingApprovalId = approval.id;
            revisingApprovalId = ''; revisionDraft = ''; changeEditor.hidden = true;
            showApprovalStage(true);
            primary.focus({preventScroll: true});
          }, 'op-wb-primary');
          const change = button('Request changes', () => {
            if (confirmingApprovalId === approval.id) {
              confirmingApprovalId = '';
              showApprovalStage(false);
              primary.focus({preventScroll: true});
              return;
            }
            revisingApprovalId = approval.id;
            changeEditor.hidden = false;
            changeField.focus({preventScroll: true});
          });
          const decline = button('Decline', () => decide(false), 'op-wb-decline');
          actions.classList.add('op-wb-approval-actions');
          actions.append(primary, decline, change);
          const showApprovalStage = confirming => {
            primary.textContent = confirming ? 'Confirm?' : 'Approve';
            change.textContent = confirming ? 'Back' : 'Request changes';
            decline.style.visibility = confirming ? 'hidden' : '';
            primary.disabled = change.disabled = decline.disabled = !context().can_control;
          };
          showApprovalStage(confirmingApprovalId === approval.id);
        } else if (approved) {
          const resume = button('Continue', () => {}, 'op-wb-approved-continue');
          resume.disabled = true; actions.append(resume);
        }
        if (pending || approved) { card.append(actions); if (changeEditor) card.append(changeEditor); }
        mount(card);
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
      if (cid !== expected) { cid = expected; try { expanded = localStorage.getItem('operator-job-expanded-v1:' + cid) === '1'; } catch (_) { expanded = false; } lastRender = ''; steerId = ''; confirmingApprovalId = ''; revisingApprovalId = ''; revisionDraft = ''; status(''); }
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
      if (cid !== thread) { preview.close(); ++previewRequest; filesList.hidden = true; filesDialog.close(); panel.hidden = true; state = null; lastRender = ''; filesRender = ''; fileSearch.value = ''; fileStatus(''); renderFilePills(); $('op-log').querySelectorAll('[data-workbench-card]').forEach(e => e.remove()); }
      renderJob(); refresh(cid !== thread);
    },
    toggleJob: () => { const job = state?.job; if (!job || !(job.goal?.trim() || job.checkpoints.length || job.constraints.length || job.decisions.length)) return false; setJobExpanded(!expanded); return true; },
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
  document.fonts?.ready.then(() => $('op-log').querySelectorAll('.op-wb-approval-heading').forEach(fitApprovalHeading));
  refresh(true);
})();
