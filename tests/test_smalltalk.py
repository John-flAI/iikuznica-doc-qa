# -*- coding: utf-8 -*-
"""«Поболтать» или «по регламентам»: приветствие не становится запросом в службу качества."""
import json
import tempfile
import unittest
from pathlib import Path

from app.agent import Agent
from app.engine import load_engine
from app.llm import Reply
from app.service import Service
from app.smalltalk import smalltalk_kind
from app.store import Store

try:                                    # python -m unittest tests.test_smalltalk
    from .test_agent import FakeLLM, ok
except ImportError:                     # python -m unittest discover -s tests
    from test_agent import FakeLLM, ok

DATA = Path(__file__).resolve().parents[1] / 'data'
STEADY = 'Я отвечаю только по корпоративным регламентам.'
CLASSIFY_MARK = 'не нашлось ничего подходящего'      # системный текст классификатора без фрагментов


def chatter(reply):
    """Поддельная модель, которая любое сообщение считает разговором не по делу."""
    def chat(messages, max_tokens=600, temperature=0):
        if CLASSIFY_MARK in messages[0]['content']:
            return ok(json.dumps({'kind': 'smalltalk', 'reply': reply}, ensure_ascii=False))
        return ok(json.dumps({'action': 'smalltalk', 'reply': reply}, ensure_ascii=False))
    llm = FakeLLM(ok('{}'))
    llm.chat = chat
    return llm


class Dictionary(unittest.TestCase):
    def test_greetings_thanks_and_about_are_recognised(self):
        for text, kind in (('привет', 'greeting'), ('Привет!', 'greeting'), ('Добрый день', 'greeting'),
                           ('привет, как дела?', 'greeting'), ('спасибо', 'thanks'), ('Большое спасибо!', 'thanks'),
                           ('спасибо за ответ', 'thanks'), ('до свидания', 'bye'), ('Кто ты?', 'about'),
                           ('что ты умеешь', 'about')):
            self.assertEqual(smalltalk_kind(text), kind, text)

    def test_question_is_never_smalltalk(self):
        for text in ('привет, как оформить отпуск', 'добрый день оформить заявку', 'Как оформить отпуск?',
                     'Кто готовит ТКП?', 'рабочий день', ''):
            self.assertIsNone(smalltalk_kind(text), text)


class WithoutModel(unittest.TestCase):
    """Словарные приветствия и благодарности обслуживаются кодом."""

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / 'state.db')
        self.engine = load_engine(DATA, with_vectors=False)

    def test_greeting_is_answered_without_model_and_without_ticket(self):
        llm = FakeLLM(ok('{}'))
        message = Service(self.engine, llm, self.store).ask(None, 'привет')
        self.assertEqual((message['route'], message['tokens']), ('smalltalk', 0))
        self.assertEqual(llm.calls, 0)
        self.assertIn('Здравствуйте', message['text'])
        self.assertIn('Задайте вопрос', message['text'])
        self.assertNotIn('ticket', message)
        self.assertEqual(self.store.tickets(), [])
        self.assertIsNone(self.store.served_model())

    def test_thanks_after_an_answer(self):
        service = Service(self.engine, FakeLLM(ok('{"kind": "question"}')), self.store)
        sid = service.ask(None, 'Как оформить командировку сотруднику?')['session_id']
        self.assertEqual(service.ask(sid, 'Спасибо!')['route'], 'smalltalk')
        self.assertEqual(len(self.store.tickets()), 1)       # письмо одно — по рабочему вопросу

    def test_chat_is_returned_to_the_topic(self):
        service = Service(self.engine, FakeLLM(ok('{}')), self.store)
        sid = service.ask(None, 'привет')['session_id']
        service.ask(sid, 'как дела')
        third = service.ask(sid, 'спасибо')                    # третий раз подряд не по регламентам
        self.assertTrue(third['text'].startswith(STEADY))
        working = service.ask(sid, 'Кто готовит ТКП?')         # рабочий вопрос после общения — как обычно
        self.assertNotEqual(working['route'], 'smalltalk')

    def test_greeting_does_not_close_an_open_clarification(self):
        def clarify(messages, max_tokens=600, temperature=0):
            return ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'В каком вы работаете?',
                                                                   'options': ['Организация проектной деятельности']}},
                                 ensure_ascii=False))
        service = Service(self.engine, FakeLLM(ok('{}')), self.store)
        service.agent.llm.chat = clarify
        sid = service.ask(None, 'Как часто надо обновлять реестр рисков?')['session_id']
        self.assertIsNotNone(self.store.open_question(sid))
        service.ask(sid, 'привет')
        self.assertIsNotNone(self.store.open_question(sid))   # уточнение ждёт ответа — кнопки остались в силе

    def test_smalltalk_is_counted_apart_from_working_questions(self):
        service = Service(self.engine, FakeLLM(ok('{"kind": "question"}')), self.store)
        sid = service.ask(None, 'Как оформить командировку сотруднику?')['session_id']
        cost = self.store.analytics()['avg_tokens']
        service.ask(sid, 'спасибо')
        report = self.store.analytics()
        self.assertEqual((report['questions'], report['smalltalk']), (1, 1))
        self.assertEqual([row['question'] for row in report['unanswered']], ['Как оформить командировку сотруднику?'])
        self.assertEqual(report['avg_tokens'], cost)           # общение не занижает стоимость рабочего вопроса


class ModelDecides(unittest.TestCase):
    """Сообщения, которых нет в словаре, разбирает модель."""

    def setUp(self):
        self.engine = load_engine(DATA, with_vectors=False)

    def agent(self, reply, strongest):
        """Агент с поддельной моделью: близость найденного задаём сами, фрагменты модели не показываем."""
        agent = Agent(self.engine, chatter(reply))
        agent.candidates = lambda query, scope=None, kb=None: {'f1:1': (None, strongest, strongest)}
        agent.messages = lambda question, history, candidates: [{'role': 'user', 'content': 'фрагменты'}]
        agent.strongest = lambda found: strongest
        return agent

    def test_nothing_found_and_model_sees_small_talk(self):
        agent = Agent(self.engine, chatter('Здравствуйте! Отвечаю только по регламентам.'))
        agent.candidates = lambda query, scope=None, kb=None: {}
        decision = agent.ask('Расскажи анекдот')
        self.assertEqual((decision.route, decision.text), ('smalltalk', 'Здравствуйте! Отвечаю только по регламентам.'))

    def test_weak_fragments_and_model_sees_small_talk(self):
        decision = self.agent('Можем поговорить о регламентах?', 0.5).ask('Расскажи анекдот')
        self.assertEqual((decision.route, decision.text), ('smalltalk', 'Можем поговорить о регламентах?'))

    def test_close_fragments_mean_a_working_question_even_if_model_wants_to_chat(self):
        decision = self.agent('Поболтаем?', 0.9).ask('Сколько заявок может быть у одного ПА?')
        self.assertEqual(decision.route, 'not_found')         # письмо в службу качества не теряется

    def test_unavailable_model_without_fragments_keeps_the_letter(self):
        agent = Agent(self.engine, FakeLLM(Reply(error='HTTP 429')))
        agent.candidates = lambda query, scope=None, kb=None: {}
        self.assertEqual(agent.ask('Расскажи анекдот').route, 'not_found')

    def test_empty_reply_is_replaced_with_a_steady_answer(self):
        decision = self.agent('', 0.0).ask('Расскажи анекдот')
        self.assertEqual((decision.route, decision.text), ('smalltalk', STEADY))

    def test_prompt_names_regulation_as_the_authority(self):
        from app.agent import SYSTEM, WRITER_SYSTEM
        self.assertIn('Регламент — утверждённый документ, он главнее', SYSTEM)
        self.assertIn('он главнее', WRITER_SYSTEM)
        self.assertNotIn('приоритетнее регламента', SYSTEM + WRITER_SYSTEM)


if __name__ == '__main__':
    unittest.main()
