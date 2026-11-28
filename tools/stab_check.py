# -*- coding: utf-8 -*-
"""Устойчивость ответов стенда: выбранные вопросы банка гоняются N раундов, по каждому — распределение первого
и итогового маршрута и доля совпадений с эталоном (модель от прогона к прогону меняет решение на 1–4 вопросах).

Запуск: python tools/stab_check.py http://127.0.0.1:8087 --rounds 6 --only B --tz [--out bench/out] [--model <id>]
       --tz — дословный пример задания к кейсу 2 с развилкой: после уточнения перебираются все варианты-регламенты.
Выход: <out>/stab.json и <out>/stab.md. Наполняет базу стенда вопросами.
"""
import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_run as bench                                        # noqa: E402  call, run_item, verdict, norm, option_text

def tz_must(option):
    """Опорный факт для ветки дословного примера из задания: ДБРЦТ — «3 рабочих дня», Организация проектной
    деятельности — «за 2 недели»; в остальных регламентах срока нет — честный итог любой (ответ или обоснованный отказ)."""
    lowered = option.lower()
    if 'дбрцт' in lowered:
        return '3 рабочих дня'
    return '2 недел' if 'организация проектной деятельности' in lowered else None


TZ_SPLIT = {'id': 'T2', 'вопрос': 'За сколько до подачи нужно инициировать заявку на расчёт?'}   # дословный пример кейса 2


def new_address():
    bench._ITEMS[0] += 1
    bench._ADDRESS[0] = '127.%d.%d.1' % (bench._ITEMS[0] // 250, bench._ITEMS[0] % 250)


def split_run(base, spec, model):
    """Дословный пример ТЗ: вопрос → уточнение → каждая ветка-регламент в своей сессии."""
    new_address()
    sid = bench.call(base, '/api/session', {'model': model} if model else {})['id']
    first = bench.call(base, '/api/ask', {'session_id': sid, 'text': spec['вопрос']})
    result = {'first': first.get('route'), 'asked': (first.get('clarify') or {}).get('question'), 'branches': []}
    if first.get('route') != 'clarify':
        result['text'] = first.get('text')
        return result
    docs = {}
    for option in (first.get('clarify') or {}).get('options') or []:
        if option.get('doc'):
            docs[option['doc']] = option['text']
    for doc, option in docs.items():
        new_address()
        sid = bench.call(base, '/api/session', {'model': model} if model else {})['id']
        again = bench.call(base, '/api/ask', {'session_id': sid, 'text': spec['вопрос']})
        row = {'doc': doc, 'option': option}
        hit = next((o.get('text') for o in (again.get('clarify') or {}).get('options') or [] if o.get('doc') == doc), None)
        final = again
        if hit:
            final = bench.call(base, '/api/ask', {'session_id': sid, 'text': hit})
            row['route'] = final.get('route')
        else:
            row['route'] = 'вариант не предложен: %s' % again.get('route')
        row.update(text=final.get('text'), seconds=final.get('seconds'), judge=(final.get('checks') or {}).get('judge'),
                   sources=[s['uid'] for s in final.get('sources') or []])
        haystack = bench.norm(' '.join([final.get('text') or ''] + [s['quote'] for s in final.get('sources') or []]))
        must = tz_must(option)
        found = bench.norm(must) in haystack if must else True
        row['ok'] = row['route'] == 'answer' and found if must else row['route'] in ('answer', 'not_found')
        row['must'] = must or 'любой честный итог'
        result['branches'].append(row)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base', nargs='?', default='http://127.0.0.1:18087')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--only', help='группы банка через запятую')
    ap.add_argument('--id', help='идентификаторы банка через запятую')
    ap.add_argument('--bank', help='файл банка (по умолчанию — bench/bank.json)')
    ap.add_argument('--out', default=str(Path(__file__).resolve().parents[1] / 'bench' / 'out'))
    ap.add_argument('--model', help='id модели каталога стенда (как в /api/health: models)')
    ap.add_argument('--tz', action='store_true', help='добавить дословный пример ТЗ кейса 2 с развилкой')
    a = ap.parse_args()
    bench.MODEL = a.model
    bench._ITEMS[0] = 50000                                   # своё адресное пространство (127.200.x.1): не пересекается с сценариями и аудитом
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    bank_file = Path(a.bank) if a.bank else Path(__file__).resolve().parents[1] / 'bench' / 'bank.json'
    items = [i for i in json.loads(bank_file.read_text(encoding='utf-8'))['вопросы']
             if (not a.only or i['группа'] in a.only.split(',')) and (not a.id or i['id'] in a.id.split(','))]
    health = bench.call(a.base, '/api/health')
    runs, started = [], time.time()
    for round_no in range(1, a.rounds + 1):
        for item in items:
            rounds, final = bench.run_item(a.base, item)
            route_ok, missing = bench.verdict(item, rounds, final)
            probe = bool(item.get('probe'))
            runs.append({'round': round_no, 'id': item['id'], 'группа': item['группа'], 'вопрос': item['вопрос'],
                         'expect': item['expect'], 'first': rounds[0]['route'], 'route': final.get('route'),
                         'ok': route_ok and not missing and not probe, 'route_ok': route_ok, 'missing': missing,
                         'judge': rounds[-1]['judge'], 'seconds': round(sum(r['seconds'] or 0 for r in rounds), 1),
                         'asked': [r['asked'] for r in rounds if r.get('asked')],
                         'text': (final.get('text') or '')[:220], 'sources': [s['uid'] for s in final.get('sources') or []]})
            print('раунд %d · %s%s · %s · %s' % (round_no, item['id'], '' if runs[-1]['ok'] else ' ✗', runs[-1]['route'],
                                                  runs[-1]['text'][:90] if not runs[-1]['ok'] else ''), flush=True)
        if a.tz:
            split = split_run(a.base, TZ_SPLIT, a.model)
            runs.append({'round': round_no, 'id': TZ_SPLIT['id'], 'группа': 'T', 'вопрос': TZ_SPLIT['вопрос'],
                         'expect': 'clarify', 'first': split['first'], 'route': '/'.join(b['route'] for b in split['branches']),
                         'ok': split['first'] == 'clarify' and bool(split['branches']) and all(b['ok'] for b in split['branches']),
                         'route_ok': split['first'] == 'clarify', 'missing': [], 'judge': '', 'text': '', 'sources': [],
                         'seconds': round(sum(b.get('seconds') or 0 for b in split['branches']), 1), 'split': split})
            print('раунд %d · %s%s · %s' % (round_no, TZ_SPLIT['id'], '' if runs[-1]['ok'] else ' ✗', runs[-1]['route']), flush=True)
        (out / 'stab.json').write_text(json.dumps({'base': a.base, 'model': a.model or health.get('model'),
                                                   'rounds': round_no, 'runs': runs}, ensure_ascii=False, indent=1),
                                       encoding='utf-8')

    lines = ['# Устойчивость: %d раунд(а) · %s · модель %s · %.1f мин' % (
        a.rounds, a.base, a.model or health.get('model'), (time.time() - started) / 60), '',
             '| id | ожидание | первые маршруты | итоговые маршруты | сошлось | ср. с |', '|---|---|---|---|---|---|']
    order = [i['id'] for i in items] + ([TZ_SPLIT['id']] if a.tz else [])
    total_ok = total = 0
    for name in order:
        mine = [r for r in runs if r['id'] == name]
        ok = sum(r['ok'] for r in mine)
        total_ok += ok if name != TZ_SPLIT['id'] else 0
        total += len(mine) if name != TZ_SPLIT['id'] else 0
        lines.append('| %s | %s | %s | %s | **%d/%d** | %s |' % (
            name, mine[0]['expect'] if mine[0]['expect'] != 'any' else '—',
            ', '.join('%s×%d' % kv for kv in Counter(r['first'] for r in mine).most_common()),
            ', '.join('%s×%d' % kv for kv in Counter(r['route'] for r in mine).most_common()),
            ok, len(mine), round(sum(r['seconds'] or 0 for r in mine) / len(mine), 1)))
    lines += ['', 'ИТОГ: %d из %d (%.0f %%)' % (total_ok, total, 100.0 * total_ok / max(total, 1)), '', '## Расхождения', '']
    for r in runs:
        if r['ok']:
            continue
        if r.get('split'):
            for b in r['split']['branches']:
                if not b['ok']:
                    lines.append('- раунд %d · %s · ветка «%s» → %s (нужно «%s») · %s' % (
                        r['round'], r['id'], b['option'][:50], b['route'], b['must'], (b.get('text') or '')[:160]))
            if r['split']['first'] != 'clarify':
                lines.append('- раунд %d · %s · без уточнения → %s · %s' % (
                    r['round'], r['id'], r['split']['first'], (r['split'].get('text') or '')[:160]))
        else:
            why = [] if r['route_ok'] else ['маршрут %s' % r['route']]
            if r['missing']:
                why.append('нет: ' + ', '.join(r['missing']))
            if r['expect'] == 'any':
                why.append('проба')
            lines.append('- раунд %d · %s · %s · %s' % (r['round'], r['id'], '; '.join(why) or 'ok?', r['text'][:160]))
    (out / 'stab.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))
    return 0


if __name__ == '__main__':
    sys.exit(main())
