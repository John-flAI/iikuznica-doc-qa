# -*- coding: utf-8 -*-
"""Чтение DOCX без сторонних библиотек: абзацы, списки с восстановленной нумерацией, таблицы.

Word хранит номера пунктов в разметке, а не в тексте. Без их восстановления ссылки
«раздел → пункт» не собрать, поэтому нумерация вычисляется здесь.
"""
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def q(tag):
    return '{%s}%s' % (W, tag)


@dataclass(frozen=True)
class Line:
    """Одна строка документа в порядке следования."""
    kind: str            # 'p' — абзац · 'li' — абзац списка · 'row' — строка таблицы · 'h' — заголовок стиля
    text: str
    label: str = ''      # восстановленная метка списка: '•', 'a.', '1.', '2.1.'
    depth: int = 0       # уровень списка
    level: int = 0       # уровень заголовка (для 'h')
    cells: tuple = ()    # ячейки строки таблицы


def _fmt(value, kind):
    if kind in (None, 'decimal'):
        return str(value)
    if kind in ('lowerLetter', 'upperLetter'):
        s, v = '', value
        while v > 0:
            v, r = divmod(v - 1, 26)
            s = chr(97 + r) + s
        return s.upper() if kind == 'upperLetter' else s
    if kind in ('lowerRoman', 'upperRoman'):
        s, v = '', value
        for n, c in ((1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'), (100, 'c'), (90, 'xc'),
                     (50, 'l'), (40, 'xl'), (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i')):
            while v >= n:
                s, v = s + c, v - n
        return s.upper() if kind == 'upperRoman' else s
    return str(value)


class Numbering:
    """numId → уровни (ilvl → формат, шаблон, старт); счётчики ведутся по numId."""

    def __init__(self, z):
        self.nums, self.counters = {}, {}
        if 'word/numbering.xml' not in z.namelist():
            return
        root = ET.fromstring(z.read('word/numbering.xml'))
        abstract = {}
        for an in root.findall(q('abstractNum')):
            levels = {}
            for lvl in an.findall(q('lvl')):
                fmt, text, start = lvl.find(q('numFmt')), lvl.find(q('lvlText')), lvl.find(q('start'))
                levels[int(lvl.get(q('ilvl')))] = (
                    fmt.get(q('val')) if fmt is not None else 'decimal',
                    text.get(q('val')) if text is not None else '',
                    int(start.get(q('val'))) if start is not None else 1)
            abstract[an.get(q('abstractNumId'))] = levels
        for num in root.findall(q('num')):
            ref = num.find(q('abstractNumId'))
            self.nums[num.get(q('numId'))] = abstract.get(ref.get(q('val')), {})

    def label(self, num_id, ilvl):
        """Метка абзаца со списком; продвигает счётчики этого списка."""
        levels = self.nums.get(str(num_id))
        if not levels:
            return ''
        counters = self.counters.setdefault(str(num_id), {})
        kind, template, start = levels.get(ilvl, ('decimal', '%1.', 1))
        counters[ilvl] = counters.get(ilvl, start - 1) + 1
        for lower in [k for k in counters if k > ilvl]:
            del counters[lower]
        if kind == 'bullet':
            return '•'
        # числа подставляются в шаблон один раз: '%1.%2.' → '2.3.'
        values = {}
        for slot in range(1, 10):
            token = '%%%d' % slot
            if token not in template:
                break
            low_kind, _, low_start = levels.get(slot - 1, (kind, '', 1))
            values[token] = _fmt(counters.get(slot - 1, low_start), low_kind)
        for token, value in values.items():
            template = template.replace(token, value)
        return template.strip()


def _styles(z):
    """styleId → (имя, уровень заголовка); учитывает цепочку basedOn."""
    raw = {}
    if 'word/styles.xml' in z.namelist():
        for st in ET.fromstring(z.read('word/styles.xml')).findall(q('style')):
            name, base, lvl = st.find(q('name')), st.find(q('basedOn')), st.find(q('pPr'))
            outline = None
            if lvl is not None and lvl.find(q('outlineLvl')) is not None:
                try:
                    outline = int(lvl.find(q('outlineLvl')).get(q('val'))) + 1
                except ValueError:
                    outline = None
            raw[st.get(q('styleId'))] = (
                (name.get(q('val')) or '').lower() if name is not None else '',
                base.get(q('val')) if base is not None else None,
                outline)

    def resolve(sid, seen=()):
        if sid not in raw or sid in seen:
            return '', None
        name, base, outline = raw[sid]
        bname, boutline = resolve(base, seen + (sid,)) if base else ('', None)
        return name or bname, outline or boutline
    return resolve


def _text(node):
    out = []
    for el in node.iter():
        if el.tag == q('t'):
            out.append(el.text or '')
        elif el.tag == q('tab'):
            out.append(' ')
    return ' '.join(''.join(out).split())


def read_docx(path):
    """DOCX → список Line в порядке документа."""
    z = zipfile.ZipFile(path)
    numbering, style = Numbering(z), _styles(z)
    body = ET.fromstring(z.read('word/document.xml')).find(q('body'))
    lines = []
    for block in body:
        if block.tag == q('p'):
            text = _text(block)
            if not text:
                continue
            ppr, sid = block.find(q('pPr')), None
            if ppr is not None and ppr.find(q('pStyle')) is not None:
                sid = ppr.find(q('pStyle')).get(q('val'))
            name, level = style(sid) if sid else ('', None)
            if level:
                lines.append(Line('h', text, level=level))
                continue
            num_id, ilvl = None, 0
            if ppr is not None and ppr.find(q('numPr')) is not None:
                npr = ppr.find(q('numPr'))
                if npr.find(q('numId')) is not None:
                    num_id = npr.find(q('numId')).get(q('val'))
                if npr.find(q('ilvl')) is not None:
                    ilvl = int(npr.find(q('ilvl')).get(q('val')))
            label = numbering.label(num_id, ilvl) if num_id not in (None, '0') else ''
            if label:
                lines.append(Line('li', text, label=label, depth=ilvl))
            else:
                lines.append(Line('p', text))
        elif block.tag == q('tbl'):
            for row in block.findall(q('tr')):
                cells = tuple(_text(cell) for cell in row.findall(q('tc')))
                lines.append(Line('row', ' | '.join(c for c in cells if c), cells=cells))
    return lines
