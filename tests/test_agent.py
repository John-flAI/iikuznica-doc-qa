# -*- coding: utf-8 -*-
"""Логика агента на поддельной модели: что проходит в ответ, а что отсекается кодом."""
import json
import unittest
from pathlib import Path

from app.agent import GIVE_UP_CHECK, Agent
from app.engine import load_engine
from app.llm import Reply

DATA = Path(__file__).resolve().parents[1] / 'data'
RISKS = 'Как часто надо обновлять реестр рисков?'


def ok(text):
    return Reply(text=text, ok=True, model='fake', prompt_tokens=1000, completion_tokens=50)


class FakeLLM:
    """Отдаёт заготовленный ответ и запоминает, что ей показали."""

    def __init__(self, reply):
        self.reply, self.calls, self.shown = reply, 0, None

    def chat(self, messages, max_tokens=600, temperature=0):
        self.calls += 1
        self.shown = '\n'.join(m['content'] for m in messages)
        return self.reply


class AgentLogic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine(DATA, with_vectors=False)      # без сети: судим по словесному покрытию

    def agent(self, reply, **kwargs):
        return Agent(self.engine, FakeLLM(reply), **kwargs)

    def one_per_document(self, question):
        groups = self.engine.search.per_document(question, k_each=3)
        return {doc: hits[0].uid for doc, hits in groups.items() if hits}

    def test_answer_keeps_only_known_citations_in_order(self):
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        agent = self.agent(ok(json.dumps({'action': 'answer', 'cites': ['нет-такого', uid, uid], 'why': ''})))
        decision = agent.ask('Сколько заявок может быть у одного ПА?')
        self.assertEqual([c[0].uid for c in decision.cites], [uid])
        self.assertEqual(decision.route, 'answer')
        self.assertEqual(decision.tokens, 1050)

    def test_citation_that_does_not_resemble_the_question_is_rejected(self):
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        agent = self.agent(ok(json.dumps({'action': 'answer', 'cites': [uid]})), min_similarity=1.01)
        self.assertEqual(agent.ask('Сколько заявок может быть у одного ПА?').route, 'not_found')

    def test_answer_from_two_regulations_becomes_a_question(self):
        uids = self.one_per_document(RISKS)
        self.assertGreaterEqual(len(uids), 2)
        agent = self.agent(ok(json.dumps({'action': 'answer', 'cites': list(uids.values())})))
        decision = agent.ask(RISKS)
        self.assertEqual(decision.route, 'clarify')
        self.assertGreaterEqual(len(decision.option_docs), 2)           # не менее двух регламентов
        self.assertTrue(set(decision.option_docs.values()) <= set(uids))
        for option in decision.options:
            self.assertIn(option, decision.option_docs)

    def test_named_regulation_narrows_the_search(self):
        candidates = self.agent(ok('{}')).candidates(RISKS, scope='f2')
        self.assertTrue(candidates)
        self.assertTrue(all(uid.startswith('f2:') for uid in candidates), list(candidates))

    def test_after_the_regulation_is_named_two_regulations_no_longer_block_the_answer(self):
        uids = self.one_per_document(RISKS)
        agent = self.agent(ok(json.dumps({'action': 'answer', 'cites': list(uids.values())})), min_similarity=0)
        decision = agent.ask(RISKS, [('В каком регламенте?', 'Организация проектной деятельности', 'f2')])
        self.assertEqual(decision.route, 'answer')
        self.assertTrue(decision.cites)
        self.assertTrue(all(c[0].doc == 'f2' for c in decision.cites))

    def test_clarification_options_naming_a_regulation_are_bound_to_it(self):
        title = self.engine.corpus.title('f4')
        agent = self.agent(ok(json.dumps({'action': 'clarify', 'clarify': {
            'question': 'В каком подразделении?', 'options': ['Регламент: «%s»' % title, 'Другое']}})))
        decision = agent.ask('За какое время назначают РП?')
        self.assertEqual(decision.route, 'clarify')
        self.assertEqual(decision.option_docs, {'Регламент: «%s»' % title: 'f4'})

    def test_explanation_with_a_foreign_number_is_not_shown(self):
        agent = self.agent(ok('{}'))
        unit = self.engine.corpus.get('f3:5.1')               # «…в течение 1 рабочего дня…»
        shown = (unit, 1.0, 1.0)
        self.assertEqual(agent.safe_why('По пункту 5.1 срок — 1 рабочий день', [shown]),
                         'По пункту 5.1 срок — 1 рабочий день')
        self.assertEqual(agent.safe_why('Срок — 5 рабочих дней', [shown]), '')
        self.assertEqual(agent.safe_why('Пункт задаёт срок приёмки заявки', [shown]), 'Пункт задаёт срок приёмки заявки')

    def test_unavailable_model_degrades_to_raw_fragments(self):
        agent = self.agent(Reply(ok=False, error='HTTP 429'))
        decision = agent.ask(RISKS)
        self.assertEqual(decision.route, 'degraded')
        self.assertEqual(decision.error, 'HTTP 429')
        self.assertGreaterEqual(len({c[0].doc for c in decision.cites}), 1)

    def test_unparseable_answer_is_not_found(self):
        for text in ('не могу ответить', '{"action": "answer", "cites": [', '[1, 2, 3]'):
            with self.subTest(text=text):
                self.assertEqual(self.agent(ok(text)).ask(RISKS).route, 'not_found')

    def test_too_many_clarifications_end_honestly(self):
        agent = self.agent(ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'Где?', 'options': ['А', 'Б']}})))
        decision = agent.ask(RISKS, [('Где?', 'А'), ('Когда?', 'Б'), ('Кто?', 'В')])
        self.assertEqual(decision.route, 'unresolved')
        self.assertEqual(agent.llm.calls, 0)                  # лимит уточнений — модель уже не зовём

    def test_off_topic_question_never_reaches_the_model(self):
        agent = self.agent(ok(json.dumps({'action': 'answer', 'cites': ['f3:5.1']})))
        decision = agent.ask('Как оформить отпуск за свой счёт?')
        self.assertEqual(decision.route, 'not_found')
        self.assertEqual(agent.llm.calls, 0)

    def test_model_sees_only_verbatim_fragments_with_addresses(self):
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        agent = self.agent(ok('{}'))
        agent.ask('Сколько заявок может быть у одного ПА?')
        unit = self.engine.corpus.get(uid)
        self.assertIn('[%s] Регламент: «Пресейловая деятельность Дирекции ИБ». Адрес: %s' % (uid, unit.address),
                      agent.llm.shown)
        self.assertIn(unit.quote, agent.llm.shown)

    def test_giving_up_on_similar_fragments_gets_a_second_opinion(self):
        """Модель сказала «нет ответа», а фрагменты близки к вопросу — один раз напоминаем про уточнение."""
        script = [ok('{"action": "not_found"}'),
                  ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'Какая ситуация?', 'options': ['А', 'Б']}}))]
        llm = FakeLLM(ok('{}'))
        shown = []

        def chat(messages, max_tokens=600, temperature=0):
            shown.append(messages)
            return script.pop(0)
        llm.chat = chat
        decision = Agent(self.engine, llm).ask('Кто готовит ТКП, если заказчик уже выбрал продукт?')
        self.assertEqual((decision.route, len(decision.replies)), ('clarify', 2))
        self.assertEqual(shown[1][-1]['content'], GIVE_UP_CHECK)

    def test_giving_up_on_distant_fragments_is_final(self):
        agent = self.agent(ok('{"action": "not_found"}'))
        unit = self.engine.corpus.get('f3:5.1')
        agent.candidates = lambda query, scope=None: {unit.uid: (unit, 0.5, 0.5)}   # похоже, но не сильно
        self.assertEqual((agent.ask(RISKS).route, agent.llm.calls), ('not_found', 1))


if __name__ == '__main__':
    unittest.main()
