# -*- coding: utf-8 -*-
"""Поиск: вопросы сотрудника другими словами находят нужный пункт."""
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace

from app.agent import Agent
from app.engine import Engine, load_engine
from app.ingest import SECTION, TEXT, Unit, load_corpus
from app.search import Search, Tokenizer, load_terms, make_morph

DATA = Path(__file__).resolve().parents[1] / 'data'

# (вопрос, документ, подстрока текста нужного атома) — вопросы не повторяют формулировку регламента
ONE_ANSWER = [
    ('Через сколько пресейл-архитектор должен взять заявку в работу после того как её назначили?',
     'f3', 'ПА обязан принять заявку'),
    ('Сколько заявок может висеть на одном архитекторе?', 'f3', 'максимум 10 заявок'),
    ('Насколько заранее нужно подать заявку на расчёт до конкурса?', 'f2', 'за 2 недели до подачи конкурса'),
    ('Заявка на расчёт в ДБРЦТ — за сколько рабочих дней до предложения заказчику?',
     'f4', 'не менее чем за 3 рабочих дня'),
    ('В какой срок координатор назначает руководителя проекта в дирекции ИБ?',
     'f5', 'Координатор департамента назначает РП'),
    ('Когда карточка проекта уходит в Отклонён?', 'f5', 'статус «Отклонён»'),
    ('Сколько дней делается точная оценка, а сколько бюджетная?', 'f4', 'для точных оценок – 5 рабочих дней'),
    ('В течение какого времени после договора РП создаёт Смету в учётной системе?',
     'f4', 'создаёт Смету в учётной системе в течение 3 рабочих дней'),
    ('Сколько времени действует расчёт проекта?', 'f3', 'Срок действия всех расчетов устанавливается'),
    ('Если заказчик полгода не решает, что делать с расчётом?', 'f3', 'в течение 6 месяцев с даты подготовки расчета'),
    ('Гарантийный срок после подписания акта выполненных работ?', 'f5', '2 лет с момента подписания Акта'),
    ('Кто отвечает за подготовку и подписание договора с заказчиком?',
     'f2', 'подписание договора с заказчиком отвечает Аккаунт-менеджер'),
    ('За какое время до начала работ нужно завести смету в дирекции ИБ?',
     'f5', 'не позднее, чем за день до даты начала работ'),
    ('Можно ли руководителю проекта начать работы, если договор ещё не подписан?',
     'f2', 'без подписанного договора запрещается'),
    ('Что нужно успеть сделать со списаниями в пятницу до двух часов дня?',
     'f4', 'по пятницам, еженедельно, до 14:00 мск'),
    ('Кто назначает РП на инициацию проекта в ДБРЦТ?', 'f4', 'назначает на инициацию проекта РП'),
]

# один вопрос — регламенты разных дирекций отвечают по-разному: нужен лучший ответ каждого
TWO_REGULATIONS = [
    ('Как часто надо обновлять реестр рисков?',
     [('f2', 'не реже одного раза в месяц'), ('f5', 'не реже одного раза в неделю')]),
    ('Если смета ещё не согласована, можно начинать работы?',
     [('f2', 'Без согласованной Сметы'), ('f5', 'При отсутствии согласованной сметы')]),
    ('За какое время назначают руководителя проекта?',
     [('f2', 'Срок назначения Руководителя проекта – 1 рабочий день'),
      ('f4', 'назначает на инициацию проекта РП не позднее 12 рабочих часов')]),
]

SYNONYMS = ['руководитель проекта', 'РП', 'проджект менеджер', 'project manager']
OFF_TOPIC = ['Как оформить отпуск за свой счёт?', 'В каком кафе пообедать рядом с офисом?',
             'Как настроить VPN на домашнем компьютере?']
HARD_TOP = 5              # вопросы, где нужный пункт не обязан быть в тройке, но обязан быть в пятёрке


def vectors_available():
    """Сервер эмбеддингов отвечает? Если нет — проверяется только словесный канал."""
    try:
        load_engine()
        return True
    except (urllib.error.URLError, OSError, ConnectionError):
        return False


class SearchQuality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.with_vectors = vectors_available()
        cls.engine = load_engine(with_vectors=cls.with_vectors)

    def found(self, question, doc, snippet, top):
        return any(h.unit.doc == doc and snippet in h.unit.text
                   for h in self.engine.search.search(question, k=top))

    def test_lemmatizer_is_available(self):
        """Без лемматизатора формы слов не сводятся — об этом надо знать, а не молчать."""
        self.assertIsNotNone(self.engine.tokenizer.morph, 'pymorphy3 не установлен')

    def test_one_answer_questions(self):
        for question, doc, snippet in ONE_ANSWER:
            with self.subTest(question=question):
                self.assertTrue(self.found(question, doc, snippet, top=HARD_TOP),
                                'нет в топ-%d: %s' % (HARD_TOP, [h.uid for h in self.engine.search.search(question, k=HARD_TOP)]))

    def test_most_answers_are_on_top(self):
        """Большинство вопросов — нужный пункт в тройке; это наш ориентир качества поиска."""
        found = sum(self.found(question, doc, snippet, top=3) for question, doc, snippet in ONE_ANSWER)
        self.assertGreaterEqual(found, 13, 'в топ-3 только %d из %d' % (found, len(ONE_ANSWER)))

    @unittest.skipUnless(vectors_available(), 'нет сервера эмбеддингов (Ollama + bge-m3)')
    def test_right_answer_is_similar_enough(self):
        """Нужный атом всегда достаточно похож на вопрос — иначе порог «не найдено» его срежет."""
        for question, doc, snippet in ONE_ANSWER:
            with self.subTest(question=question):
                hit = next(h for h in self.engine.search.search(question, k=HARD_TOP)
                           if h.unit.doc == doc and snippet in h.unit.text)
                self.assertGreaterEqual(hit.similarity, 0.5)

    @unittest.skipUnless(vectors_available(), 'нет сервера эмбеддингов (Ollama + bge-m3)')
    def test_question_spans_two_regulations(self):
        """Ответ каждой дирекции должен быть виден, даже когда другая дирекция отвечает иначе."""
        for question, expected in TWO_REGULATIONS:
            with self.subTest(question=question):
                groups = self.engine.search.per_document(question, k_each=3)
                for doc, snippet in expected:
                    self.assertTrue(any(snippet in h.unit.text for h in groups.get(doc, [])),
                                    'нет в топ-3 документа %s: %s' % (doc, [h.uid for h in groups.get(doc, [])]))

    def test_abbreviations_and_spellings_agree(self):
        """«РП», «руководитель проекта», «проджект менеджер», «project manager» — один запрос."""
        for wording in SYNONYMS:
            with self.subTest(wording=wording):
                self.assertTrue(self.found('Срок назначения ' + wording, 'f2', 'Срок назначения Руководителя проекта',
                                           top=3 if self.with_vectors else 10))

    def test_off_topic_questions_look_unfound(self):
        """Вопрос не по регламентам не должен выглядеть найденным.

        Словесного покрытия мало: у короткого вопроса может остаться один токен («офис»), и он
        найдётся с покрытием 100 %. Поэтому при наличии эмбеддингов судим по сходству.
        """
        for question in OFF_TOPIC:
            with self.subTest(question=question):
                top = self.engine.search.search(question, k=1)[0]
                low = top.similarity < 0.5 if self.with_vectors else top.coverage < 0.3
                self.assertTrue(low, 'покрытие %.2f, сходство %.2f: %s' % (top.coverage, top.similarity, top.uid))


class WordsOnly(unittest.TestCase):
    """Словесный канал без сервера эмбеддингов — то, что работает всегда."""

    @classmethod
    def setUpClass(cls):
        corpus = load_corpus(DATA)
        cls.search = Search(corpus.units, Tokenizer(make_morph(), load_terms(DATA / 'terms.json')))

    def test_yo_and_e_are_one_letter(self):
        self.assertEqual(self.search.tokenizer.lemmas('расчёт'), ['расчет'])

    def test_verb_and_noun_share_a_stem(self):
        self.assertIn('назнач', self.search.tokenizer.tokens('назначили'))
        self.assertIn('назнач', self.search.tokenizer.tokens('назначения'))

    def test_abbreviation_survives(self):
        self.assertIn('ПА', self.search.tokenizer.lemmas('ПА обязан принять'))

    def test_term_dictionary_replaces_the_spelling(self):
        tokens = self.search.tokenizer.tokens('проджект менеджер')
        self.assertIn('РП', tokens)
        self.assertNotIn('менеджер', tokens)


class EmbeddingsOutage(unittest.TestCase):
    """Сервер эмбеддингов не ответил: стенд работает по словам, а не падает на каждом вопросе."""

    def setUp(self):
        corpus = load_corpus(DATA)
        self.search = Search(corpus.units, Tokenizer(make_morph(), load_terms(DATA / 'terms.json')))
        self.down = False
        self.search.embedder = SimpleNamespace(embed=self.embed)
        self.search.vectors = SimpleNamespace(search=lambda vector, k=30: [], append=lambda uid, row: None)
        self.engine = Engine(corpus, self.search, self.search.embedder)
        self.agent = Agent(self.engine, None)

    def embed(self, texts):
        if self.down:
            raise urllib.error.URLError('сервер эмбеддингов недоступен')
        return [[1.0, 0.0] for _ in texts]

    def test_question_is_answered_by_words_while_the_server_is_down(self):
        self.down = True
        hits = self.search.search('Сколько заявок может быть у одного ПА?', k=5)
        self.assertTrue(any(h.unit.doc == 'f3' and 'максимум 10 заявок' in h.unit.text for h in hits))
        self.assertFalse(self.search.semantic)                          # близость — словесным покрытием, пока сервер молчит
        self.assertEqual(self.agent.relevance(0.99, 0.4), 0.4)

    def test_knowledge_entry_is_added_by_words_when_embeddings_fail(self):
        self.down = True
        self.engine.corpus.add_document('kb', 'База знаний: ответы службы качества')
        self.engine.add(Unit('kb:1', 'kb', '', 'Агентский договор подписывает заместитель директора',
                             'Ответ службы качества', (('', 'Ответ службы качества', SECTION),), TEXT))
        self.assertIn('kb:1', self.search.by_uid)                        # запись ищется словами; вектор — после перезапуска

    def test_the_semantic_channel_is_tried_again_after_the_pause(self):
        self.down = True
        self.search.search('Сколько заявок может быть у одного ПА?', k=5)
        self.assertFalse(self.search.semantic)
        self.down, self.search.vectors_down = False, 0.0                 # сервер ожил, пауза кончилась
        self.assertTrue(self.search.semantic)
        self.assertEqual(self.agent.relevance(0.99, 0.4), 0.99)


if __name__ == '__main__':
    unittest.main()
