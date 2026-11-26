# -*- coding: utf-8 -*-
# <@паспорт файл="tools/build_terms.py" дата="2026-11-26">
# @что: собрать data/terms.json — словарь терминов для поиска — из глоссария заказчика
#   (data/glossary.tsv) и дополнительных форм (data/terms_extra.json).
# @запуск: python tools/build_terms.py data/
# </@паспорт>
"""Глоссарий → словарь терминов. Каждая запись: аббревиатура → формы расшифровки."""
import json
import sys
from pathlib import Path


def build(data_dir):
    data_dir = Path(data_dir)
    terms = {}
    source = 'Глоссарий задачи №1 (чат задачи, организатор, 26.11)'
    for line in (data_dir / 'glossary.tsv').read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        left, _, right = line.partition('\t')
        for abbr in (a.strip() for a in left.split(',')):
            if abbr and right.strip():
                terms[abbr] = {'forms': [right.strip()], 'source': source}
    for abbr, spec in json.loads((data_dir / 'terms_extra.json').read_text(encoding='utf-8')).items():
        terms.setdefault(abbr, {'forms': [], 'source': spec['source']})
        terms[abbr]['forms'] += [f for f in spec['forms'] if f not in terms[abbr]['forms']]
    out = data_dir / 'terms.json'
    out.write_text(json.dumps(terms, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return len(terms), out


if __name__ == '__main__':
    count, path = build(sys.argv[1] if len(sys.argv) > 1 else 'data')
    print('терминов:', count, '→', path)
