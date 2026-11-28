# -*- coding: utf-8 -*-
"""Сквозные сценарии стенда по HTTP — кейсы задания глазами жюри: ответ по регламенту, уточняющий диалог,
«нет ответа» → запрос → служба качества → ответ в чат → база знаний → повторный вопрос → изоляция другого сотрудника.

Запуск: python tools/scenarios.py http://127.0.0.1:8087      печатает маршрут, токены, время и цитаты на каждый шаг.
Снаружи (https://адрес стенда) все сценарии идут с одного адреса — шаг изоляции тогда не показателен.
"""
import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8087'
NO_ANSWER = 'Кто должен подписывать агентский договор?'   # пример заказчика из ТЗ; слова «агентский» в регламентах нет


_CALLS = [0]
_SCENARIO = [0]
_ADDRESS = ['']          # адрес текущего сценария: владелец на стенде — по адресу (изоляция П3)


def next_address():
    """Свой адрес на сценарий: все вызовы сценария — от одного сотрудника, чужая база знаний ему не видна."""
    _SCENARIO[0] += 1
    return '127.%d.%d.1' % (_SCENARIO[0] // 250, _SCENARIO[0] % 250)


def call(path, body=None, method=None):
    _CALLS[0] += 1
    data = json.dumps(body).encode('utf-8') if body is not None else (b'{}' if method == 'POST' else None)
    headers = {'Content-Type': 'application/json'}
    if _ADDRESS[0]:      # один адрес на весь сценарий: и вопрос, и письмо, и ответ службы качества — одна система
        headers['X-Forwarded-For'] = _ADDRESS[0]
    req = urllib.request.Request(BASE + path, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.loads(response.read().decode('utf-8'))


def ask(session, text):
    started = time.perf_counter()
    message = call('/api/ask', {'session_id': session, 'text': text})
    print('  → %s · %s ток. · %.1f с%s' % (message['route'], message['tokens'], time.perf_counter() - started,
                                       ' · ' + message['clarify']['question'] if message.get('clarify') else ''))
    if message.get('text'):
        print('  Агент: %s' % message['text'][:300])
    if message.get('checks'):
        print('  Проверка: %s (редакций: %d)' % (message['checks'].get('judge'), message['checks'].get('attempts', 0)))
    for source in message.get('sources', [])[:1]:
        print('     %s | %s' % (source['doc'], source['address'][:80]))
    return message


def pick(options):
    texts = [o['text'] for o in options]
    return next((t for t in texts if 'Пресейл' in t), texts[0])


def scenario(title, question, expect_last):
    _ADDRESS[0] = next_address()     # у каждого сценария — свой сотрудник со своей чистой системой
    print('\n### %s\n  Сотрудник: %s' % (title, question))
    session = call('/api/session', {})['id']
    message = ask(session, question)
    rounds = 0
    while message['route'] == 'clarify' and message['clarify']['options'] and rounds < 3:
        option = pick(message['clarify']['options'])
        print('  Сотрудник выбирает: %s' % option)
        message = ask(session, option)
        rounds += 1
    ok = message['route'] == expect_last
    print('  %s (ожидалось: %s)' % ('OK' if ok else 'РАСХОЖДЕНИЕ', expect_last))
    return session, message, ok


def main():
    results = []
    results.append(scenario('Кейс 1а — ответ по регламенту', 'Сколько заявок может быть у одного ПА?', 'answer')[2])
    results.append(scenario('Кейс 2 — разные регламенты', 'Как часто надо обновлять реестр рисков?', 'answer')[2])
    results.append(scenario('Кейс 2 — условие в вопросе', 'Кто готовит ТКП, если заказчик уже выбрал продукт?', 'answer')[2])

    print('\n### Кейс 1б → 3 — нет ответа, служба качества, база знаний\n  Сотрудник: %s' % NO_ANSWER)
    _ADDRESS[0] = next_address()     # сотрудник и отвечающая служба качества — один адрес (одна демо-система)
    session = call('/api/session', {})['id']
    message = ask(session, NO_ANSWER)
    rounds = 0
    while message['route'] == 'clarify' and rounds < 3:      # сотрудник стоит на своём: именно агентский
        message = ask(session, 'Именно агентский договор — не договор с заказчиком и не с субподрядчиком.')
        rounds += 1
    has_ticket = 'ticket' in message
    print('  %s (ожидалось: письмо с кнопкой «Отправить»)' % ('OK' if has_ticket else 'РАСХОЖДЕНИЕ'))
    results.append(has_ticket)
    if not has_ticket:
        return
    ticket = message['ticket']['id']
    call('/api/tickets/%d/send' % ticket, method='POST')
    print('  Письмо отправлено, запрос №%d — в очереди службы качества' % ticket)
    call('/api/tickets/%d/answer' % ticket, {'answer': 'Агентский договор от лица компании подписывает заместитель директора по правовым вопросам;'
           ' проект доверенности заранее согласуется с юридическим отделом.', 'author': 'Иван'})
    history = call('/api/session/' + session)['messages']
    delivered = [m for m in history if m['role'] == 'qc']
    print('  %s (ожидалось: ответ службы качества в чате сотрудника)' % ('OK' if delivered else 'РАСХОЖДЕНИЕ'))
    results.append(bool(delivered))

    print('\n### Кейс 3 — повторный вопрос того же сотрудника после ответа службы качества')
    repeat = ask(session, NO_ANSWER)
    rounds = 0
    while repeat['route'] == 'clarify' and repeat['clarify']['options'] and rounds < 3:
        repeat = ask(session, pick(repeat['clarify']['options']))
        rounds += 1
    used = repeat['route'] == 'answer' and any(s['doc'] == 'kb' for s in repeat['sources'])
    print('  %s (ожидалось: ответ из базы знаний — запись своя)' % ('OK' if used else 'РАСХОЖДЕНИЕ'))
    results.append(used)

    print('\n### Кейс 4 — изоляция: другой сотрудник видит чистую систему\n  Другой сотрудник: %s' % NO_ANSWER)
    _ADDRESS[0] = next_address()
    other = call('/api/session', {})['id']
    again = ask(other, NO_ANSWER)
    clean = again['route'] == 'not_found' and 'ticket' in again \
        and not any(s['doc'] == 'kb' for s in again.get('sources') or [])
    print('  %s (ожидалось: чужой ответ базы знаний не подмешался — готовится своё письмо)' % ('OK' if clean else 'РАСХОЖДЕНИЕ'))
    if not clean:                                      # диагностика расхождения: что реально вернул агент другому сотруднику
        print('    факт: route=%s · sources=%s · ticket=%s · %s' % (
            again.get('route'), [s.get('doc') for s in again.get('sources') or []], 'ticket' in again,
            (again.get('notice') or again.get('text') or '')[:120]))
    results.append(clean)

    report = call('/api/analytics')     # статистика последнего сотрудника: у каждого адреса она своя
    print('\nИТОГО: %d из %d · вопросов у последнего сотрудника: %d · маршруты: %s' % (
        sum(results), len(results), report['questions'], report['routes']))


if __name__ == '__main__':
    main()
