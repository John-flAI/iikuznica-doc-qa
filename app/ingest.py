# -*- coding: utf-8 -*-
"""Регламент → разделы → пункты → адресуемые абзацы.

Атом — наименьший кусок текста, на который можно сослаться: раздел, пункт, подпункт
или абзац под ними (примечание, элемент списка, ячейка таблицы). У каждого атома есть
дословный текст и адрес — цепочка «раздел → пункт → подпункт».

Правила разбора (проверены на корпусе заказчика, tests/test_ingest.py):
  «5. Название»            раздел, если номер идёт по порядку; номер не по порядку
                           внутри пункта — литеральный элемент списка («3. …»);
  «5.2. Текст»             пункт;  «5.2.1. Текст» — подпункт;
  «a. Текст», «а) Текст»   элемент под текущим пунктом, соседние буквы — одна глубина;
  абзац или «•» без номера принадлежит ближайшему пункту сверху;
  строка таблицы           каждая непустая ячейка — атом с заголовком столбца в адресе.
"""
import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .docx import read_docx

RE_SUB = re.compile(r'^(\d+\.\d+\.\d+)\.\s+(.+)$', re.S)
RE_CLAUSE = re.compile(r'^(\d+\.\d+)\.\s+(.+)$', re.S)
RE_SECTION = re.compile(r'^(\d+)\.\s+(.+)$', re.S)
RE_LETTER = re.compile(r'^([a-zа-яё])[.)]\s+(.+)$', re.S)
RE_NUM_LABEL = re.compile(r'^\d+(?:\.\d+)*\.$')

SECTION, CLAUSE, SUB, TEXT = 'section', 'clause', 'sub', 'text'
CITABLE = (CLAUSE, SUB, TEXT)


@dataclass(frozen=True)
class Unit:
    uid: str
    doc: str
    number: str            # '5.2.1' или ''
    text: str              # дословный текст без номера
    title: str             # название — только у раздела
    crumbs: tuple          # ((номер, название или ''), ...) от раздела до подпункта
    kind: str
    label: str = ''        # метка внутри пункта: 'a.', '1.'; '' у обычных абзацев

    @property
    def quote(self):
        """Дословная цитата так, как она читается в документе."""
        if self.number and self.kind in (CLAUSE, SUB):
            return '%s. %s' % (self.number, self.text)
        if self.label:
            return '%s %s' % (self.label, self.text)
        return self.text

    @property
    def address(self):
        """Адрес для человека: «раздел 5 «Название» → п. 5.2 → п. 5.2.3 → подпункт a»."""
        chain = []
        for number, heading, role in self.crumbs:
            if role == SECTION:
                chain.append('раздел %s «%s»' % (number, heading) if number else '«%s»' % heading)
            elif role == CLAUSE:
                chain.append('п. %s' % number)
            elif role == 'label':
                chain.append('подпункт %s' % number.rstrip('.'))
            elif role == 'column':
                chain.append('графа «%s»' % number)
        if self.kind == SECTION:
            chain.append('раздел %s «%s»' % (self.number, self.title) if self.number else self.title)
        elif self.kind in (CLAUSE, SUB):
            chain.append('п. %s' % self.number)
        return ' → '.join(chain) or 'документ'

    @property
    def search_text(self):
        """Текст для поиска: адрес + содержание."""
        return ' '.join(part for part in (self.address, self.text) if part)


@dataclass
class Corpus:
    docs: dict             # id → {'title', 'file', 'raw'}
    units: list

    def __post_init__(self):
        self._index = {u.uid: u for u in self.units}
        assert len(self._index) == len(self.units), 'uid атомов должны быть уникальны'

    def title(self, doc_id):
        return self.docs[doc_id]['title']

    def get(self, uid):
        return self._index.get(uid)


class _Parser:
    """Разбор одного документа."""

    def __init__(self, doc_id):
        self.doc_id = doc_id
        self.title = None
        self.units = []
        self.sec = self.clause = self.sub = None     # (номер, название) — текущие узлы
        self.stack = []                              # [(глубина, метка)] — элементы внутри пункта
        self.last_section = 0
        self.table_header = None                     # строка-заголовок текущей таблицы
        self.duplicates = Counter()
        self.text_no = Counter()
        self.heading_no = 0

    # ---- служебное ----

    def _add(self, uid, kind, number, text, title='', label='', crumbs=()):
        self.duplicates[uid] += 1
        if self.duplicates[uid] > 1:                 # номер повторяется в исходнике — разводим суффиксом
            uid = '%s!%d' % (uid, self.duplicates[uid])
        self.units.append(Unit(uid, self.doc_id, number, text, title, tuple(crumbs), kind, label))

    def _parents(self, kind):
        chain = []
        if self.sec and kind != SECTION:
            chain.append(self.sec + (SECTION,))
        if self.clause and kind in (SUB, TEXT):
            chain.append(self.clause + (CLAUSE,))
        if self.sub and kind == TEXT:
            chain.append(self.sub + (CLAUSE,))
        return chain

    def _base(self):
        for node in (self.sub, self.clause, self.sec):
            if node:
                return node[0]
        return '0'

    def _anchor(self):
        """Ближайший пункт, к которому относятся абзацы без номера."""
        return self.sub or self.clause

    # ---- атомы ----

    def _numbered(self, number, body, kind):
        crumbs = self._parents(kind)
        self.stack = []
        if kind == SUB:
            self.sub = (number, '')
        elif kind == CLAUSE:
            self.clause, self.sub = (number, ''), None
        else:
            self.sec, self.clause, self.sub = (number, body), None, None
        self._add('%s:%s' % (self.doc_id, number), kind, number,
                  '' if kind == SECTION else body, title=body if kind == SECTION else '', crumbs=crumbs)

    def _text(self, body, column=''):
        """Абзац без номера: примечание, продолжение, ячейка таблицы."""
        base = self._base()
        self.text_no[base] += 1
        crumbs = self._parents(TEXT) + ([(column, '', 'column')] if column else [])
        self._add('%s:%s~%d' % (self.doc_id, base, self.text_no[base]), TEXT, '', body, crumbs=crumbs)

    def _item(self, depth, label, body):
        """Элемент с литеральной меткой («a.», «1.») под текущим пунктом."""
        self.stack = [s for s in self.stack if s[0] < depth] + [(depth, label)]
        crumbs = self._parents(TEXT) + [(lab, '', 'label') for _, lab in self.stack]
        self._add('%s:%s~%s' % (self.doc_id, self._base(), '~'.join(lab for _, lab in self.stack)),
                  TEXT, '', body, label=label, crumbs=crumbs)

    def _table_row(self, cells):
        if self.table_header is None:
            filled = [c for c in cells if c]
            self.table_header = cells if len(filled) >= 2 and all(len(c) <= 60 for c in filled) else ()
            if self.table_header:
                return
        for i, cell in enumerate(cells):
            if cell:
                self._text(cell, column=self.table_header[i] if i < len(self.table_header) else '')

    # ---- разбор ----

    def parse(self, lines):
        for line in lines:
            if line.kind == 'row':
                self._table_row(line.cells)
                continue
            self.table_header = None
            text = line.text
            if self.title is None and not _looks_numbered(text):
                self.title = text
                continue
            if line.kind == 'h':
                self._heading(text)
                continue
            sub, clause = RE_SUB.match(text), RE_CLAUSE.match(text)
            if line.label == '•' and (sub or clause):
                # номер стоит в тексте после маркера списка: «• 20.2.1. Сроки…»
                self._numbered(*(sub or clause).groups(), SUB if sub else CLAUSE)
            elif line.kind == 'li':
                if RE_NUM_LABEL.match(line.label) or RE_LETTER.match(line.label + ' '):
                    self._item(line.depth, line.label, text)
                else:
                    self._text(text)
            elif sub:
                self._numbered(*sub.groups(), SUB)
            elif clause:
                self._numbered(*clause.groups(), CLAUSE)
            else:
                self._plain(text)
        return self.title or self.doc_id, self.units

    def _heading(self, text):
        self.heading_no += 1
        self.sec, self.clause, self.sub, self.stack = ('', text), None, None, []
        self._add('%s:h%d' % (self.doc_id, self.heading_no), SECTION, '', '', title=text)

    def _plain(self, text):
        section = RE_SECTION.match(text)
        if section:
            number = int(section.group(1))
            if number != self.last_section + 1 and self._anchor():
                self._item(0, '%d.' % number, section.group(2))   # литеральный «3.» внутри пункта
            else:
                self.last_section = number          # номер по порядку — это раздел
                self._numbered(section.group(1), section.group(2), SECTION)
            return
        letter = RE_LETTER.match(text)
        if letter and self._anchor():
            numeric_parent = self.stack and RE_NUM_LABEL.match(self.stack[-1][1])
            depth = self.stack[-1][0] + (1 if numeric_parent else 0)
            self._item(depth, letter.group(1) + '.', letter.group(2))
            return
        self._text(text)


def _looks_numbered(text):
    return bool(RE_SUB.match(text) or RE_CLAUSE.match(text) or RE_SECTION.match(text))


def parse_document(doc_id, lines):
    """Строкы документа → (название, атомы)."""
    return _Parser(doc_id).parse(lines)


def load_corpus(data_dir):
    """data/corpus.json → Corpus; raw — весь текст документов для проверки цитат."""
    data_dir = Path(data_dir)
    manifest = json.loads((data_dir / 'corpus.json').read_text(encoding='utf-8'))
    docs, units = {}, []
    for entry in manifest:
        lines = read_docx(data_dir / entry['file'])
        title, doc_units = parse_document(entry['id'], lines)
        docs[entry['id']] = {'title': title, 'file': entry['file'],
                             'raw': '\n'.join(l.text for l in lines)}
        units.extend(doc_units)
    return Corpus(docs, units)
