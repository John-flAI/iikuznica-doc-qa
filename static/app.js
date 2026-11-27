'use strict';
/* Стенд: чат сотрудника, служба качества, документы с подсветкой, статистика. Без библиотек. */

const $ = (selector, root = document) => root.querySelector(selector);
const app = $('#app'), messagesEl = $('#messages'), input = $('#ask-input'), sendBtn = $('#ask-send');
const panel = $('#panel'), docEl = $('#doc'), docPick = $('#doc-pick');

const ROUTE_NAMES = {
  answer: 'ответ по регламенту', clarify: 'уточняющий вопрос', not_found: 'нет ответа в регламентах',
  unresolved: 'однозначного ответа нет', degraded: 'модель недоступна', qc: 'ответ службы качества',
  kb: 'ответ из базы знаний — ответ службы качества', smalltalk: 'общение не по регламентам',
};
const CHECK_NAMES = {
  passed: 'проверка на опору в регламенте пройдена', rewritten: 'текст переписан после проверки',
  failed: 'текст не прошёл проверку — пункты дословно', unavailable: 'проверка недоступна',
  invalid: 'модель не вернула текст',
};
const THINKING_STEPS = ['Ищу в регламентах…', 'Модель решает: ответить, уточнить или признать, что ответа нет…', 'Проверяю ответ…'];

const mode = ({'/user': 'user', '/qc': 'qc', '/analytics': 'analytics'})[location.pathname] || 'full';
app.classList.add('mode-' + mode);
if (mode !== 'full') app.classList.remove('panel-closed');

let sid = localStorage.getItem('regqa.sid') || null;
let lastId = 0, busy = false, tab = 'qc', tickets = [], ticketMap = {}, sources = [];
const shown = new Set();          // номера сообщений, которые уже на экране: опрос сессии их не повторяет
let pendingUser = null;           // вопрос, показанный до ответа сервера, — ждёт своего номера

/* ---- базовое ---- */

async function api(path, options) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body.detail || response.statusText);
    error.status = response.status;
    throw error;
  }
  return body;
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function button(cls, text, onClick) {
  const node = el('button', cls, text);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function scrollDown() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

let thinkTimer = null;
function thinking(on) {
  $('#thinking').hidden = !on;
  clearInterval(thinkTimer);
  if (!on) return;
  let step = 0;
  const show = () => { $('#thinking-steps').replaceChildren(el('b', '', '▸ ' + THINKING_STEPS[step])); };
  show();
  thinkTimer = setInterval(() => { step = (step + 1) % THINKING_STEPS.length; show(); }, 2200);
}

/* ---- сообщения ---- */

function sourceCard(source) {
  const card = button('source' + (source.doc === 'kb' ? ' kb' : ''), '', () => {
    sources = [source];
    openPanel('docs');
    showDocument(source);
  });
  card.append(el('div', 'where', source.doc_title + ' · ' + source.address),
              el('div', 'quote', '«' + source.quote + '»'));
  return card;
}

function ticketBlock(message) {
  const meta = message.meta || {};
  const ticket = meta.ticket, state = ticketMap[ticket.id] || { status: 'draft' };
  const box = el('div', 'ticket');
  box.append(el('div', 'where', 'Письмо в службу качества, запрос №' + ticket.id),
             el('pre', '', ticket.letter));
  if (state.status === 'draft') {
    const row = el('div', 'row');
    row.append(button('primary gold', 'Отправить в службу качества', async (event) => {
      event.target.disabled = true;
      const from = event.target.getBoundingClientRect();     // кнопку заменит «Отправлено» — запоминаем, где она была
      try {
        await api('/api/tickets/' + ticket.id + '/send', { method: 'POST' });
        ticketMap[ticket.id] = { status: 'open' };
        row.replaceWith(el('div', 'sent', 'Отправлено. Ответ службы качества появится в этом чате.'));
        await flyToPanel(from);
        openPanel('qc');
      } catch (error) {
        row.replaceWith(el('div', 'notice', 'Не получилось отправить: ' + error.message));
      }
    }));
    box.append(row);
  } else {
    box.append(el('div', 'sent', state.status === 'answered'
      ? 'Отправлено. Служба качества ответила — см. ниже.'
      : 'Отправлено. Ответ службы качества появится в этом чате.'));
  }
  return box;
}

function show(entry) {
  if (entry.id) {
    if (shown.has(entry.id)) return;            // уже показан сразу после запроса
    shown.add(entry.id);
    lastId = Math.max(lastId, entry.id);
    if (pendingUser && entry.role === 'user' && entry.text === pendingUser) { pendingUser = null; return; }
  }
  const meta = typeof entry.meta === 'string' ? JSON.parse(entry.meta || '{}') : (entry.meta || {});
  if (entry.role === 'user') {
    const node = el('div', 'msg user');
    node.append(el('div', 'bubble', entry.text));
    messagesEl.append(node);
  } else if (entry.role === 'system') {
    messagesEl.append(el('div', 'msg system', entry.text));
  } else if (entry.role === 'qc') {
    const node = el('div', 'msg qc');
    node.append(el('div', 'who', 'Служба качества' + (meta.author && meta.author !== 'Служба качества' ? ' · ' + meta.author : '') +
                   (meta.ticket_id ? ' · запрос №' + meta.ticket_id : '')),
                el('div', 'bubble', entry.text));
    if (meta.ticket_id) node.append(el('div', 'meta', 'Добавлено в базу знаний: повторный вопрос агент ответит сам.'));
    messagesEl.append(node);
  } else if (entry.role === 'agent') {
    const node = el('div', 'msg agent');
    node.append(el('div', 'who', 'Агент'));
    if (meta.text) node.append(el('div', 'bubble', meta.text));
    if (meta.notice) node.append(el('div', 'notice', meta.notice));
    if (meta.priority && meta.conflict) {
      node.append(el('div', 'priority', 'Регламент — утверждённый документ, он главнее. Ответ службы качества показан как консультация по применению; при расхождении применяйте регламент.'));
    }
    if (meta.sources && meta.sources.length) {
      const box = el('div', 'sources');
      meta.sources.forEach((source) => box.append(sourceCard(source)));
      node.append(box);
    }
    if (meta.clarify && meta.clarify.options.length) {
      const group = el('div', 'options');
      meta.clarify.options.forEach((option, i) => {
        const chip = button('option', '', () => {
          group.querySelectorAll('.option').forEach((o) => o.classList.add('picked'));
          send(option.text);
        });
        chip.textContent = option.text;
        group.append(chip);
      });
      node.append(group, el('div', 'meta', 'Выберите вариант или напишите свой ответ.'));
    }
    if (meta.ticket) node.append(ticketBlock({ meta }));
    if (meta.retry) node.append(button('primary', 'Повторить вопрос', () => send(lastQuestion)));
    const fromKnowledge = (meta.sources || []).some((source) => source.doc === 'kb');
    const facts = [fromKnowledge ? ROUTE_NAMES.kb : ROUTE_NAMES[meta.route] || meta.route];
    if (meta.checks) facts.push(CHECK_NAMES[meta.checks.judge] || meta.checks.judge);
    if (meta.tokens) facts.push(meta.tokens + ' ток.');
    if (meta.seconds) facts.push(meta.seconds + ' с');
    if (meta.clarifications) facts.push('уточнений: ' + meta.clarifications);
    if (meta.route === 'smalltalk') facts.push((meta.models || []).length ? 'отвечено моделью' : 'без обращения к модели');
    node.append(el('div', 'meta', facts.join(' · ')));
    messagesEl.append(node);
  }
  scrollDown();
}

let lastQuestion = '';
async function send(text) {
  text = (text || '').trim();
  if (busy || !text) return;
  busy = true;
  sendBtn.disabled = true;
  lastQuestion = text;
  thinking(true);
  show({ role: 'user', text });
  pendingUser = text;
  input.value = '';
  try {
    const message = await api('/api/ask', { method: 'POST', body: JSON.stringify({ session_id: sid, text }) });
    sid = message.session_id;
    localStorage.setItem('regqa.sid', sid);
    thinking(false);
    show({ role: 'agent', text: message.text, meta: message });
    if (message.id) { shown.add(message.id); lastId = Math.max(lastId, message.id); }
  } catch (error) {
    thinking(false);
    // лимит стенда — не ошибка, а защита бюджета: текст сервера показываем как есть
    show({ role: 'system', text: error.status === 429 ? error.message : 'Не получилось: ' + error.message });
  } finally {
    busy = false;
    sendBtn.disabled = false;
    input.focus();
  }
}

/* ---- правая панель ---- */

function openPanel(name) {
  app.classList.remove('panel-closed');
  showTab(name || tab);
}

function showTab(name) {
  tab = name;
  app.querySelectorAll('.tab').forEach((node) => node.classList.toggle('active', node.dataset.tab === name));
  app.querySelectorAll('.tab-body').forEach((node) => node.classList.toggle('active', node.id === 'tab-' + name));
  if (name === 'qc') loadTickets();
  if (name === 'stats') loadStats();
  if (name === 'docs' && sources.length) showDocument(sources[0]);
}

$('#rail').addEventListener('click', () => openPanel('qc'));
$('#panel-close').addEventListener('click', () => app.classList.add('panel-closed'));
$('#panel-collapse').addEventListener('click', () => app.classList.add('panel-closed'));   // узкая кнопка на границе чата и колонны
app.querySelectorAll('.tab[data-tab]').forEach((node) => node.addEventListener('click', () => showTab(node.dataset.tab)));

/* золотая линия: письмо уходит из чата к службе качества (панель открыта — в её заголовок,
   закрыта — к рельсе справа, откуда панель потом открывается) */
function flyToPanel(a) {
  return new Promise((resolve) => {
    const svg = $('#flight'), path = $('#flight-path'), dot = $('#fly-dot');
    const b = panel.getBoundingClientRect();
    const shown = b.width > 0 && !app.classList.contains('panel-closed');
    const x1 = a.right, y1 = a.top + a.height / 2;
    const x2 = shown ? b.left + 70 : svg.clientWidth - 22, y2 = shown ? b.top + 28 : Math.round(svg.clientHeight / 2);
    path.setAttribute('d', `M ${x1} ${y1} C ${x1 + 160} ${y1}, ${x2 - 160} ${y2}, ${x2} ${y2}`);
    const length = path.getTotalLength();
    path.style.strokeDasharray = length;
    path.style.strokeDashoffset = length;
    const started = performance.now();
    const frame = (now) => {
      const t = Math.min((now - started) / 900, 1);
      const point = path.getPointAtLength(length * t);
      path.style.strokeDashoffset = length * (1 - t);
      dot.setAttribute('cx', point.x);
      dot.setAttribute('cy', point.y);
      if (t < 1) requestAnimationFrame(frame);
      else setTimeout(() => { path.setAttribute('d', ''); dot.setAttribute('cx', -20); resolve(); }, 350);
    };
    requestAnimationFrame(frame);
  });
}

/* ---- служба качества ---- */

async function loadTickets() {
  tickets = await api('/api/tickets');
  tickets.forEach((ticket) => { ticketMap[ticket.id] = ticket; });
  const list = $('#qc-list');
  list.replaceChildren();
  if (!tickets.length) list.append(el('div', 'empty', 'Запросов пока нет. Они появятся здесь, когда сотрудник отправит письмо из чата.'));
  tickets.forEach((ticket) => {
    const item = button('qc-item ' + ticket.status, '', () => selectTicket(ticket.id));
    item.append(el('span', 'n', '№' + ticket.id + ' · ' + ticket.question.slice(0, 90)),
                el('span', 'st', { open: 'ждёт ответа', answered: 'отвечено' }[ticket.status] || ticket.status));
    list.append(item);
  });
}

function selectTicket(id) {
  const ticket = tickets.find((t) => t.id === id);
  if (!ticket) return;
  app.querySelectorAll('.qc-item').forEach((node) => node.classList.toggle('active', node.classList.contains('qc-item') && node.textContent.startsWith('№' + id + ' ')));
  const box = $('#qc-detail');
  box.replaceChildren(el('div', 'qc-detail'));
  const card = box.firstChild;
  card.append(el('h3', '', 'Запрос №' + ticket.id), el('div', 'n', ticket.question));
  if (ticket.context) card.append(el('div', 'context', 'Что нашлось по теме:\n' + ticket.context));
  if (ticket.status === 'open') {
    const form = el('div', 'qc-form');
    const author = el('input'), answer = el('textarea');
    author.placeholder = 'Ваше имя'; author.value = 'Служба качества';
    answer.placeholder = 'Напишите верный ответ — он уйдёт сотруднику в чат и станет ответом базы знаний';
    form.append(author, answer);
    form.append(button('primary', 'Отправить ответ сотруднику', async (event) => {
      event.target.disabled = true;
      try {
        await api('/api/tickets/' + ticket.id + '/answer', { method: 'POST', body: JSON.stringify({ answer: answer.value, author: author.value }) });
        form.replaceWith(el('div', 'qc-done', 'Ответ отправлен сотруднику и добавлен в базу знаний: теперь агент ответит на этот вопрос сам.'));
        await loadTickets();
        await tick();                     // ответ службы сразу появляется в чате — без ожидания опроса сессии
      } catch (error) {
        form.append(el('div', 'notice', 'Не получилось: ' + error.message));
        event.target.disabled = false;
      }
    }));
    card.append(form);
  } else if (ticket.status === 'answered') {
    card.append(el('div', 'answer', ticket.answer),
                el('div', 'qc-done', 'Ответил: ' + ticket.answered_by + ' · ' + (ticket.answered_at || '').slice(0, 16).replace('T', ' ') + '. Запись добавлена в базу знаний.'));
  }
}

$('#weekly').addEventListener('click', async () => {
  const out = $('#weekly-out');
  out.hidden = false;
  out.textContent = 'Готовлю отчёт…';
  try {
    const report = await api('/api/weekly?days=7');
    out.textContent = report.text;
  } catch (error) {
    out.textContent = 'Не получилось: ' + error.message;
  }
});

/* ---- документы ---- */

async function showDocument(source) {
  docPick.replaceChildren();
  sources.forEach((s) => {
    const item = button(s === source ? 'active' : '', s.doc_title + ' · ' + s.address.slice(0, 80), () => showDocument(s));
    docPick.append(item);
  });
  docEl.replaceChildren();
  if (source.doc === 'kb') {
    docEl.append(el('h2', '', 'Ответ службы качества'), el('p', 'text', source.quote));
    return;
  }
  docEl.append(el('div', 'empty', 'Открываю регламент…'));
  try {
    const document = await api('/api/document/' + source.doc);
    docEl.replaceChildren(el('h2', '', document.title));
    document.blocks.forEach((block) => {
      let node;
      if (block.kind === 'section') node = el('h2', '', (block.number ? block.number + '. ' : '') + block.text);
      else if (block.kind === 'clause') node = el('p', 'clause', block.number + '. ' + block.text);
      else if (block.kind === 'sub') node = el('p', 'sub', block.number + '. ' + block.text);
      else node = el('p', 'text', (block.label ? block.label + ' ' : '') + block.text);
      node.dataset.uid = block.uid;
      docEl.append(node);
    });
    const target = docEl.querySelector('[data-uid="' + CSS.escape(source.uid) + '"]');
    if (target) {
      target.classList.add('hl');
      target.scrollIntoView({ block: 'center' });
    } else {
      docEl.append(el('div', 'notice', 'Точное место не найдено — показан весь регламент.'));
    }
  } catch (error) {
    docEl.replaceChildren(el('div', 'notice', 'Не получилось открыть документ: ' + error.message));
  }
}

/* ---- статистика ---- */

async function loadStats() {
  const box = $('#tab-stats');
  try {
    const report = await api('/api/analytics');
    box.replaceChildren();
    const cards = el('div', 'cards');
    [[report.questions, 'вопросов'], [report.avg_tokens, 'токенов на вопрос в среднем'],
     [report.avg_seconds, 'секунд модели на вопрос'], [(report.tickets.open || 0), 'ждут ответа службы качества'],
     [(report.tickets.answered || 0), 'ответов в базе знаний']].forEach(([value, label]) => {
      const card = el('div', 'card');
      card.append(el('b', '', String(value)), el('span', '', label));
      cards.append(card);
    });
    box.append(cards, el('h4', 'sec', 'Маршруты'));
    const routes = el('div', 'routes');
    Object.entries(report.routes).sort((a, b) => b[1] - a[1]).forEach(([route, n]) => {
      const row = el('div');
      row.append(el('span', '', ROUTE_NAMES[route] || route), el('b', '', String(n)));
      routes.append(row);
    });
    if (report.smalltalk) {
      const row = el('div');
      row.append(el('span', '', ROUTE_NAMES.smalltalk), el('b', '', String(report.smalltalk)));
      routes.append(row);
    }
    box.append(routes, el('h4', 'sec', 'Вопросы без ответа — что внести в документацию'));
    if (!report.unanswered.length) box.append(el('div', 'empty', 'Предложений нет.'));
    report.unanswered.forEach((row, i) => box.append(el('p', '', (i + 1) + '. ' + row.question + ' — ' + row.n + ' раз(а)')));
  } catch (error) {
    box.replaceChildren(el('div', 'notice', 'Не получилось: ' + error.message));
  }
}

/* ---- фоновые обновления ---- */

async function tick() {
  if (busy || !sid) return;
  try {
    if (tab === 'qc' && !app.classList.contains('panel-closed')) await loadTickets();
    const history = await api('/api/session/' + sid + '?after=' + lastId);
    history.messages.forEach((entry) => show(entry));
  } catch (error) { /* сервер недоступен — попробуем в следующий раз */ }
}

/* ---- ввод ---- */

$('#ask-form').addEventListener('submit', (event) => { event.preventDefault(); send(input.value); });
input.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(input.value); }
});
/* ---- несколько чатов: вкладки в шапке, у каждого своя сессия (свой диалог и память) ---- */

let chats = [];                   // [{ sid, name }] — список чатов, активный = совпадает с sid

function loadChats() {
  try { chats = JSON.parse(localStorage.getItem('regqa.chats') || '[]'); } catch (error) { chats = []; }
  if (!Array.isArray(chats)) chats = [];
  chats = chats.filter((c) => c && c.sid && c.name);
}

function saveChats() {
  localStorage.setItem('regqa.chats', JSON.stringify(chats));
}

async function openChat(chat) {
  closeChatMenu();
  sid = chat.sid; lastId = 0; sources = []; shown.clear(); pendingUser = null;
  localStorage.setItem('regqa.sid', sid);
  messagesEl.replaceChildren();
  try {
    const history = await api('/api/session/' + sid);
    history.messages.forEach((entry) => show(entry));
  } catch (error) { /* история недоступна — начнём с пустого */ }
  renderChats();
  scrollDown();
}

async function createChat() {
  if (busy) return;
  const session = await newSession();
  const chat = { sid: session.id, name: 'Чат ' + (chats.length + 1) };
  chats.push(chat);
  saveChats();
  await openChat(chat);
}

async function closeChat(chat) {
  chats = chats.filter((c) => c !== chat);
  saveChats();
  if (chat.sid !== sid) { renderChats(); return; }
  if (chats.length) { await openChat(chats[chats.length - 1]); return; }
  sid = null;
  await createChat();             // закрыли последний — начнём свежий «Чат 1»
}

function renameChat(chat) {
  renderChats(chat);
}

function renderChats(renaming) {
  const box = $('#chat-tabs');
  box.replaceChildren();
  chats.forEach((chat) => {
    if (renaming === chat) {
      const chip = button('chat-chip active', '', () => {});
      const field = el('input');
      field.value = chat.name;
      field.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') field.blur();
        if (event.key === 'Escape') { field.value = chat.name; field.blur(); }
      });
      field.addEventListener('blur', () => {
        chat.name = field.value.trim() || ('Чат ' + (chats.indexOf(chat) + 1));
        saveChats();
        renderChats();
      });
      chip.replaceChildren(field);
      box.append(chip);
      field.focus();
    } else {
      const chip = button('chat-chip' + (chat.sid === sid ? ' active' : ''), '', () => {
        if (!busy && chat.sid !== sid) openChat(chat);
      });
      chip.textContent = chat.name;
      chip.addEventListener('contextmenu', (event) => openChatMenu(event, chat));
      box.append(chip);
    }
  });
  const add = button('ghost', '+ Новый чат', () => { if (!busy) createChat(); });
  add.type = 'button';
  box.append(add);
}

function openChatMenu(event, chat) {
  event.preventDefault();
  closeChatMenu();
  const menu = el('div', 'context-menu');
  menu.append(button('', 'Переименовать', () => { closeChatMenu(); renameChat(chat); }),
              button('danger', 'Закрыть чат', () => { closeChatMenu(); closeChat(chat); }));
  menu.style.left = Math.min(event.clientX, window.innerWidth - 190) + 'px';
  menu.style.top = Math.min(event.clientY, window.innerHeight - 110) + 'px';
  document.body.append(menu);
  setTimeout(() => {
    document.addEventListener('click', closeChatMenu, { once: true });
    document.addEventListener('keydown', closeChatMenu, { once: true });
    document.addEventListener('contextmenu', (e) => { if (!e.defaultPrevented) closeChatMenu(); }, { once: true });
  }, 0);
}

function closeChatMenu() {
  const menu = document.querySelector('.context-menu');
  if (menu) menu.remove();
}

/* ---- темы оформления: Сталь и Графит (html[data-theme]), выбор в localStorage ---- */

const THEME_LIST = ['steel', 'graphite'];

function applyTheme(name) {
  if (!THEME_LIST.includes(name)) name = 'steel';   // сохранённое имя устарело — берём Сталь
  document.documentElement.dataset.theme = name;
  localStorage.setItem('regqa.theme', name);
  document.querySelectorAll('.theme-tabs .theme-chip').forEach((b) => b.classList.toggle('active', b.dataset.theme === name));
}

document.querySelectorAll('.theme-tabs .theme-chip').forEach((b) => b.addEventListener('click', () => applyTheme(b.dataset.theme)));
applyTheme(localStorage.getItem('regqa.theme') || 'steel');

/* ---- модели на выбор: кнопки-переключатели на вкладке «Настройки», выбор хранится в сессии ---- */

async function newSession() {
  const model = localStorage.getItem('regqa.model');
  return api('/api/session', { method: 'POST', body: JSON.stringify(model ? { model } : {}) });
}

function setupModels(health) {
  // вкладка «Настройки» правой колонны: кнопки-переключатели, действующая модель подсвечена
  const group = $('#model-switch');
  $('#tab-settings-button').hidden = false;
  let current = localStorage.getItem('regqa.model');
  if (!health.models.some((m) => m.id === current)) current = health.model_default;
  const paint = () => group.querySelectorAll('.model-chip').forEach((node) => {
    const on = node.dataset.model === current;
    node.classList.toggle('active', on);
    node.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const choose = async (id) => {                      // действует сразу во всех чатах и запоминается для новых
    if (id === current) return;
    current = id;
    paint();
    try {
      const results = await Promise.allSettled(chats.map((chat) => api('/api/session/' + chat.sid + '/model',
        { method: 'POST', body: JSON.stringify({ model: id }) })));
      if (!results.some((r) => r.status === 'fulfilled')) throw new Error('модель не переключилась');
      localStorage.setItem('regqa.model', id);
    } catch (error) {
      current = health.model_default;
      localStorage.removeItem('regqa.model');
      paint();
    }
  };
  health.models.forEach((m) => {
    const node = button('model-chip', m.label, () => choose(m.id));
    node.dataset.model = m.id;
    node.setAttribute('role', 'radio');
    group.append(node);
  });
  paint();
}

/* ---- старт ---- */

(async function init() {
  try {
    const health = await api('/api/health');
    const examples = $('#examples'), drop = $('#examples-drop');
    if (health.examples && health.examples.length) {
      drop.hidden = false;
      health.examples.forEach((question) => examples.append(button('', question, () => {
        drop.open = false;           // выбрали тестовый вопрос — список закрывается, вопрос уходит в чат
        send(question);
      })));
    }
    if (health.ask_limit > 0) $('#limit-note').textContent =
      'Демонстрационный стенд ответов по регламенту: до ' + health.ask_limit + ' вопросов в час с одного IP.';
    if (health.models && health.models.length > 1) setupModels(health);
  } catch (error) { /* примеры не обязательны */ }
  try {
    ticketMap = Object.fromEntries((await api('/api/tickets')).map((ticket) => [ticket.id, ticket]));
  } catch (error) { ticketMap = {}; }
  loadChats();
  if (!chats.length) {
    // первый запуск (или чистый localStorage): один диалог «Чат 1»; старый sid мигрирует в него
    if (sid) chats = [{ sid, name: 'Чат 1' }];
    else {
      try { chats = [{ sid: (await newSession()).id, name: 'Чат 1' }]; }
      catch (error) {
        messagesEl.append(el('div', 'msg system', 'Сервер не отвечает. Обновите страницу позже.'));
        return;
      }
    }
    saveChats();
  }
  await openChat(chats.find((c) => c.sid === sid) || chats[0]);
  if (mode === 'qc') { showTab('qc'); }
  if (mode === 'analytics') { showTab('stats'); }
  setInterval(tick, 2500);
  input.focus();
})();
