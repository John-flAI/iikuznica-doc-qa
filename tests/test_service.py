# -*- coding: utf-8 -*-
"""Сквозной сценарий на поддельной модели: чат, уточнение, служба качества, база знаний."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from app.engine import load_engine
from app.service import KNOWLEDGE, Service, kb_unit
from app.store import Store

try:                                    # python -m unittest tests.test_service
    from .test_agent import FakeLLM, ok
except ImportError:                     # python -m unittest discover -s tests
    from test_agent import FakeLLM, ok

DATA = Path(__file__).resolve().parents[1] / 'data'
OFF_TOPIC = 'Как оформить отпуск за свой счёт?'
EXPERT_ANSWER = ('Право подписи документов от лица компании имеет генеральный директор и сотрудники '
                 'по доверенности; реестр доверенностей ведёт канцелярия.')
TITLE_F2 = 'Организация проектной деятельности'


def first_citation(messages):
    return re.search(r'\[(f\d:[^\]\s]+)\]', messages[-1]['content']).group(1)


class ServiceFlow(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / 'state.db')
        self.engine = load_engine(DATA, with_vectors=False)

    def service(self, reply=None, decide=None):
        """Поддельная модель: либо фиксированный ответ, либо функция от показанных ей сообщений."""
        llm = FakeLLM(reply)
        if decide is not None:
            def chat(messages, max_tokens=600, temperature=0):
                system = messages[0]['content']
                if 'Вы пишете ответ сотруднику' in system:
                    return ok('{"text": "Ответ по регламенту."}')
                if 'Вы проверяете ответ агента' in system:
                    return ok('{"supported": true, "unsupported": []}')
                return decide(messages)
            llm.chat = chat
        return Service(self.engine, llm, self.store)

    def test_answer_closes_the_question_and_counts_tokens(self):
        service = self.service(decide=lambda m: ok(json.dumps({'action': 'answer', 'cites': [first_citation(m)]})))
        message = service.ask(None, 'Сколько заявок может быть у одного ПА?')
        self.assertEqual(message['route'], 'answer')
        self.assertTrue(all(s['quote'] for s in message['sources']))
        self.assertIsNone(self.store.open_question(self._last_session()))
        report = self.store.analytics()
        self.assertEqual((report['questions'], report['routes']['answer']), (1, 1))
        self.assertEqual(report['tokens_in'] + report['tokens_out'], message['tokens'])

    def _last_session(self):
        return self.store._all('SELECT id FROM sessions ORDER BY created DESC LIMIT 1')[0]['id']

    def test_clarification_then_answer_in_one_thread(self):
        script = [
            lambda m: ok(json.dumps({'action': 'clarify', 'clarify': {
                'question': 'В каком регламенте?', 'options': ['Регламент: «%s»' % TITLE_F2]}})),
            lambda m: ok(json.dumps({'action': 'answer', 'cites': [first_citation(m)], 'why': ''})),
        ]
        service = self.service(decide=lambda m: script.pop(0)(m))
        first = service.ask(None, 'Как часто надо обновлять реестр рисков?')
        self.assertEqual(first['route'], 'clarify')
        opened = self.store.open_question(self._last_session())
        self.assertEqual(opened['meta'], json.dumps({'awaiting': 'В каком регламенте?', 'options': [
            {'text': 'Регламент: «%s»' % TITLE_F2, 'doc': 'f2'}], 'clarifications': []}, ensure_ascii=False))
        second = service.ask(opened['session_id'], first['clarify']['options'][0]['text'])
        self.assertEqual(second['route'], 'answer')
        self.assertEqual(second['clarifications'], 1)
        self.assertIsNone(self.store.open_question(opened['session_id']))

    def test_no_answer_becomes_a_letter_then_an_expert_answer_joins_the_knowledge_base(self):
        service = self.service(decide=lambda m: ok('{"action": "not_found"}'))
        first = service.ask(None, 'Кто имеет право подписывать документы от лица компании?')
        self.assertEqual(first['route'], 'not_found')
        ticket_id = first['ticket']['id']
        self.assertIn('«Кто имеет право подписывать документы от лица компании?»', first['ticket']['letter'])
        self.assertEqual(self.store.ticket(ticket_id)['status'], 'draft')
        self.assertEqual([row['question'] for row in self.store.analytics()['unanswered']],
                         ['Кто имеет право подписывать документы от лица компании?'])

        service.send(ticket_id)
        self.assertEqual(self.store.ticket(ticket_id)['status'], 'open')
        service.answer(ticket_id, EXPERT_ANSWER, 'Иван')
        self.assertEqual(self.store.ticket(ticket_id)['status'], 'answered')
        self.assertEqual(self.store.analytics()['unanswered'], [])      # на вопрос уже ответили — не «что внести в документацию»
        address = kb_unit(self.store.ticket(ticket_id)).address.casefold()
        self.assertEqual((address.count('службы качества'), address.count('иван')), (1, 1))

        service.ask(None, OFF_TOPIC)                                     # автор по умолчанию — не двоится в адресе
        service.answer(self.store.tickets('draft')[0]['id'], EXPERT_ANSWER, 'Служба качества')
        self.assertEqual(kb_unit(self.store.tickets('answered')[0]).address.casefold().count('службы качества'), 1)

        session_id = self._last_session()
        delivered = self.store.messages(session_id)[-1]
        self.assertEqual((delivered['role'], delivered['text']), ('qc', EXPERT_ANSWER))
        self.assertEqual(json.loads(delivered['meta'])['sources'][0]['doc'], KNOWLEDGE)

        # повторный вопрос находит ответ службы качества — та же база, тот же поиск
        again = self.service(decide=lambda m: ok('{"action": "not_found"}'))
        hits = again.engine.search.search('Кто имеет право подписывать документы от лица компании?', k=5)
        self.assertIn('kb:%d' % ticket_id, [h.uid for h in hits])

    def test_expert_answer_outranks_the_regulation(self):
        service = self.service(decide=lambda m: ok('{"action": "not_found"}'))
        service.ask(None, 'Кто имеет право подписывать документы от лица компании?')
        service.answer(service.store.tickets()[0]['id'], EXPERT_ANSWER, 'Иван')

        def both(messages):
            uids = re.findall(r'\[([^\]]+)\]', messages[-1]['content'])
            return ok(json.dumps({'action': 'answer', 'cites': uids}))
        mixed = self.service(decide=both).ask(None, 'Кто имеет право подписывать документы от лица компании?')
        docs = {s['doc'] for s in mixed['sources']}
        self.assertIn(KNOWLEDGE, docs)
        self.assertTrue(mixed['priority'])
        self.assertEqual(mixed['conflict'], any(d != KNOWLEDGE for d in docs))

    def test_knowledge_base_survives_a_restart(self):
        service = self.service(decide=lambda m: ok('{"action": "not_found"}'))
        service.ask(None, 'Кто имеет право подписывать документы от лица компании?')
        service.answer(service.store.tickets()[0]['id'], EXPERT_ANSWER, 'Иван')
        restarted = Service(load_engine(DATA, with_vectors=False), FakeLLM(ok('{}')), self.store)
        self.assertIsNotNone(restarted.engine.corpus.get('kb:1'))
        self.assertEqual(restarted.engine.corpus.title(KNOWLEDGE), 'База знаний: ответы службы качества')

    def test_unavailable_model_degrades_and_keeps_the_thread_open(self):
        from app.llm import Reply
        service = self.service(decide=lambda m: Reply(error='HTTP 429'))
        message = service.ask(None, 'Как часто надо обновлять реестр рисков?')
        self.assertEqual((message['route'], message['retry'], message['notice']), ('degraded', True, 'HTTP 429'))
        self.assertIsNone(self.store.open_question(self._last_session()))

    def test_rejected_key_is_not_called_a_temporary_outage(self):
        from app.llm import Reply
        service = self.service(decide=lambda m: Reply(error='HTTP 401'))
        message = service.ask(None, 'Как часто надо обновлять реестр рисков?')
        self.assertEqual((message['route'], message['retry'], message['notice']), ('degraded', False, 'HTTP 401'))
        self.assertIn('ключ не задан или не принят', message['text'])    # без совета «повторите через минуту»

    def test_weekly_report_says_so_when_there_is_nothing_to_propose(self):
        service = self.service(decide=lambda m: ok('{"action": "not_found"}'))
        self.assertEqual(service.weekly()['text'], 'Предложений нет.')
        service.ask(None, 'Кто имеет право подписывать документы от лица компании?')
        report = service.weekly(days=7)
        self.assertEqual(report['proposals'][0]['n'], 1)
        self.assertIn('Кто имеет право', report['text'])

    def test_off_topic_question_costs_one_classifier_call_and_still_gets_a_letter(self):
        calls = []

        def decide(messages):
            calls.append(messages)
            return ok('{"kind": "question"}')                 # классификатор: это рабочий вопрос

        message = self.service(decide=decide).ask(None, OFF_TOPIC)
        self.assertEqual((message['route'], message['ticket']['id']), ('not_found', 1))
        self.assertEqual(len(calls), 1)                       # один короткий вызов — без фрагментов
        self.assertNotIn('Фрагменты регламентов', '\n'.join(m['content'] for m in calls[0]))

    def test_document_is_served_in_reading_order(self):
        service = self.service(reply=ok('{}'))
        document = service.document('f3')
        self.assertEqual(document['title'], 'Пресейловая деятельность Дирекции ИБ')
        self.assertEqual(document['blocks'][0]['kind'], 'section')
        self.assertEqual([b['uid'] for b in document['blocks'] if b['kind'] in ('clause', 'sub')][:1], ['f3:1.1'])
        with self.assertRaises(KeyError):
            service.document('no-such-regulation')


if __name__ == '__main__':
    unittest.main()
