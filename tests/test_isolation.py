# -*- coding: utf-8 -*-
"""Изоляция по адресу: владелец — адрес сотрудника, чужое не видно и не меняется.

Два владельца через заголовок X-Forwarded-For — так же, как их различит reverse proxy на стенде.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent import Agent
from app.engine import load_engine
from app.service import Service
from app.store import Store
from app.web import create_app

try:                                    # python -m unittest tests.test_isolation
    from .test_agent import FakeLLM, JUDGE_MARK, WRITER_MARK, ok
except ImportError:                     # python -m unittest discover -s tests
    from test_agent import FakeLLM, JUDGE_MARK, WRITER_MARK, ok

DATA = Path(__file__).resolve().parents[1] / 'data'
FIRST = {'x-forwarded-for': '203.0.113.1'}       # первый сотрудник: адрес = владелец
SECOND = {'x-forwarded-for': '203.0.113.2'}      # второй
QUESTION = 'Кто имеет право подписывать документы от лица компании?'
EXPERT = ('Право подписи документов от лица компании имеет генеральный директор и сотрудники '
          'по доверенности; реестр доверенностей ведёт канцелярия.')
CATALOG = [{'id': 'default', 'label': 'Модель по умолчанию', 'model': 'm1'}]


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


def stand(decide, catalog=None):
    """Стенд на поддельной модели: вместе со стойкой отдаём хранилище и модель — чтобы подглядывать."""
    llm = FakeLLM(ok('{}'))
    llm.chat = decide_all(decide)
    store = Store(Path(tempfile.mkdtemp()) / 'state.db')
    engine = load_engine(DATA, with_vectors=False)
    service = (Service(engine, llm, store, llms={'default': FakeLLM(ok('{}'))}, catalog=catalog)
               if catalog else Service(engine, llm, store))
    return TestClient(create_app(service, with_vectors=False, ask_limit=0)), store, llm


class Sessions(unittest.TestCase):
    """Сессии принадлежат адресу: чужая история не существует, чужой идентификатор начинает чистую сессию."""

    def test_history_of_another_owner_is_404(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        mine = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}, headers=FIRST).json()
        client.post('/api/ask', json={'text': 'Как оформить отпуск за свой счёт?'}, headers=SECOND)
        self.assertEqual(client.get('/api/session/' + mine['session_id'], headers=FIRST).status_code, 200)
        self.assertEqual(client.get('/api/session/' + mine['session_id'], headers=SECOND).status_code, 404)
        self.assertEqual(client.get('/api/session/no-such-session', headers=FIRST).status_code, 404)

    def test_question_with_a_foreign_session_gets_its_own_clean_session(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        first = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}, headers=FIRST).json()
        foreign = client.post('/api/ask', json={'session_id': first['session_id'],
                                                'text': 'Как оформить отпуск за свой счёт?'}, headers=SECOND).json()
        self.assertNotEqual(foreign['session_id'], first['session_id'])        # второму выдана новая сессия
        own = client.get('/api/session/' + first['session_id'], headers=FIRST).json()['messages']
        self.assertEqual([m['role'] for m in own], ['user', 'agent'])          # история первой не изменилась
        other = client.get('/api/session/' + foreign['session_id'], headers=SECOND).json()['messages']
        self.assertEqual([m['role'] for m in other], ['user', 'agent'])        # чужих сообщений в новой нет

    def test_model_of_a_foreign_session_is_not_switched(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'), catalog=CATALOG)
        mine = client.post('/api/ask', json={'text': 'Как оформить командировку?'}, headers=FIRST).json()['session_id']
        foreign = client.post('/api/ask', json={'text': 'Как оформить отпуск?'}, headers=SECOND).json()['session_id']
        self.assertEqual(client.post('/api/session/%s/model' % mine, json={'model': 'default'},
                                     headers=SECOND).status_code, 404)         # чужую сессию не трогаем
        self.assertEqual(client.post('/api/session/%s/model' % foreign, json={'model': 'default'},
                                     headers=SECOND).status_code, 200)         # свою — можно
        self.assertEqual(client.post('/api/session/%s/model' % mine, json={'model': 'no-such'},
                                     headers=FIRST).status_code, 400)          # неизвестная модель — как раньше


class LegacySessions(unittest.TestCase):
    """Старая сессия без владельца (запись до изоляции) достаётся первому обратившемуся."""

    def test_ask_claims_the_ownerless_session(self):
        client, store, _ = stand(lambda m: ok('{"action": "not_found"}'))
        store.session('old-session')                     # как в старой базе: владелец пустой
        first = client.post('/api/ask', json={'session_id': 'old-session', 'text': 'Как оформить командировку?'},
                            headers=FIRST).json()
        self.assertEqual(first['session_id'], 'old-session')     # присвоена первому обратившемуся
        second = client.post('/api/ask', json={'session_id': 'old-session', 'text': 'Как оформить отпуск?'},
                             headers=SECOND).json()
        self.assertNotEqual(second['session_id'], 'old-session') # второму — чистая новая сессия
        history = client.get('/api/session/old-session', headers=FIRST).json()['messages']
        self.assertEqual([m['role'] for m in history], ['user', 'agent'])   # чужой вопрос в историю не попал

    def test_history_claims_the_ownerless_session(self):
        client, store, _ = stand(lambda m: ok('{"action": "not_found"}'))
        store.session('old-session')
        self.assertEqual(client.get('/api/session/old-session', headers=FIRST).status_code, 200)
        self.assertEqual(client.get('/api/session/old-session', headers=SECOND).status_code, 404)


class Tickets(unittest.TestCase):
    """Служба качества: в очереди только свои запросы, чужие нельзя отправить и нельзя ответить."""

    def test_ticket_list_shows_only_your_own(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        mine = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}, headers=FIRST).json()
        theirs = client.post('/api/ask', json={'text': 'Как оформить отпуск за свой счёт?'}, headers=SECOND).json()
        # черновики в общем списке не показываются — смотрим очередь черновиков каждого
        self.assertEqual([t['id'] for t in client.get('/api/tickets?status=draft', headers=FIRST).json()],
                         [mine['ticket']['id']])
        self.assertEqual([t['id'] for t in client.get('/api/tickets?status=draft', headers=SECOND).json()],
                         [theirs['ticket']['id']])
        client.post('/api/tickets/%d/send' % mine['ticket']['id'], headers=FIRST)
        self.assertEqual([t['id'] for t in client.get('/api/tickets', headers=FIRST).json()], [mine['ticket']['id']])
        self.assertEqual(client.get('/api/tickets', headers=SECOND).json(), [])   # чужое отправленное ему не видно

    def test_foreign_ticket_cannot_be_sent_or_answered(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        ticket = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'},
                             headers=FIRST).json()['ticket']['id']
        self.assertEqual(client.post('/api/tickets/%d/send' % ticket, headers=SECOND).status_code, 404)
        self.assertEqual(client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'Ответ.'},
                                     headers=SECOND).status_code, 404)
        self.assertEqual(client.post('/api/tickets/%d/send' % ticket, headers=FIRST).status_code, 200)
        self.assertEqual(client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'Ответ службы.'},
                                     headers=FIRST).status_code, 200)

    def test_answered_ticket_refuses_a_second_answer(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        ticket = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'},
                             headers=FIRST).json()['ticket']['id']
        client.post('/api/tickets/%d/send' % ticket, headers=FIRST)
        client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'Ответ службы.'}, headers=FIRST)
        second = client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'Другой ответ.'}, headers=FIRST)
        self.assertEqual(second.status_code, 409)                # повторный ответ закрыт отказом, а не перезаписью
        self.assertIn('уже ответили', second.json()['detail'])
        self.assertEqual(client.get('/api/tickets?status=answered', headers=FIRST).json()[0]['answer'],
                         'Ответ службы.')                        # первый ответ не испорчен


class Analytics(unittest.TestCase):
    """Статистика каждого — его собственные вопросы, чужих в ней нет."""

    def test_analytics_and_weekly_count_only_own_questions(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}, headers=FIRST)
        client.post('/api/ask', json={'text': 'Как оформить отпуск за свой счёт?'}, headers=SECOND)
        report = client.get('/api/analytics', headers=FIRST).json()
        self.assertEqual((report['questions'], report['routes']), (1, {'not_found': 1}))
        self.assertEqual([row['question'] for row in client.get('/api/weekly', headers=SECOND).json()['proposals']],
                         ['Как оформить отпуск за свой счёт?'])


class Shared(unittest.TestCase):
    """Здоровье, регламенты и страницы — общие: владелец им не нужен."""

    def test_health_and_documents_are_common(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        self.assertEqual([client.get('/api/health', headers=h).status_code for h in (FIRST, SECOND)], [200, 200])
        first = client.get('/api/document/f3', headers=FIRST)
        second = client.get('/api/document/f3', headers=SECOND)
        self.assertEqual((first.status_code, first.text), (second.status_code, second.text))


class Knowledge(unittest.TestCase):
    """База знаний: ответ службы качества виден только владельцу запроса."""

    def test_knowledge_answer_is_served_only_to_its_owner(self):
        client, _, llm = stand(lambda m: ok('{"action": "not_found"}'))
        first = client.post('/api/ask', json={'text': QUESTION}, headers=FIRST).json()
        ticket = first['ticket']['id']
        client.post('/api/tickets/%d/send' % ticket, headers=FIRST)
        client.post('/api/tickets/%d/answer' % ticket, json={'answer': EXPERT}, headers=FIRST)

        shown = []

        def cites_everything(messages):                  # модель цитирует всё, что ей показали
            shown.append(messages[-1]['content'])
            return ok(json.dumps({'action': 'answer', 'cites': re.findall(r'\[([^\]]+)\]', messages[-1]['content'])}))
        llm.chat = decide_all(cites_everything)

        own = client.post('/api/ask', json={'text': QUESTION}, headers=FIRST).json()
        self.assertIn('kb', {s['doc'] for s in own['sources']})            # свой ответ нашёлся и попал в ответ
        other = client.post('/api/ask', json={'text': QUESTION}, headers=SECOND).json()
        self.assertNotIn('kb', {s['doc'] for s in other['sources']})       # чужому — только регламенты
        self.assertNotIn('[kb:%d]' % ticket, shown[-1])                    # чужая запись модели даже не показана

    def test_knowledge_document_and_counter_belong_to_the_owner(self):
        client, _, _ = stand(lambda m: ok('{"action": "not_found"}'))
        ticket = client.post('/api/ask', json={'text': QUESTION}, headers=FIRST).json()['ticket']['id']
        client.post('/api/tickets/%d/send' % ticket, headers=FIRST)
        client.post('/api/tickets/%d/answer' % ticket, json={'answer': EXPERT}, headers=FIRST)

        own = client.get('/api/document/kb', headers=FIRST).json()['blocks']
        self.assertEqual([block['uid'] for block in own], ['kb:%d' % ticket])
        self.assertEqual((client.get('/api/document/kb', headers=SECOND).json()['blocks'],
                          client.get('/api/document/kb').json()['blocks']), ([], []))    # чужой и без адреса — пусто
        self.assertEqual([client.get('/api/health', headers=h).json()['knowledge'] for h in (FIRST, SECOND)], [1, 0])


class KnowledgeUnits(unittest.TestCase):
    """База знаний на уровне агента: чужая запись отбрасывается до отбора кандидатов и не занимает место."""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / 'state.db')
        self.engine = load_engine(DATA, with_vectors=False)
        self.service = Service(self.engine, FakeLLM(ok('{}')), self.store)

    def ticket(self, owner, answer):
        """Ответ службы качества на запрос владельца — и сразу запись в индексе базы знаний."""
        session = self.store.session(owner=owner)['id']
        tid = self.store.create_ticket(session, None, QUESTION, '')
        self.store.answer_ticket(tid, answer, 'Иван')
        self.service.learn(self.store.ticket(tid))
        return 'kb:%d' % tid

    def agent(self, decide):
        llm = FakeLLM(ok('{}'))
        llm.chat = decide_all(decide)
        return Agent(self.engine, llm)

    def test_model_sees_only_allowed_kb_units(self):
        own, foreign = self.ticket('203.0.113.1', EXPERT), self.ticket('203.0.113.2', EXPERT)
        shown = []

        def cites(messages):
            shown.append(messages[-1]['content'])
            return ok(json.dumps({'action': 'answer', 'cites': re.findall(r'\[([^\]]+)\]', messages[-1]['content'])}))

        decision = self.agent(cites).ask(QUESTION, kb={own})
        self.assertIn(own, [c[0].uid for c in decision.cites])
        self.assertNotIn(foreign, [c[0].uid for c in decision.cites])
        self.assertIn('[%s]' % own, shown[0])            # своя запись — во фрагментах
        self.assertNotIn('[%s]' % foreign, shown[0])     # чужая — отсеяна ещё до показа модели
        self.agent(cites).ask(QUESTION)                  # kb=None (владелец не важен) — обе записи на месте
        self.assertIn('[%s]' % foreign, shown[-1])

    def test_foreign_kb_units_do_not_take_the_per_document_slot(self):
        own = self.ticket('203.0.113.1', EXPERT)
        for i in range(3):                               # три чужих записи — ровно лимит на документ
            self.ticket('203.0.113.%d' % (i + 2),
                        '%s ТКП готовит дирекция, реестр рисков обновляет РП раз в месяц.' % EXPERT)
        found = Agent(self.engine, FakeLLM(ok('{}'))).candidates(QUESTION, kb={own})
        self.assertIn(own, found)                        # своя проходит: чужие отсеяны до обрезки по k

    def test_degraded_and_unresolved_also_keep_only_own_units(self):
        own, foreign = self.ticket('203.0.113.1', EXPERT), self.ticket('203.0.113.2', EXPERT)
        from app.llm import Reply
        degraded = self.agent(lambda m: Reply(error='HTTP 429')).ask(QUESTION, kb={own})
        self.assertIn(own, [c[0].uid for c in degraded.cites])           # запасной показ — тоже только своя
        self.assertNotIn(foreign, [c[0].uid for c in degraded.cites])
        clarify = ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'Где?', 'options': ['А', 'Б']}}))
        agent = self.agent(clarify)
        agent.max_clarifications = 0                                     # сразу «неоднозначно» — best_of_each
        unresolved = agent.ask(QUESTION, kb={own})
        self.assertIn(own, [c[0].uid for c in unresolved.cites])
        self.assertNotIn(foreign, [c[0].uid for c in unresolved.cites])


if __name__ == '__main__':
    unittest.main()
