import { api } from './api.js';

let items = [];
let sources = [];
let filter = 'all';
let dependencies = {};

function safeUrl(value) {
  try {
    const url = new URL(value, location.origin);
    return ['http:', 'https:'].includes(url.protocol) ? url.href : '#';
  } catch {
    return '#';
  }
}

function itemDate(item) {
  return item.published_at ?? item.published ?? item.date ?? item.created_at ?? item.fetched_at ?? null;
}

function itemSource(item) {
  return item.source_title ?? item.feed_title ?? item.source ?? item.feed?.title ?? 'Источник';
}

function toDate(value) {
  if (value instanceof Date) return value;
  if (typeof value === 'number' || (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value.trim()))) {
    const number = Number(value);
    return new Date(Math.abs(number) < 1e12 ? number * 1000 : number);
  }
  return new Date(value);
}

function initials(value) {
  const parts = String(value || '').trim().split(/\s+/u).filter(Boolean);
  return (parts.length > 1 ? `${parts[0][0]}${parts[1][0]}` : parts[0]?.slice(0, 2) || 'R').toUpperCase();
}

function relativeTime(value) {
  if (!value) return '';
  const date = toDate(value);
  if (Number.isNaN(date.getTime())) return '';
  const seconds = Math.max(0, (Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return 'только что';
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин. назад`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч. назад`;
  if (seconds < 172800) return 'вчера';
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}

function svgIcon(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${id}`);
  svg.append(use);
  return svg;
}

function sourceAvatar(name) {
  const avatar = document.createElement('span');
  avatar.className = 'source-avatar';
  avatar.textContent = initials(name);
  return avatar;
}

function renderOverview() {
  const root = document.getElementById('overview-feed');
  if (!root) return;
  if (!items.length) {
    root.innerHTML = '<div class="empty-state compact"><p>В ленте пока нет материалов.</p></div>';
    return;
  }
  root.replaceChildren(...items.slice(0, 3).map((item) => {
    const story = document.createElement('div');
    const content = document.createElement('div');
    const link = document.createElement('a');
    const meta = document.createElement('small');
    const source = itemSource(item);
    story.className = 'preview-story';
    link.href = safeUrl(item.url ?? item.link);
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = item.title || 'Без названия';
    meta.textContent = `${source} · ${relativeTime(itemDate(item))}`;
    content.append(link, meta);
    story.append(sourceAvatar(source), content);
    return story;
  }));
}

function filteredItems() {
  if (filter !== 'today') return items;
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return items.filter((item) => toDate(itemDate(item)).getTime() >= start);
}

function renderItems() {
  const root = document.getElementById('feed-list');
  const count = document.getElementById('feed-count');
  if (!root) return;
  const visible = filteredItems();
  if (count) count.textContent = `${visible.length} ${visible.length === 1 ? 'материал' : 'материалов'}`;
  if (!visible.length) {
    root.innerHTML = '<div class="card empty-state"><strong>Лента пуста</strong><p>Добавьте RSS или Atom-источник в личном режиме.</p></div>';
    return;
  }
  root.replaceChildren(...visible.map((item) => {
    const article = document.createElement('article');
    const content = document.createElement('div');
    const title = document.createElement('h2');
    const link = document.createElement('a');
    const excerpt = document.createElement('p');
    const meta = document.createElement('div');
    const sourceMeta = document.createElement('span');
    const time = document.createElement('span');
    const external = document.createElement('a');
    const source = itemSource(item);
    const href = safeUrl(item.url ?? item.link);
    article.className = 'card feed-item';
    content.className = 'feed-item-content';
    link.href = href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = item.title || 'Без названия';
    excerpt.textContent = item.excerpt ?? item.summary ?? item.description ?? 'Откройте материал, чтобы прочитать подробнее.';
    meta.className = 'feed-meta';
    sourceMeta.textContent = source;
    time.textContent = relativeTime(itemDate(item));
    meta.append(sourceMeta, time);
    title.append(link);
    content.append(title, excerpt, meta);
    external.className = 'external-button';
    external.href = href;
    external.target = '_blank';
    external.rel = 'noopener noreferrer';
    external.setAttribute('aria-label', `Открыть: ${item.title || 'материал'}`);
    external.append(svgIcon('i-external'));
    article.append(sourceAvatar(source), content, external);
    return article;
  }));
}

function renderSources() {
  const root = document.getElementById('feed-sources');
  if (!root) return;
  if (!api.session.authenticated) {
    root.innerHTML = '<div class="empty-state compact"><p>Войдите, чтобы управлять источниками.</p></div>';
    return;
  }
  if (!sources.length) {
    root.innerHTML = '<div class="empty-state compact"><p>Источники ещё не добавлены.</p></div>';
    return;
  }
  root.replaceChildren(...sources.map((source) => {
    const row = document.createElement('div');
    const details = document.createElement('span');
    const name = document.createElement('strong');
    const url = document.createElement('small');
    const remove = document.createElement('button');
    const title = source.title || source.name || 'RSS';
    row.className = 'source-item';
    name.textContent = title;
    url.textContent = source.url || '';
    details.append(name, url);
    remove.type = 'button';
    remove.setAttribute('aria-label', `Удалить источник ${title}`);
    remove.append(svgIcon('i-trash'));
    remove.addEventListener('click', () => removeSource(source));
    row.append(sourceAvatar(title), details, remove);
    return row;
  }));
}

async function loadSources() {
  if (!api.session.authenticated) {
    sources = [];
    renderSources();
    return;
  }
  try {
    sources = await api.getFeeds();
  } catch (error) {
    sources = [];
    if (error.status !== 401) dependencies.toast?.('Источники не загружены', error.message, 'error');
  }
  renderSources();
}

async function removeSource(source) {
  if (!confirm(`Удалить RSS-источник «${source.title || source.name || 'RSS'}»?`)) return;
  try {
    await api.deleteFeed(source.id);
    dependencies.toast?.('Источник удалён', source.title || source.name || 'RSS');
    await Promise.all([loadSources(), loadFeed()]);
  } catch (error) {
    dependencies.toast?.('Не удалось удалить', error.message, 'error');
  }
}

export async function loadFeed({ quiet = false } = {}) {
  try {
    items = await api.getFeedItems();
    items.sort((a, b) => toDate(itemDate(b)) - toDate(itemDate(a)));
    renderItems();
    renderOverview();
    dependencies.onUpdate?.(items);
    return items;
  } catch (error) {
    items = [];
    renderItems();
    renderOverview();
    if (!quiet) dependencies.toast?.('Лента не обновлена', error.message, 'error');
    return [];
  }
}

export function initFeeds(options = {}) {
  dependencies = options;
  document.getElementById('show-feed-form')?.addEventListener('click', () => {
    if (!api.session.authenticated) return dependencies.requireAuth?.();
    const form = document.getElementById('add-feed-form');
    form.hidden = false;
    form.querySelector('input')?.focus();
  });
  document.querySelector('[data-cancel-feed]')?.addEventListener('click', () => { document.getElementById('add-feed-form').hidden = true; });
  document.getElementById('add-feed-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!api.session.authenticated) return dependencies.requireAuth?.();
    const data = new FormData(event.currentTarget);
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      const url = data.get('url').trim();
      let title = data.get('title').trim();
      if (!title) {
        try { title = new URL(url).hostname.replace(/^www\./, ''); } catch { title = 'RSS'; }
      }
      await api.addFeed({ title, url });
      event.currentTarget.reset();
      event.currentTarget.hidden = true;
      dependencies.toast?.('Источник добавлен', 'Материалы появятся после обновления');
      await Promise.all([loadSources(), loadFeed()]);
    } catch (error) {
      dependencies.toast?.('Источник не добавлен', error.message, 'error');
    } finally {
      submit.disabled = false;
    }
  });
  document.getElementById('refresh-feed')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      if (api.session.authenticated) await api.refreshFeeds();
      await Promise.all([loadFeed(), loadSources()]);
    } catch (error) {
      dependencies.toast?.('Обновление не запущено', error.message, 'error');
    } finally {
      event.currentTarget.disabled = false;
    }
  });
  document.querySelectorAll('[data-feed-filter]').forEach((button) => {
    button.addEventListener('click', () => {
      filter = button.dataset.feedFilter;
      document.querySelectorAll('[data-feed-filter]').forEach((item) => item.classList.toggle('is-active', item === button));
      renderItems();
    });
  });
  document.addEventListener('portal:session', () => loadSources());
  loadSources();
  return loadFeed({ quiet: true });
}
