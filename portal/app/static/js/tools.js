import { QrCode } from './qrcode.js';

let activeTool = 'json';
let lastQrSvg = '';

function byId(id) {
  return document.getElementById(id);
}

function setMessage(id, message = '', tone = '') {
  const element = byId(id);
  if (!element) return;
  element.textContent = message;
  element.className = `tool-message${tone ? ` ${tone}` : ''}`;
}

async function copyText(value) {
  const text = String(value || '');
  if (!text) throw new Error('Нечего копировать');
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.className = 'clipboard-helper';
  document.body.append(area);
  area.select();
  const copied = document.execCommand('copy');
  area.remove();
  if (!copied) throw new Error('Копирование не поддерживается');
}

function readCopyTarget(id) {
  const element = byId(id);
  if (!element) return '';
  return 'value' in element ? element.value : element.textContent;
}

function unicodeToBase64(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToUnicode(value) {
  const clean = value.replace(/\s+/g, '');
  const binary = atob(clean);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

function randomIndex(length) {
  if (!Number.isSafeInteger(length) || length < 1) throw new RangeError('Invalid alphabet');
  const range = 0x1_0000_0000;
  const limit = Math.floor(range / length) * length;
  const buffer = new Uint32Array(1);
  do crypto.getRandomValues(buffer); while (buffer[0] >= limit);
  return buffer[0] % length;
}

function randomFrom(alphabet) {
  return alphabet[randomIndex(alphabet.length)];
}

function shuffle(values) {
  for (let index = values.length - 1; index > 0; index -= 1) {
    const next = randomIndex(index + 1);
    [values[index], values[next]] = [values[next], values[index]];
  }
  return values;
}

export function createQrSvg(text, border = 4) {
  if (!text) throw new Error('Введите текст или ссылку');
  const qr = QrCode.encodeText(text, QrCode.Ecc.MEDIUM);
  const size = qr.size + border * 2;
  const path = [];
  for (let y = 0; y < qr.size; y += 1) {
    for (let x = 0; x < qr.size; x += 1) {
      if (qr.getModule(x, y)) path.push(`M${x + border},${y + border}h1v1h-1z`);
    }
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" role="img" aria-label="QR-код"><rect width="100%" height="100%" fill="#fff"/><path d="${path.join('')}" fill="#111820"/></svg>`;
}

export function renderQr(text, container) {
  const source = createQrSvg(text);
  const parsed = new DOMParser().parseFromString(source, 'image/svg+xml');
  const svg = document.importNode(parsed.documentElement, true);
  container.classList.remove('empty');
  container.replaceChildren(svg);
  return source;
}

export function downloadSvg(source, filename = 'qr-code.svg') {
  const blob = new Blob([source], { type: 'image/svg+xml;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function selectTool(name) {
  const next = document.querySelector(`[data-tool-panel="${CSS.escape(name)}"]`);
  const tab = document.querySelector(`[data-tool-tab="${CSS.escape(name)}"]`);
  if (!next || !tab) return false;
  activeTool = name;
  document.querySelectorAll('[data-tool-panel]').forEach((panel) => { panel.hidden = panel !== next; });
  document.querySelectorAll('[data-tool-tab]').forEach((button) => button.setAttribute('aria-selected', String(button === tab)));
  tab.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'smooth' });
  return true;
}

export function initTools({ toast } = {}) {
  document.querySelectorAll('[data-tool-tab]').forEach((tab) => {
    tab.addEventListener('click', () => selectTool(tab.dataset.toolTab));
    tab.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowUp', 'ArrowRight', 'ArrowLeft'].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...document.querySelectorAll('[data-tool-tab]')];
      const delta = ['ArrowDown', 'ArrowRight'].includes(event.key) ? 1 : -1;
      const target = tabs[(tabs.indexOf(tab) + delta + tabs.length) % tabs.length];
      selectTool(target.dataset.toolTab);
      target.focus();
    });
  });

  document.querySelectorAll('[data-copy-target]').forEach((button) => {
    button.addEventListener('click', async () => {
      try {
        await copyText(readCopyTarget(button.dataset.copyTarget));
        toast?.('Скопировано', 'Значение в буфере обмена');
      } catch (error) {
        toast?.('Не удалось скопировать', error.message, 'error');
      }
    });
  });

  const formatJson = (indent) => {
    try {
      const input = byId('json-input').value;
      if (!input.trim()) throw new Error('Вставьте JSON');
      byId('json-output').value = JSON.stringify(JSON.parse(input), null, indent);
      setMessage('json-message', 'JSON корректен', 'success');
    } catch (error) {
      byId('json-output').value = '';
      setMessage('json-message', error.message, 'error');
    }
  };
  byId('json-format')?.addEventListener('click', () => formatJson(2));
  byId('json-minify')?.addEventListener('click', () => formatJson(0));
  byId('json-clear')?.addEventListener('click', () => {
    byId('json-input').value = '';
    byId('json-output').value = '';
    setMessage('json-message');
    byId('json-input').focus();
  });

  byId('base64-encode')?.addEventListener('click', () => {
    try {
      byId('base64-output').value = unicodeToBase64(byId('base64-input').value);
      setMessage('base64-message', 'Закодировано', 'success');
    } catch (error) {
      setMessage('base64-message', error.message, 'error');
    }
  });
  byId('base64-decode')?.addEventListener('click', () => {
    try {
      byId('base64-output').value = base64ToUnicode(byId('base64-input').value);
      setMessage('base64-message', 'Декодировано', 'success');
    } catch {
      byId('base64-output').value = '';
      setMessage('base64-message', 'Некорректная Base64-строка или UTF-8', 'error');
    }
  });

  const generateUuids = () => {
    const count = Number(byId('uuid-count').value);
    const values = Array.from({ length: count }, () => crypto.randomUUID());
    byId('uuid-output').textContent = values.join('\n');
  };
  byId('uuid-generate')?.addEventListener('click', generateUuids);
  byId('uuid-count')?.addEventListener('change', generateUuids);
  generateUuids();

  const updateTimestamp = () => { byId('timestamp-now').textContent = Math.floor(Date.now() / 1000); };
  updateTimestamp();
  setInterval(updateTimestamp, 1000);
  byId('timestamp-convert')?.addEventListener('click', () => {
    const raw = byId('timestamp-input').value.trim();
    const target = byId('timestamp-result');
    if (!raw) {
      target.innerHTML = '<p class="tool-message error">Введите timestamp или дату</p>';
      return;
    }
    let date;
    if (/^-?\d+(?:\.\d+)?$/.test(raw)) {
      const value = Number(raw);
      date = new Date(Math.abs(value) < 1e12 ? value * 1000 : value);
    } else {
      date = new Date(raw);
    }
    if (Number.isNaN(date.getTime())) {
      target.innerHTML = '<p class="tool-message error">Не удалось распознать дату</p>';
      return;
    }
    const values = [
      ['Локально', date.toLocaleString('ru-RU', { dateStyle: 'full', timeStyle: 'medium' })],
      ['ISO 8601', date.toISOString()],
      ['Секунды', String(Math.floor(date.getTime() / 1000))],
      ['Миллисекунды', String(date.getTime())],
    ];
    target.replaceChildren(...values.map(([label, value]) => {
      const row = document.createElement('div');
      row.className = 'conversion-line';
      const name = document.createElement('span');
      const code = document.createElement('code');
      const copy = document.createElement('button');
      name.textContent = label;
      code.textContent = value;
      copy.type = 'button';
      copy.className = 'row-action';
      copy.setAttribute('aria-label', `Копировать ${label}`);
      copy.innerHTML = '<svg><use href="#i-copy"/></svg>';
      copy.addEventListener('click', () => copyText(value).then(() => toast?.('Скопировано', label)));
      row.append(name, code, copy);
      return row;
    }));
  });

  const convertUrl = (operation) => {
    try {
      const input = byId('url-input').value;
      byId('url-output').value = operation === 'encode' ? encodeURIComponent(input) : decodeURIComponent(input);
      setMessage('url-message', operation === 'encode' ? 'Закодировано' : 'Декодировано', 'success');
    } catch (error) {
      byId('url-output').value = '';
      setMessage('url-message', error.message, 'error');
    }
  };
  byId('url-encode')?.addEventListener('click', () => convertUrl('encode'));
  byId('url-decode')?.addEventListener('click', () => convertUrl('decode'));

  const generatePassword = () => {
    const lower = 'abcdefghijkmnopqrstuvwxyz';
    const groups = [lower];
    if (byId('password-uppercase').checked) groups.push('ABCDEFGHJKLMNPQRSTUVWXYZ');
    if (byId('password-numbers').checked) groups.push('23456789');
    if (byId('password-symbols').checked) groups.push('!@#$%^&*()-_=+[]{};:,.?');
    const length = Number(byId('password-length').value);
    const alphabet = groups.join('');
    const chars = groups.map(randomFrom);
    while (chars.length < length) chars.push(randomFrom(alphabet));
    byId('password-output').textContent = shuffle(chars).join('');
    const entropy = Math.floor(length * Math.log2(alphabet.length));
    byId('password-strength').textContent = `≈ ${entropy} бит энтропии`;
  };
  byId('password-length')?.addEventListener('input', () => {
    byId('password-length-value').textContent = byId('password-length').value;
    generatePassword();
  });
  ['password-uppercase', 'password-numbers', 'password-symbols'].forEach((id) => byId(id)?.addEventListener('change', generatePassword));
  byId('password-generate')?.addEventListener('click', generatePassword);
  generatePassword();

  byId('qr-generate')?.addEventListener('click', () => {
    const input = byId('qr-input').value.trim();
    try {
      lastQrSvg = renderQr(input, byId('qr-output'));
      byId('qr-download').disabled = false;
      setMessage('qr-message', 'QR-код создан локально', 'success');
    } catch (error) {
      setMessage('qr-message', error instanceof RangeError ? 'Слишком много данных для QR-кода' : error.message, 'error');
    }
  });
  byId('qr-download')?.addEventListener('click', () => {
    if (lastQrSvg) downloadSvg(lastQrSvg);
  });

  return { get activeTool() { return activeTool; }, selectTool };
}
