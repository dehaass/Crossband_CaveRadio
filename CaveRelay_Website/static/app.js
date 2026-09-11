const $ = (selector) => document.querySelector(selector);
let allMessages = [];

function setResult(selector, message, error = false) {
  const node = $(selector);
  node.textContent = message;
  node.classList.toggle('error', error);
}

function healthLabel(ok) { return ok ? 'ONLINE' : 'OFFLINE'; }

function updateHealthBadge(selector, ok) {
  const badge = $(selector);
  badge.className = `health-badge ${ok ? 'online' : 'offline'}`;
  badge.innerHTML = `<span class="status-dot ${ok ? 'good' : 'bad'}"></span>${healthLabel(ok)}`;
}

async function refresh() {
  await Promise.all([refreshHealth(), refreshMessages()]);
}

async function refreshHealth() {
  const healthResponse = await fetch(`/api/health?ts=${Date.now()}`, { cache: 'no-store' });
  const health = await healthResponse.json();
  const fldigiOk = Boolean(health.fldigi?.healthy);
  const aprsOk = Boolean(health.aprs?.connected && health.aprs?.running);
  const overallOk = Boolean(health.healthy);
  $('#overall-dot').className = `status-dot ${overallOk ? 'good' : 'bad'}`;
  $('#overall-label').textContent = overallOk ? 'All systems nominal' : 'Attention required';
  updateHealthBadge('#fldigi-badge', fldigiOk);
  updateHealthBadge('#aprs-badge', aprsOk);
  $('#fldigi-status').textContent = fldigiOk ? 'READY' : 'OFFLINE';
  $('#fldigi-detail').textContent = health.fldigi?.status || health.fldigi?.last_error || 'XML-RPC status';
  $('#aprs-status').textContent = aprsOk ? 'READY' : 'OFFLINE';
  $('#aprs-detail').textContent = aprsOk ? `${health.aprs.host}:${health.aprs.port}` : (health.aprs.last_error || 'KISS TCP status');
  $('#last-checked').textContent = `Last checked: ${new Date().toLocaleTimeString()}`;
}

async function refreshMessages() {
  const messagesResponse = await fetch(`/api/messages?limit=100&ts=${Date.now()}`, { cache: 'no-store' });
  const messages = await messagesResponse.json();
  $('#message-count').textContent = messages.length;
  allMessages = messages;
  updateSuggestions(messages);
  renderLogs();
}

function updateSuggestions(messages) {
  const destinations = new Set();
  const relays = new Set();
  messages.forEach((entry) => {
    if (entry.transport === 'aprs_rx' && entry.source) destinations.add(entry.source);
    if (entry.transport === 'aprs_tx' && entry.destination) destinations.add(entry.destination);
    if ((entry.transport === 'fldigi_rx' || entry.transport === 'fldigi') && entry.via) relays.add(entry.via);
  });
  $('#aprs-destinations').innerHTML = [...destinations].sort().map((value) => `<option value="${escapeHtml(value)}"></option>`).join('');
  $('#fldigi-relays').innerHTML = [...relays].sort().map((value) => `<option value="${escapeHtml(value)}"></option>`).join('');
}

function renderLogs() {
  renderLog('aprs', allMessages.filter((entry) => ['aprs_rx', 'aprs_tx'].includes(entry.transport)));
  renderLog('fldigi', allMessages.filter((entry) => ['fldigi_rx', 'fldigi_tx', 'fldigi'].includes(entry.transport)));
}

function renderLog(kind, sourceEntries) {
  const search = $(`#${kind}-search`).value.trim().toLowerCase();
  const direction = $(`#${kind}-direction`).value;
  const sort = $(`#${kind}-sort`).value;
  const crcFilter = kind === 'fldigi' ? $(`#${kind}-crc`).value : 'all';
  let entries = sourceEntries.filter((entry) => {
    const haystack = JSON.stringify(entry).toLowerCase();
    const directionMatch = direction === 'all' || entry.transport === direction;
    const crcMatch = crcFilter === 'all' || (crcFilter === 'valid' && entry.checksum_valid === true) || (crcFilter === 'invalid' && entry.checksum_valid === false);
    return directionMatch && crcMatch && (!search || haystack.includes(search));
  });
  entries.sort((left, right) => {
    if (sort === 'transport') return String(left.transport).localeCompare(String(right.transport));
    const leftTime = String(left.received_at || '');
    const rightTime = String(right.received_at || '');
    return sort === 'oldest' ? leftTime.localeCompare(rightTime) : rightTime.localeCompare(leftTime);
  });
  $(`#${kind}-count`)?.replaceChildren(document.createTextNode(`${entries.length} record${entries.length === 1 ? '' : 's'}`));
  const body = $(`#${kind}-log-body`);
  if (!entries.length) {
    body.innerHTML = `<tr><td colspan="${kind === 'aprs' ? 4 : 6}" class="empty">No matching traffic.</td></tr>`;
    return;
  }
  body.innerHTML = entries.map((entry, index) => {
    const detail = kind === 'aprs'
      ? ((entry.parsed && entry.parsed.message) || entry.message || entry.info || '—')
      : (entry.message || entry.info || entry.raw || '—');
    let crc = '—';
    let crcClass = '';
    if (typeof entry.checksum_valid === 'boolean') {
      crc = entry.checksum_valid ? 'valid' : 'mismatch';
      crcClass = entry.checksum_valid ? 'good' : 'bad';
    }
    const from = kind === 'aprs' ? (entry.source || '—') : (entry.from_call || entry.source || '—');
    const to = kind === 'aprs' ? ((entry.parsed && entry.parsed.to) || entry.destination || '—') : (entry.to_call || entry.destination || '—');
    const via = entry.via || '—';
    const messageId = entry.parsed?.message_id || '—';
    const snStatus = entry.sn_status || '—';
    const direction = entry.transport.endsWith('_rx') ? 'RX' : 'TX';
    const columns = kind === 'aprs'
      ? `<td class="time">${escapeHtml(entry.received_at || '—')}</td><td class="transport">${direction}</td><td class="route">${escapeHtml(from)}</td><td class="route">${escapeHtml(to)}</td><td class="detail">${escapeHtml(detail)}</td><td class="sn">${escapeHtml(messageId)}</td>`
      : `<td class="time">${escapeHtml(entry.received_at || '—')}</td><td class="transport">${direction}</td><td class="route">${escapeHtml(from)}</td><td class="route">${escapeHtml(to)}</td><td class="route">${escapeHtml(via)}</td><td class="detail">${escapeHtml(detail)}</td><td class="sn">${escapeHtml(snStatus)}</td>`;
    return `<tr class="log-row" tabindex="0" data-log-kind="${kind}" data-log-index="${index}">${columns}</tr>`;
  }).join('');
  body.querySelectorAll('.log-row').forEach((row) => {
    row.addEventListener('click', () => openDetails(entries[Number(row.dataset.logIndex)]));
    row.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        openDetails(entries[Number(row.dataset.logIndex)]);
      }
    });
  });
}

function openDetails(entry) {
  const drawer = $('#detail-drawer');
  const title = entry.message || entry.info || entry.transport || 'Message details';
  $('#detail-title').textContent = title;
  const fields = buildDetailFields(entry).map(([key, value]) => `<div class="detail-field"><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(formatDetailValue(value))}</dd></div>`).join('');
  $('#detail-content').innerHTML = `<dl>${fields}</dl>`;
  $('#detail-backdrop').hidden = false;
  drawer.classList.add('open');
  drawer.setAttribute('aria-hidden', 'false');
  $('#detail-close').focus();
}

function buildDetailFields(entry) {
  if (entry.transport === 'aprs_rx' || entry.transport === 'aprs_tx') {
    const parsed = entry.parsed || {};
    return [
      ['timestamp', entry.received_at],
      ['transport', entry.transport],
      ['source', entry.source || '—'],
      ['to', parsed.to || entry.destination || '—'],
      ['message', parsed.message || entry.message || '—'],
      ['message_id', parsed.message_id || '—'],
      ['path', entry.path || '—'],
    ];
  }

  const fields = [
    ['timestamp', entry.received_at],
    ['transport', entry.transport],
    ['from', entry.from_call],
    ['to', entry.to_call],
    ['via', entry.via],
    ['message', entry.message],
    ['checksum', entry.checksum],
  ];
  if (entry.transport !== 'fldigi_tx') {
    fields.push(
      ['S/N Ave', entry.sn_average],
      ['S/N Status', entry.sn_status],
      ['S/N Samples', entry.sn_samples],
    );
  }
  return fields;
}

function formatDetailValue(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '—';
  return String(value);
}

function closeDetails() {
  $('#detail-backdrop').hidden = true;
  $('#detail-drawer').classList.remove('open');
  $('#detail-drawer').setAttribute('aria-hidden', 'true');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[character]));
}

async function submitForm(event, endpoint, resultSelector, fields) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = Object.fromEntries(new FormData(form).entries());
  setResult(resultSelector, 'Sending...');
  try {
    const response = await fetch(endpoint, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    setResult(resultSelector, result.packets ? `Sent ${result.packets} APRS packet${result.packets === 1 ? '' : 's'}.` : 'Transmitted.');
    form.reset();
    await refresh();
  } catch (error) {
    setResult(resultSelector, error.message, true);
  }
}

$('#refresh-button').addEventListener('click', refresh);
$('#detail-close').addEventListener('click', closeDetails);
$('#detail-backdrop').addEventListener('click', closeDetails);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeDetails(); });
$('#aprs-form').addEventListener('submit', (event) => submitForm(event, '/api/send/aprs', '#aprs-result'));
$('#fldigi-form').addEventListener('submit', (event) => submitForm(event, '/api/send/fldigi', '#fldigi-result'));
['aprs-search', 'aprs-direction', 'aprs-sort', 'fldigi-search', 'fldigi-direction', 'fldigi-crc', 'fldigi-sort'].forEach((id) => {
  $(`#${id}`).addEventListener('input', renderLogs);
  $(`#${id}`).addEventListener('change', renderLogs);
});
refresh().catch((error) => { $('#overall-label').textContent = error.message; });
setInterval(() => refreshHealth().catch(() => {
  $('#overall-dot').className = 'status-dot bad';
  $('#overall-label').textContent = 'Health check failed';
  updateHealthBadge('#fldigi-badge', false);
  updateHealthBadge('#aprs-badge', false);
  $('#last-checked').textContent = `Last checked: ${new Date().toLocaleTimeString()} (error)`;
}), 3000);
setInterval(() => refreshMessages().catch(() => {}), 10000);
