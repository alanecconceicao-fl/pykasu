const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const basePath = document.body.dataset.basePath || '';
const appPath = (route) => `${basePath}${route}`;
let visitSequence;
let lastState;
let searchTimer;
let refreshTimer;

function params() {
  const search = new URLSearchParams({ window: $('#window')?.value || '24h' });
  search.set('ack', document.body.dataset.page === 'updates' ? '1' : '0');
  if ($('#source')?.value) search.set('source', $('#source').value);
  if ($('#search')?.value.trim()) search.set('q', $('#search').value.trim());
  if (visitSequence !== undefined) search.set('since', String(visitSequence));
  return search;
}

function prettyDate(value) {
  if (!value) return 'Discovered';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? 'Discovered' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date);
}

function renderUpdates(state) {
  const select = $('#source');
  const selected = select.value;
  select.innerHTML = '<option value="">All sources</option>' + state.sources.filter(s => s.enabled).map(s => `<option value="${s.id}">${escapeHtml(s.name)}</option>`).join('');
  select.value = selected;
  $('#count').textContent = `${state.items.length} ${state.items.length === 1 ? 'update' : 'updates'}`;
  $('#items').innerHTML = state.items.length ? state.items.map(item => {
    const label = item.summary_status === 'feed-excerpt' ? 'Based on feed excerpt' : item.summary_status === 'ready' ? 'Source summary' : item.summary_status === 'unavailable' ? 'Summary unavailable' : 'Summary pending';
    const summary = item.summary ? `<p class="summary">${escapeHtml(item.summary)} <span class="summary-label">${label}</span></p>` : `<p class="summary muted">${label}</p>`;
    return `<article class="item ${item.is_new ? 'is-new' : ''}">
      <div class="item-meta">${item.is_new ? '<span class="new-label">NEW</span>' : ''}<span>${escapeHtml(item.source_names || 'Unknown source')}</span><time datetime="${escapeHtml(item.published_at || item.discovered_at)}">${prettyDate(item.published_at || item.discovered_at)}</time></div>
      <h2><a href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(item.headline)}</a><span class="external" aria-hidden="true">↗</span></h2>${summary}
    </article>`;
  }).join('') : `<div class="empty"><span class="empty-icon">◷</span><h2>No updates in this view</h2><p>Add a source or widen the time period to see more reporting.</p><a href="${appPath('/sources')}" class="button accent">Manage sources →</a></div>`;
  const successful = state.sources.filter(s => s.enabled && s.last_success_at);
  $('#freshness').textContent = successful.length ? `Latest source check ${prettyDate(successful.map(s => s.last_success_at).sort().at(-1))}` : 'Waiting for first feed check';
  $('#status').textContent = state.refresh.active ? `Refreshing feeds · ${state.refresh.sources_done} of ${state.refresh.sources_total} checked` : state.refresh.errors?.length ? 'Some sources could not be refreshed. Saved updates are still available.' : 'Your saved updates are up to date.';
}

function renderSources(state) {
  const sources = state.sources;
  $('#source-count').textContent = sources.filter(s => s.enabled).length;
  $('#source-list').innerHTML = sources.length ? sources.map(source => `<article class="source-card" data-id="${source.id}">
    <div class="source-card-head"><div class="source-avatar">${escapeHtml(source.name.slice(0, 1).toUpperCase())}</div><div class="source-title"><h2>${escapeHtml(source.name)}</h2><p>${escapeHtml(source.feed_url)}</p></div><span class="pill ${source.enabled ? 'enabled' : 'disabled'}">${source.enabled ? 'Enabled' : 'Disabled'}</span></div>
    <div class="source-details"><span>${source.last_success_at ? `Last checked ${prettyDate(source.last_success_at)}` : 'Not checked yet'}</span>${source.last_error ? `<span class="error">${escapeHtml(source.last_error)}</span>` : ''}</div>
    <div class="source-options"><label class="check"><input class="enabled-toggle" type="checkbox" ${source.enabled ? 'checked' : ''}><span>Enabled</span></label><label class="check"><input class="extract-toggle" type="checkbox" ${source.extract_articles ? 'checked' : ''}><span>Extract articles</span></label><button class="text-button save-source">Save changes</button></div>
  </article>`).join('') : '<div class="empty"><span class="empty-icon">＋</span><h2>No sources yet</h2><p>Add an RSS or Atom feed to get started.</p></div>';
}

async function loadState() {
  const response = await fetch(`${appPath('/api/state')}?${params()}`, { cache: 'no-store' });
  const state = await response.json();
  if (!response.ok) throw new Error(state.message || 'Could not load application state');
  if (visitSequence === undefined) visitSequence = state.acknowledgedSequence;
  lastState = state;
  if (document.body.dataset.page === 'updates') renderUpdates(state);
  else renderSources(state);
  return state;
}

async function startRefresh() {
  await fetch(appPath('/api/refresh'), { method: 'POST', cache: 'no-store' });
  watchRefresh();
}

function watchRefresh() {
  if (refreshTimer) return;
  refreshTimer = setInterval(async () => {
    try {
      const statusResponse = await fetch(appPath('/api/refresh-status'), { cache: 'no-store' });
      const status = await statusResponse.json();
      if (document.body.dataset.page === 'updates' && status.new_items_available) $('#refresh-banner').hidden = false;
      if (status.active) {
        if (document.body.dataset.page === 'updates') $('#status').textContent = `Refreshing feeds · ${status.sources_done} of ${status.sources_total} checked`;
      } else {
        clearInterval(refreshTimer); refreshTimer = null;
        if (document.body.dataset.page === 'updates' && status.headlines_saved > 0) $('#refresh-banner').hidden = false;
        else await loadState();
      }
    } catch { /* Keep saved page content visible while polling. */ }
  }, 2000);
}

if (document.body.dataset.page === 'updates') {
  $('#refresh').addEventListener('click', async () => { $('#refresh').disabled = true; $('#status').textContent = 'Checking your sources…'; try { await startRefresh(); } finally { $('#refresh').disabled = false; } });
  $('#show-updates').addEventListener('click', async () => { $('#refresh-banner').hidden = true; await loadState(); });
  $('#window').addEventListener('change', () => { void loadState(); });
  $('#source').addEventListener('change', () => { void loadState(); });
  $('#search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => void loadState(), 250); });
} else {
  $('#source-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    const response = await fetch(appPath('/api/sources'), { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: values.name, feed_url: values.feed_url, enabled: true, extract_articles: form.elements.extract_articles.checked }) });
    const result = await response.json();
    showNotice(response.ok ? 'Source saved. It will be checked shortly.' : (result.message || 'Could not save this source.'), !response.ok);
    if (response.ok) { form.reset(); form.elements.extract_articles.checked = true; visitSequence = undefined; await loadState(); await startRefresh(); }
  });
  $('#source-list').addEventListener('click', async event => {
    const button = event.target.closest('.save-source');
    if (!button) return;
    const card = button.closest('.source-card');
    const source = lastState.sources.find(s => Number(s.id) === Number(card.dataset.id));
    const response = await fetch(appPath(`/api/sources/${source.id}`), { method: 'PATCH', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ name: source.name, feed_url: source.feed_url, enabled: card.querySelector('.enabled-toggle').checked, extract_articles: card.querySelector('.extract-toggle').checked }) });
    const result = await response.json();
    showNotice(response.ok ? 'Source settings saved.' : (result.message || 'Could not save source settings.'), !response.ok);
    if (response.ok) { visitSequence = undefined; await loadState(); await startRefresh(); }
  });
}

function showNotice(message, error = false) {
  const notice = $('#notice');
  notice.textContent = message;
  notice.classList.toggle('error-notice', error);
  notice.hidden = false;
  setTimeout(() => { notice.hidden = true; }, 6000);
}

loadState().then(() => startRefresh()).catch(error => {
  const status = $('#status') || $('#notice');
  if (status) { status.hidden = false; status.textContent = error.message; }
});
