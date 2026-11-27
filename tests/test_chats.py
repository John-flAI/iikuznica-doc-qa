# -*- coding: utf-8 -*-
"""Несколько чатов на сервере: список сессий, имена, закрытие — и изоляция памяти между сессиями.

Два владельца через заголовок X-Forwarded-For — так же, как их различит reverse proxy на стенде.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.engine import load_engine
from app.service import Service
from app.store import Store
from app.web import create_app

try:                                    # python -m unittest tests.test_chats
    from .test_agent import FakeLLM, JUDGE_MARK, WRITER_MARK, ok
except ImportError:                     # python -m unittest discover -s tests
    from test_agent import FakeLLM, JUDGE_MARK, WRITER_MARK, ok

DATA = Path(__file__).resolve().parents[1] / 'data'
FIRST = {'x-forwarded-for': '203.0.113.1'}       # первый сотрудник: адрес = владелец
SECOND = {'x-forwarded-for': '203.0.113.2'}      # второй
QUESTION = 'Кто имеет право подписывать документы от лица компании?'
EXPERT = ('Право подписи документов от лица компании имеет генеральный директор и сотрудники '
          'по доверенности; реестр доверенностей ведёт канцелярия.')
CLARIFY = ok(json.dumps({'action': 'clarify', 'clarify': {
    'question': 'В каком регламенте?', 'options': ['Пресейл', 'Другое']}}))


def decide_all(decide):
    """Поддельная модель на всём цикле: решение задаёт decide, текст и вердикт — заготовки."""
    def chat(messages, max_tokens=600, temperature=0):
        system = messages[0]['content']
        if JUDGE_MARK in system:
            return ok('{"supported": true, "unsupported": []}')
        if WRITER_MARK in system:
            return ok('{"text": "Ответ по регламенту."}')
        return decide(messages)
    return chat


def stand(decide=None):
    """Стенд на поддельной модели: вместе со стойкой отдаём хранилище и модель — чтобы подглядывать."""
    llm = FakeLLM(ok('{}'))
    llm.chat = decide or (lambda messages: ok('{"action": "not_found"}'))
    store = Store(Path(tempfile.mkdtemp()) / 'state.db')
    service = Service(load_engine(DATA, with_vectors=False), llm, store)
    return TestClient(create_app(service, with_vectors=False, ask_limit=0)), store, llm


class ChatList(unittest.TestCase):
    """GET /api/sessions: свои открытые чаты, сначала недавние по активности, не больше 50."""

    def test_list_shows_only_own_open_sessions(self):
        client, _, _ = stand()
        mine = client.post('/api/session', json={}, headers=FIRST).json()['id']
        theirs = client.post('/api/session', json={}, headers=SECOND).json()['id']
        rows = client.get('/api/sessions', headers=FIRST).json()
        self.assertEqual([r['id'] for r in rows], [mine])
        self.assertEqual(set(rows[0]), {'id', 'name', 'created', 'last'})     # владельца в ответе нет
        self.assertEqual([r['id'] for r in client.get('/api/sessions', headers=SECOND).json()], [theirs])

    def test_list_is_ordered_by_recent_activity(self):
        client, store, _ = stand()
        store.session('old', owner='203.0.113.1')
        store.session('fresh', owner='203.0.113.1')
        store.session('active', owner='203.0.113.1')
        store._run("UPDATE sessions SET created=? WHERE id=?", '2026-01-01T10:00:00+03:00', 'old')
        store._run("UPDATE sessions SET created=? WHERE id=?", '2026-01-02T10:00:00+03:00', 'fresh')
        store._run("UPDATE sessions SET created=? WHERE id=?", '2026-01-01T09:00:00+03:00', 'active')
        store._run("INSERT INTO messages(session_id, ts, role, text) VALUES(?,?,?,?)",
                   'active', '2026-01-03T10:00:00+03:00', 'user', 'Вопрос')
        rows = client.get('/api/sessions', headers=FIRST).json()
        self.assertEqual([r['id'] for r in rows], ['active', 'fresh', 'old'])   # недавняя активность — сверху
        self.assertEqual([r['last'] for r in rows], ['2026-01-03T10:00:00+03:00',   # у активного — время сообщения
                                                     '2026-01-02T10:00:00+03:00',
                                                     '2026-01-01T10:00:00+03:00'])  # без сообщений — время создания

    def test_list_is_capped_at_fifty(self):
        client, _, _ = stand()
        for _ in range(55):
            client.post('/api/session', json={}, headers=FIRST)
        client.post('/api/session', json={}, headers=SECOND)                    # чужая в счёт не идёт
        self.assertEqual(len(client.get('/api/sessions', headers=FIRST).json()), 50)
        self.assertEqual(len(client.get('/api/sessions', headers=SECOND).json()), 1)


class ChatName(unittest.TestCase):
    """POST /api/session/{id}/name: имя до 100 знаков, пустое — по умолчанию."""

    def test_rename_trims_and_clears(self):
        client, _, _ = stand()
        sid = client.post('/api/session', json={}, headers=FIRST).json()['id']
        renamed = client.post('/api/session/%s/name' % sid, json={'name': '  Рабочие вопросы  '}, headers=FIRST)
        self.assertEqual((renamed.status_code, renamed.json()), (200, {'id': sid, 'name': 'Рабочие вопросы'}))
        self.assertEqual(client.get('/api/sessions', headers=FIRST).json()[0]['name'], 'Рабочие вопросы')
        empty = client.post('/api/session/%s/name' % sid, json={'name': '   '}, headers=FIRST)
        self.assertEqual((empty.status_code, empty.json()), (200, {'id': sid, 'name': ''}))   # пустое — по умолчанию

    def test_name_longer_than_100_is_400(self):
        client, _, _ = stand()
        sid = client.post('/api/session', json={}, headers=FIRST).json()['id']
        too_long = client.post('/api/session/%s/name' % sid, json={'name': 'и' * 101}, headers=FIRST)
        self.assertEqual(too_long.status_code, 400)
        self.assertIn('не более 100 знаков', too_long.json()['detail'])
        self.assertEqual(client.post('/api/session/%s/name' % sid, json={'name': 'и' * 100},
                                     headers=FIRST).status_code, 200)          # ровно 100 — можно

    def test_foreign_closed_or_missing_session_is_404(self):
        client, _, _ = stand()
        mine = client.post('/api/session', json={}, headers=FIRST).json()['id']
        theirs = client.post('/api/session', json={}, headers=SECOND).json()['id']
        self.assertEqual(client.post('/api/session/%s/name' % theirs, json={'name': 'Х'},
                                     headers=FIRST).status_code, 404)
        self.assertEqual(client.post('/api/session/no-such/name', json={'name': 'Х'},
                                     headers=FIRST).status_code, 404)
        client.delete('/api/session/%s' % mine, headers=FIRST)
        self.assertEqual(client.post('/api/session/%s/name' % mine, json={'name': 'Х'},
                                     headers=FIRST).status_code, 404)          # закрытая — как несуществующая


class ChatClose(unittest.TestCase):
    """DELETE /api/session/{id}: мягкое закрытие — закрытый чат ведёт себя как несуществующий, данные остаются."""

    def test_closed_session_behaves_as_nonexistent(self):
        client, _, _ = stand()
        sid = client.post('/api/session', json={}, headers=FIRST).json()['id']
        closed = client.delete('/api/session/%s' % sid, headers=FIRST)
        self.assertEqual((closed.status_code, closed.json()), (200, {'id': sid, 'closed': True}))
        self.assertEqual(client.get('/api/session/' + sid, headers=FIRST).status_code, 404)         # история
        self.assertEqual(client.post('/api/session/%s/model' % sid, json={'model': 'm1'},
                                     headers=FIRST).status_code, 404)                              # модель
        again = client.post('/api/ask', json={'session_id': sid, 'text': 'Как оформить командировку?'},
                            headers=FIRST).json()
        self.assertNotEqual(again['session_id'], sid)               # вопрос открыл новую чистую сессию
        history = client.get('/api/session/' + again['session_id'], headers=FIRST).json()['messages']
        self.assertEqual([m['role'] for m in history], ['user', 'agent'])    # чужих сообщений в ней нет
        self.assertNotIn(sid, [s['id'] for s in client.get('/api/sessions', headers=FIRST).json()])
        self.assertEqual(client.delete('/api/session/%s' % sid, headers=FIRST).status_code, 404)   # повторно

    def test_foreign_session_cannot_be_closed(self):
        client, _, _ = stand()
        mine = client.post('/api/session', json={}, headers=FIRST).json()['id']
        self.assertEqual(client.delete('/api/session/%s' % mine, headers=SECOND).status_code, 404)
        self.assertEqual(client.get('/api/session/' + mine, headers=FIRST).status_code, 200)   # не пострадала

    def test_closing_a_chat_keeps_tickets_analytics_and_knowledge(self):
        client, _, llm = stand(lambda m: ok('{"action": "not_found"}'))
        first = client.post('/api/ask', json={'text': QUESTION}, headers=FIRST).json()
        ticket = first['ticket']['id']
        client.post('/api/tickets/%d/send' % ticket, headers=FIRST)
        client.delete('/api/session/%s' % first['session_id'], headers=FIRST)     # закрыли чат с письмом
        self.assertEqual([t['id'] for t in client.get('/api/tickets', headers=FIRST).json()], [ticket])
        client.post('/api/tickets/%d/answer' % ticket, json={'answer': EXPERT}, headers=FIRST)
        self.assertEqual(client.get('/api/analytics', headers=FIRST).json()['questions'], 1)   # вопрос не потерян

        def cites_everything(messages):                          # модель цитирует всё, что ей показали
            return ok(json.dumps({'action': 'answer', 'cites': re.findall(r'\[([^\]]+)\]', messages[-1]['content'])}))
        llm.chat = decide_all(cites_everything)
        again = client.post('/api/ask', json={'text': QUESTION}, headers=FIRST).json()
        self.assertIn('kb', {s['doc'] for s in again['sources']})   # запись базы знаний из закрытого чата жива


class Isolation(unittest.TestCase):
    """У каждого чата своя память: ждущее уточнение одного чата не видно и не продолжается в другом."""

    def setUp(self):
        script = [CLARIFY]                                   # первый вопрос — уточнение, дальше — «нет ответа»

        def decide(messages):
            return script.pop(0) if script else ok('{"action": "not_found"}')
        self.client, self.store, _ = stand(decide)

    def test_clarification_of_one_chat_does_not_continue_in_another_of_the_same_owner(self):
        first = self.client.post('/api/ask', json={'session_id': 'a', 'text': 'Как часто надо обновлять реестр рисков?'},
                                 headers=FIRST).json()
        self.assertEqual(first['route'], 'clarify')
        # в чате B того же владельца текст-вариант не продолжает уточнение чата A — начинается новый вопрос
        second = self.client.post('/api/ask', json={'session_id': 'b', 'text': 'Пресейл'}, headers=FIRST).json()
        self.assertEqual((second['route'], second['clarifications']), ('not_found', 0))
        self.assertIsNotNone(self.store.open_question('a'))     # уточнение A по-прежнему ждёт своего ответа
        self.assertIsNone(self.store.open_question('b'))        # у B был свой отдельный вопрос
        self.assertEqual([m['text'] for m in self.store.messages('a') if m['role'] == 'user'],
                         ['Как часто надо обновлять реестр рисков?'])
        self.assertEqual([m['text'] for m in self.store.messages('b') if m['role'] == 'user'], ['Пресейл'])

    def test_clarification_of_one_owner_does_not_continue_for_another(self):
        first = self.client.post('/api/ask', json={'session_id': 'a', 'text': 'Как часто надо обновлять реестр рисков?'},
                                 headers=FIRST).json()
        self.assertEqual(first['route'], 'clarify')
        second = self.client.post('/api/ask', json={'session_id': 'a', 'text': 'Пресейл'}, headers=SECOND).json()
        self.assertNotEqual(second['session_id'], 'a')          # второму — своя чистая сессия
        self.assertEqual((second['route'], second['clarifications']), ('not_found', 0))
        self.assertIsNotNone(self.store.open_question('a'))     # чужое уточнение не тронуто


if __name__ == '__main__':
    unittest.main()
