import { api } from './api.js';

const fallbackArticles = [
  { slug: 'git', title: 'Git — ежедневные команды', summary: 'Состояние, ветки, история и безопасная отмена изменений.', tags: ['git', 'workflow'] },
  { slug: 'linux', title: 'Linux — быстрая диагностика', summary: 'Файлы, процессы, сеть, systemd и журналы.', tags: ['linux', 'shell'] },
  { slug: 'docker', title: 'Docker — контейнеры и Compose', summary: 'Проверка, логи, сборка и обновление сервисов.', tags: ['docker', 'compose'] },
  { slug: 'http', title: 'HTTP — коды и заголовки', summary: 'Методы, статусы, curl и диагностика кэша.', tags: ['http', 'web'] },
  { slug: 'regex', title: 'Регулярные выражения', summary: 'Синтаксис, группы и практические шаблоны.', tags: ['regex', 'text'] },
];

let articles = [];
let activeSlug = '';
let dependencies = {};
const articleCache = new Map();

function textElement(tag, value, className = '') {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = value;
  return element;
}

function renderInline(text, parent) {
  const pattern = /(`[^`]+`|\[([^\]]+)\]\(([^)]+)\)|\*\*([^*]+)\*\*|\*([^*]+)\*)/g;
  let index = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > index) parent.append(document.createTextNode(text.slice(index, match.index)));
    if (match[0].startsWith('`')) parent.append(textElement('code', match[0].slice(1, -1)));
    else if (match[2] != null) {
      const link = textElement('a', match[2]);
      try {
        const url = new URL(match[3], location.origin);
        link.href = ['http:', 'https:'].includes(url.protocol) ? url.href : '#';
      } catch {
        link.href = '#';
      }
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      parent.append(link);
    } else if (match[4] != null) parent.append(textElement('strong', match[4]));
    else parent.append(textElement('em', match[5]));
    index = pattern.lastIndex;
  }
  if (index < text.length) parent.append(document.createTextNode(text.slice(index)));
}

function isTableDivider(line) {
  return /^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(line);
}

function tableCells(line) {
  return line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
}

function markdownFragment(markdown) {
  const fragment = document.createDocumentFragment();
  const lines = String(markdown || '').replace(/\r\n?/g, '\n').split('\n');
  let index = 0;
  let list = null;
  let paragraph = [];

  const closeList = () => { list = null; };
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const element = document.createElement('p');
    renderInline(paragraph.join(' '), element);
    fragment.append(element);
    paragraph = [];
  };

  while (index < lines.length) {
    const line = lines[index];
    const trimmed = line.trim();
    if (trimmed.startsWith('```')) {
      flushParagraph(); closeList();
      const language = trimmed.slice(3).trim();
      const codeLines = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) {
        codeLines.push(lines[index]);
        index += 1;
      }
      const pre = document.createElement('pre');
      const code = textElement('code', codeLines.join('\n'));
      if (language) code.dataset.language = language;
      pre.append(code);
      fragment.append(pre);
    } else if (/^#{1,3}\s/.test(trimmed)) {
      flushParagraph(); closeList();
      const level = trimmed.match(/^#+/)[0].length;
      const heading = document.createElement(`h${level}`);
      renderInline(trimmed.slice(level).trim(), heading);
      fragment.append(heading);
    } else if (index + 1 < lines.length && trimmed.includes('|') && isTableDivider(lines[index + 1])) {
      flushParagraph(); closeList();
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headerRow = document.createElement('tr');
      tableCells(line).forEach((cell) => { const th = document.createElement('th'); renderInline(cell, th); headerRow.append(th); });
      thead.append(headerRow);
      table.append(thead);
      const tbody = document.createElement('tbody');
      index += 2;
      while (index < lines.length && lines[index].includes('|') && lines[index].trim()) {
        const row = document.createElement('tr');
        tableCells(lines[index]).forEach((cell) => { const td = document.createElement('td'); renderInline(cell, td); row.append(td); });
        tbody.append(row);
        index += 1;
      }
      index -= 1;
      table.append(tbody);
      fragment.append(table);
    } else if (/^[-*]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
      flushParagraph();
      const ordered = /^\d+\./.test(trimmed);
      if (!list || list.tagName !== (ordered ? 'OL' : 'UL')) {
        list = document.createElement(ordered ? 'ol' : 'ul');
        fragment.append(list);
      }
      const item = document.createElement('li');
      renderInline(trimmed.replace(ordered ? /^\d+\.\s+/ : /^[-*]\s+/, ''), item);
      list.append(item);
    } else if (trimmed.startsWith('>')) {
      flushParagraph(); closeList();
      const quote = document.createElement('blockquote');
      renderInline(trimmed.replace(/^>\s?/, ''), quote);
      fragment.append(quote);
    } else if (!trimmed || trimmed.startsWith('<!--')) {
      flushParagraph(); closeList();
    } else {
      closeList();
      paragraph.push(trimmed);
    }
    index += 1;
  }
  flushParagraph();
  return fragment;
}

function renderList(query = '') {
  const root = document.getElementById('reference-list');
  if (!root) return;
  const normalized = query.trim().toLocaleLowerCase('ru-RU');
  const visible = articles.filter((article) => !normalized || [article.title, article.summary, ...(article.tags || [])]
    .join(' ').toLocaleLowerCase('ru-RU').includes(normalized));
  if (!visible.length) {
    root.innerHTML = '<div class="empty-state compact"><p>Ничего не найдено</p></div>';
    return;
  }
  root.replaceChildren(...visible.map((article) => {
    const button = document.createElement('button');
    const mark = textElement('span', (article.title || article.slug).trim()[0]?.toUpperCase() || '#', 'ref-icon');
    const details = document.createElement('span');
    const title = textElement('strong', article.title || article.slug);
    const summary = textElement('small', article.summary || (article.tags || []).join(' · '));
    button.type = 'button';
    button.className = `reference-item${article.slug === activeSlug ? ' is-active' : ''}`;
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(article.slug === activeSlug));
    details.append(title, summary);
    button.append(mark, details);
    button.addEventListener('click', () => openReference(article.slug));
    return button;
  }));
}

function renderArticle(article) {
  const root = document.getElementById('reference-article');
  const head = document.createElement('header');
  const kicker = textElement('span', 'Шпаргалка', 'section-kicker');
  const title = textElement('h2', article.title || article.slug);
  const summary = textElement('p', article.summary || 'Практические команды и короткие пояснения.');
  const tags = document.createElement('div');
  const content = document.createElement('div');
  head.className = 'article-head';
  tags.className = 'article-tags';
  (article.tags || []).forEach((tag) => tags.append(textElement('span', tag)));
  head.append(kicker, title, summary, tags);
  content.className = 'markdown';
  const lines = String(article.content || 'Содержимое недоступно.').replace(/\r\n?/g, '\n').split('\n');
  const bodyLines = lines.filter((line, index) => !(index === 0 && line.trim().startsWith('# ')) && !line.trim().startsWith('<!-- tags:'));
  const firstText = bodyLines.findIndex((line) => line.trim());
  if (firstText >= 0 && bodyLines[firstText].trim() === String(article.summary || '').trim()) bodyLines.splice(firstText, 1);
  content.append(markdownFragment(bodyLines.join('\n')));
  root.replaceChildren(head, content);
}

export async function openReference(slug) {
  if (!articles.some((article) => article.slug === slug)) slug = articles[0]?.slug;
  if (!slug) return;
  activeSlug = slug;
  renderList(document.getElementById('reference-search')?.value || '');
  const root = document.getElementById('reference-article');
  root.innerHTML = '<div class="empty-state"><span class="spinner"></span><p>Открываем статью…</p></div>';
  try {
    let article = articleCache.get(slug);
    if (!article) {
      article = await api.getReference(slug);
      articleCache.set(slug, article);
    }
    renderArticle(article);
  } catch (error) {
    root.innerHTML = '<div class="empty-state"><strong>Статья недоступна</strong><p>Попробуйте обновить страницу чуть позже.</p></div>';
    dependencies.toast?.('Справочник не загрузился', error.message, 'error');
  }
}

export async function loadReference() {
  try {
    articles = await api.getReferenceIndex();
    if (!articles.length) articles = fallbackArticles;
  } catch {
    articles = fallbackArticles;
  }
  const order = new Map(['git', 'linux', 'docker', 'http', 'regex'].map((slug, index) => [slug, index]));
  articles.sort((a, b) => (order.get(a.slug) ?? 99) - (order.get(b.slug) ?? 99) || a.title.localeCompare(b.title, 'ru'));
  renderList();
  await openReference(activeSlug || articles[0]?.slug);
  dependencies.onUpdate?.(articles);
  return articles;
}

export function initReference(options = {}) {
  dependencies = options;
  document.getElementById('reference-search')?.addEventListener('input', (event) => renderList(event.currentTarget.value));
  return loadReference();
}
