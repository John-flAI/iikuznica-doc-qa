# -*- coding: utf-8 -*-
"""Поиск по смыслу: открытая модель эмбеддингов через OpenAI-совместимый сервер.

Индекс строится один раз по всем атомам и хранится на диске; при работе модель нужна
только чтобы посчитать вектор вопроса. Используется BGE-M3 (MIT): свободно ставится
локально и допускает локальную эксплуатацию — требование организаторов к embeddings.
"""
import json
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np


class Embedder:
    """/v1/embeddings: работает и с локальным сервером (Ollama), и с облаком."""

    def __init__(self, base_url, model, api_key=None, timeout=120):
        self.url = base_url.rstrip('/') + '/embeddings'
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _call(self, texts):
        headers = {'Content-Type': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        req = urllib.request.Request(self.url, data=json.dumps({'model': self.model, 'input': texts}).encode('utf-8'),
                                     headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=self.timeout) as response:
            data = json.loads(response.read().decode('utf-8'))['data']
        return [item['embedding'] for item in sorted(data, key=lambda item: item['index'])]

    def embed(self, texts, batch=32):
        out = []
        for at in range(0, len(texts), batch):
            out.extend(self._call(texts[at:at + batch]))
        return out


class VectorIndex:
    """Единичные векторы атомов; поиск — косинусное сходство."""

    def __init__(self, uids, matrix):
        if len(uids) != len(matrix):
            raise ValueError('число векторов не совпадает с числом атомов')
        self.uids = list(uids)
        self.matrix = np.asarray(matrix, dtype=np.float32)
        self.matrix /= np.linalg.norm(self.matrix, axis=1, keepdims=True) + 1e-12

    @classmethod
    def build(cls, units, embedder):
        vectors = embedder.embed([unit.search_text for unit in units])
        return cls([unit.uid for unit in units], np.array(vectors, dtype=np.float32))

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(path, matrix=self.matrix, uids=np.array(self.uids))

    @classmethod
    def load(cls, path):
        with np.load(Path(path), allow_pickle=False) as data:
            return cls(data['uids'].tolist(), data['matrix'])

    def append(self, uid, vector):
        """Новый атом (ответ службы качества) — в общую матрицу."""
        row = np.asarray(vector, dtype=np.float32)
        self.uids.append(uid)
        self.matrix = np.vstack([self.matrix, row / (np.linalg.norm(row) + 1e-12)])

    def search(self, vector, k=30):
        query = np.asarray(vector, dtype=np.float32)
        query /= np.linalg.norm(query) + 1e-12
        similarity = self.matrix @ query
        top = np.argsort(-similarity)[:k]
        return [(self.uids[i], float(similarity[i])) for i in top]
