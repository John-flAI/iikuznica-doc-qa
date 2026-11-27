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


def client_for(decide):
    """Стенд на поддельной модели с пустым хранилищем."""
    llm = FakeLLM(ok('{}'))
    llm.chat = lambda messages, max_tokens=600, temperature=0: decide(messages)
    service = Service(load_engine(DATA, with_vectors=False), llm, Store(Path(tempfile.mkdtemp()) / 'state.db'))
    return TestClient(create_app(service, with_vectors=False))


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
        self.assertIsNone(self.client.get('/api/health').json()['model_served'])
        self.client.post('/api/ask', json={'text': 'Сколько заявок может быть у одного ПА?'})
        self.assertEqual(self.client.get('/api/health').json()['model_served'], 'fake')

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
        answered = self.client.post('/api/tickets/%d/answer' % ticket,
                                    json={'answer': 'Командировка оформляется заявкой в ERP.', 'author': 'Иван'}).json()
        self.assertEqual((answered['status'], answered['answered_by']), ('answered', 'Иван'))
        self.assertEqual(self.client.post('/api/tickets/%d/answer' % ticket,
                                          json={'answer': ''}).status_code, 400)
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


if __name__ == '__main__':
    unittest.main()
