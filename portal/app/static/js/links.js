import { api } from './api.js';
import { downloadSvg, renderQr } from './tools.js';

let links = [];
let lastQr = '';
let dependencies = {};

function shortUrl(link) {
  const code = link.code ?? link.slug ?? link.id;
  return link.short_url || link.shortUrl || `${location.origin}/s/${encodeURIComponent(code)}`;
}

function targetUrl(link) {
  return link.target_url ?? link.url ?? link.target ?? '';
}

function svgIcon(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${id}`);
  svg.append(use);
  return svg;
}

async function copy(value) {
  if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
  const area = document.createElement('textarea');
  area.value = value;
  area.readOnly = true;
  area.className = 'clipboard-helper';
  document.body.append(area);
  area.select();
  const result = document.execCommand('copy');
  area.remove();
  if (!result) throw new Error('Копирование недоступно');
}

function formatDate(value) {
  if (!value) return '—';
  const numeric = typeof value === 'number' || (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value.trim()));
  const number = numeric ? Number(value) : NaN;
  const date = numeric ? new Date(Math.abs(number) < 1e12 ? number * 1000 : number) : new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}

function renderResult(link) {
  const root = document.getElementById('short-link-result');
  const url = shortUrl(link);
  const label = document.createElement('span');
  const row = document.createElement('div');
  const anchor = document.createElement('a');
  const button = document.createElement('button');
  label.textContent = 'Ссылка готова';
  row.className = 'short-result-row';
  anchor.href = url;
  anchor.target = '_blank';
  anchor.rel = 'noopener noreferrer';
  anchor.textContent = url;
  button.type = 'button';
  button.className = 'icon-button';
  button.setAttribute('aria-label', 'Копировать короткую ссылку');
  button.append(svgIcon('i-copy'));
  button.addEventListener('click', () => copy(url)
    .then(() => dependencies.toast?.('Ссылка скопирована', url))
    .catch((error) => dependencies.toast?.('Не удалось скопировать', error.message, 'error')));
  row.append(anchor, button);
  root.replaceChildren(label, row);
  root.hidden = false;
  try {
    lastQr = renderQr(url, document.getElementById('link-qr-output'));
    document.getElementById('link-qr-download').disabled = false;
  } catch (error) {
    dependencies.toast?.('QR-код не создан', error.message, 'error');
  }
}

function renderList() {
  const root = document.getElementById('links-list');
  if (!root) return;
  if (!api.session.authenticated) {
    root.innerHTML = '<div class="empty-state compact"><p>Войдите, чтобы увидеть созданные ссылки.</p></div>';
    return;
  }
  if (!links.length) {
    root.innerHTML = '<div class="empty-state compact"><strong>Ссылок пока нет</strong><p>Создайте первую короткую ссылку выше.</p></div>';
    return;
  }
  const header = document.createElement('div');
  header.className = 'link-row header';
  ['Короткий адрес', 'Назначение', 'Переходы', 'Создана', ''].forEach((value) => {
    const cell = document.createElement('span');
    cell.textContent = value;
    header.append(cell);
  });
  const rows = links.map((link) => {
    const row = document.createElement('div');
    const short = document.createElement('a');
    const target = document.createElement('span');
    const clicks = document.createElement('span');
    const date = document.createElement('span');
    const actions = document.createElement('div');
    const copyButton = document.createElement('button');
    const remove = document.createElement('button');
    const url = shortUrl(link);
    const code = link.code ?? link.slug ?? link.id;
    row.className = 'link-row';
    short.className = 'short-code';
    short.href = url;
    short.target = '_blank';
    short.rel = 'noopener noreferrer';
    short.textContent = `/s/${code}`;
    target.className = 'target-link';
    target.textContent = targetUrl(link);
    target.title = targetUrl(link);
    clicks.className = 'link-stat';
    clicks.textContent = String(link.clicks ?? link.visits ?? 0);
    date.className = 'link-date';
    date.textContent = formatDate(link.created_at ?? link.createdAt);
    actions.className = 'link-actions';
    copyButton.className = 'row-action';
    copyButton.type = 'button';
    copyButton.title = 'Копировать';
    copyButton.setAttribute('aria-label', `Копировать /s/${code}`);
    copyButton.append(svgIcon('i-copy'));
    copyButton.addEventListener('click', () => copy(url).then(() => dependencies.toast?.('Скопировано', url)));
    remove.className = 'row-action';
    remove.type = 'button';
    remove.title = 'Удалить';
    remove.setAttribute('aria-label', `Удалить /s/${code}`);
    remove.append(svgIcon('i-trash'));
    remove.addEventListener('click', () => removeLink(link));
    actions.append(copyButton, remove);
    row.append(short, target, clicks, date, actions);
    return row;
  });
  root.replaceChildren(header, ...rows);
}

async function removeLink(link) {
  const code = link.code ?? link.slug ?? link.id;
  if (!confirm(`Удалить короткую ссылку /s/${code}?`)) return;
  try {
    await api.deleteLink(code);
    dependencies.toast?.('Ссылка удалена', `/s/${code}`);
    await loadLinks();
  } catch (error) {
    dependencies.toast?.('Не удалось удалить', error.message, 'error');
  }
}

export async function loadLinks({ quiet = false } = {}) {
  if (!api.session.authenticated) {
    links = [];
    renderList();
    return links;
  }
  try {
    links = await api.getLinks();
    renderList();
    return links;
  } catch (error) {
    links = [];
    renderList();
    if (!quiet && error.status !== 401) dependencies.toast?.('Ссылки не загружены', error.message, 'error');
    return [];
  }
}

export function initLinks(options = {}) {
  dependencies = options;
  document.getElementById('short-link-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!api.session.authenticated) return dependencies.requireAuth?.();
    const form = event.currentTarget;
    const data = new FormData(form);
    const submit = form.querySelector('[type="submit"]');
    const payload = { target_url: data.get('url').trim() };
    if (data.get('code').trim()) payload.code = data.get('code').trim();
    submit.disabled = true;
    try {
      const link = await api.createLink(payload);
      renderResult(link);
      form.reset();
      dependencies.toast?.('Короткая ссылка создана', shortUrl(link));
      await loadLinks();
    } catch (error) {
      dependencies.toast?.('Ссылка не создана', error.message, 'error');
    } finally {
      submit.disabled = false;
    }
  });
  document.getElementById('link-qr-download')?.addEventListener('click', () => {
    if (lastQr) downloadSvg(lastQr, 'short-link-qr.svg');
  });
  document.getElementById('refresh-links')?.addEventListener('click', () => loadLinks());
  document.addEventListener('portal:session', () => loadLinks({ quiet: true }));
  renderList();
}
