const STORAGE_KEY = 'ffknd.encrypted-notes.v1';
const FORMAT = 'ffknd-notes';
const VERSION = 1;
const ITERATIONS = 310_000;
const MIN_ITERATIONS = 100_000;
const MAX_ITERATIONS = 2_000_000;
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

function assertCrypto() {
  if (!globalThis.crypto?.subtle || !globalThis.crypto?.getRandomValues) {
    throw new Error('Этот браузер не поддерживает Web Crypto');
  }
}

function bytesToBase64(bytes) {
  let binary = '';
  const chunk = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}

function base64ToBytes(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

async function deriveKey(password, salt, iterations = ITERATIONS) {
  if (!Number.isSafeInteger(iterations) || iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) {
    throw new Error('Некорректные параметры защиты блокнота');
  }
  const material = await crypto.subtle.importKey(
    'raw',
    encoder.encode(password),
    { name: 'PBKDF2' },
    false,
    ['deriveKey'],
  );
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations, hash: 'SHA-256' },
    material,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
}

function readRecord() {
  let raw;
  try {
    raw = localStorage.getItem(STORAGE_KEY);
  } catch {
    throw new Error('Локальное хранилище браузера недоступно');
  }
  if (!raw) return null;
  try {
    const record = JSON.parse(raw);
    if (record.format !== FORMAT || record.version !== VERSION) throw new Error('format');
    if (!record.kdf?.salt || !record.cipher?.iv || !record.data) throw new Error('fields');
    return record;
  } catch {
    throw new Error('Формат зашифрованного блокнота повреждён');
  }
}

function writeRecord(record) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(record));
  } catch {
    throw new Error('Не удалось сохранить блокнот в этом браузере');
  }
}

function normalizeNotes(payload) {
  if (!payload || !Array.isArray(payload.notes)) throw new Error('Содержимое блокнота повреждено');
  return payload.notes
    .filter((note) => note && typeof note === 'object')
    .map((note) => ({
      id: String(note.id || crypto.randomUUID()),
      title: String(note.title || ''),
      body: String(note.body || ''),
      createdAt: String(note.createdAt || new Date().toISOString()),
      updatedAt: String(note.updatedAt || note.createdAt || new Date().toISOString()),
    }));
}

async function encryptPayload(key, salt, notes, iterations = ITERATIONS) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const payload = encoder.encode(JSON.stringify({ notes, savedAt: new Date().toISOString() }));
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: encoder.encode(`${FORMAT}:v${VERSION}`), tagLength: 128 },
    key,
    payload,
  );
  return {
    format: FORMAT,
    version: VERSION,
    kdf: { name: 'PBKDF2', hash: 'SHA-256', iterations, salt: bytesToBase64(salt) },
    cipher: { name: 'AES-GCM', tagLength: 128, iv: bytesToBase64(iv) },
    data: bytesToBase64(new Uint8Array(ciphertext)),
    updatedAt: new Date().toISOString(),
  };
}

export function hasVault() {
  try {
    return localStorage.getItem(STORAGE_KEY) !== null;
  } catch {
    return false;
  }
}

export async function createVault(password) {
  assertCrypto();
  if (typeof password !== 'string' || password.length < 8) {
    throw new Error('Используйте мастер-пароль длиной не менее 8 символов');
  }
  if (hasVault()) throw new Error('Блокнот уже существует');
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await deriveKey(password, salt);
  const initialNote = {
    id: crypto.randomUUID(),
    title: 'Первая заметка',
    body: 'Этот текст зашифрован локально. Можно удалить его и начать писать.',
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
  writeRecord(await encryptPayload(key, salt, [initialNote]));
  return { key, salt, iterations: ITERATIONS, notes: [initialNote] };
}

export async function unlockVault(password) {
  assertCrypto();
  const record = readRecord();
  if (!record) throw new Error('Сначала создайте локальный блокнот');
  try {
    const salt = base64ToBytes(record.kdf.salt);
    const iv = base64ToBytes(record.cipher.iv);
    const ciphertext = base64ToBytes(record.data);
    const iterations = Number(record.kdf.iterations);
    if (salt.length < 16 || salt.length > 64 || iv.length !== 12 || ciphertext.length < 17) {
      throw new Error('Содержимое блокнота повреждено');
    }
    if (!Number.isSafeInteger(iterations) || iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) {
      throw new Error('Некорректные параметры защиты блокнота');
    }
    const key = await deriveKey(password, salt, iterations);
    const plaintext = await crypto.subtle.decrypt(
      {
        name: 'AES-GCM',
        iv,
        additionalData: encoder.encode(`${FORMAT}:v${VERSION}`),
        tagLength: 128,
      },
      key,
      ciphertext,
    );
    const payload = JSON.parse(decoder.decode(plaintext));
    return { key, salt, iterations, notes: normalizeNotes(payload) };
  } catch (error) {
    if (error.message?.includes('поврежден') || error.message?.includes('параметры')) throw error;
    throw new Error('Неверный мастер-пароль или повреждённые данные');
  }
}

export async function saveVault(context, notes) {
  assertCrypto();
  if (!context?.key || !context?.salt) throw new Error('Блокнот заблокирован');
  const normalized = normalizeNotes({ notes });
  writeRecord(await encryptPayload(context.key, context.salt, normalized, context.iterations));
  return { ...context, notes: normalized };
}

export function removeVault() {
  localStorage.removeItem(STORAGE_KEY);
}

export function vaultUpdatedAt() {
  try {
    return readRecord()?.updatedAt || null;
  } catch {
    return null;
  }
}
