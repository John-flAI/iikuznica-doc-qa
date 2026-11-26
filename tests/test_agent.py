# -*- coding: utf-8 -*-
"""Логика агента на поддельной модели: что проходит в ответ, а что отсекается кодом и судьёй."""
import json
import unittest
from pathlib import Path

from app.agent import GIVE_UP_CHECK, Agent
from app.engine import load_engine
from app.llm import Reply

DATA = Path(__file__).resolve().parents[1] / 'data'
RISKS = 'Как часто надо обновлять реестр рисков?'
DEADLINE = 'В какой срок ПА обязан принять заявку в работу?'
WRITER_MARK, JUDGE_MARK = 'Вы пишете ответ сотруднику', 'Вы проверяете ответ агента'


def ok(text):
    return Reply(text=text, ok=True, model='fake', prompt_tokens=1000, completion_tokens=50)


def answer(*uids):
    return ok(json.dumps({'action': 'answer', 'cites': list(uids)}))


def flow(decide, texts='Ответ по регламенту.', verdict=None):
    """Поддельная модель на всём цикле ответа: решение → текст → вердикт судьи."""
    queue = list(texts) if isinstance(texts, (list, tuple)) else None

    def chat(messages, max_tokens=600, temperature=0):
        system = messages[0]['content']
        if JUDGE_MARK in system:
            return verdict or ok('{"supported": true, "unsupported": []}')
        if WRITER_MARK in system:
            return ok(json.dumps({'text': queue.pop(0) if queue else texts}, ensure_ascii=False))
        return decide(messages) if callable(decide) else decide
    return chat


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

    def flowing(self, decide, texts='Ответ по регламенту.', verdict=None):
        llm = FakeLLM(ok('{}'))
        llm.chat = flow(decide, texts, verdict)
        return Agent(self.engine, llm)

    def one_per_document(self, question):
        groups = self.engine.search.per_document(question, k_each=3)
        return {doc: hits[0].uid for doc, hits in groups.items() if hits}

    # ---- выбор пунктов ----

    def test_answer_keeps_only_known_citations_in_order(self):
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        decide = ok(json.dumps({'action': 'answer', 'cites': ['нет-такого', uid, uid], 'why': ''}))
        decision = self.flowing(decide).ask('Сколько заявок может быть у одного ПА?')
        self.assertEqual((decision.route, decision.text), ('answer', 'Ответ по регламенту.'))
        self.assertEqual([c[0].uid for c in decision.cites], [uid])
        self.assertEqual((decision.checks['attempts'], decision.checks['judge']), (1, 'passed'))
        self.assertEqual(sorted(decision.usage.by_step), ['decide', 'judge', 'write'])   # три вызова модели
        self.assertEqual(decision.tokens, 3 * 1050)

    def test_citation_that_does_not_resemble_the_question_is_rejected(self):
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        self.assertEqual(self.agent(answer(uid), min_similarity=1.01).ask(DEADLINE).route, 'not_found')

    def test_answer_from_two_regulations_becomes_a_question(self):
        uids = self.one_per_document(RISKS)
        self.assertGreaterEqual(len(uids), 2)
        decision = self.agent(answer(*uids.values())).ask(RISKS)
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
        agent = self.agent(answer(*uids.values()), min_similarity=0)
        decision = agent.ask(RISKS, [('В каком регламенте?', 'Организация проектной деятельности', 'f2')])
        self.assertEqual(decision.route, 'answer')
        self.assertTrue(decision.cites)
        self.assertTrue(all(c[0].doc == 'f2' for c in decision.cites))

    def test_clarification_options_naming_a_regulation_are_bound_to_it(self):
        title = self.engine.corpus.title('f4')
        reply = ok(json.dumps({'action': 'clarify', 'clarify': {
            'question': 'В каком подразделении?', 'options': ['Регламент: «%s»' % title, 'Другое']}}))
        decision = self.agent(reply).ask('За какое время назначают РП?')
        self.assertEqual(decision.route, 'clarify')
        self.assertEqual(decision.option_docs, {'Регламент: «%s»' % title: 'f4'})

    def test_unavailable_model_degrades_to_raw_fragments(self):
        decision = self.agent(Reply(error='HTTP 429')).ask(RISKS)
        self.assertEqual((decision.route, decision.error), ('degraded', 'HTTP 429'))
        self.assertGreaterEqual(len({c[0].doc for c in decision.cites}), 1)

    def test_unparseable_answer_is_not_found(self):
        for text in ('не могу ответить', '{"action": "answer", "cites": [', '[1, 2, 3]'):
            with self.subTest(text=text):
                self.assertEqual(self.agent(ok(text)).ask(RISKS).route, 'not_found')

    def test_too_many_clarifications_end_honestly(self):
        reply = ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'Где?', 'options': ['А', 'Б']}}))
        agent = self.agent(reply)
        decision = agent.ask(RISKS, [('Где?', 'А'), ('Когда?', 'Б'), ('Кто?', 'В')])
        self.assertEqual(decision.route, 'unresolved')
        self.assertEqual(agent.llm.calls, 0)                  # лимит уточнений — модель уже не зовём

    def test_off_topic_question_never_reaches_the_model(self):
        agent = self.agent(answer('f3:5.1'))
        self.assertEqual(agent.ask('Как оформить отпуск за свой счёт?').route, 'not_found')
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
        script = ['{"action": "not_found"}',
                  json.dumps({'action': 'clarify', 'clarify': {'question': 'Какая ситуация?', 'options': ['А', 'Б']}})]
        shown = []

        def chat(messages, max_tokens=600, temperature=0):
            shown.append(messages)
            return ok(script.pop(0))
        llm = FakeLLM(ok('{}'))
        llm.chat = chat
        decision = Agent(self.engine, llm).ask('Кто готовит ТКП, если заказчик уже выбрал продукт?')
        self.assertEqual((decision.route, len(decision.replies)), ('clarify', 2))
        self.assertEqual(shown[1][-1]['content'], GIVE_UP_CHECK)

    def test_giving_up_on_distant_fragments_is_final(self):
        agent = self.agent(ok('{"action": "not_found"}'))
        unit = self.engine.corpus.get('f3:5.1')
        agent.candidates = lambda query, scope=None: {unit.uid: (unit, 0.5, 0.5)}   # похоже, но не сильно
        self.assertEqual((agent.ask(RISKS).route, agent.llm.calls), ('not_found', 1))

    # ---- текст ответа и его проверка ----

    def test_text_with_a_foreign_number_is_rewritten_before_any_judge(self):
        texts = ['Заявку нужно принять за 5 рабочих дней.', 'ПА обязан принять заявку в работу в течение 1 рабочего дня.']
        decision = self.flowing(answer('f3:5.1'), texts).ask(DEADLINE)
        self.assertEqual((decision.text, decision.checks['attempts'], decision.checks['judge']),
                         (texts[1], 2, 'rewritten'))
        self.assertEqual(sorted(decision.usage.by_step), ['decide', 'judge', 'write'])
        self.assertEqual(decision.usage.by_step['judge']['calls'], 1)   # судью звали один раз — после чистой редакции
        self.assertEqual(decision.usage.by_step['write']['calls'], 2)

    def test_text_the_judge_rejects_ends_as_verbatim_clauses(self):
        verdict = ok('{"supported": false, "unsupported": ["рекомендуется согласовать с юристами"]}')
        decision = self.flowing(answer('f3:5.1'), 'Согласуйте с юристами.', verdict).ask(DEADLINE)
        self.assertEqual((decision.text, decision.checks['judge'], decision.checks['attempts']), ('', 'failed', 2))

    def test_foreign_facts_are_found_by_code(self):
        unit = self.engine.corpus.get('f3:5.1')                  # «…в течение 1 рабочего дня …»
        cites = [(unit, 1.0, 1.0)]
        numbers, short = Agent(self.engine, None).foreign_facts('За 5 дней ТКП и СБ.', cites)
        self.assertEqual(numbers, ['число «5» есть в ответе, но его нет в пунктах'])
        self.assertEqual(short, ['сокращение «ТКП» не встречается в пунктах', 'сокращение «СБ» не встречается в пунктах'])
        numbers, short = Agent(self.engine, None).foreign_facts('ПА обязан принять заявку в течение 1 рабочего дня.', cites)
        self.assertEqual((numbers, short), ([], []))           # «ПА» есть в адресе и тексте пункта

    def test_unavailable_writer_leaves_clauses_verbatim(self):
        def writer_is_down(messages, max_tokens=600, temperature=0):
            if WRITER_MARK in messages[0]['content']:
                return Reply(error='HTTP 429')
            return answer('f3:5.1')
        llm = FakeLLM(ok('{}'))
        llm.chat = writer_is_down
        decision = Agent(self.engine, llm).ask(DEADLINE)
        self.assertEqual((decision.route, decision.text, decision.checks['judge']), ('answer', '', 'unavailable'))


if __name__ == '__main__':
    unittest.main()
