# -*- coding: utf-8 -*-
"""Лексический поиск по атомам: леммы, аббревиатуры, словарь терминов, ранжирование BM25.

Зачем несколько слоёв в токене:
  лемма        — «заявки», «заявку», «заявка» должны совпасть с вопросом в любой форме;
  основа       — глагол и существительное от одного корня: «назначили» и «назначения» → «назнач»;
  аббревиатура — «ПА», «РП», «ТКП» не приводятся к лемме, иначе «ПА» станет предлогом «па»;
  словарь      — «РП» и «руководитель проекта» это одно и то же (глоссарий заказчика).
"""
import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .ingest import CITABLE

try:
    import pymorphy3
except ImportError:            # без лемматизатора поиск работает, но различает формы слов
    pymorphy3 = None

WORD = re.compile(r'[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9\-]*')
ABBR = re.compile(r'^[A-ZА-ЯЁ0-9][A-ZА-ЯЁ0-9\-]*[A-ZА-ЯЁ0-9]$')

STOP = set('''и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только её ее мне
вот от меня ещё еще нет о из ему или ни быть был была были было вас вам ведь там потом себя ничего они тут
где надо для мы их чем сам чтоб чтобы раз тоже себе ж тогда кто этот этого эта эти эту который которая
которые которых какой какая какие каких когда куда зачем почему сколько ли либо нибудь
должен должна должно должны обязан обязана обязано обязаны можно нельзя возможно нужно необходимо'''.split())

PREFIX_MIN = 8      # основа берётся у слов не короче этого
PREFIX_LEN = 6      # длина основы: «назначить» и «назначение» → «назнач»


@dataclass(frozen=True)
class Hit:
    unit: object
    score: float            # слитая оценка каналов
    coverage: float         # доля весов терминов вопроса, найденных в атоме: 0..1
    similarity: float = 0.0  # сходство с вопросом по эмбеддингам: 0..1; 0, если их нет

    @property
    def uid(self):
        return self.unit.uid


def _norm(text):
    """В регламентах пишут «расчет», сотрудник спросит «расчёт» — это одно слово."""
    return text.replace('Ё', 'Е').replace('ё', 'е')


def _find(tokens, phrase):
    """Позиция последовательности лемм phrase в tokens или -1."""
    n = len(phrase)
    return next((i for i in range(len(tokens) - n + 1) if tokens[i:i + n] == phrase), -1)


class Tokenizer:
    """Текст → токены с подстановкой терминов из словаря."""

    def __init__(self, morph=None, terms=None):
        self.morph = morph
        self.rules = [(abbr, [self.lemmas(form) for form in spec.get('forms', [])])
                      for abbr, spec in (terms or {}).items()]
        self._lemma = lru_cache(maxsize=300_000)(self._lemma)

    def _lemma(self, word):
        """Словарная форма тоже может содержать «ё» — нормализуем и её."""
        if self.morph is None:
            return word
        return _norm(self.morph.parse(word)[0].normal_form)

    def lemmas(self, text):
        """Леммы слов текста; числа и аббревиатуры остаются как есть."""
        return [w if w.isdigit() or ABBR.match(w) else self._lemma(w.lower()) for w in WORD.findall(_norm(text))]

    def _apply_terms(self, lemmas):
        """Словарь терминов: форма расшифровки заменяется на аббревиатуру с канонической формой."""
        for abbr, forms in self.rules:
            if abbr in lemmas:
                lemmas += [t for t in forms[0] if t not in lemmas]
                continue
            for form in forms:
                at = _find(lemmas, form)
                if at >= 0:
                    keep = lemmas[:at] + lemmas[at + len(form):]
                    lemmas = keep[:at] + [abbr] + forms[0] + keep[at:]
                    break
        return lemmas

    def tokens(self, text):
        out = []
        for token in self._apply_terms(self.lemmas(text)):
            if token in STOP or len(token) < 2:
                continue
            out.append(token)
            if not ABBR.match(token) and len(token) >= PREFIX_MIN:
                out.append(token[:PREFIX_LEN])
        return out


def load_terms(path):
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def make_morph():
    return pymorphy3.MorphAnalyzer() if pymorphy3 else None


class Search:
    """Два канала — по словам (BM25) и по смыслу (эмбеддинги) — слитые обратными рангами.

    Словами находится точное: номера, сроки, аббревиатуры. Смыслом — вопрос другими
    словами: «взять в работу» находит «принять заявку». Слияние обратных рангов не
    требует настраивать вес каналов.
    """

    k1, b = 1.5, 0.75
    pool = 30               # столько лучших атомов отдаёт каждый канал перед слиянием

    def __init__(self, units, tokenizer, vectors=None, embedder=None):
        self.tokenizer = tokenizer
        self.units = [u for u in units if u.kind in CITABLE]
        self.by_uid = {u.uid: u for u in self.units}
        self.vectors, self.embedder = vectors, embedder
        self.tf = [Counter(tokenizer.tokens(u.search_text)) for u in self.units]
        self.dl = [sum(tf.values()) for tf in self.tf]
        self.avgdl = (sum(self.dl) / len(self.dl)) if self.dl else 0.0
        self.df = Counter(t for tf in self.tf for t in tf)
        self.n = len(self.units)

    def idf(self, token):
        return math.log(1 + (self.n - self.df[token] + 0.5) / (self.df[token] + 0.5))

    def lexical(self, query, k):
        """Словесный канал: сначала атомы, покрывающие больше терминов вопроса."""
        weights = {t: self.idf(t) for t in set(self.tokenizer.tokens(query)) if self.df[t]}
        total = sum(weights.values())
        hits = []
        for i, tf in enumerate(self.tf):
            matched = weights.keys() & tf.keys()
            if not matched:
                continue
            norm = self.k1 * (1 - self.b + self.b * self.dl[i] / (self.avgdl or 1))
            score = sum(weights[t] * tf[t] * (self.k1 + 1) / (tf[t] + norm) for t in matched)
            hits.append(Hit(self.units[i], score, sum(weights[t] for t in matched) / (total or 1)))
        hits.sort(key=lambda h: (-h.coverage, -h.score))
        return hits[:k]

    def search(self, query, k=10):
        """Атомы по убыванию слитой оценки."""
        hits = {}
        for rank, hit in enumerate(self.lexical(query, self.pool), 1):
            hits[hit.uid] = Hit(hit.unit, 1 / (60 + rank), hit.coverage)
        if self.vectors is not None and self.embedder is not None:
            for rank, (uid, similarity) in enumerate(self.vectors.search(self.embedder.embed([query])[0], self.pool), 1):
                found = hits.get(uid)
                weight = 1 / (60 + rank)
                if found:
                    hits[uid] = Hit(found.unit, found.score + weight, found.coverage, similarity)
                else:
                    hits[uid] = Hit(self.by_uid[uid], weight, 0.0, similarity)
        return sorted(hits.values(), key=lambda h: -h.score)[:k]

    def per_document(self, query, k_each=2):
        """Лучшие атомы каждого регламента — так видны разные ответы дирекций на один вопрос."""
        groups = {}
        for hit in self.search(query, k=self.pool * 2):
            if len(groups.setdefault(hit.unit.doc, [])) < k_each:
                groups[hit.unit.doc].append(hit)
        return groups
