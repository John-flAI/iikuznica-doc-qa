# -*- coding: utf-8 -*-
"""Хранилище на SQLite: диалоги, вопросы и их маршруты, запросы в службу качества, вызовы модели.

Нужно для трёх вещей: диалог с уточнениями, очередь службы качества с базой знаний
и статистика с токенами — по ней считается стоимость запроса.
"""
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

UNSET = object()          # «поле не передано» — отличается от None, «очистить»

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, created TEXT, mode TEXT, email TEXT DEFAULT '', model TEXT DEFAULT '',
    owner TEXT DEFAULT '',                -- владелец сессии: адрес сотрудника (изоляция по адресу)
    name TEXT DEFAULT '',                 -- имя чата на вкладке; пустое — без имени
    closed INTEGER DEFAULT 0);            -- закрытый чат ведёт себя как несуществующий, данные остаются
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, role TEXT, text TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, ts TEXT, question TEXT,
    route TEXT, uids TEXT, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    seconds REAL DEFAULT 0, model TEXT, turns INTEGER DEFAULT 0, ticket_id INTEGER, meta TEXT);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, question_id INTEGER,
    question TEXT, context TEXT, email TEXT DEFAULT '',
    status TEXT DEFAULT 'draft',              -- draft: ждёт кнопки «Отправить» · open: в очереди · answered
    answer TEXT, answered_by TEXT, answered_at TEXT);
CREATE TABLE IF NOT EXISTS letters (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, owner TEXT,
    kind TEXT,                                -- question: вопрос в службу качества · answer: ответ службы качества
    mail_from TEXT, mail_to TEXT, mail_cc TEXT DEFAULT '', subject TEXT, text TEXT,
    ticket_id INTEGER,
    delivered INTEGER);                       -- NULL: SMTP не задан · 0: ошибка отправки · 1: доставлено
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, session_id TEXT, question_id INTEGER,
    step TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, seconds REAL,
    ok INTEGER, error TEXT);
"""


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.local = threading.local()
        self._all_conns = []                                     # все открытые соединения — для close_all (сброс стенда)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}
            if 'model' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT ''")   # база переживает обновление
            if 'owner' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT DEFAULT ''")   # старые сессии — без владельца
            if 'name' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT DEFAULT ''")    # старые чаты — без имени
            if 'closed' not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN closed INTEGER DEFAULT 0")  # старые чаты — открыты

    def connect(self):
        conn = getattr(self.local, 'conn', None)
        if conn is None:
            conn = self.local.conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
            self._all_conns.append(conn)                         # реестр для close_all (сброс стенда)
        return conn

    def close_all(self):
        """Закрыть все соединения всех потоков: перед сбросом стенда файл базы переименовывается в бэкап,
        а на Windows открытые соединения переименование блокируют."""
        for conn in list(self._all_conns):
            try:
                conn.close()
            except sqlite3.Error:
                pass
        self._all_conns = []
        self.local = threading.local()                           # старые соединения больше не выдаются

    def _one(self, sql, *args):
        row = self.connect().execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql, *args):
        return [dict(row) for row in self.connect().execute(sql, args).fetchall()]

    def _run(self, sql, *args):
        with self.connect() as conn:
            cursor = conn.execute(sql, args)
            return cursor.lastrowid

    # ---- сессии и диалог ----

    def session(self, session_id=None, mode='full', email='', model=None, owner=None):
        """Сессия владельца: чужой или закрытый идентификатор не даёт доступа — создаётся своя сессия с пустой историей.

        Сессии нет → создаётся с этим владельцем; владелец совпал → возвращается;
        владелец у записи пустой (старая база) → присваивается первому обратившемуся;
        владелец другой или чат закрыт → новая сессия с новым идентификатором, теми же mode/model/email
        и чистой историей.
        owner=None — владелец не важен (прямая работа с хранилищем): как раньше.
        """
        found = self._one('SELECT * FROM sessions WHERE id=?', session_id) if session_id else None
        if found and owner is not None:
            if not found['closed'] and not found['owner']:     # старая запись — первому обратившемуся
                self._run('UPDATE sessions SET owner=? WHERE id=?', owner, session_id)
                return self._one('SELECT * FROM sessions WHERE id=?', session_id)
            if not found['closed'] and found['owner'] == owner:
                return found
            session_id, mode, email, model = uuid.uuid4().hex[:12], found['mode'], found['email'], found['model']
            found = None                       # чужая или закрытая — своя новая сессия, история чистая
        if found:
            return found
        session_id = session_id or uuid.uuid4().hex[:12]
        self._run('INSERT INTO sessions(id, created, mode, email, model, owner) VALUES(?,?,?,?,?,?)',
                  session_id, now(), mode, email, model or '', owner or '')
        return self._one('SELECT * FROM sessions WHERE id=?', session_id)

    def session_for(self, session_id, owner):
        """Сессия этого владельца для чтения или None: нет такой, чужая или закрытая — None.

        Старую запись без владельца присваиваем первому обратившемуся — после этого она только его.
        В отличие от session() ничего не создаёт: несуществующий идентификатор остаётся несуществующим.
        """
        found = self._one('SELECT * FROM sessions WHERE id=?', session_id)
        if not found or found['closed'] or (found['owner'] and found['owner'] != owner):
            return None
        if not found['owner']:
            self._run('UPDATE sessions SET owner=? WHERE id=?', owner, session_id)
            found['owner'] = owner
        return found

    def sessions_of(self, owner, limit=50):
        """Свои открытые сессии, сначала недавние по активности: {id, name, created, last}.

        last — время последнего сообщения; сообщений нет — время создания. Закрытые и чужие не попадают.
        """
        return self._all(
            "SELECT s.id, s.name, s.created, "
            "COALESCE((SELECT MAX(ts) FROM messages WHERE session_id=s.id), s.created) last "
            "FROM sessions s WHERE s.owner=? AND s.closed=0 ORDER BY last DESC LIMIT ?", owner, limit)

    def rename_session(self, session_id, name):
        """Дать чату имя (пустое — имя по умолчанию); владелец и открытость проверяет маршрут."""
        self._run('UPDATE sessions SET name=? WHERE id=?', name, session_id)
        return self._one('SELECT id, name FROM sessions WHERE id=?', session_id)

    def close_session(self, session_id, owner):
        """Закрыть свою открытую сессию: пометка closed=1, данные остаются. Чужая/закрытая/нет — None."""
        found = self.session_for(session_id, owner)
        if found is None:
            return None
        self._run('UPDATE sessions SET closed=1 WHERE id=?', session_id)
        return found

    def set_model(self, session_id, model):
        self._run('UPDATE sessions SET model=? WHERE id=?', model or '', session_id)

    def set_email(self, session_id, email):
        self._run('UPDATE sessions SET email=? WHERE id=?', email, session_id)

    def add_message(self, session_id, role, text, meta=None):
        return self._run('INSERT INTO messages(session_id, ts, role, text, meta) VALUES(?,?,?,?,?)',
                         session_id, now(), role, text, _json(meta) if meta else None)

    def messages(self, session_id, after_id=0):
        return self._all('SELECT * FROM messages WHERE session_id=? AND id>? ORDER BY id', session_id, after_id)

    # ---- вопросы ----

    def open_question(self, session_id):
        """Вопрос, по которому агент ждёт ответа сотрудника на уточнение."""
        return self._one('SELECT * FROM questions WHERE session_id=? AND route IS NULL ORDER BY id DESC LIMIT 1',
                         session_id)

    def start_question(self, session_id, question):
        return self._run('INSERT INTO questions(session_id, ts, question, meta) VALUES(?,?,?,?)',
                         session_id, now(), question, '{}')

    def touch_question(self, question_id, route=UNSET, uids=None, reply=None, turns=None, ticket_id=None,
                       awaiting=UNSET, options=UNSET, clarifications=None):
        """Накопить вызов модели и записать состояние диалога.

        UNSET — «не менять»; None — «очистить». Так «ответ получен, уточнения закрыты» (awaiting=None)
        не спутать с «поле не передано».
        """
        question = self._one('SELECT * FROM questions WHERE id=?', question_id)
        meta = _load(question['meta'])
        sets, args = [], []
        for column, value in (('route', route), ('turns', turns), ('ticket_id', ticket_id)):
            if value is not UNSET and value is not None:
                sets.append('%s=?' % column)
                args.append(value)
        if uids is not None:
            sets.append('uids=?')
            args.append(_json(uids))
        if reply is not None:
            sets.append('tokens_in=tokens_in+?, tokens_out=tokens_out+?, seconds=seconds+?, model=?')
            args += [reply.prompt_tokens + reply.retry_prompt_tokens,
                     reply.completion_tokens + reply.retry_completion_tokens, round(reply.seconds, 2), reply.model]
        for key, value in (('awaiting', awaiting), ('options', options)):
            if value is UNSET:
                continue
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        if clarifications is not None:
            meta['clarifications'] = clarifications
        sets.append('meta=?')
        args.append(_json(meta))
        self._run('UPDATE questions SET %s WHERE id=?' % ', '.join(sets), *args, question_id)

    # ---- вызовы модели ----

    def log_llm(self, reply, step, session_id=None, question_id=None):
        self._run('INSERT INTO llm_calls(ts, session_id, question_id, step, model, prompt_tokens, '
                  'completion_tokens, seconds, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?)',
                  now(), session_id, question_id, step, reply.model,
                  reply.prompt_tokens + reply.retry_prompt_tokens,
                  reply.completion_tokens + reply.retry_completion_tokens, round(reply.seconds, 2),
                  int(reply.ok), reply.error or None)

    def served_model(self):
        """Модель, которая реально ответила на последний запрос (имя из ответа API, а не из настроек)."""
        row = self._one('SELECT model FROM llm_calls WHERE ok=1 AND model IS NOT NULL ORDER BY id DESC LIMIT 1')
        return row['model'] if row else None

    # ---- служба качества ----

    def create_ticket(self, session_id, question_id, question, context, email=''):
        return self._run('INSERT INTO tickets(ts, session_id, question_id, question, context, email) '
                         'VALUES(?,?,?,?,?,?)', now(), session_id, question_id, question, context, email)

    def ticket(self, ticket_id, owner=None):
        """Запрос по номеру; с владельцем — только свой: чужой и несуществующий одинаково «не найден»."""
        row = self._one('SELECT * FROM tickets WHERE id=?', ticket_id)
        if row is None or owner is None:
            return row
        session = self._one('SELECT owner FROM sessions WHERE id=?', row['session_id'])
        return row if session and session['owner'] == owner else None

    def tickets(self, status=None, owner=None):
        """Запросы службы качества; с владельцем — только те, что пришли из его сессий."""
        if owner is None:                          # без владельца — все (база знаний при запуске, тесты)
            if status:
                return self._all('SELECT * FROM tickets WHERE status=? ORDER BY id DESC', status)
            return self._all('SELECT * FROM tickets ORDER BY id DESC')
        scope = 'session_id IN (SELECT id FROM sessions WHERE owner=?)'
        if status:
            return self._all('SELECT * FROM tickets WHERE %s AND status=? ORDER BY id DESC' % scope, owner, status)
        return self._all('SELECT * FROM tickets WHERE %s ORDER BY id DESC' % scope, owner)

    def send_ticket(self, ticket_id):
        self._run("UPDATE tickets SET status='open' WHERE id=? AND status='draft'", ticket_id)
        return self.ticket(ticket_id)

    def answer_ticket(self, ticket_id, answer, author):
        self._run("UPDATE tickets SET status='answered', answer=?, answered_by=?, answered_at=? WHERE id=?",
                  answer, author, now(), ticket_id)
        return self.ticket(ticket_id)

    def ticket_owner(self, ticket_id):
        """Владелец запроса — владелец сессии сотрудника, который его задал; переписка — часть запроса."""
        row = self._one('SELECT s.owner AS owner FROM tickets t JOIN sessions s ON s.id=t.session_id WHERE t.id=?',
                        ticket_id)
        return row['owner'] if row else None

    # ---- почта ----

    def add_letter(self, owner, kind, mail_from, mail_to, subject, text, ticket_id, mail_cc=''):
        """Новое письмо владельца; delivered пока NULL — решится при попытке отправки."""
        return self._run('INSERT INTO letters(created, owner, kind, mail_from, mail_to, mail_cc, subject, text, '
                         'ticket_id, delivered) VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                         now(), owner or '', kind, mail_from, mail_to, mail_cc, subject, text, ticket_id)

    def letter(self, letter_id, owner=None):
        """Письмо по номеру; с владельцем — только своё: чужое и несуществующее одинаково «не найдено»."""
        row = self._one('SELECT * FROM letters WHERE id=?', letter_id)
        if row is None or owner is None:
            return row
        return row if row['owner'] == owner else None

    def letters(self, owner, address, limit=100):
        """Ящик роли: письма владельца, где его адрес стоит во from, to или cc; новые сверху."""
        return self._all(
            "SELECT * FROM letters WHERE owner=? AND (mail_from=? OR mail_to=? "
            "OR (',' || REPLACE(mail_cc, ' ', '') || ',') LIKE ?) ORDER BY id DESC LIMIT ?",
            owner or '', address, address, '%,' + address + ',%', limit)

    def set_delivered(self, letter_id, delivered):
        """Пометить доставку: NULL — SMTP не задан, 0 — не ушло, 1 — доставлено."""
        self._run('UPDATE letters SET delivered=? WHERE id=?', delivered, letter_id)

    def letter_delivered(self, ticket_id):
        """Общий статус писем запроса: None — SMTP не задан, False — хоть одно не доставлено, True — все доставлены."""
        statuses = [row['delivered'] for row in self._all('SELECT delivered FROM letters WHERE ticket_id=?', ticket_id)]
        if not statuses or all(s is None for s in statuses):
            return None
        return all(bool(s) for s in statuses)

    # ---- статистика ----

    def analytics(self, days=None, owner=None):
        """Маршруты, токены и вопросы без ответа — за всё время или за последние days дней.

        «Общение не по регламентам» (smalltalk) считается отдельно: это не рабочий вопрос, и он не должен
        ни занижать среднюю стоимость вопроса, ни попадать в предложения «что внести в документацию».
        owner — чьи вопросы считать (None — все): у каждого адреса своя картина, чужие вопросы не видны.
        """
        scope, base = '', []
        if owner is not None:                        # свои вопросы: сессии этого владельца
            scope, base = ' AND session_id IN (SELECT id FROM sessions WHERE owner=?)', [owner]
        live = 'route IS NOT NULL AND route != \'smalltalk\''
        where, args = ('WHERE %s%s AND ts>=?' % (live, scope), base + [since(days)]) if days \
            else ('WHERE %s%s' % (live, scope), base)
        # на что служба уже ответила этому владельцу — не предложение; чужие ответы он не видит
        answered = "AND id NOT IN (SELECT question_id FROM tickets WHERE status='answered'%s)" % (
            ' AND session_id IN (SELECT id FROM sessions WHERE owner=?)' if owner is not None else '')
        conn = self.connect()
        routes = {row['route']: row['n'] for row in conn.execute(
            'SELECT route, COUNT(*) n FROM questions %s GROUP BY route' % where, args)}
        tokens = conn.execute('SELECT COALESCE(SUM(tokens_in),0) a, COALESCE(SUM(tokens_out),0) b, '
                              'COUNT(*) n, COALESCE(AVG(tokens_in+tokens_out),0) avg, '
                              'COALESCE(AVG(seconds),0) sec FROM questions %s' % where, args).fetchone()
        smalltalk = conn.execute("SELECT COUNT(*) n FROM questions WHERE route='smalltalk'%s"
                                 % (scope + ' AND ts>=?' if days else scope), args).fetchone()['n']
        unanswered = [dict(row) for row in conn.execute(
            "SELECT question, COUNT(*) n FROM questions %s AND route IN ('not_found','unresolved') %s "
            'GROUP BY question ORDER BY n DESC, MAX(id) DESC' % (where, answered), args + ([owner] if owner is not None else []))]
        return {'questions': sum(routes.values()), 'routes': routes, 'smalltalk': smalltalk,
                'tokens_in': tokens['a'], 'tokens_out': tokens['b'],
                'avg_tokens': round(tokens['avg']), 'avg_seconds': round(tokens['sec'], 1),
                'tickets': {s: len(self.tickets(s, owner=owner)) for s in ('draft', 'open', 'answered')},
                'unanswered': unanswered}


def since(days):
    return (datetime.now(timezone.utc).astimezone() - timedelta(days=days)).isoformat(timespec='seconds')


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _load(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}
