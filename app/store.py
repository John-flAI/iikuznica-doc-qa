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
    id TEXT PRIMARY KEY, created TEXT, mode TEXT, email TEXT DEFAULT '', model TEXT DEFAULT '');
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
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            if 'model' not in {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}:
                conn.execute("ALTER TABLE sessions ADD COLUMN model TEXT DEFAULT ''")   # база переживает обновление

    def connect(self):
        conn = getattr(self.local, 'conn', None)
        if conn is None:
            conn = self.local.conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA journal_mode=WAL')
        return conn

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

    def session(self, session_id=None, mode='full', email='', model=None):
        found = self._one('SELECT * FROM sessions WHERE id=?', session_id) if session_id else None
        if found:
            return found
        session_id = session_id or uuid.uuid4().hex[:12]
        self._run('INSERT INTO sessions(id, created, mode, email, model) VALUES(?,?,?,?,?)',
                  session_id, now(), mode, email, model or '')
        return self._one('SELECT * FROM sessions WHERE id=?', session_id)

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

    def ticket(self, ticket_id):
        return self._one('SELECT * FROM tickets WHERE id=?', ticket_id)

    def tickets(self, status=None):
        if status:
            return self._all('SELECT * FROM tickets WHERE status=? ORDER BY id DESC', status)
        return self._all('SELECT * FROM tickets ORDER BY id DESC')

    def send_ticket(self, ticket_id):
        self._run("UPDATE tickets SET status='open' WHERE id=? AND status='draft'", ticket_id)
        return self.ticket(ticket_id)

    def answer_ticket(self, ticket_id, answer, author):
        self._run("UPDATE tickets SET status='answered', answer=?, answered_by=?, answered_at=? WHERE id=?",
                  answer, author, now(), ticket_id)
        return self.ticket(ticket_id)

    # ---- статистика ----

    def analytics(self, days=None):
        """Маршруты, токены и вопросы без ответа — за всё время или за последние days дней.

        «Общение не по регламентам» (smalltalk) считается отдельно: это не рабочий вопрос, и он не должен
        ни занижать среднюю стоимость вопроса, ни попадать в предложения «что внести в документацию».
        """
        live = 'route IS NOT NULL AND route != \'smalltalk\''
        where, args = ('WHERE %s AND ts>=?' % live, [since(days)]) if days else ('WHERE %s' % live, [])
        conn = self.connect()
        routes = {row['route']: row['n'] for row in conn.execute(
            'SELECT route, COUNT(*) n FROM questions %s GROUP BY route' % where, args)}
        tokens = conn.execute('SELECT COALESCE(SUM(tokens_in),0) a, COALESCE(SUM(tokens_out),0) b, '
                              'COUNT(*) n, COALESCE(AVG(tokens_in+tokens_out),0) avg, '
                              'COALESCE(AVG(seconds),0) sec FROM questions %s' % where, args).fetchone()
        smalltalk = conn.execute("SELECT COUNT(*) n FROM questions WHERE route='smalltalk'%s"
                                 % (' AND ts>=?' if days else ''), args).fetchone()['n']
        unanswered = [dict(row) for row in conn.execute(
            "SELECT question, COUNT(*) n FROM questions %s AND route IN ('not_found','unresolved') "
            "AND id NOT IN (SELECT question_id FROM tickets WHERE status='answered') "
            'GROUP BY question ORDER BY n DESC, MAX(id) DESC' % where, args)]      # на что служба уже ответила — не предложение
        return {'questions': sum(routes.values()), 'routes': routes, 'smalltalk': smalltalk,
                'tokens_in': tokens['a'], 'tokens_out': tokens['b'],
                'avg_tokens': round(tokens['avg']), 'avg_seconds': round(tokens['sec'], 1),
                'tickets': {s: len(self.tickets(s)) for s in ('draft', 'open', 'answered')},
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
