const $ = (selector) => document.querySelector(selector);
let logEntries = [];
const knownSources = new Set();

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({ '&':'&amp;', '<':'&gt;', '>':'&gt;', "'":'&#39;', '"':'&quot;' }[character]));
}

function populateSources(entries) {
  const sourceSelect = $('#log-source');
  const selectedSource = sourceSelect.value;
  entries.map((entry) => entry.source).filter(Boolean).forEach((source) => knownSources.add(source));
  const sources = [...knownSources].sort();
  sourceSelect.innerHTML = '<option value="">All sources</option>' + sources.map((source) => `<option value="${escapeHtml(source)}">${escapeHtml(source)}</option>`).join('');
  sourceSelect.value = sources.includes(selectedSource) ? selectedSource : '';
}

function renderLogs() {
  const search = $('#log-search').value.trim().toLowerCase();
  const entries = logEntries.filter((entry) => !search || JSON.stringify(entry).toLowerCase().includes(search));
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
  if ($('#log-level').value) params.set('level', $('#log-level').value);
  if ($('#log-source').value) params.set('source', $('#log-source').value);
  const response = await fetch(`/api/logs?${params}`, { cache: 'no-store' });
  if (!response.ok) throw new Error('Unable to load system logs');
  logEntries = await response.json();
  populateSources(logEntries);
  renderLogs();
  $('#last-checked').textContent = `Last checked: ${new Date().toLocaleTimeString()}`;
}

$('#refresh-button').addEventListener('click', () => refreshLogs().catch(showError));
$('#log-search').addEventListener('input', renderLogs);
['log-level', 'log-source'].forEach((id) => $("#" + id).addEventListener('change', () => refreshLogs().catch(showError)));

function showError(error) {
  $('#log-body').innerHTML = `<tr><td colspan="4" class="empty">${escapeHtml(error.message)}</td></tr>`;
}

refreshLogs().catch(showError);
setInterval(() => refreshLogs().catch(showError), 10000);