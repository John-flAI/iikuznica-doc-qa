# -*- coding: utf-8 -*-
"""Логика агента на поддельной модели: что проходит в ответ, а что отсекается кодом и судьёй."""
import json
import re
import unittest
from pathlib import Path

from app.agent import GIVE_UP_CHECK, Agent, subject_words
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

    def flowing(self, decide, texts='Ответ по регламенту.', verdict=None, **kwargs):
        llm = FakeLLM(ok('{}'))
        llm.chat = flow(decide, texts, verdict)
        return Agent(self.engine, llm, **kwargs)

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
        """Один и тот же предмет регулируют два регламента — агент не выбирает сам, а спрашивает, какой его.
        (Похожий по словам пункт о план-графике сюда больше не попадает: проверка предмета его отбрасывает.)"""
        first, second = self.engine.corpus.get('f2:18.2~11'), self.engine.corpus.get('f4:15.3~3')
        agent = self.agent(answer(first.uid, second.uid))
        agent.candidates = lambda query, scope=None: {u.uid: (u, 1.0, 1.0) for u in (first, second)}
        decision = agent.ask(RISKS)
        self.assertEqual((decision.route, decision.offsubject), ('clarify', []))
        self.assertEqual(sorted(decision.option_docs.values()), ['f2', 'f4'])
        for option in decision.options:
            self.assertIn(option, decision.option_docs)

    def test_same_answer_in_several_regulations_is_given_at_once(self):
        """Регламенты говорят одно и то же, модель это подтвердила (same), код сверил сроки — уточнять нечего."""
        question = 'Кто отвечает за закрытие проекта в учётных системах?'
        units = [self.engine.corpus.get('f4:22.1'), self.engine.corpus.get('f5:33.1')]     # одинаковый абзац-дубль
        agent = self.agent(ok('{}'))
        found = agent.candidates(question)
        self.assertTrue(all(u.uid in found for u in units), list(found))
        decide = ok(json.dumps({'action': 'answer', 'cites': [u.uid for u in units], 'same': True}))
        decision = self.flowing(decide).ask(question)
        self.assertEqual(decision.route, 'answer')
        self.assertEqual(len({c[0].doc for c in decision.cites}), 2)

    def test_model_saying_same_does_not_outweigh_differing_terms(self):
        """Модель написала same, а в пунктах месяц против недели — код всё равно уточняет."""
        month, week = self.engine.corpus.get('f2:18.2~11'), self.engine.corpus.get('f5:23.4~6')
        self.assertIn('месяц', month.text)
        self.assertIn('неделю', week.text)
        decide = ok(json.dumps({'action': 'answer', 'cites': [month.uid, week.uid], 'same': True}))
        agent = self.agent(ok('{}'))
        agent.candidates = lambda query, scope=None: {month.uid: (month, 1.0, 1.0), week.uid: (week, 1.0, 1.0)}
        llm = FakeLLM(ok('{}'))
        llm.chat = flow(decide)
        agent.llm = llm
        self.assertEqual(agent.ask('Как часто надо обновлять реестр рисков?').route, 'clarify')

    def test_regulation_named_in_the_question_narrows_the_search(self):
        """Сотрудник сам назвал регламент — переспрашивать, в каком он работает, не нужно."""
        question = RISKS + ' Это по регламенту «Организация проектной деятельности».'
        agent = self.agent(ok('{}'), min_similarity=0)
        scope, rest = agent.split_regulation(question)
        self.assertEqual(scope, 'f2')
        self.assertNotIn('Организация проектной деятельности', rest)      # слова названия поиск не разбавляют
        found = agent.candidates(rest, scope)
        self.assertTrue(found)
        decide = ok(json.dumps({'action': 'answer', 'cites': [next(iter(found))]}))
        llm = FakeLLM(ok('{}'))
        llm.chat = flow(decide)
        agent.llm = llm
        decision = agent.ask(question)
        self.assertEqual(decision.route, 'answer')
        self.assertTrue(all(c[0].doc == 'f2' for c in decision.cites))
        self.assertEqual(agent.named_regulation('Как часто обновлять реестр рисков в организации проектной деятельности?'), 'f2')
        self.assertIsNone(agent.named_regulation('Риски в ДБРЦТ и Дирекции ИБ'))      # «ИБ» — два регламента
        self.assertIsNone(agent.named_regulation('Какова организация совещаний по проекту?'))   # слова те же, названия нет
        self.assertEqual(agent.named_regulation('Кто назначает РП в ДБРЦТ?'), 'f4')

    def test_named_regulation_keeps_candidates_even_on_low_word_overlap(self):
        """Регламент назван, а слова вопроса в его пунктах почти не встречаются — всё равно показываем модели
        лучшие пункты этого регламента: «нет ответа» пусть решает она, а не порог."""
        from types import SimpleNamespace
        agent = self.agent(ok('{}'))
        unit = self.engine.corpus.get('f2:1.1')
        weak = SimpleNamespace(uid=unit.uid, similarity=0.2, coverage=0.2, unit=unit)
        search = agent.engine.search
        agent.engine.search = SimpleNamespace(
            per_document=lambda query, k_each=3: {'f2': [weak]}, vectors=search.vectors)
        try:
            self.assertEqual(agent.candidates('любой вопрос'), {})                            # без названия — отсекается
            self.assertEqual(list(agent.candidates('любой вопрос', scope='f2')), [unit.uid])   # с названием — показываем
        finally:
            agent.engine.search = search

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

    def chatty(self, second):
        """Поддельная модель: переспрашивает «предоставлена ли полная информация», а после напоминания — как задано."""
        def decide(messages):
            if GIVE_UP_CHECK in messages[-1]['content']:
                return second(messages)
            return ok(json.dumps({'action': 'clarify', 'clarify': {
                'question': 'Предоставлена ли полная информация по проекту?', 'options': ['Да', 'Нет']}},
                ensure_ascii=False))
        agent = self.flowing(decide)
        agent.strongest = lambda found: 0.9                    # фрагменты близки к вопросу — прямой ответ в них есть
        return agent

    def test_needless_clarification_gets_one_reminder_and_then_an_answer(self):
        shown = []

        def first(messages):
            shown.append(messages)
            return answer(re.search(r'\[(f\d:[^\]\s]+)\]', '\n'.join(m['content'] for m in messages)).group(1))

        decision = self.chatty(first).ask('В какой срок координатор назначает руководителя проекта в дирекции ИБ?')
        self.assertEqual((decision.route, decision.text), ('answer', 'Ответ по регламенту.'))
        self.assertEqual(len(shown), 1)                         # напоминание — только одно

    def test_persistent_model_may_still_ask(self):
        always = lambda messages: ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'Да или нет?', 'options': ['Да']}},
                                                ensure_ascii=False))
        decision = self.chatty(always).ask(RISKS)
        self.assertEqual((decision.route, decision.question), ('clarify', 'Да или нет?'))

    def test_clarification_about_regulations_is_not_second_guessed(self):
        reply = ok(json.dumps({'action': 'clarify', 'clarify': {'question': 'К какому регламенту относится ваш проект?',
                                                                'options': ['А', 'Б']}}, ensure_ascii=False))
        agent = self.agent(reply)
        agent.strongest = lambda found: 0.9
        self.assertEqual(agent.ask(RISKS).route, 'clarify')
        self.assertEqual(agent.llm.calls, 1)                   # нужный вопрос модель задаёт сразу

    def test_off_topic_question_never_reaches_the_decision(self):
        """Подходящих фрагментов нет: решение по фрагментам не принимается — один вызов классификатора
        («рабочий вопрос или разговор не по делу»), и фрагменты ему не показываются."""
        agent = self.agent(answer('f3:5.1'))
        decision = agent.ask('Как оформить отпуск за свой счёт?')
        self.assertEqual((decision.route, agent.llm.calls), ('not_found', 1))
        self.assertNotIn('Фрагменты регламентов', agent.llm.shown)
        self.assertIn('не нашлось ничего подходящего', agent.llm.shown)

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

    def test_citation_with_a_dropped_paragraph_suffix_still_answers(self):
        """Модель сослалась на пункт без суффикса абзаца — берём ближайший абзац этого пункта, а не «нет ответа»."""
        uid = self.one_per_document('Сколько заявок может быть у одного ПА?')['f3']
        self.assertIn('~', uid)
        decide = ok(json.dumps({'action': 'answer', 'cites': [uid.split('~')[0]]}))
        decision = self.flowing(decide).ask('Сколько заявок может быть у одного ПА?')
        self.assertEqual((decision.route, [c[0].uid for c in decision.cites]), ('answer', [uid]))

    def test_clause_about_another_subject_is_not_an_answer(self):
        """Судья видит, что пункт о другом предмете (приоритеты рисков при вопросе о приоритетах заявок)."""
        verdict = ok('{"supported": false, "offtopic": true, "unsupported": []}')
        decision = self.flowing(answer('f3:5.1'), 'Ответ не по адресу.', verdict).ask(DEADLINE)
        self.assertEqual((decision.route, decision.cites, decision.text), ('not_found', [], ''))

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

    def test_unreadable_judge_verdict_is_not_a_pass(self):
        """Невалидный вердикт судьи — не «пройдено»: одна переписка, затем пункты дословно."""
        for broken in ('{"supported": true', '', 'Подтверждаю без ошибок.'):
            with self.subTest(verdict=broken):
                decision = self.flowing(answer('f3:5.1'), 'Ответ по регламенту.', ok(broken)).ask(DEADLINE)
                self.assertEqual((decision.route, decision.text, decision.checks['judge']), ('answer', '', 'failed'))
                self.assertEqual((decision.usage.by_step['judge']['calls'], decision.checks['attempts']), (2, 2))

    def test_unreadable_verdict_then_a_clear_one(self):
        """Первая проверка не разобралась — текст переписан, вторая явно подтвердила: идёт вторая редакция."""
        writer = flow(answer('f3:5.1'), ['Первая редакция.', 'Вторая редакция.'])
        verdicts = iter([ok('{"supported": true'), ok('{"supported": true, "unsupported": []}')])

        def chat(messages, max_tokens=600, temperature=0):
            return next(verdicts) if JUDGE_MARK in messages[0]['content'] else writer(messages, max_tokens, temperature)
        llm = FakeLLM(ok('{}'))
        llm.chat = chat
        decision = Agent(self.engine, llm).ask(DEADLINE)
        self.assertEqual((decision.route, decision.text, decision.checks['judge']), ('answer', 'Вторая редакция.', 'rewritten'))

    def test_clause_about_risks_is_not_an_answer_about_requests(self):
        """C04 банка: пункт о приоритетах рисков не отвечает на вопрос о приоритетах заявок — и судья
        этого не замечает. Предметное слово вопроса проверяем по тексту самого пункта."""
        unit = self.engine.corpus.get('f5:24.7.3~2')
        self.assertIn('приоритет 2', unit.quote)

        def agent():
            agent = self.agent(answer(unit.uid))
            agent.candidates = lambda query, scope=None: {unit.uid: (unit, 1.0, 1.0)}
            return agent
        decision = agent().ask('По каким критериям заявке присваивается приоритет 2?')
        self.assertEqual((decision.route, decision.text, [d[0] for d in decision.offsubject]), ('not_found', '', [unit.uid]))
        decision = agent().ask('По каким критериям риск получает приоритет 2?')
        self.assertEqual((decision.route, decision.offsubject), ('answer', []))

    def test_subject_words_skip_question_words_and_verbs(self):
        """Предметные слова — только о предмете: «сколько», «какой», «надо», «одном» и глаголы не в счёт."""
        self.assertEqual(subject_words('Сколько заявок может висеть на одном архитекторе?'), {'заявок', 'архитектор'})
        self.assertEqual(subject_words('Как часто надо обновлять реестр рисков?'), {'реестр', 'риск'})
        self.assertEqual(subject_words('Можно ли работать без сметы?'), {'смет'})
        self.assertEqual(subject_words('Кто готовит ТКП, если заказчик уже выбрал продукт?'), {'заказчик', 'продукт', 'ТКП'})

    def test_question_without_subject_words_is_not_blocked(self):
        """В вопросе нет предметных слов — проверка не мешает отвечать."""
        unit = self.engine.corpus.get('f3:5.2.3~3')
        agent = self.agent(answer(unit.uid))
        agent.candidates = lambda query, scope=None: {unit.uid: (unit, 1.0, 1.0)}
        decision = agent.ask('Сколько это делается?')
        self.assertEqual((decision.route, decision.offsubject, decision.checks['offsubject']), ('answer', [], []))
