import { createVault, hasVault, removeVault, saveVault, unlockVault, vaultUpdatedAt } from './crypto.js';

let context = null;
let activeId = null;
let dependencies = {};
let saveTimer = 0;
let lockTimer = 0;
let dirty = false;
let saveChain = Promise.resolve();

function root() {
  return document.getElementById('notes-root');
}

function icon(id) {
  return `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
}

function formatDate(value, detailed = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ru-RU', detailed
    ? { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }
    : { day: 'numeric', month: 'short' });
}

function updateOverview() {
  const label = document.getElementById('overview-note-state');
  if (!label) return;
  if (context) label.textContent = `${context.notes.length} заметок · блокнот открыт`;
  else if (hasVault()) {
    const updated = vaultUpdatedAt();
    label.textContent = updated ? `Зашифровано · ${formatDate(updated, true)}` : 'Зашифрованное хранилище заблокировано';
  } else label.textContent = 'Хранилище ещё не создано';
}

function resetAutoLock() {
  clearTimeout(lockTimer);
  if (!context) return;
  lockTimer = setTimeout(() => lockNotes('Блокнот заблокирован после 15 минут бездействия'), 15 * 60 * 1000);
}

function setSaveState(message, saved = false) {
  const state = document.querySelector('.save-state');
  if (!state) return;
  state.replaceChildren();
  if (saved) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-check');
    svg.append(use);
    state.append(svg);
  }
  state.append(document.createTextNode(message));
}

function queueSave() {
  dirty = true;
  setSaveState('Есть несохранённые изменения');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSave, 600);
}

async function flushSave() {
  clearTimeout(saveTimer);
  if (!context || !dirty) return saveChain;
  dirty = false;
  const current = context;
  const snapshot = current.notes.map((note) => ({ ...note }));
  setSaveState('Шифруем и сохраняем…');
  saveChain = saveChain.then(async () => {
    if (!context) return;
    try {
      const saved = await saveVault(current, snapshot);
      if (context === current) context = { ...saved, notes: context.notes };
      setSaveState(`Сохранено ${new Date().toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`, true);
      updateOverview();
    } catch (error) {
      dirty = true;
      setSaveState('Не удалось сохранить');
      dependencies.toast?.('Блокнот не сохранён', error.message, 'error');
    }
  });
  return saveChain;
}

function renderLocked() {
  context = null;
  activeId = null;
  clearTimeout(lockTimer);
  const exists = hasVault();
  document.getElementById('notes-heading-actions').replaceChildren();
  root().innerHTML = `
    <div class="notes-unlock">
      <div class="card unlock-card">
        <span class="unlock-icon">${icon('i-lock')}</span>
        <h2>${exists ? 'Блокнот заблокирован' : 'Создайте зашифрованный блокнот'}</h2>
        <p>${exists ? 'Введите мастер-пароль. Он используется только для локальной расшифровки.' : 'Заметки будут храниться в localStorage только в зашифрованном виде.'}</p>
        <form id="vault-form">
          <label><span>Мастер-пароль</span><input name="password" type="password" minlength="8" autocomplete="${exists ? 'current-password' : 'new-password'}" required></label>
          ${exists ? '' : '<label><span>Повторите пароль</span><input name="confirmation" type="password" minlength="8" autocomplete="new-password" required></label>'}
          <p class="form-error" id="vault-error" role="alert"></p>
          <button class="button primary full-width" type="submit">${exists ? 'Открыть блокнот' : 'Создать блокнот'}</button>
          ${exists ? '<button class="button ghost full-width" id="remove-vault" type="button">Удалить локальный блокнот</button>' : ''}
        </form>
        <div class="vault-help">${icon('i-alert')}<span>Восстановить забытый мастер-пароль невозможно. Сохраните его в менеджере паролей.</span></div>
      </div>
    </div>`;
  updateOverview();

  document.getElementById('vault-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const password = form.elements.password.value;
    const error = document.getElementById('vault-error');
    const submit = form.querySelector('[type="submit"]');
    error.textContent = '';
    if (!exists && password !== form.elements.confirmation.value) {
      error.textContent = 'Пароли не совпадают';
      return;
    }
    submit.disabled = true;
    submit.textContent = exists ? 'Расшифровываем…' : 'Создаём ключ…';
    try {
      context = exists ? await unlockVault(password) : await createVault(password);
      form.reset();
      activeId = context.notes[0]?.id || null;
      renderWorkspace();
      dependencies.toast?.(exists ? 'Блокнот открыт' : 'Блокнот создан', 'Автоблокировка через 15 минут');
    } catch (caught) {
      error.textContent = caught.message;
      submit.disabled = false;
      submit.textContent = exists ? 'Открыть блокнот' : 'Создать блокнот';
    }
  });

  document.getElementById('remove-vault')?.addEventListener('click', () => {
    if (!confirm('Удалить зашифрованный блокнот из этого браузера? Восстановить его будет невозможно.')) return;
    if (!confirm('Последнее подтверждение: удалить все локальные заметки?')) return;
    removeVault();
    dependencies.toast?.('Локальный блокнот удалён', 'Данные удалены из этого браузера');
    renderLocked();
  });
}

function activeNote() {
  return context?.notes.find((note) => note.id === activeId) || null;
}

function renderNoteList() {
  const list = document.querySelector('.notes-list');
  if (!list || !context) return;
  if (!context.notes.length) {
    list.innerHTML = '<div class="empty-state compact"><p>Заметок пока нет</p></div>';
    return;
  }
  const sorted = [...context.notes].sort((a, b) => new Date(b.updatedAt) - new Date(a.updatedAt));
  list.replaceChildren(...sorted.map((note) => {
    const button = document.createElement('button');
    const title = document.createElement('strong');
    const preview = document.createElement('span');
    const time = document.createElement('time');
    button.type = 'button';
    button.className = `note-list-item${note.id === activeId ? ' is-active' : ''}`;
    title.textContent = note.title.trim() || 'Без названия';
    preview.textContent = note.body.replace(/\s+/g, ' ').trim() || 'Пустая заметка';
    time.textContent = formatDate(note.updatedAt, true);
    button.append(title, preview, time);
    button.addEventListener('click', () => {
      activeId = note.id;
      renderNoteList();
      renderEditor();
      resetAutoLock();
    });
    return button;
  }));
}

function renderEditor() {
  const editor = document.querySelector('.note-editor');
  if (!editor) return;
  const note = activeNote();
  if (!note) {
    editor.innerHTML = '<div class="empty-state"><strong>Новая мысль?</strong><p>Создайте заметку кнопкой слева.</p><button type="button" class="button primary" data-empty-new-note>Создать заметку</button></div>';
    editor.querySelector('[data-empty-new-note]')?.addEventListener('click', addNote);
    return;
  }
  editor.innerHTML = `
    <div class="note-editor-head">
      <input class="note-title-input" aria-label="Название заметки" maxlength="180" placeholder="Без названия">
      <button class="icon-button" type="button" data-delete-note aria-label="Удалить заметку" title="Удалить заметку">${icon('i-trash')}</button>
    </div>
    <textarea class="note-body-input" aria-label="Текст заметки" spellcheck="true" placeholder="Начните писать…"></textarea>
    <div class="note-editor-foot"><span class="note-stats"></span><span class="save-state">Сохранено локально</span></div>`;
  const title = editor.querySelector('.note-title-input');
  const body = editor.querySelector('.note-body-input');
  title.value = note.title;
  body.value = note.body;

  const updateStats = () => {
    const words = body.value.trim() ? body.value.trim().split(/\s+/u).length : 0;
    editor.querySelector('.note-stats').textContent = `${body.value.length} символов · ${words} слов`;
  };
  const update = () => {
    note.title = title.value;
    note.body = body.value;
    note.updatedAt = new Date().toISOString();
    updateStats();
    renderNoteList();
    queueSave();
    resetAutoLock();
  };
  title.addEventListener('input', update);
  body.addEventListener('input', update);
  editor.querySelector('[data-delete-note]').addEventListener('click', deleteActiveNote);
  updateStats();
}

function addNote() {
  if (!context) return;
  const note = {
    id: crypto.randomUUID(),
    title: '',
    body: '',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  context.notes.unshift(note);
  activeId = note.id;
  renderNoteList();
  renderEditor();
  queueSave();
  document.querySelector('.note-title-input')?.focus();
  resetAutoLock();
}

function deleteActiveNote() {
  const note = activeNote();
  if (!note || !confirm(`Удалить заметку «${note.title.trim() || 'Без названия'}»?`)) return;
  context.notes = context.notes.filter((item) => item.id !== note.id);
  activeId = context.notes[0]?.id || null;
  renderNoteList();
  renderEditor();
  queueSave();
}

function renderWorkspace() {
  if (!context) return renderLocked();
  const actions = document.getElementById('notes-heading-actions');
  actions.innerHTML = `<button class="button secondary" type="button" id="lock-notes">${icon('i-lock')}Заблокировать</button>`;
  root().innerHTML = `
    <div class="card notes-workspace">
      <aside class="notes-list-pane">
        <div class="notes-list-head"><strong>Заметки</strong><button class="icon-button" type="button" data-new-note aria-label="Новая заметка">${icon('i-plus')}</button></div>
        <div class="notes-list"></div>
      </aside>
      <div class="note-editor"></div>
    </div>`;
  document.getElementById('lock-notes').addEventListener('click', () => lockNotes());
  document.querySelector('[data-new-note]').addEventListener('click', addNote);
  root().addEventListener('pointerdown', resetAutoLock, { passive: true });
  root().addEventListener('keydown', resetAutoLock, { passive: true });
  renderNoteList();
  renderEditor();
  updateOverview();
  resetAutoLock();
}

async function lockNotes(message = '') {
  await flushSave();
  context = null;
  activeId = null;
  dirty = false;
  renderLocked();
  if (message) dependencies.toast?.('Блокнот заблокирован', message);
}

export function initNotes(options = {}) {
  dependencies = options;
  renderLocked();
  window.addEventListener('beforeunload', (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = '';
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && dirty) flushSave();
  });
  return { lock: lockNotes, updateOverview };
}
