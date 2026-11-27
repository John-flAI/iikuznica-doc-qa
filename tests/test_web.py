# -*- coding: utf-8 -*-
"""Веб-стенд через HTTP: страницы, чат, служба качества, документы, статистика — на поддельной модели."""
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app.engine import load_engine
from app.service import Service
from app.store import Store
from app.web import create_app

DATA = Path(__file__).resolve().parents[1] / 'data'

try:
    from .test_agent import FakeLLM, ok
except ImportError:
    from test_agent import FakeLLM, ok


def client_for(decide, ask_limit=0):
    """Стенд на поддельной модели с пустым хранилищем; лимит вопросов по умолчанию выключен."""
    llm = FakeLLM(ok('{}'))
    llm.chat = lambda messages, max_tokens=600, temperature=0: decide(messages)
    service = Service(load_engine(DATA, with_vectors=False), llm, Store(Path(tempfile.mkdtemp()) / 'state.db'))
    return TestClient(create_app(service, with_vectors=False, ask_limit=ask_limit))


class Stand(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = client_for(lambda m: ok('{"action": "not_found"}'))

    def test_pages_and_robots(self):
        for path in ('/', '/user', '/qc', '/analytics'):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn('Disallow: /', self.client.get('/robots.txt').text)
        for asset in ('/static/style.css', '/static/app.js'):
            self.assertEqual(self.client.get(asset).status_code, 200)

    def test_health_names_models(self):
        health = self.client.get('/api/health').json()
        self.assertEqual(health['regulations'], 4)
        self.assertTrue(health['examples'])
        self.assertIn('model_served', health)           # модель из ответа API, а не только из настроек

    def test_health_shows_the_model_that_actually_answered(self):
        """Провайдер может обслужить запрос другой моделью: показываем имя из ответа, а не из конфигурации."""
        client = client_for(lambda m: ok('{"kind": "question"}'))
        self.assertIsNone(client.get('/api/health').json()['model_served'])
        client.post('/api/ask', json={'text': 'Сколько заявок может быть у одного ПА?'})
        self.assertEqual(client.get('/api/health').json()['model_served'], 'fake')

    def test_new_session_is_created_on_request(self):
        first = self.client.post('/api/session').json()['id']
        second = self.client.post('/api/session').json()['id']
        self.assertTrue(first and second and first != second)
        self.assertEqual(self.client.get('/api/session/' + first).json(), {'id': first, 'messages': []})

    def test_ask_without_session_creates_one(self):
        reply = self.client.post('/api/ask', json={'text': 'Как оформить отпуск за свой счёт?'}).json()
        self.assertEqual(reply['route'], 'not_found')
        self.assertTrue(reply['session_id'])
        self.assertTrue(reply['ticket']['letter'])
        history = self.client.get('/api/session/' + reply['session_id']).json()['messages']
        self.assertEqual([m['role'] for m in history], ['user', 'agent'])
        # клиент показывает ответ сразу и опрашивает сессию с его номера — повторов в чате не будет
        self.assertEqual(reply['id'], history[-1]['id'])
        self.assertEqual(self.client.get('/api/session/' + reply['session_id'],
                                         params={'after': reply['id']}).json()['messages'], [])

    def test_empty_question_is_rejected(self):
        self.assertEqual(self.client.post('/api/ask', json={'text': '   '}).status_code, 400)

    def test_letter_goes_to_quality_service_and_back(self):
        reply = self.client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}).json()
        ticket = reply['ticket']['id']
        self.assertEqual(self.client.post('/api/tickets/%d/send' % ticket).status_code, 200)
        self.assertEqual(self.client.post('/api/tickets/%d/send' % ticket).status_code, 409)   # повторно нельзя
        opened = self.client.get('/api/tickets?status=open').json()
        self.assertEqual([t['id'] for t in opened], [ticket])
        self.assertEqual(self.client.post('/api/tickets/%d/answer' % ticket,
                                          json={'answer': ''}).status_code, 400)   # пустой ответ — отклонён
        answered = self.client.post('/api/tickets/%d/answer' % ticket,
                                    json={'answer': 'Командировка оформляется заявкой в ERP.', 'author': 'Иван'}).json()
        self.assertEqual((answered['status'], answered['answered_by']), ('answered', 'Иван'))
        repeat = self.client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'Другой ответ.'})
        self.assertEqual(repeat.status_code, 409)          # повторный ответ закрыт отказом: он перезаписывал ответ
        self.assertIn('уже ответили', repeat.json()['detail'])
        history = self.client.get('/api/session/' + reply['session_id']).json()['messages']
        self.assertEqual([m['role'] for m in history][-2:], ['system', 'qc'])
        self.assertIn('Командировка', history[-1]['text'])
        self.assertEqual(self.client.get('/api/health').json()['knowledge'], 1)

    def test_document_is_served_and_unknown_is_404(self):
        document = self.client.get('/api/document/f3').json()
        self.assertEqual(document['title'], 'Пресейловая деятельность Дирекции ИБ')
        self.assertIn('5.1', [b['number'] for b in document['blocks']])
        self.assertEqual(self.client.get('/api/document/no-such').status_code, 404)

    def test_analytics_and_weekly(self):
        self.client.post('/api/ask', json={'text': 'Как оформить отпуск за свой счёт?'})
        report = self.client.get('/api/analytics').json()
        self.assertGreaterEqual(report['questions'], 1)
        self.assertGreaterEqual(report['routes']['not_found'], 1)
        self.assertIn('отпуск', self.client.get('/api/weekly').json()['text'])

    def test_limit_is_announced_in_health(self):
        self.assertEqual(self.client.get('/api/health').json()['ask_limit'], 0)


class AskLimit(unittest.TestCase):
    """Стенд демонстрационный: с одного адреса — не больше лимита вопросов в час."""

    def setUp(self):
        self.address = {'x-forwarded-for': '203.0.113.9'}
        self.question = {'text': 'Как оформить командировку сотруднику?'}

    def limited(self, limit=3):
        return client_for(lambda m: ok('{"kind": "question"}'), ask_limit=limit)

    def test_fourth_question_from_one_address_is_declined_with_retry_after(self):
        client = self.limited(3)
        for _ in range(3):
            self.assertEqual(client.post('/api/ask', json=self.question, headers=self.address).status_code, 200)
        fourth = client.post('/api/ask', json=self.question, headers=self.address)
        self.assertEqual(fourth.status_code, 429)
        self.assertIn('не более 3 вопросов в час с одного адреса', fourth.json()['detail'])
        self.assertIn('бережём бюджет модели', fourth.json()['detail'])
        self.assertGreater(int(fourth.headers['retry-after']), 0)
        self.assertEqual(client.get('/api/health').json()['ask_limit'], 3)

    def test_other_addresses_have_their_own_count(self):
        client = self.limited(3)
        for _ in range(4):
            client.post('/api/ask', json=self.question, headers=self.address)
        other = client.post('/api/ask', json=self.question)                  # другой адрес — без заголовка прокси
        self.assertEqual(other.status_code, 200)

    def test_client_cannot_pick_its_own_address(self):
        client = self.limited(3)
        for _ in range(3):
            client.post('/api/ask', json=self.question, headers=self.address)
        forged = {'x-forwarded-for': '9.9.9.9, 203.0.113.9'}                 # свой адрес клиент дописал впереди
        self.assertEqual(client.post('/api/ask', json=self.question, headers=forged).status_code, 429)

    def test_greeting_is_not_counted_as_a_question(self):
        client = self.limited(3)
        for _ in range(3):
            client.post('/api/ask', json=self.question, headers=self.address)
        declined = client.post('/api/ask', json={'text': 'привет'}, headers=self.address)
        self.assertEqual(declined.status_code, 429)                          # лимит — на запрос, а не на вызов модели

    def test_empty_question_does_not_spend_the_limit(self):
        client = self.limited(2)
        empty = client.post('/api/ask', json={'text': '   '}, headers=self.address)
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()['detail'], 'Вопрос пустой')
        self.assertEqual([client.post('/api/ask', json=self.question, headers=self.address).status_code
                          for _ in range(2)], [200, 200])       # оба вопроса прошли — пустой не потратил квоту
        self.assertEqual(client.post('/api/ask', json=self.question, headers=self.address).status_code, 429)

    def test_zero_disables_the_limit(self):
        client = self.limited(0)
        self.assertEqual([client.post('/api/ask', json=self.question, headers=self.address).status_code
                          for _ in range(25)], [200] * 25)


class FieldLimits(unittest.TestCase):
    """Поля запросов ограничены по длине: слишком длинные отклоняет валидация — до службы и модели."""

    def test_too_long_question_is_declined_before_the_model(self):
        calls = []

        def decide(messages):
            calls.append(1)
            return ok('{"kind": "question"}')
        client = client_for(decide)
        reply = client.post('/api/ask', json={'text': 'в' * 501})
        self.assertEqual(reply.status_code, 422)
        self.assertIn('не более 500 знаков', reply.text)      # понятный русский текст ошибки
        self.assertEqual(calls, [])                           # до модели вопрос не дошёл

    def test_question_of_exactly_500_characters_passes(self):
        client = client_for(lambda m: ok('{"kind": "question"}'))
        self.assertEqual(client.post('/api/ask', json={'text': 'в' * 500}).status_code, 200)

    def test_too_long_answer_and_author_are_declined(self):
        client = client_for(lambda m: ok('{"action": "not_found"}'))
        ticket = client.post('/api/ask', json={'text': 'Как оформить командировку сотрудника?'}).json()['ticket']['id']
        too_long = client.post('/api/tickets/%d/answer' % ticket, json={'answer': 'а' * 2001})
        self.assertEqual(too_long.status_code, 422)
        self.assertIn('не более 2000 знаков', too_long.text)
        too_long_name = client.post('/api/tickets/%d/answer' % ticket,
                                    json={'answer': 'Ответ.', 'author': 'и' * 101})
        self.assertEqual(too_long_name.status_code, 422)
        self.assertIn('не более 100 знаков', too_long_name.text)
        self.assertEqual(client.post('/api/tickets/%d/answer' % ticket,          # запрос не испорчен отказами
                                     json={'answer': 'Командировка оформляется заявкой в ERP.'}).status_code, 200)


class NoStaleClient(unittest.TestCase):
    """После обновления стенда браузер не должен смешать новую страницу со старым app.js: страницы и статика —
    без эвристического кэша, браузер каждый раз сверяет файл по ETag."""

    def test_pages_and_static_ask_the_browser_to_revalidate(self):
        client = TestClient(create_app(None, with_vectors=False))
        for path in ('/', '/user', '/qc', '/analytics', '/static/app.js', '/static/style.css'):
            self.assertEqual(client.get(path).headers.get('cache-control'), 'no-cache', path)
        script = client.get('/static/app.js')
        same = client.get('/static/app.js', headers={'if-none-match': script.headers['etag']})
        self.assertEqual((same.status_code, same.headers.get('cache-control')), (304, 'no-cache'))


class ModelSwitch(unittest.TestCase):
    """Переключатель моделей: каталог в health, выбор хранится в сессии, вопрос идёт агенту выбранной модели."""
    CATALOG = [{'id': 'default', 'label': 'Модель по умолчанию', 'model': 'm1'},
               {'id': 'second', 'label': 'Вторая модель', 'model': 'm2'}]

    def stand(self, with_catalog=True):
        calls = []

        def fake(model_id):
            llm = FakeLLM(ok(model_id))              # не-JSON → агент честно отвечает «нет ответа»
            original = llm.chat

            def chat(messages, max_tokens=600, temperature=0):
                calls.append(model_id)
                return original(messages, max_tokens, temperature)
            llm.chat = chat
            return llm
        engine = load_engine(DATA, with_vectors=False)
        store = Store(Path(tempfile.mkdtemp()) / 'state.db')
        service = (Service(engine, fake('default'), store, llms={m['id']: fake(m['id']) for m in self.CATALOG},
                           catalog=self.CATALOG) if with_catalog else Service(engine, fake('default'), store))
        return TestClient(create_app(service, with_vectors=False, ask_limit=0)), calls

    @staticmethod
    def ask(client, sid):
        return client.post('/api/ask', json={'session_id': sid, 'text': 'Как оформить командировку сотрудника?'})

    def test_health_lists_the_models_and_the_default(self):
        client, _ = self.stand()
        health = client.get('/api/health').json()
        self.assertEqual(([m['id'] for m in health['models']], health['model_default']), (['default', 'second'], 'default'))
        self.assertEqual(health['model'], 'm1')          # health называет модель каталога, а не запасного клиента

    def test_without_a_catalog_the_stand_has_a_single_model(self):
        client, _ = self.stand(with_catalog=False)
        health = client.get('/api/health').json()
        self.assertEqual((health['models'], health['model_default']), ([], None))

    def test_session_without_a_choice_uses_the_default_model(self):
        client, calls = self.stand()
        sid = client.post('/api/session', json={}).json()['id']
        self.assertEqual(self.ask(client, sid).status_code, 200)
        self.assertEqual(calls, ['default'])

    def test_model_chosen_when_the_session_is_created_answers(self):
        client, calls = self.stand()
        sid = client.post('/api/session', json={'model': 'second'}).json()['id']
        self.ask(client, sid)
        self.assertEqual(calls, ['second'])

    def test_switching_the_model_changes_who_answers_next(self):
        client, calls = self.stand()
        sid = client.post('/api/session', json={}).json()['id']
        self.ask(client, sid)
        switched = client.post('/api/session/%s/model' % sid, json={'model': 'second'})
        self.assertEqual((switched.status_code, switched.json()), (200, {'id': sid, 'model': 'second'}))
        self.ask(client, sid)
        self.assertEqual(calls, ['default', 'second'])

    def test_unknown_model_is_declined_and_never_breaks_the_chat(self):
        client, calls = self.stand()
        sid = client.post('/api/session', json={'model': 'no-such-model'}).json()['id']
        self.assertEqual(client.post('/api/session/%s/model' % sid, json={'model': 'no-such-model'}).status_code, 400)
        self.ask(client, sid)
        self.assertEqual(calls, ['default'])


if __name__ == '__main__':
    unittest.main()
