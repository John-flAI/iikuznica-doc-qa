'use strict';
/* Стенд: чат сотрудника, служба качества, документы с подсветкой, статистика. Без библиотек. */

const $ = (selector, root = document) => root.querySelector(selector);
const app = $('#app'), messagesEl = $('#messages'), input = $('#ask-input'), sendBtn = $('#ask-send');
const panel = $('#panel'), docEl = $('#doc'), docPick = $('#doc-pick');

const ROUTE_NAMES = {
  answer: 'ответ по регламенту', clarify: 'уточняющий вопрос', not_found: 'нет ответа в регламентах',
  unresolved: 'однозначного ответа нет', degraded: 'модель недоступна', qc: 'ответ службы качества',
  kb: 'ответ из базы знаний — ответ службы качества',
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
  if (!response.ok) throw new Error(body.detail || response.statusText);
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
      node.append(el('div', 'priority', 'Ответ службы качества приоритетнее регламента: регламент может быть в актуализации.'));
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
    node.append(el('div', 'meta', facts.join(' · ')));
    messagesEl.append(node);
  }
  scrollDown();
}

function welcome() {
  messagesEl.append(el('div', 'msg system',
    'Задайте вопрос по регламентам — например, один из примеров под полем ввода. ' +
    'Если ответа в регламентах нет, агент не станет его выдумывать и подготовит запрос в службу качества.'));
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
    show({ role: 'system', text: 'Не получилось: ' + error.message });
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
$('#new-dialog').addEventListener('click', async () => {
  sid = null; lastId = 0; sources = []; shown.clear(); pendingUser = null;
  messagesEl.replaceChildren();
  welcome();
  const session = await api('/api/session', { method: 'POST' });
  sid = session.id;
  localStorage.setItem('regqa.sid', sid);
});

/* ---- старт ---- */

(async function init() {
  try {
    const health = await api('/api/health');
    const examples = $('#examples');
    health.examples.forEach((question) => examples.append(button('', question, () => send(question))));
  } catch (error) { /* примеры не обязательны */ }
  try {
    ticketMap = Object.fromEntries((await api('/api/tickets')).map((ticket) => [ticket.id, ticket]));
  } catch (error) { ticketMap = {}; }
  if (sid) {
    try {
      const history = await api('/api/session/' + sid);
      history.messages.forEach((entry) => show(entry));
    } catch (error) { sid = null; }
  }
  if (!sid) {
    try {
      const session = await api('/api/session', { method: 'POST' });
      sid = session.id;
      localStorage.setItem('regqa.sid', sid);
    } catch (error) {
      messagesEl.append(el('div', 'msg system', 'Сервер не отвечает. Обновите страницу позже.'));
      return;
    }
  }
  if (!messagesEl.children.length) welcome();
  if (mode === 'qc') { showTab('qc'); }
  if (mode === 'analytics') { showTab('stats'); }
  setInterval(tick, 2500);
  input.focus();
})();
