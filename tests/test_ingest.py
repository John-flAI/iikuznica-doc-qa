# -*- coding: utf-8 -*-
"""Разбор регламентов: структура и инварианты."""
import unittest
from collections import Counter
from pathlib import Path

from app.docx import Line
from app.ingest import CITABLE, CLAUSE, SECTION, SUB, TEXT, load_corpus, parse_document

DATA = Path(__file__).resolve().parents[1] / 'data'


def by_snippet(corpus, doc, snippet):
    """Атом документа, в тексте которого есть подстрока — ровно один."""
    found = [u for u in corpus.units if u.doc == doc and snippet in u.text]
    assert len(found) == 1, 'ожидался один атом с «%s» в %s, найдено %d' % (snippet, doc, len(found))
    return found[0]


class CorpusStructure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_corpus(DATA)

    def test_titles_and_sections(self):
        expected = {'f2': ('Организация проектной деятельности', 24),
                    'f3': ('Пресейловая деятельность Дирекции ИБ', 16),
                    'f4': ('Проектная деятельность ДБРЦТ (Департамента бизнес-решений и цифровой трансформации)', 23),
                    'f5': ('Проектная деятельность Дирекции ИБ', 34)}
        for doc, (title, sections) in expected.items():
            kinds = Counter(u.kind for u in self.corpus.units if u.doc == doc)
            self.assertEqual(self.corpus.title(doc), title)
            self.assertEqual(kinds[SECTION], sections, doc)

    def test_uid_are_unique(self):
        uids = [u.uid for u in self.corpus.units]
        self.assertEqual(len(uids), len(set(uids)))

    def test_nothing_lost(self):
        """Каждая цитируемая строка — дословно в исходном документе, и текст не потерян."""
        for doc, info in self.corpus.docs.items():
            covered = sum(len(u.text) for u in self.corpus.units if u.doc == doc and u.kind in CITABLE)
            self.assertGreater(covered, 0.9 * len(info['raw']), doc)

    def test_every_quote_is_verbatim(self):
        for unit in self.corpus.units:
            if unit.kind in CITABLE:
                self.assertIn(unit.text, self.corpus.docs[unit.doc]['raw'], unit.uid)

    def test_no_text_before_first_section(self):
        """Абзац без адреса — это потерянный текст; таких быть не должно."""
        stray = [u.uid for u in self.corpus.units if u.kind == TEXT and not u.crumbs]
        self.assertEqual(stray, [])

    def test_clause_address(self):
        unit = self.corpus.get('f3:5.1')
        self.assertEqual(unit.address, 'раздел 5 «Завершение подготовки пресейла» → п. 5.1')
        self.assertTrue(unit.quote.startswith('5.1. ПА обязан принять заявку в работу'))

    def test_subclause_address(self):
        unit = self.corpus.get('f5:8.3.1')
        self.assertEqual(unit.address,
                         'раздел 8 «Заключение договора с Заказчиком» → п. 8.3 → п. 8.3.1')

    def test_note_under_clause_keeps_its_address(self):
        unit = by_snippet(self.corpus, 'f3', 'У одного ПА может быть максимум 10 заявок')
        self.assertEqual(unit.address,
                         'раздел 5 «Завершение подготовки пресейла» → п. 5.2 → п. 5.2.3')

    def test_list_item_inside_clause(self):
        """«b.» под «1.» — сосед «a.», а не его ребёнок; parent — числовой элемент."""
        unit = by_snippet(self.corpus, 'f3', 'Если Заказчик явно обозначил, что ему не потребуются услуги')
        self.assertEqual(unit.address,
                         'раздел 1 «Постановка задачи на пресейл» → п. 1.3 → подпункт 1 → подпункт b')
        self.assertEqual(unit.quote[:2], 'b.')

    def test_letter_items_switch_numeric_parent(self):
        unit = by_snippet(self.corpus, 'f3', 'более 30% стоимости поставки, BDM формирует ТКП')
        self.assertEqual(unit.address,
                         'раздел 1 «Постановка задачи на пресейл» → п. 1.3 → подпункт 2 → подпункт b')

    def test_number_after_bullet_is_a_subclause(self):
        unit = self.corpus.get('f2:20.2.1')
        self.assertEqual((unit.kind, unit.text), (SUB, 'Сроки реализации проекта;'))

    def test_table_cell_gets_its_column(self):
        unit = by_snippet(self.corpus, 'f5', 'Итоговый расчёт проекта по утверждённой форме')
        self.assertEqual(unit.address,
                         'раздел 1 «Запрос на инициацию проекта» → п. 1.2 → графа «Обязательно»')


class SyntheticDocument(unittest.TestCase):
    """Правила разбора на маленьком документе, собранном вручную."""

    def parse(self, *lines):
        return parse_document('t', [Line(kind, text, **(extra[0] if extra else {}))
                                    for kind, text, *extra in lines])

    def test_sections_clauses_and_plain_text(self):
        title, units = self.parse(
            ('p', 'Регламент тестовый'),
            ('p', '1. Первый раздел'),
            ('p', '1.1. Текст пункта.'),
            ('p', 'Продолжение пункта.'),
            ('p', '2. Второй раздел'),
            ('p', '2.1.1. Подпункт.'))
        self.assertEqual(title, 'Регламент тестовый')
        self.assertEqual([(u.kind, u.number, u.text) for u in units], [
            (SECTION, '1', ''), (CLAUSE, '1.1', 'Текст пункта.'), (TEXT, '', 'Продолжение пункта.'),
            (SECTION, '2', ''), (SUB, '2.1.1', 'Подпункт.')])
        self.assertEqual(units[2].address, 'раздел 1 «Первый раздел» → п. 1.1')

    def test_literal_number_inside_clause_is_not_a_section(self):
        """Номер внутри пункта, не совпадающий со следующим разделом, — элемент списка."""
        _, units = self.parse(
            ('p', 'Регламент'), ('p', '1. Раздел'), ('p', '1.1. Пункт'),
            ('p', '3. Первый вариант'), ('p', 'a. Буквенный'), ('p', 'b. Соседний'),
            ('p', '4. Второй вариант'), ('p', '2. Следующий раздел'))
        kinds = [(u.kind, u.number or u.label) for u in units]
        self.assertEqual(kinds, [(SECTION, '1'), (CLAUSE, '1.1'), (TEXT, '3.'), (TEXT, 'a.'),
                                 (TEXT, 'b.'), (TEXT, '4.'), (SECTION, '2')])
        self.assertEqual(units[4].address,
                         'раздел 1 «Раздел» → п. 1.1 → подпункт 3 → подпункт b')

    def test_next_section_number_wins_inside_clause(self):
        """Граница правила: номер, равный следующему разделу, читается как раздел."""
        _, units = self.parse(
            ('p', 'Регламент'), ('p', '1. Раздел'), ('p', '1.1. Пункт'),
            ('p', '2. Название раздела'), ('p', '2.1. Пункт'))
        self.assertEqual([u.kind for u in units], [SECTION, CLAUSE, SECTION, CLAUSE])

    def test_table_with_header_row(self):
        _, units = self.parse(
            ('p', 'Регламент'), ('p', '1. Раздел'), ('p', '1.1. Пункт'),
            ('row', 'Обязательно | Срок', {'cells': ('Обязательно', 'Срок')}),
            ('row', 'Смета | 3 дня', {'cells': ('Смета', '3 дня')}))
        cells = [u for u in units if u.kind == TEXT]
        self.assertEqual([u.text for u in cells], ['Смета', '3 дня'])
        self.assertEqual(cells[0].address, 'раздел 1 «Раздел» → п. 1.1 → графа «Обязательно»')


if __name__ == '__main__':
    unittest.main()
