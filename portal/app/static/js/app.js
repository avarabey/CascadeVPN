import { api } from './api.js';
import { initFeeds } from './feeds.js';
import { initLinks } from './links.js';
import { initNotes } from './notes.js';
import { initReference, openReference } from './reference.js';
import { initStatus } from './status.js';
import { initTools, selectTool } from './tools.js';

const routes = {
  overview: 'Обзор',
  status: 'Статус сервисов',
  tools: 'Инструменты',
  notes: 'Блокнот',
  feed: 'RSS-лента',
  links: 'Ссылки и QR',
  reference: 'Справочник',
};

const searchItems = [
  { id: 'route-overview', group: 'Разделы', title: 'Обзор', detail: 'Главный экран', mark: '⌂', action: () => navigate('overview') },
  { id: 'route-status', group: 'Разделы', title: 'Статус сервисов', detail: 'Доступность и задержки', mark: '◉', action: () => navigate('status') },
  { id: 'route-tools', group: 'Разделы', title: 'Веб-инструменты', detail: 'Локальные преобразования', mark: '◇', action: () => navigate('tools') },
  { id: 'route-notes', group: 'Разделы', title: 'Зашифрованный блокнот', detail: 'Локальные заметки', mark: 'N', action: () => navigate('notes') },
  { id: 'route-feed', group: 'Разделы', title: 'RSS-лента', detail: 'Материалы из подписок', mark: 'R', action: () => navigate('feed') },
  { id: 'route-links', group: 'Разделы', title: 'Ссылки и QR', detail: 'Сокращение адресов', mark: '↗', action: () => navigate('links') },
  { id: 'route-reference', group: 'Разделы', title: 'Технический справочник', detail: 'Команды и рецепты', mark: '⌘', action: () => navigate('reference') },
  ...[
    ['json', 'JSON formatter', 'Проверка и форматирование', '{ }'],
    ['base64', 'Base64', 'Кодирование Unicode', '64'],
    ['uuid', 'UUID v4', 'Генератор идентификаторов', '#'],
    ['timestamp', 'Unix timestamp', 'Дата и время', '◷'],
    ['url', 'URL encode / decode', 'Компоненты адреса', '%'],
    ['password', 'Генератор паролей', 'Web Crypto', '*'],
    ['qr', 'QR-код', 'Локальное SVG', '▦'],
  ].map(([id, title, detail, mark]) => ({
    id: `tool-${id}`,
    group: 'Инструменты',
    title,
    detail,
    mark,
    action: () => { navigate('tools'); requestAnimationFrame(() => selectTool(id)); },
  })),
];

let referenceItems = [];
let selectedCommand = 0;

function svgIcon(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${id}`);
  svg.append(use);
  return svg;
}

export function toast(title, message = '', tone = 'success') {
  const region = document.getElementById('toast-region');
  const element = document.createElement('div');
  const content = document.createElement('div');
  const strong = document.createElement('strong');
  const detail = document.createElement('span');
  element.className = `toast${tone === 'error' ? ' error' : ''}`;
  strong.textContent = title;
  detail.textContent = message;
  content.append(strong);
  if (message) content.append(detail);
  element.append(svgIcon(tone === 'error' ? 'i-alert' : 'i-check'), content);
  region.append(element);
  const remove = () => {
    element.addEventListener('transitionend', () => element.remove(), { once: true });
    element.setAttribute('data-leaving', '');
    setTimeout(() => element.remove(), 350);
  };
  const timer = setTimeout(remove, 4200);
  element.addEventListener('click', () => { clearTimeout(timer); remove(); }, { once: true });
  return element;
}

function currentRoute() {
  const route = location.hash.replace(/^#/, '').split(/[/?]/)[0];
  return Object.hasOwn(routes, route) ? route : 'overview';
}

function navigate(route) {
  if (!Object.hasOwn(routes, route)) route = 'overview';
  if (currentRoute() === route && location.hash) showRoute(route);
  else location.hash = route;
}

function showRoute(route = currentRoute()) {
  document.querySelectorAll('[data-view]').forEach((view) => {
    const active = view.dataset.view === route;
    view.hidden = !active;
    view.classList.toggle('is-active', active);
  });
  document.querySelectorAll('[data-route-link]').forEach((link) => {
    const active = link.dataset.routeLink === route;
    link.classList.toggle('is-active', active && link.classList.contains('nav-item'));
    if (link.classList.contains('nav-item')) link.setAttribute('aria-current', active ? 'page' : 'false');
  });
  document.title = `${routes[route]} · ffknd`;
  closeSidebar();
  window.scrollTo({ top: 0, behavior: 'auto' });
}

function openSidebar() {
  const shell = document.querySelector('.app-shell');
  shell.dataset.sidebar = 'open';
  const button = document.querySelector('[data-open-sidebar]');
  button?.setAttribute('aria-expanded', 'true');
}

function closeSidebar() {
  const shell = document.querySelector('.app-shell');
  shell.dataset.sidebar = 'closed';
  document.querySelector('[data-open-sidebar]')?.setAttribute('aria-expanded', 'false');
}

function effectiveTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'dark' || explicit === 'light') return explicit;
  return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function updateThemeControl() {
  const dark = effectiveTheme() === 'dark';
  const toggle = document.getElementById('theme-toggle');
  toggle?.querySelector('use')?.setAttribute('href', dark ? '#i-sun' : '#i-moon');
  toggle?.setAttribute('aria-label', dark ? 'Включить светлую тему' : 'Включить тёмную тему');
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#0c1b22' : '#10232c');
}

function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('ffknd.theme') || 'auto'; } catch { /* private mode */ }
  document.documentElement.dataset.theme = ['light', 'dark'].includes(saved) ? saved : 'auto';
  updateThemeControl();
  document.getElementById('theme-toggle')?.addEventListener('click', () => {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('ffknd.theme', next); } catch { /* private mode */ }
    updateThemeControl();
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
    if (document.documentElement.dataset.theme === 'auto') updateThemeControl();
  });
}

function updateClock() {
  const now = new Date();
  const hour = now.getHours();
  const greeting = hour < 5 ? 'Доброй ночи' : hour < 12 ? 'Доброе утро' : hour < 18 ? 'Добрый день' : 'Добрый вечер';
  const greetingLabel = document.getElementById('greeting-label');
  const date = document.getElementById('current-date');
  if (greetingLabel) greetingLabel.textContent = greeting;
  if (date) {
    date.dateTime = now.toISOString();
    date.textContent = now.toLocaleDateString('ru-RU', { weekday: 'long', day: 'numeric', month: 'long' });
  }
}

function updateSessionUi(session = api.session) {
  const button = document.getElementById('session-button');
  if (!button) return;
  button.classList.toggle('is-authenticated', session.authenticated);
  button.querySelector('span:last-child').textContent = session.authenticated ? 'Личный режим' : 'Войти';
  button.title = session.authenticated ? 'Выйти из личного режима' : 'Войти в личный режим';
  button.setAttribute('aria-label', button.title);
}

function openAuth() {
  if (api.session.authenticated) return;
  const dialog = document.getElementById('auth-dialog');
  document.getElementById('auth-error').textContent = '';
  if (typeof dialog.showModal === 'function') dialog.showModal();
  else dialog.setAttribute('open', '');
  setTimeout(() => dialog.querySelector('input')?.focus(), 30);
}

function closeAuth() {
  const dialog = document.getElementById('auth-dialog');
  if (typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
}

function initAuth() {
  document.addEventListener('portal:session', (event) => updateSessionUi(event.detail));
  updateSessionUi();
  document.querySelectorAll('[data-open-auth]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!api.session.authenticated) return openAuth();
      if (!confirm('Выйти из личного режима на этом устройстве?')) return;
      try {
        await api.logout();
        toast('Сессия завершена', 'Приватные настройки скрыты');
      } catch (error) {
        toast('Не удалось выйти', error.message, 'error');
      }
    });
  });
  document.querySelector('[data-close-auth]')?.addEventListener('click', closeAuth);
  const dialog = document.getElementById('auth-dialog');
  dialog?.addEventListener('click', (event) => { if (event.target === dialog) closeAuth(); });
  document.getElementById('auth-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('[type="submit"]');
    const error = document.getElementById('auth-error');
    submit.disabled = true;
    error.textContent = '';
    try {
      await api.login({ password: form.elements.password.value });
      form.reset();
      closeAuth();
      toast('Личный режим включён', 'Настройки и история доступны');
    } catch (caught) {
      error.textContent = caught.status === 401 ? 'Неверный пароль' : caught.message;
      form.elements.password.select();
    } finally {
      submit.disabled = false;
    }
  });
}

function allSearchItems() {
  return [...searchItems, ...referenceItems];
}

function filteredSearchItems(query) {
  const normalized = query.trim().toLocaleLowerCase('ru-RU');
  const values = allSearchItems();
  if (!normalized) return values.slice(0, 12);
  return values.filter((item) => `${item.title} ${item.detail} ${item.group}`.toLocaleLowerCase('ru-RU').includes(normalized)).slice(0, 18);
}

function renderCommandResults(query = '') {
  const root = document.getElementById('command-results');
  const values = filteredSearchItems(query);
  selectedCommand = Math.min(selectedCommand, Math.max(0, values.length - 1));
  if (!values.length) {
    root.innerHTML = '<div class="empty-state compact"><p>Ничего не найдено</p></div>';
    return;
  }
  let lastGroup = '';
  const nodes = [];
  values.forEach((item, index) => {
    if (item.group !== lastGroup) {
      nodes.push(Object.assign(document.createElement('div'), { className: 'command-group-label', textContent: item.group }));
      lastGroup = item.group;
    }
    const button = document.createElement('button');
    const mark = document.createElement('span');
    const content = document.createElement('span');
    const title = document.createElement('strong');
    const detail = document.createElement('small');
    button.type = 'button';
    button.className = `command-result${index === selectedCommand ? ' is-selected' : ''}`;
    button.dataset.commandIndex = String(index);
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(index === selectedCommand));
    mark.textContent = item.mark || '#';
    title.textContent = item.title;
    detail.textContent = item.detail;
    content.append(title, detail);
    button.append(mark, content);
    button.addEventListener('mouseenter', () => {
      selectedCommand = index;
      root.querySelectorAll('.command-result').forEach((result) => {
        const active = Number(result.dataset.commandIndex) === index;
        result.classList.toggle('is-selected', active);
        result.setAttribute('aria-selected', String(active));
      });
    });
    button.addEventListener('click', () => runCommand(item));
    nodes.push(button);
  });
  root.replaceChildren(...nodes);
}

function runCommand(item) {
  document.getElementById('search-dialog').close();
  item.action();
}

function openSearch() {
  const dialog = document.getElementById('search-dialog');
  const input = document.getElementById('command-search');
  selectedCommand = 0;
  input.value = '';
  renderCommandResults();
  if (!dialog.open) dialog.showModal();
  setTimeout(() => input.focus(), 20);
}

function initSearch() {
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  document.querySelector('.platform-mod').textContent = mac ? '⌘' : 'Ctrl';
  document.querySelector('[data-open-search]')?.addEventListener('click', openSearch);
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
    }
  });
  const dialog = document.getElementById('search-dialog');
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  const input = document.getElementById('command-search');
  input.addEventListener('input', () => { selectedCommand = 0; renderCommandResults(input.value); });
  input.addEventListener('keydown', (event) => {
    const values = filteredSearchItems(input.value);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (!values.length) return;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      selectedCommand = (selectedCommand + delta + values.length) % values.length;
      renderCommandResults(input.value);
      document.querySelector('.command-result.is-selected')?.scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'Enter' && values[selectedCommand]) {
      event.preventDefault();
      runCommand(values[selectedCommand]);
    }
  });
}

function initNavigation() {
  window.addEventListener('hashchange', () => showRoute());
  document.querySelector('[data-open-sidebar]')?.addEventListener('click', openSidebar);
  document.querySelector('[data-close-sidebar]')?.addEventListener('click', closeSidebar);
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeSidebar(); });
  document.querySelectorAll('[data-route-button]').forEach((button) => button.addEventListener('click', () => navigate(button.dataset.routeButton)));
  document.querySelectorAll('[data-tool-shortcut]').forEach((button) => button.addEventListener('click', () => {
    navigate('tools');
    requestAnimationFrame(() => selectTool(button.dataset.toolShortcut));
  }));
  document.querySelectorAll('[data-reference-shortcut]').forEach((button) => button.addEventListener('click', () => {
    navigate('reference');
    requestAnimationFrame(() => openReference(button.dataset.referenceShortcut));
  }));
  if (!location.hash) history.replaceState(null, '', '#overview');
  showRoute();
}

async function init() {
  initTheme();
  updateClock();
  setInterval(updateClock, 60_000);
  initNavigation();
  initAuth();
  initSearch();

  try { await api.getSession(); } catch { updateSessionUi(); }

  initTools({ toast });
  initNotes({ toast });
  initStatus({ toast, requireAuth: openAuth });
  initFeeds({ toast, requireAuth: openAuth });
  initLinks({ toast, requireAuth: openAuth });
  initReference({
    toast,
    onUpdate: (articles) => {
      referenceItems = articles.map((article) => ({
        id: `reference-${article.slug}`,
        group: 'Справочник',
        title: article.title,
        detail: article.summary || (article.tags || []).join(' · '),
        mark: article.title?.[0]?.toUpperCase() || '#',
        action: () => { navigate('reference'); requestAnimationFrame(() => openReference(article.slug)); },
      }));
    },
  });

  api.health().then((health) => {
    const label = document.querySelector('.version-label');
    if (label && health?.version) label.textContent = `ffknd portal · ${health.version}`;
  }).catch(() => {});
}

init().catch((error) => {
  console.error(error);
  toast('Портал запущен не полностью', error.message, 'error');
});
