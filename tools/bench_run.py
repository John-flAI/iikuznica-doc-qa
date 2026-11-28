# -*- coding: utf-8 -*-
"""Замеры стенда на банке вопросов (bench/bank.json): маршрут, уточнения, токены, секунды, судья, опорный факт.

Запуск: python tools/bench_run.py http://127.0.0.1:8087 [--only A,B] [--id A01,D04] [--out bench/out] [--model <id>]
       python tools/bench_run.py --report bench/out/замеры.json        отчёт по готовому прогону, без обращений к модели
Ожидания — в самом банке; здесь только прогон и арифметика. Наполняет базу стенда вопросами.
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]                # корень репозитория
OUT = ROOT / 'bench' / 'out'
DECLINE = 'Нет, к проектной деятельности это не относится.'


_CALLS = [0]
_ITEMS = [0]
_ADDRESS = ['']          # адрес текущего вопроса банка: владелец на стенде — по адресу (изоляция П3)


def call(base, path, data=None):
    _CALLS[0] += 1
    # один адрес на весь вопрос банка: его уточнения и письма живут в одной системе, чужие — не подмешиваются
    # (заголовок доверяет последний адрес; запрос идёт с самого сервера, минуя прокси)
    headers = {'Content-Type': 'application/json'}
    if _ADDRESS[0]:
        headers['X-Forwarded-For'] = _ADDRESS[0]
    request = urllib.request.Request(base + path, data=json.dumps(data).encode('utf-8') if data is not None else None,
                                     headers=headers)
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.loads(response.read().decode('utf-8'))


def norm(text):
    return re.sub(r'\s+', ' ', (text or '').lower().replace('ё', 'е').replace('\u00a0', ' '))


def option_text(option):
    return option['text'] if isinstance(option, dict) else str(option)


def tally(values):
    """Счётчик: значение → сколько раз (пустые не считаем)."""
    counts = {}
    for value in values:
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


MODEL = None                      # id модели из каталога стенда (--model)


def run_item(base, item):
    _ITEMS[0] += 1                                   # у каждого вопроса банка — свой адрес-сотрудник
    _ADDRESS[0] = '127.%d.%d.1' % (_ITEMS[0] // 250, _ITEMS[0] % 250)
    sid = call(base, '/api/session', {'model': MODEL} if MODEL else {})['id']   # модель — по --model (переключатель)
    text, rounds, final = item['вопрос'], [], {}
    for _ in range(4):                                   # вопрос + не более трёх уточнений
        final = call(base, '/api/ask', {'session_id': sid, 'text': text})
        rounds.append({'question': text, 'route': final.get('route'), 'tokens': final.get('tokens'),
                       'seconds': final.get('seconds'), 'judge': (final.get('checks') or {}).get('judge'),
                       'text': final.get('text'), 'asked': (final.get('clarify') or {}).get('question'),
                       'models': final.get('models') or [], 'attempts': sum(r.get('attempts', 0) for r in (final.get('usage') or {}).get('by_step', {}).values()) if isinstance(final.get('usage'), dict) else None,
                       'sources': [{'uid': s['uid'], 'address': s['address'], 'quote': s['quote']}
                                   for s in final.get('sources') or []]})
        if final.get('route') != 'clarify':
            break
        options = (final.get('clarify') or {}).get('options') or []
        if item['группа'] == 'C':
            text = DECLINE
        elif item.get('choose'):
            hit = [option_text(o) for o in options if item['choose'].lower() in option_text(o).lower()]
            if not hit:
                rounds[-1]['note'] = 'вариант «%s» среди кнопок не найден: %s' % (
                    item['choose'], [option_text(o) for o in options])
                break
            text = hit[0]
        else:
            break
    return rounds, final


def verdict(item, rounds, final):
    expect, last = item['expect'], final.get('route')
    if expect == 'any':
        route_ok = True
    elif item['группа'] == 'C':
        route_ok = last in ('not_found', 'unresolved')
    elif expect == 'clarify':
        route_ok = rounds[0]['route'] == 'clarify' and last == item.get('then', 'answer')
    else:
        route_ok = last == expect
    haystack = norm(' '.join([final.get('text') or ''] + [s['quote'] for s in final.get('sources') or []]))
    missing = [fact for fact in item.get('must') or [] if norm(fact) not in haystack]
    return route_ok, missing


def report(rows, meta, out):
    """Замеры → замеры.md. Возвращает число расхождений."""
    graded = [r for r in rows if not r['probe']]
    groups = {g: [r for r in graded if r['группа'] == g] for g in sorted({r['группа'] for r in graded})}
    tokens = sorted(r['tokens'] for r in graded)
    seconds = [r['seconds'] for r in graded]

    def avg(values):
        return round(sum(values) / len(values), 1) if values else 0

    judges, routes = tally(r['judge'] for r in graded), tally(r['route'] for r in graded)
    models = tally(m for r in rows for step in r['rounds'] for m in step.get('models') or [])
    by_group = '; '.join('%s: %d/%d' % (g, sum(r['verdict'] == 'OK' for r in groups[g]), len(groups[g])) for g in groups)
    lines = ['# Замеры стенда на банке вопросов', '',
             'Стенд: %s · модель %s · эмбеддинги %s · %d регламента · %s МСК.' % (
                 meta['stand'], meta['model'], meta['embeddings'], meta['regulations'], meta['date']),
             'Банк — `%s`; полный вывод ответов — `замеры.json`.' % meta['bank'], '',
             '## Итог', '',
             '- Зачётных вопросов: **%d**, сошлось **%d** (%s); проб с нестрогим ожиданием — %d.' % (
                 len(graded), sum(r['verdict'] == 'OK' for r in graded), by_group, len(rows) - len(graded)),
             '- Токенов на вопрос: в среднем %s, медиана %s, максимум %s.' % (
                 avg(tokens), tokens[len(tokens) // 2] if tokens else 0, tokens[-1] if tokens else 0),
             '- Секунд модели на вопрос: в среднем %s, максимум %s.' % (avg(seconds), max(seconds) if seconds else 0),
             '- Модели, которые реально отвечали (по кругам): %s.' % (', '.join('%s — %d кругов' % (k, models[k]) for k in sorted(models, key=lambda k: -models[k])) or '—'),
             '- Проверка текста судьёй: %s.' % ', '.join('%s — %d' % (k, judges[k]) for k in sorted(judges)),
             '- Итоговые маршруты: %s.' % ', '.join('%s — %d' % (k, routes[k]) for k in sorted(routes)),
             '- Уточняющих вопросов агент задал: %d (вопросов с уточнением — %d из %d).' % (
                 sum(r['clarifications'] for r in graded), sum(r['clarifications'] > 0 for r in graded), len(graded)),
             '- Затрачено на прогон: %d токенов, %.1f мин.' % (sum(r['tokens'] for r in rows), meta['minutes']), '',
             '## Вопросы', '',
             '| № | Вопрос | Ожидание | Получено | Уточнений | Судья | Токенов | Секунд | Итог |',
             '|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append('| %s | %s | %s | %s | %d | %s | %d | %s | **%s** |' % (
            r['id'], r['вопрос'], r['expect'] + ('→' + r['then'] if r.get('then') else ''), r['route'],
            r['clarifications'], r['judge'] or '—', r['tokens'], str(r['seconds']).replace('.', ','), r['verdict']))
    failed = [r for r in graded if r['verdict'] != 'OK']
    lines += ['', '## Расхождения — разбор', '']
    if not failed:
        lines.append('Расхождений нет.')
    for r in failed:
        why = []
        if not r['route_ok']:
            why.append('маршрут «%s», ожидался «%s»' % (r['route'], r['expect']))
        if r['missing']:
            why.append('в ответе и цитатах нет: ' + ', '.join('«%s»' % m for m in r['missing']))
        lines += ['**%s.** %s' % (r['id'], r['вопрос']), '',
                  '- Круги: ' + ' → '.join('%s (%s ток.)' % (x['route'], x['tokens']) for x in r['rounds']),
                  '- Причина: ' + '; '.join(why) + '.',
                  '- Ответ: ' + (r['text'] or '—').replace('\n', ' ')]
        for step in r['rounds']:
            if step.get('asked'):
                lines.append('- Уточнение агента: ' + step['asked'])
        for source in r['rounds'][-1]['sources'][:3]:
            lines.append('- Источник: %s · %s · «%s»' % (source['uid'], source['address'], source['quote'][:160]))
        lines.append('')
    probes = [r for r in rows if r['probe']]
    if probes:
        lines += ['## Пробы (ожидание нестрогое)', '']
        for r in probes:
            lines.append('- **%s** %s → %s, %d уточнений, %d ток., %s с. Ответ: %s' % (
                r['id'], r['вопрос'], r['route'], r['clarifications'], r['tokens'],
                str(r['seconds']).replace('.', ','), (r['text'] or '—').replace('\n', ' ')))
    Path(out).mkdir(parents=True, exist_ok=True)
    (Path(out) / 'замеры.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines[lines.index('## Итог'):lines.index('## Вопросы')]))
    return len(failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base', nargs='?', default='http://127.0.0.1:18087')
    ap.add_argument('--only', help='группы через запятую')
    ap.add_argument('--id', help='идентификаторы через запятую')
    ap.add_argument('--out', help='куда писать замеры (по умолчанию — bench/out)')
    ap.add_argument('--bank', help='файл банка (по умолчанию — <out>/банк_вопросов.json)')
    ap.add_argument('--report', help='готовый замеры.json: собрать только отчёт')
    ap.add_argument('--model', help='id модели из каталога стенда — как в переключателе (/api/health: models)')
    a = ap.parse_args()
    global MODEL
    MODEL = a.model
    out = Path(a.out) if a.out else OUT
    bank_file = Path(a.bank) if a.bank else ROOT / 'bench' / 'bank.json'

    if a.report:                                         # отчёт по уже натеклянному файлу — без прогона
        source = Path(a.report)
        rows = json.loads(source.read_text(encoding='utf-8'))
        if bank_file.exists():                           # ожидания в банке могли уточнить — вердикты пересчитываем
            items = {i['id']: i for i in json.loads(bank_file.read_text(encoding='utf-8'))['вопросы']}
            for r in rows:
                item = items.get(r['id'])
                if not item:
                    continue
                r['probe'], r['expect'] = bool(item.get('probe')), item['expect']
                r['route_ok'], r['missing'] = verdict(item, r['rounds'], {
                    'route': r['route'], 'text': r['text'], 'sources': r['rounds'][-1]['sources']})
                r['verdict'] = 'ПРОБА' if r['probe'] else 'OK' if r['route_ok'] and not r['missing'] else 'FAIL'
        try:
            health = call(a.base, '/api/health')
        except Exception:                                # стенд недоступен — отчёт всё равно нужен
            health = {}
        meta = {'stand': 'стенд (по готовому прогону)', 'model': health.get('model', '—'), 'embeddings': health.get('embeddings', '—'),
                'regulations': health.get('regulations', 0),
                'date': time.strftime('%d.%m.%Y %H:%M', time.localtime(source.stat().st_mtime + 0)),
                'bank': Path(a.bank or 'bench/bank.json').name, 'minutes': sum(r.get('wall', 0) for r in rows) / 60}
        return 1 if report(rows, meta, out) else 0

    bank = json.loads(bank_file.read_text(encoding='utf-8'))
    items = [i for i in bank['вопросы']
             if (not a.only or i['группа'] in a.only.split(',')) and (not a.id or i['id'] in a.id.split(','))]
    health = call(a.base, '/api/health')
    rows, started = [], time.time()
    for item in items:
        began = time.time()
        rounds, final = run_item(a.base, item)
        route_ok, missing = verdict(item, rounds, final)
        probe = bool(item.get('probe'))
        rows.append({'id': item['id'], 'группа': item['группа'], 'вопрос': item['вопрос'], 'expect': item['expect'],
                     'then': item.get('then'), 'route': final.get('route'), 'first': rounds[0]['route'],
                     'clarifications': len(rounds) - 1, 'tokens': sum(r['tokens'] or 0 for r in rounds),
                     'seconds': round(sum(r['seconds'] or 0 for r in rounds), 1),
                     'judge': rounds[-1]['judge'], 'source': (final.get('sources') or [{}])[0].get('uid', ''),
                     'route_ok': route_ok, 'missing': missing, 'probe': probe,
                     'verdict': 'ПРОБА' if probe else 'OK' if route_ok and not missing else 'FAIL',
                     'text': final.get('text'), 'rounds': rounds, 'wall': round(time.time() - began, 1)})
        out.mkdir(parents=True, exist_ok=True)
        (out / 'замеры.json').write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding='utf-8')
        print('%s %s · %s · уточнений %d · %s ток. · %s с · %s' % (
            rows[-1]['verdict'], item['id'], rows[-1]['route'], len(rounds) - 1, rows[-1]['tokens'],
            rows[-1]['seconds'], '' if route_ok and not missing else 'расхождение: %s %s' % (
                '' if route_ok else 'маршрут %s (ожидался %s)' % (rows[-1]['route'], item['expect']),
                ('нет фактов: ' + ', '.join(missing)) if missing else '')), flush=True)
    meta = {'stand': 'стенд (внутри сервера)' if ':8087' in a.base else a.base,
            'model': health.get('model'), 'embeddings': health.get('embeddings'), 'regulations': health.get('regulations', 0),
            'date': time.strftime('%d.%m.%Y %H:%M', time.gmtime(time.time() + 3 * 3600)),
            'bank': bank_file.name, 'minutes': (time.time() - started) / 60}
    failed = report(rows, meta, out)
    print('файлы:', out / 'замеры.md', '·', out / 'замеры.json')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
