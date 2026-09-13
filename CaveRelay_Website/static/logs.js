const $ = (selector) => document.querySelector(selector);
let logEntries = [];
let relayLogContent = '';
const knownSources = new Set();
let activeTab = 'activity';
// Offset (ms) between the server's clock and this browser's clock, so the
// displayed "system time" tracks the server (which stamps the log entries),
// not whatever time the client happens to have set.
let serverTimeOffsetMs = 0;

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&':'&amp;', '<':'&gt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[character]));
}

function switchTab(tab) {
  activeTab = tab;
  const isActivity = tab === 'activity';
  $('#tab-activity').classList.toggle('active', isActivity);
  $('#tab-activity').setAttribute('aria-selected', isActivity ? 'true' : 'false');
  $('#tab-relay').classList.toggle('active', !isActivity);
  $('#tab-relay').setAttribute('aria-selected', !isActivity ? 'true' : 'false');
  $('#view-activity').hidden = !isActivity;
  $('#view-relay').hidden = isActivity;
  window.location.hash = isActivity ? '#activity' : '#relay';
  if (isActivity) {
    renderLogs();
    refreshLogs().catch(showError);
  } else {
    renderRelayLog();
    refreshRelayLog().catch(showRelayError);
  }
}

function populateSources(entries) {
  const sourceSelect = $('#log-source');
  const selectedSource = sourceSelect.value;
  entries.map((entry) => entry.source).filter(Boolean).forEach((source) => knownSources.add(source));
  const sources = [...knownSources].sort();
  sourceSelect.innerHTML = '<option value="">All sources</option>' + sources.map((source) => `<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`).join('');
  sourceSelect.value = sources.includes(selectedSource) ? selectedSource : '';
}

function checkedLevels() {
  return [...document.querySelectorAll('#log-level-filters input:checked')].map((checkbox) => checkbox.value);
}

function dateRangeBounds() {
  const sinceMs = Number($('#log-since').value || 0);
  if (!sinceMs) return { start: null };
  // Measure "how far back" from the server's clock, not the browser's own.
  return { start: Date.now() + serverTimeOffsetMs - sinceMs };
}

function renderLogs() {
  if (activeTab !== 'activity') return;
  const search = $('#log-search').value.trim().toLowerCase();
  const levels = checkedLevels();
  const { start } = dateRangeBounds();
  const entries = logEntries
    .filter((entry) => levels.includes((entry.level || 'UNKNOWN').toUpperCase()))
    .filter((entry) => !search || JSON.stringify(entry).toLowerCase().includes(search))
    .filter((entry) => {
      if (start === null) return true;
      const entryTime = entry.timestamp ? new Date(entry.timestamp).getTime() : NaN;
      if (Number.isNaN(entryTime)) return true;
      return entryTime >= start;
    });
  $('#log-count').textContent = `${entries.length} record${entries.length === 1 ? '' : 's'}`;
  const body = $('#log-body');
  if (!entries.length) {
    body.innerHTML = '<tr><td colspan="4" class="empty">No matching log records.</td></tr>';
    return;
  }
  body.innerHTML = entries.map((entry) => `<tr><td class="time">${escapeHtml(entry.timestamp || '—')}</td><td><span class="log-level log-level-${escapeHtml(entry.level || 'UNKNOWN').toLowerCase()}">${escapeHtml(entry.level || 'UNKNOWN')}</span></td><td class="route">${escapeHtml(entry.source || '—')}</td><td class="detail">${escapeHtml(entry.message || '—')}${entry.exception ? `<pre class="log-exception">${escapeHtml(entry.exception)}</pre>` : ''}</td></tr>`).join('');
}

async function refreshLogs() {
  const params = new URLSearchParams({ limit: '1000' });
  if ($('#log-source').value) params.set('source', $('#log-source').value);
  const response = await fetch(`/api/logs?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error('Unable to load system logs');
  logEntries = await response.json();
  populateSources(logEntries);
  renderLogs();
  $('#last-checked').textContent = `Last checked: ${new Date().toLocaleTimeString()}`;
}

function renderRelayLog() {
  if (activeTab !== 'relay') return;
  const search = $('#relay-log-search').value.trim().toLowerCase();
  const output = $('#relay-log-output');
  const wrap = $('#relay-log-wrap');
  
  if (!relayLogContent) {
    output.textContent = 'relay.log is empty or not yet created.';
    $('#log-count').textContent = '0 lines';
    return;
  }

  const lines = relayLogContent.split('\n');
  const filtered = search ? lines.filter((line) => line.toLowerCase().includes(search)) : lines;
  $('#log-count').textContent = `${filtered.length} line${filtered.length === 1 ? '' : 's'}${search ? ` (of ${lines.length})` : ''}`;
  output.textContent = filtered.join('\n') || 'No matching lines in relay.log.';
  
  if ($('#relay-autoscroll').checked) {
    wrap.scrollTop = wrap.scrollHeight;
  }
}

async function refreshRelayLog() {
  const response = await fetch(`/api/relay_log?ts=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error('Unable to load relay.log');
  const data = await response.json();
  relayLogContent = data.content || '';
  renderRelayLog();
  $('#last-checked').textContent = `Last checked: ${new Date().toLocaleTimeString()}`;
}

async function refreshActive() {
  if (activeTab === 'activity') {
    await refreshLogs();
  } else {
    await refreshRelayLog();
  }
}

function updateClock() {
  const now = new Date(Date.now() + serverTimeOffsetMs);
  const pad = (value) => String(value).padStart(2, '0');
  const datePart = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const timePart = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  $('#live-clock').textContent = `${datePart} ${timePart}`;
}

async function syncServerTime() {
  const requestedAt = Date.now();
  const response = await fetch('/api/time', { cache: 'no-store' });
  if (!response.ok) return;
  const { epoch_ms: serverEpochMs } = await response.json();
  // Approximate the network round-trip so the offset reflects the moment the
  // server actually read its clock, not when the response arrived.
  const roundTripMs = Date.now() - requestedAt;
  serverTimeOffsetMs = serverEpochMs + roundTripMs / 2 - Date.now();
  updateClock();
}

function showError(error) {
  $('#log-body').innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(error.message)}</td></tr>`;
}

function showRelayError(error) {
  $('#relay-log-output').textContent = `Error: ${error.message}`;
}

$('#tab-activity').addEventListener('click', () => switchTab('activity'));
$('#tab-relay').addEventListener('click', () => switchTab('relay'));
$('#refresh-button').addEventListener('click', () => refreshActive().catch(activeTab === 'activity' ? showError : showRelayError));
$('#log-search').addEventListener('input', renderLogs);
$('#log-source').addEventListener('change', () => refreshLogs().catch(showError));
document.querySelectorAll('#log-level-filters input').forEach((checkbox) => checkbox.addEventListener('change', renderLogs));
$('#log-since').addEventListener('change', renderLogs);

$('#relay-log-search').addEventListener('input', renderRelayLog);
$('#relay-autoscroll').addEventListener('change', () => {
  if ($('#relay-autoscroll').checked) {
    $('#relay-log-wrap').scrollTop = $('#relay-log-wrap').scrollHeight;
  }
});
$('#relay-wrap').addEventListener('change', () => {
  $('#relay-log-output').classList.toggle('wrap', $('#relay-wrap').checked);
});

updateClock();
setInterval(updateClock, 1000);
syncServerTime().catch(() => {});
setInterval(() => syncServerTime().catch(() => {}), 30000);

if (window.location.hash === '#relay') {
  switchTab('relay');
} else {
  switchTab('activity');
}

setInterval(() => refreshActive().catch(() => {}), 6000);