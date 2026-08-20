import { api } from './api.js';

let services = [];
let dependencies = {};

function statusOf(service) {
  const raw = String(service.status ?? service.state ?? '').toLowerCase();
  if (['up', 'ok', 'healthy', 'online', 'success'].includes(raw)) return 'ok';
  if (['slow', 'degraded', 'warning', 'warn'].includes(raw)) return 'slow';
  if (['down', 'offline', 'failed', 'error', 'unreachable'].includes(raw)) return 'down';
  return 'unknown';
}

function latencyOf(service) {
  const value = Number(service.latency_ms ?? service.response_time_ms ?? service.latency);
  return Number.isFinite(value) && value >= 0 ? Math.round(value) : null;
}

function checkedAtOf(service) {
  return service.checked_at ?? service.last_checked_at ?? service.updated_at ?? null;
}

function toDate(value) {
  if (value instanceof Date) return value;
  if (typeof value === 'number' || (typeof value === 'string' && /^-?\d+(?:\.\d+)?$/.test(value.trim()))) {
    const number = Number(value);
    return new Date(Math.abs(number) < 1e12 ? number * 1000 : number);
  }
  return new Date(value);
}

function timeAgo(value) {
  if (!value) return 'ещё не проверен';
  const date = toDate(value);
  if (Number.isNaN(date.getTime())) return 'нет данных';
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 10) return 'только что';
  if (seconds < 60) return `${seconds} сек. назад`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} мин. назад`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} ч. назад`;
  return date.toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' });
}

function icon(id) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#${id}`);
  svg.append(use);
  return svg;
}

function createDot(tone) {
  const dot = document.createElement('i');
  dot.className = `dot ${tone === 'unknown' ? '' : tone}`.trim();
  dot.setAttribute('aria-hidden', 'true');
  return dot;
}

function renderOverview() {
  const root = document.getElementById('overview-services');
  if (!root) return;
  if (!services.length) {
    root.innerHTML = '<div class="empty-state compact"><p>Проверки ещё не настроены.</p></div>';
    return;
  }
  root.replaceChildren(...services.slice(0, 4).map((service) => {
    const row = document.createElement('div');
    const name = document.createElement('strong');
    const latency = document.createElement('time');
    const status = statusOf(service);
    row.className = 'overview-service';
    name.textContent = service.name || 'Без названия';
    latency.textContent = latencyOf(service) == null ? timeAgo(checkedAtOf(service)) : `${latencyOf(service)} ms`;
    row.append(createDot(status), name, latency);
    return row;
  }));
}

function makeSummaryCard(label, value, detail, tone) {
  const card = document.createElement('article');
  card.className = 'card summary-card';
  const title = document.createElement('span');
  const strong = document.createElement('strong');
  const small = document.createElement('small');
  title.textContent = label;
  strong.textContent = value;
  small.textContent = detail;
  if (tone) {
    strong.className = 'summary-status';
    strong.prepend(createDot(tone));
  }
  card.append(title, strong, small);
  return card;
}

function renderSummary(serverSummary = null) {
  const root = document.getElementById('status-summary');
  if (!root) return;
  const up = services.filter((service) => statusOf(service) === 'ok').length;
  const down = services.filter((service) => statusOf(service) === 'down').length;
  const slow = services.filter((service) => statusOf(service) === 'slow').length;
  const latencies = services.map(latencyOf).filter(Number.isFinite);
  const average = Number(serverSummary?.avg_latency_ms ?? serverSummary?.average_latency_ms)
    || (latencies.length ? Math.round(latencies.reduce((sum, value) => sum + value, 0) / latencies.length) : 0);
  const overallTone = down ? 'down' : slow ? 'slow' : up ? 'ok' : 'unknown';
  const overallText = down ? 'Есть сбои' : slow ? 'Есть задержки' : up ? 'Всё работает' : 'Нет данных';
  root.replaceChildren(
    makeSummaryCard('Общее состояние', overallText, down ? `${down} недоступно` : 'Последняя выборка', overallTone),
    makeSummaryCard('Доступны', String(serverSummary?.up ?? up), `из ${serverSummary?.total ?? services.length} сервисов`),
    makeSummaryCard('Средний ответ', average ? `${Math.round(average)} ms` : '—', latencies.length ? 'по доступным сервисам' : 'ожидание проверки'),
    makeSummaryCard('Инциденты', String(serverSummary?.down ?? down), down ? 'нужно внимание' : 'в текущей выборке'),
  );
  const indicator = document.getElementById('nav-status-indicator');
  if (indicator) indicator.className = `nav-indicator ${down || slow ? 'issue' : up ? 'ok' : ''}`.trim();
}

function renderTable() {
  const root = document.getElementById('service-table');
  if (!root) return;
  if (!services.length) {
    root.innerHTML = '<div class="empty-state"><strong>Нет проверок</strong><p>В личном режиме можно добавить первый сервис для наблюдения.</p></div>';
    return;
  }
  const header = document.createElement('div');
  header.className = 'service-row header';
  ['Сервис', 'Адрес', 'Ответ', 'Проверка', ''].forEach((text) => {
    const cell = document.createElement('span');
    cell.textContent = text;
    header.append(cell);
  });
  const rows = services.map((service) => {
    const row = document.createElement('div');
    const name = document.createElement('div');
    const strong = document.createElement('strong');
    const url = document.createElement('span');
    const latency = document.createElement('span');
    const time = document.createElement('span');
    const remove = document.createElement('button');
    const status = statusOf(service);
    row.className = 'service-row';
    name.className = 'service-name';
    strong.textContent = service.name || 'Без названия';
    name.append(createDot(status), strong);
    url.className = 'service-url';
    url.textContent = service.url || (api.session.authenticated ? '—' : 'скрыто в публичном режиме');
    latency.className = 'service-latency';
    latency.textContent = latencyOf(service) == null ? '—' : `${latencyOf(service)} ms`;
    time.className = 'service-time';
    time.textContent = timeAgo(checkedAtOf(service));
    remove.className = 'row-action';
    remove.type = 'button';
    remove.title = 'Удалить проверку';
    remove.setAttribute('aria-label', `Удалить ${service.name || 'сервис'}`);
    remove.append(icon('i-trash'));
    remove.hidden = !api.session.authenticated;
    remove.addEventListener('click', () => removeService(service));
    row.append(name, url, latency, time, remove);
    return row;
  });
  root.replaceChildren(header, ...rows);
}

function renderAll(summary) {
  renderOverview();
  renderSummary(summary);
  renderTable();
  const latest = services.map(checkedAtOf).filter(Boolean).map(toDate).filter((date) => !Number.isNaN(date.getTime())).sort((a, b) => a - b).at(-1);
  const updated = document.getElementById('status-updated');
  if (updated) updated.textContent = latest ? `Обновлено ${timeAgo(latest)}` : 'Нет завершённых проверок';
  dependencies.onUpdate?.(services);
}

async function removeService(service) {
  if (!confirm(`Удалить проверку «${service.name || 'Без названия'}»?`)) return;
  try {
    await api.deleteService(service.id);
    dependencies.toast?.('Проверка удалена', service.name || 'Сервис');
    await loadStatus();
  } catch (error) {
    dependencies.toast?.('Не удалось удалить', error.message, 'error');
  }
}

export async function loadStatus({ quiet = false } = {}) {
  try {
    const data = await api.getServices();
    services = data.services || [];
    renderAll(data.summary);
    return services;
  } catch (error) {
    services = [];
    renderAll();
    const root = document.getElementById('service-table');
    if (root) root.innerHTML = '<div class="empty-state"><strong>Нет связи с проверками</strong><p>Данные появятся после восстановления соединения.</p></div>';
    if (!quiet) dependencies.toast?.('Статус не обновлён', error.message, 'error');
    return [];
  }
}

export function initStatus(options = {}) {
  dependencies = options;
  const formCard = document.getElementById('service-form');
  document.getElementById('show-service-form')?.addEventListener('click', () => {
    if (!api.session.authenticated) return dependencies.requireAuth?.();
    formCard.hidden = false;
    formCard.querySelector('input')?.focus();
  });
  document.querySelector('[data-cancel-service]')?.addEventListener('click', () => { formCard.hidden = true; });
  document.getElementById('add-service-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!api.session.authenticated) return dependencies.requireAuth?.();
    const data = new FormData(event.currentTarget);
    const submit = event.currentTarget.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api.addService({ name: data.get('name').trim(), url: data.get('url').trim() });
      event.currentTarget.reset();
      formCard.hidden = true;
      dependencies.toast?.('Сервис добавлен', 'Первая проверка скоро появится');
      await loadStatus();
    } catch (error) {
      dependencies.toast?.('Не удалось добавить', error.message, 'error');
    } finally {
      submit.disabled = false;
    }
  });
  document.getElementById('refresh-status')?.addEventListener('click', async (event) => {
    event.currentTarget.disabled = true;
    try {
      if (api.session.authenticated) await api.checkServices();
      await loadStatus();
    } catch (error) {
      dependencies.toast?.('Проверка не запущена', error.message, 'error');
    } finally {
      event.currentTarget.disabled = false;
    }
  });
  // Refetch on both login and logout so private URL/error fields never survive in memory.
  document.addEventListener('portal:session', () => loadStatus({ quiet: true }));
  return loadStatus({ quiet: true });
}
