# -*- coding: utf-8 -*-
"""Сборка движка: корпус → токенизатор → поиск (словесный + векторный).

Запуск: python -m app.engine            построить векторный индекс, если его нет или он устарел.
"""
import hashlib
import re
import sys

from . import config
from .ingest import CITABLE, load_corpus
from .search import Search, Tokenizer, load_terms, make_morph
from .vectors import Embedder, VectorIndex


class Engine:
    """Корпус и поиск по нему — общая часть для агента, веб-слоя и тестов."""

    def __init__(self, corpus, search, embedder=None):
        self.corpus = corpus
        self.search = search
        self.embedder = embedder

    @property
    def tokenizer(self):
        return self.search.tokenizer

    def add(self, unit):
        """Новый атом во все индексы — на лету, без пересборки."""
        self.corpus.add(unit)
        self.search.add([unit])
        if self.search.vectors is not None and self.embedder is not None:
            self.search.vectors.append(unit.uid, self.embedder.embed([unit.search_text])[0])


def corpus_digest(corpus):
    """Отпечаток корпуса: по нему видно, что векторный индекс устарел."""
    digest = hashlib.sha1()
    for unit in corpus.units:
        digest.update(unit.uid.encode('utf-8'))
        digest.update(unit.search_text.encode('utf-8'))
    return digest.hexdigest()


def index_path(digest):
    name = re.sub(r'[^A-Za-z0-9._-]+', '_', config.EMBED_MODEL)
    return config.DATA_DIR / 'index' / ('%s.%s.npz' % (name, digest[:12]))


def load_engine(data_dir=None, with_vectors=True):
    data_dir = config.DATA_DIR if data_dir is None else data_dir
    corpus = load_corpus(data_dir)
    tokenizer = Tokenizer(make_morph(), load_terms(data_dir / 'terms.json'))
    embedder, vectors = None, None
    if with_vectors:
        embedder = Embedder(config.EMBED_BASE_URL, config.EMBED_MODEL, config.EMBED_API_KEY)
        path = index_path(corpus_digest(corpus))
        vectors = VectorIndex.load(path) if path.exists() else build_vectors(corpus, embedder, path)
    return Engine(corpus, Search(corpus.units, tokenizer, vectors, embedder), embedder)


def build_vectors(corpus, embedder, path=None):
    """Построить и сохранить векторный индекс по текущему корпусу."""
    path = path or index_path(corpus_digest(corpus))
    vectors = VectorIndex.build([u for u in corpus.units if u.kind in CITABLE], embedder)
    vectors.save(path)
    return vectors


if __name__ == '__main__':
    engine = load_engine(with_vectors=False)
    target = index_path(corpus_digest(engine.corpus))
    if target.exists():
        print('индекс актуален:', target)
    else:
        build_vectors(engine.corpus, Embedder(config.EMBED_BASE_URL, config.EMBED_MODEL, config.EMBED_API_KEY), target)
        print('индекс построен:', target)
    sys.exit(0)
