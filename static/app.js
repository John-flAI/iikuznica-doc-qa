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

const ICONS = {   // значки карточки запроса: бумажный самолётик — черновик, галочка — отправлено
  send: '<path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
};
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  svg.innerHTML = ICONS[name];
  return svg;
}

let lastUserText = '';   // последний показанный вопрос сотрудника: запасной источник текста вопроса для карточки (старый сервер не присылает ticket.question)

function ticketIco(name) {
  const node = el('span', 't-ico');
  node.setAttribute('aria-hidden', 'true');
  node.append(icon(name));
  return node;
}

// после отправки и в истории на месте карточки — компактная строка; текст «ожидайте ответа» пишет в чат сервер
// своим сообщением (route = sent), поэтому второго предложения здесь нет
function ticketDone(id, status, result) {
  const done = el('div', 'ticket done');
  done.dataset.ticket = id;   // по номеру карточку находит форма ответа службы качества — меняет «отправлен» на «отвечен» без перезагрузки
  done.append(ticketIco('check'), el('span', 't-text', 'Запрос № ' + id + ' · ' + (status === 'answered' ? 'отвечен' : 'отправлен')));
  if (result && result.letter_delivered === true) done.append(el('span', 'sent-mail', 'Письмо отправлено в службу качества'));   // только когда сервер подтвердил доставку
  return done;
}

// карточка «нет ответа» — деловая форма (слово владельца 28.11): номер, вопрос сотрудника дословно, одна кнопка «Отправить».
// Текст письма сотрудник не видит — оно уходит в ящик службы качества
function ticketBlock(message) {
  const meta = message.meta || {};
  const ticket = meta.ticket, state = ticketMap[ticket.id] || { status: 'draft' };
  if (state.status !== 'draft') return ticketDone(ticket.id, state.status);
  // вопрос дословно — поле сервера; на старом сервере его нет — строка письма в «кавычках-ёлочках» или вопрос из ленты выше
  const question = ticket.question || (String(ticket.letter || '').match(/^«(.+)»\s*$/m) || [])[1] || lastUserText || '';
  const box = el('div', 'ticket');
  box.dataset.ticket = ticket.id;
  const head = el('div', 't-head');
  head.append(ticketIco('send'), el('b', 't-title', 'Запрос в службу качества'), el('span', 't-num', '№ ' + ticket.id));
  const q = el('p', 't-q');
  q.append(el('span', 't-label', 'Ваш вопрос'), question ? '«' + question + '»' : 'не сохранился');
  const row = el('div', 'row');
  const submit = button('primary qc', '', async (event) => {
    const target = event.currentTarget;             // сама кнопка (внутри — значок: event.target мог бы оказаться им)
    target.disabled = true;
    const from = target.getBoundingClientRect();     // карточку заменит компактная строка — запоминаем, где была кнопка
    try {
      const result = await api('/api/tickets/' + ticket.id + '/send', { method: 'POST' });
      ticketMap[ticket.id] = { status: 'open' };
      box.replaceWith(ticketDone(ticket.id, 'open', result));
      if (!demoOn()) applyDemo(true, true);   // запрос уходит в ящик службы качества — тестовые инструменты показываются, колонна откроется на ящике
      loadMail();                             // не ждём: новое письмо должно быть в списке, когда линия долетит
      await flyToPanel(from);
      openPanel('qc');
    } catch (error) {
      row.replaceWith(el('div', 'notice', 'Не получилось отправить: ' + error.message));
    }
  });
  submit.append(icon('send'), el('span', '', 'Отправить'));
  row.append(submit);
  box.append(head, q, row);
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
    lastUserText = entry.text;
    messagesEl.append(node);
  } else if (entry.role === 'system') {
    // об отправке запроса (route = sent) сервер пишет своей строкой — показываем её строкой-статусом службы качества; текст не меняется
    messagesEl.append(el('div', 'msg system' + (meta.route === 'sent' ? ' sent' : ''), entry.text));
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
    node.append(el('div', 'meta', facts.join(' · ')), copyButton(meta.text));   // «Копировать» видна только в вариантах, где она предусмотрена (CSS)
    messagesEl.append(node);
  }
  const last = messagesEl.lastElementChild;                    // индекс сообщения — для каскадной механики
  if (last && last.classList.contains('msg')) last.style.setProperty('--i', String(messagesEl.children.length - 1));
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
  // тестовая вкладка (data-demo в разметке) пока инструменты скрыты не открывается — показываем документы
  const demoTab = app.querySelector('.tab[data-tab="' + name + '"]');
  if (demoTab && demoTab.hasAttribute('data-demo') && !panel.classList.contains('demo-on')) name = 'docs';
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

/* линия полёта: запрос уходит из чата к службе качества (панель открыта — в её заголовок,
   закрыта — к рельсе справа, откуда панель потом открывается) */
function flyToPanel(a) {
  const b = panel.getBoundingClientRect();
  const shown = b.width > 0 && !app.classList.contains('panel-closed');
  const x1 = a.right, y1 = a.top + a.height / 2;
  const x2 = shown ? b.left + 70 : $('#flight').clientWidth - 22, y2 = shown ? b.top + 28 : Math.round($('#flight').clientHeight / 2);
  return fly(x1, y1, x2, y2);
}

/* ответ службы качества возвращается той же линией: из колонны — в ленту чата, к месту, где появится сообщение */
function flyToChat(a) {
  const m = messagesEl.getBoundingClientRect();
  return fly(a.left, a.top + a.height / 2, m.right - Math.min(m.width * .55, 420), m.bottom - 84);
}

function fly(x1, y1, x2, y2) {
  return new Promise((resolve) => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) return resolve();   // без движения линия не летит
    const path = $('#flight-path'), dot = $('#fly-dot'), k = x2 >= x1 ? 160 : -160;
    path.setAttribute('d', `M ${x1} ${y1} C ${x1 + k} ${y1}, ${x2 - k} ${y2}, ${x2} ${y2}`);
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
  if (!tickets.length) list.append(el('div', 'empty', 'Запросов пока нет. Они появятся здесь, когда сотрудник отправит запрос из чата.'));
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
      const from = event.target.getBoundingClientRect();   // кнопку заменит «Ответ отправлен» — запоминаем, где она была
      try {
        const result = await api('/api/tickets/' + ticket.id + '/answer', { method: 'POST', body: JSON.stringify({ answer: answer.value, author: author.value }) });
        const done = el('div', 'qc-done', 'Ответ отправлен сотруднику и добавлен в базу знаний: теперь агент ответит на этот вопрос сам.');
        if (result && result.letter_delivered === true) done.append(el('div', 'sent-mail', 'Письма доставлены — службе качества и сотруднику'));
        form.replaceWith(done);
        await loadTickets();
        await loadMail();                 // в ящике службы качества появляется исходящее «Ответ службы качества»
        busy = true;
        try { await flyToChat(from); } finally { busy = false; }   // линия возвращается в чат; опрос сессии ждёт её
        await tick();                     // ответ службы сразу появляется в чате — без ожидания опроса сессии
        const card = messagesEl.querySelector('.ticket[data-ticket="' + ticket.id + '"]');   // карточка запроса в чате — сразу «отвечен», без перезагрузки
        if (card) card.replaceWith(ticketDone(ticket.id, 'answered'));
        // слово владельца: колонна сворачивается с задержкой 1–2 с, когда сообщение дошло до чата; за эти полторы
        // секунды в колонну тыкнули (следующий ответ) — оставляем открытой
        let held = false;
        const hold = () => { held = true; };
        panel.addEventListener('pointerdown', hold);
        await new Promise((resolve) => setTimeout(resolve, 1500));
        panel.removeEventListener('pointerdown', hold);
        if (!held) app.classList.add('panel-closed');
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
    docEl.append(el('h2', 'kb', 'Ответ службы качества'), el('p', 'text', source.quote));
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
      chip.addEventListener('mousedown', (event) => { if (event.button === 1) event.preventDefault(); });   // колёсико: без автопрокрутки страницы
      chip.addEventListener('auxclick', (event) => { if (event.button === 1 && !busy) closeChat(chat); });   // закрыть чат, как вкладку в браузере
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

/* ---- тема (Графит = тёмная, Сталь = светлая) и оформление (Аврора, Слэк): две кнопки-переключателя
   в доке поверх шапки, выбор в localStorage. Имена атрибутов прежние: html[data-theme], html[data-motion].
   В каждой кнопке лежат оба значка; видимый выбирает CSS по тем же атрибутам (style.css, .dock-btn .half).
   Остальные 8 вариантов витрины — в архиве для статьи: sprint/_out/дизайн/36-statya/ ---- */

const THEME_LIST = ['steel', 'graphite'], THEME_WORDS = { steel: ['светлая', 'светлой'], graphite: ['тёмная', 'тёмной'] };   // именительный, творительный
const SKIN_LIST = ['v-aurora', 'v-slack'], SKIN_WORDS = { 'v-aurora': 'Аврора', 'v-slack': 'Слэк' };
const root = document.documentElement, themeBtn = $('#theme-toggle'), skinBtn = $('#skin-toggle');
const nextIn = (list, name) => list[(list.indexOf(name) + 1) % list.length];
const hit = (node, selector) => (node instanceof Element ? node.closest(selector) : null);

function applyTheme(name) {
  if (!THEME_LIST.includes(name)) name = 'graphite';   // сохранённое имя устарело — берём тёмную
  root.dataset.theme = name;
  localStorage.setItem('regqa.theme', name);
  themeBtn.setAttribute('aria-checked', String(name === 'graphite'));
  themeBtn.dataset.tip = 'Тема: ' + THEME_WORDS[name][0] + ' · нажмите — станет ' + THEME_WORDS[nextIn(THEME_LIST, name)][1];
}

function applySkin(name) {
  if (!SKIN_LIST.includes(name)) name = 'v-aurora';    // сохранённое имя устарело (была витрина вариантов) — берём Аврору
  root.dataset.motion = name;
  localStorage.setItem('regqa.motion', name);
  const next = SKIN_WORDS[nextIn(SKIN_LIST, name)];
  skinBtn.dataset.tip = 'Оформление: ' + SKIN_WORDS[name] + ' · нажмите — станет ' + next;
  skinBtn.setAttribute('aria-label', 'Оформление: ' + SKIN_WORDS[name] + '. Переключить на ' + next);
  // переключили оформление — сообщения входят заново: механику видно сразу, не нужно ждать нового ответа
  messagesEl.classList.add('replay');
  void messagesEl.offsetWidth;
  messagesEl.classList.remove('replay');
}

themeBtn.addEventListener('click', () => applyTheme(nextIn(THEME_LIST, root.dataset.theme)));
skinBtn.addEventListener('click', () => applySkin(nextIn(SKIN_LIST, root.dataset.motion)));
applyTheme(localStorage.getItem('regqa.theme') || 'graphite');
applySkin(localStorage.getItem('regqa.motion') || 'v-aurora');

/* ---- страница входа (демонстрационная): почта и пароль заполнены заранее и не меняются, «Войти» открывает
   стенд, «Выход» в доке возвращает сюда. Отметка входа — regqa.login (ключ контракта почты, _out/почта_отчёт.md);
   «Войти» вызывает POST /api/login — сервер ничего не проверяет. Прочая ошибка сервера показывается здесь же
   и в стенд не пускает; «маршрута нет» (404/405 — сервер без почты) оставляет вход просто оформлением, как было.
   Адреса — из /api/health → accounts (setupMail), до ответа — заглушка ACCOUNTS ---- */

const ACCOUNTS = {
  client: { name: 'Сотрудник', email: 'client1@docii.aized.ru' },
  quality: { name: 'Служба качества', email: 's.kachestva@docii.aized.ru' },
  agent: { name: 'ДокИИ', email: 'agent@docii.aized.ru' },
};
let accounts = ACCOUNTS;
function paintGate() {
  $('#gate-email').value = accounts.client.email;
  $('#gate-pass').value = 'docii-demo';                // точки-заглушка: у стенда пароля нет
}
paintGate();

function showGate(on) {
  root.classList.toggle('gate-first', on);             // html.gate-first прячет стенд и показывает страницу входа (CSS)
  (on ? $('#gate-enter') : input).focus();
}

if (localStorage.getItem('regqa.entered')) localStorage.setItem('regqa.login', 'client');   // вошёл до почты — остаётся в стенде
$('#gate-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const enter = $('#gate-enter'), errorBox = $('#gate-error');
  enter.disabled = true;
  errorBox.hidden = true;
  try {
    await api('/api/login', { method: 'POST', body: JSON.stringify({ role: 'client' }) });
  } catch (error) {
    if (error.status !== 404 && error.status !== 405) {
      errorBox.textContent = 'Не получилось войти: ' + error.message;
      errorBox.hidden = false;
      enter.disabled = false;
      return;
    }
  }
  localStorage.setItem('regqa.login', 'client');
  enter.disabled = false;
  showGate(false);
});
$('#exit-btn').addEventListener('click', () => {       // «Выйти»: чаты и история (regqa.chats, regqa.sid) остаются
  hideTip();
  localStorage.removeItem('regqa.login');
  showGate(true);
});
if (root.classList.contains('gate-first')) showGate(true);

/* ---- тестовые инструменты стенда — служба качества, почта клиента, статистика, настройки: это не продукт, а показ «как это
   работает». По умолчанию скрыты, в правой колонне остаётся «Документы»; третья кнопка дока их показывает,
   выбор в localStorage. Скрытие — только оформление (style.css: .panel без demo-on прячет вкладки с data-demo),
   логика вкладок не тронута. Запросов к API не делает ---- */

const DEMO_TABS = ['qc', 'mail', 'stats', 'settings'], demoBtn = $('#demo-toggle');
const demoOn = () => panel.classList.contains('demo-on');
function applyDemo(on, remember) {
  panel.classList.toggle('demo-on', on);
  if (remember) localStorage.setItem('regqa.demo', on ? '1' : '0');
  demoBtn.setAttribute('aria-checked', String(on));
  demoBtn.dataset.tip = on ? 'Тестовые инструменты: показаны · нажмите — скрыть'
                           : 'Тестовые инструменты: скрыты · нажмите — показать службу качества, почту клиента, статистику и настройки';
  demoBtn.setAttribute('aria-label', 'Тестовые инструменты: ' + (on ? 'показаны. Скрыть' : 'скрыты. Показать'));
  $('#rail').dataset.tip = on ? 'Открыть: служба качества, почту клиента, документы, статистику, настройки' : 'Открыть документы';
  if (!on && DEMO_TABS.includes(tab)) showTab('docs');   // скрыли инструменты, стоя на одном, — остаёмся в документах
}
demoBtn.addEventListener('click', () => { applyDemo(!demoOn(), true); loadMail(); });   // показали инструменты — заодно обновляем ящики
applyDemo(mode === 'full' ? localStorage.getItem('regqa.demo') === '1' : true, false);   // без сохранённого выбора — скрыты; экраны /qc и /analytics — сами инструменты
if (mode === 'full') showTab('docs');

/* ---- вкладка «Настройки» (тестовый инструмент): три блока по слову владельца — «модели, айпи, почта». Модель —
   кнопками-переключателями: список #model-switch в шапке заполняет исполнитель (setupModels), он же хранит выбор и
   ходит на сервер; список скрыт оформлением, а кнопки только ставят в нём значение и вызывают его же событие change —
   логика запросов не тронута. «Стенд и API» и «Почта» — справочные (блок «почта» ниже) ---- */

const modelPick = $('#model-switch'), modelChips = $('#model-chips');
function paintModelChips() {
  const options = Array.from(modelPick.options);
  $('#model-empty').hidden = options.length > 0;
  modelChips.replaceChildren(...options.map((option) => {
    const chip = button('model-chip' + (option.selected ? ' active' : ''), option.text, () => {
      if (option.selected) return;
      modelPick.value = option.value;
      modelPick.dispatchEvent(new Event('change'));
      paintModelChips();
      setTimeout(paintModelChips, 1500);   // сервер не переключил — список вернёт прежнюю модель, кнопки догоняют
    });
    chip.dataset.model = option.value;
    chip.setAttribute('role', 'radio');
    chip.setAttribute('aria-checked', String(option.selected));
    return chip;
  }));
}
app.querySelectorAll('.tab[data-tab="settings"]').forEach((node) => node.addEventListener('click', () => { paintModelChips(); loadMail(); }));   // кнопки моделей и справочные адреса почты

/* ---- почта (имитация): ящик службы качества — на одноимённой вкладке (письмо + под ним прежняя карточка запроса
   с формой ответа); ящик сотрудника — вкладка «Почта клиента» (слово владельца 28.11: почту клиента оставить,
   так и обозвать, держать в тестовых инструментах — на стартовом экране её не видно). Контракт — _out/почта_отчёт.md: GET /api/letters?role=quality|client (список),
   GET /api/letter/{id} (целиком). Здесь только чтение: список, письмо и счётчик непрочитанных (прочитанное помним
   в браузере, regqa.read). На сервере без почты (нет accounts в health, либо 404/405) — html.no-mail: «Почта клиента» и блок почты
   на «Настройках» спрятаны, у службы качества остаётся прежний список запросов ---- */

const MAIL_BOXES = {   // ящики тестовых инструментов: где список, где открытое письмо, счётчик непрочитанных и что писать в пустом ящике
  quality: { list: '#qc-mail-list', detail: '#qc-letter', name: '#qc-mail-name', addr: '#qc-mail-addr', badge: '#qc-badge',
             empty: 'Писем пока нет. Здесь появится письмо, когда сотрудник отправит запрос из чата.' },
  client: { list: '#client-mail-list', detail: '#client-letter', name: '#client-mail-name', addr: '#client-mail-addr', badge: '#mail-badge', title: 'Почта клиента',
            empty: 'Писем пока нет. Здесь появятся уведомление о запросе в службу качества и ответ на него.' },
};
const QC_MAIL = MAIL_BOXES.quality;
const KIND_WORDS = { question: 'вопрос', notice: 'уведомление', reply: 'ответ', answer: 'ответ' };
const boxes = { quality: null, client: null };        // письма ящиков из API; null — ещё не получены
let mailOn = true, mailBusy = false, openedLetter = null;   // { role, id } — открытое письмо, подсвечено в списке

const readMarks = () => { try { return JSON.parse(localStorage.getItem('regqa.read') || '{}'); } catch (error) { return {}; } };
const isRead = (role, id) => (readMarks()[role] || []).includes(id);
const unreadIn = (role) => (boxes[role] || []).filter((letter) => !letter.outgoing && !isRead(role, letter.id)).length;   // непрочитанные — только входящие
const who = (address) => { const found = Object.values(accounts).find((account) => account.email === address); return found ? found.name : address; };
const when = (iso) => { const date = new Date(iso); return isNaN(date) ? '' : date.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }); };

function setBadge(node, count) {
  node.textContent = count > 0 ? String(count) : '';   // ноль не пишем: имя вкладки не должно звучать «Почта 0»
  node.hidden = !(count > 0);
}

function letterRow(letter, role) {
  const current = openedLetter && openedLetter.role === role && openedLetter.id === letter.id;
  const row = button('letter' + (!letter.outgoing && !isRead(role, letter.id) ? ' unread' : '') + (current ? ' active' : ''), '', () => openLetter(letter, role));
  row.append(el('span', 'dot'), el('span', 'subj', letter.subject), el('span', 'when', when(letter.created)),
             el('span', 'route', (letter.outgoing ? '→ ' : '') + who(letter.outgoing ? letter.to : letter.from) +
                               ' · ' + (letter.outgoing ? 'отправлено' : 'входящее') + (KIND_WORDS[letter.kind] ? ' · ' + KIND_WORDS[letter.kind] : '')));
  return row;
}

function paintMail() {
  if (!mailOn) return;
  for (const [role, box] of Object.entries(MAIL_BOXES)) {   // оба ящика одним кодом: служба качества и «Почта клиента»
    if (!accounts[role]) continue;
    setBadge($(box.badge), unreadIn(role));
    $(box.name).textContent = box.title || accounts[role].name;
    $(box.addr).textContent = accounts[role].email;
    const letters = boxes[role];
    $(box.list).replaceChildren(...(letters && letters.length ? letters.map((letter) => letterRow(letter, role)) : [el('div', 'empty', box.empty)]));
  }
  paintMailInfo();
}

/* «Настройки», справочные блоки (слово владельца: модели, «айпи», почта). Только показ того, что уже прислал сервер */
function fact(name, value) {
  const row = el('div', 'fact');
  row.append(el('dt', '', name), el('dd', '', value));
  return row;
}
function paintStandFacts() {
  const models = (standHealth && standHealth.models) || [];
  $('#stand-facts').replaceChildren(
    fact('Адрес стенда (IP)', location.host),
    fact('API моделей', models.length ? models.map((m) => m.label.replace(' · ', ' — ')).join(', ') : 'на сервере не объявлено'),
    fact('Лимит вопросов', standHealth && standHealth.ask_limit > 0 ? 'до ' + standHealth.ask_limit + ' в час с одного IP' : 'без лимита'),
  );
}
function paintMailInfo() {   // «Настройки», блок «Почта»: адреса стенда и отправка — сами письма на вкладке «Почта клиента»
  $('#mail-facts').replaceChildren(
    fact('Сотрудник', accounts.client.email),
    fact('Служба качества', accounts.quality.email),
    fact('Агент', accounts.agent.email),
    fact('Отправка', standHealth && standHealth.mail && standHealth.mail.smtp ? 'включена — письма уходят на почтовый сервер стенда' : 'выключена — письма хранятся на стенде'),
  );
}

/* список ящиков: оба спрашиваем, пока тестовые инструменты показаны (счётчик непрочитанных на вкладках), — либо открыта сама вкладка */
async function loadMail() {
  if (!mailOn || mailBusy) return;
  mailBusy = true;
  const roles = [];
  if (demoOn() || tab === 'qc') roles.push('quality');
  if (demoOn() || tab === 'mail') roles.push('client');
  try {
    const lists = await Promise.all(roles.map((role) => api('/api/letters?role=' + role)));
    roles.forEach((role, i) => { boxes[role] = lists[i]; });
  } catch (error) {
    if (error.status === 404 || error.status === 405) { mailOff(); return; }   // сервер без почты
  } finally { mailBusy = false; }
  paintMail();
}
function mailOff() {
  mailOn = false;
  root.classList.add('no-mail');     // CSS: прятать «Почту», в ящике службы качества показать прежний список запросов
}

async function openLetter(letter, role) {
  openedLetter = { role, id: letter.id };
  const marks = readMarks();
  marks[role] = [...new Set([...(marks[role] || []), letter.id])].slice(-300);
  localStorage.setItem('regqa.read', JSON.stringify(marks));
  const box = $(MAIL_BOXES[role].detail);
  box.replaceChildren(el('div', 'empty', 'Открываю письмо…'));
  if (role === 'quality') $('#qc-detail').replaceChildren();   // под письмом службы качества — карточка запроса; прежнюю убираем, пока письмо идёт
  paintMail();
  try {
    const full = await api('/api/letter/' + letter.id);
    const card = el('article', 'letter-full');
    card.append(el('h3', '', full.subject),
                el('div', 'letter-meta', 'От: ' + who(full.from) + ' · ' + full.from + (full.cc ? '   Копия: ' + full.cc : '')),
                el('div', 'letter-meta', 'Кому: ' + who(full.to) + ' · ' + full.to + '   ' + when(full.created)),
                el('pre', 'letter-text', full.text));
    box.replaceChildren(card);
    if (role === 'quality' && full.ticket_id) selectTicket(full.ticket_id);   // форма ответа — существующая карточка запроса; письма клиента только читают
    box.scrollIntoView({ block: 'nearest', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
  } catch (error) {
    box.replaceChildren(el('div', 'notice', 'Не получилось открыть письмо: ' + error.message));
  }
}

let standHealth = null;                          // ответ /api/health: адреса, модели, лимит, SMTP
async function setupMail() {
  try {
    standHealth = await api('/api/health');
    if (standHealth.accounts) accounts = { ...ACCOUNTS, ...standHealth.accounts };
    else mailOff();
  } catch (error) { mailOff(); }
  paintGate();
  paintStandFacts();
  paintMail();
  loadMail();
}
app.querySelectorAll('.tab[data-tab="qc"], .tab[data-tab="mail"]').forEach((node) => node.addEventListener('click', loadMail));
setupMail();
setInterval(() => { if (!document.hidden) loadMail(); }, 4000);          // новые письма и счётчик непрочитанных

/* ---- подсказки при наведении: одна всплывашка на все кнопки. Текст — из data-tip, иначе из HINTS,
   иначе из старого title. Запросов к API не делает ---- */

const HINTS = [
  ['#gate-enter', 'Открыть стенд — данные заполнены заранее'],
  ['#ask-send', 'Отправить вопрос · Enter'],
  ['.examples-drop summary', 'Готовые вопросы для проверки стенда'],
  ['.examples button', 'Задать этот вопрос'],
  ['.chat-tabs .ghost', 'Новый диалог с чистой историей'],
  ['.chat-chip', 'Клик колёсиком — закрыть чат, правый клик — переименовать или закрыть'],
  ['#panel-collapse', 'Свернуть колонну'],
  ['#panel-close', 'Свернуть колонну'],
  ['.tab[data-tab="docs"]', 'Открыть документы'],
  ['.tab[data-tab="qc"]', 'Тестовый инструмент: ящик службы качества — здесь отвечают сотруднику'],
  ['.tab[data-tab="mail"]', 'Тестовый инструмент: почта клиента — сюда приходят уведомления и ответы службы качества'],
  ['.letter', 'Открыть письмо'],
  ['.tab[data-tab="stats"]', 'Тестовый инструмент: статистика вопросов'],
  ['.tab[data-tab="settings"]', 'Тестовый инструмент: выбор модели'],
  ['.model-chip', 'Отвечать этой моделью'],
  ['.msg-copy', 'Скопировать ответ в буфер обмена'],
  ['.source', 'Открыть документ и подсветить это место'],
  ['.option', 'Выбрать этот вариант — он уйдёт как ваш ответ'],
  ['.ticket .primary', 'Запрос уйдёт в службу качества — ответ придёт в этот чат'],
  ['.msg.agent > .primary', 'Задать последний вопрос ещё раз'],
  ['.qc-item', 'Показать запрос'],
  ['.qc-form .primary', 'Ответ уйдёт сотруднику в чат и попадёт в базу знаний'],
  ['#weekly', 'Отчёт службы качества за 7 дней'],
  ['.doc-pick button', 'Открыть этот источник'],
  ['.context-menu button:not(.danger)', 'Переименовать: Enter — сохранить, Esc — отмена'],
  ['.context-menu .danger', 'Закрыть чат — диалог убирается из списка'],
];
const CONTROL = 'button, summary, [data-tip], [role="button"], [role="radio"], [role="switch"]';
const tipBox = el('div', 'tip');
tipBox.id = 'tip';
tipBox.setAttribute('role', 'tooltip');
document.body.append(tipBox);
let tipOwner = null, tipWait = null;

function tipText(control) {
  if (control.dataset.tip) return control.dataset.tip;
  if (!control.dataset.title && control.title) {       // родную всплывашку браузера гасим — будет наша
    control.dataset.title = control.title;
    control.title = '';
  }
  const hint = HINTS.find(([selector]) => control.matches(selector));
  return control.dataset.title || (hint ? hint[1] : '');
}

function showTip(control) {
  const text = tipText(control);
  if (!text) return;
  tipOwner = control;
  tipBox.textContent = text;
  control.setAttribute('aria-describedby', 'tip');
  tipBox.classList.add('on');
  const box = control.getBoundingClientRect(), size = tipBox.getBoundingClientRect();
  const width = window.innerWidth, height = window.innerHeight;
  const clampX = (x) => Math.min(Math.max(8, x), width - size.width - 8);
  const clampY = (y) => Math.min(Math.max(8, y), height - size.height - 8);
  let x = clampX(box.left + box.width / 2 - size.width / 2), y;   // по умолчанию — по центру над кнопкой
  if (box.height <= 200 && box.top - size.height - 8 >= 8) y = box.top - size.height - 8;            // над (хоть впритык к краю)
  else if (box.height <= 200 && box.bottom + size.height + 8 <= height) y = box.bottom + 8;      // под
  else {                                                                                        // высокий элемент (рельса, полоска на границе) — сбоку
    y = clampY(box.top + box.height / 2 - size.height / 2);
    x = box.left - size.width - 10 >= 8 ? box.left - size.width - 10 : clampX(box.right + 10);
  }
  tipBox.style.left = Math.round(x) + 'px';
  tipBox.style.top = Math.round(y) + 'px';
}

function hideTip() {
  clearTimeout(tipWait);
  tipWait = null;
  if (tipOwner) { tipOwner.removeAttribute('aria-describedby'); tipOwner = null; }
  tipBox.classList.remove('on');
}

document.addEventListener('pointerover', (event) => {
  const control = event.pointerType === 'touch' ? null : hit(event.target, CONTROL);
  if (control === tipOwner) return;
  hideTip();
  if (!control || control.disabled) return;
  tipWait = setTimeout(() => { tipWait = null; showTip(control); }, 220);   // мимо кнопки проезжаем без всплывашки
});
document.addEventListener('pointerdown', hideTip);
document.addEventListener('click', (event) => {       // после клика подсказка кнопки дока меняет текст — показываем новый, если указатель остался на кнопке
  const control = hit(event.target, CONTROL);
  hideTip();
  if (!control || !control.matches(':hover')) return;
  tipWait = setTimeout(() => { tipWait = null; showTip(control); }, 350);
});
document.addEventListener('scroll', hideTip, true);    // подсказка прибита к экрану — при прокрутке убираем
document.documentElement.addEventListener('pointerleave', hideTip);
document.addEventListener('focusin', (event) => {     // с клавиатуры подсказка появляется сразу у кнопки в фокусе
  if (event.target.matches('input, textarea, select')) return hideTip();
  const control = hit(event.target, CONTROL);
  if (control) showTip(control); else hideTip();
});
document.addEventListener('focusout', hideTip);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') hideTip(); });
window.addEventListener('blur', hideTip);

/* список «Тестовые вопросы» сворачивается не только кнопкой, но и кликом мимо него — по любому полю или кнопке; и по Esc */
document.addEventListener('pointerdown', (event) => {
  const drop = $('#examples-drop');
  if (drop.open && !drop.contains(event.target)) drop.open = false;
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') $('#examples-drop').open = false;
});

/* ---- презентационные мелочки оформлений: запросов к API не делают ---- */

/* кнопка «Копировать» у ответа агента; показывает её только оформление «Слэк» (CSS) */
function copyButton(text) {
  const node = button('msg-copy', 'Копировать', async () => {
    try { await navigator.clipboard.writeText(text || ''); node.textContent = 'Скопировано'; }
    catch (error) { node.textContent = 'Не удалось скопировать'; }
    setTimeout(() => { node.textContent = 'Копировать'; }, 1400);
  });
  node.title = 'Скопировать ответ в буфер обмена';
  return node;
}

/* ---- модели на выбор: переключатель в шапке, выбор хранится в сессии ---- */

async function newSession() {
  const model = localStorage.getItem('regqa.model');
  return api('/api/session', { method: 'POST', body: JSON.stringify(model ? { model } : {}) });
}

function setupModels(health) {
  const pick = $('#model-switch');
  $('#model-note').hidden = false;
  health.models.forEach((m) => pick.append(new Option(m.label, m.id)));
  const saved = localStorage.getItem('regqa.model');
  pick.value = health.models.some((m) => m.id === saved) ? saved : health.model_default;
  pick.addEventListener('change', async () => {
    const chosen = pick.value;
    localStorage.setItem('regqa.model', chosen);
    try { await api('/api/session/' + sid + '/model', { method: 'POST', body: JSON.stringify({ model: chosen }) }); }
    catch (error) { localStorage.removeItem('regqa.model'); pick.value = health.model_default; }
  });
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
