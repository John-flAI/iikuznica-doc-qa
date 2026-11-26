# -*- coding: utf-8 -*-
"""Служба: склеивает агента, хранилище и базу знаний — то, что видят чат и служба качества.

Диалог: вопрос → агент → ответ, уточнение или запрос в службу качества. Если ответа нет,
в чате появляется готовое письмо и кнопка «Отправить». Сотрудник службы качества отвечает —
ответ попадает в чат сотрудника и в базу знаний. Ответ службы качества главнее регламента
(так ответил заказчик: регламент может быть в актуализации).
"""
import json
import time

from .agent import KNOWLEDGE, Agent
from .ingest import SECTION, TEXT, Unit

KB_TITLE = 'База знаний: ответы службы качества'

EXAMPLES = [
    'Сколько заявок может быть у одного ПА?',
    'Как часто надо обновлять реестр рисков?',
    'Может ли РП начать проект, если договор ещё не подписан?',
    'Кто готовит ТКП, если заказчик уже выбрал продукт?',
]

NOTICES = {
    'not_found': 'В регламентах нет ответа на этот вопрос, поэтому я его не выдумываю. '
                 'Подготовил письмо в службу качества.',
    'unresolved': 'Дать однозначный ответ не получилось: в регламентах несколько вариантов, '
                  'и уточнения не сняли различие. Подготовил письмо в службу качества.',
    'degraded': 'Модель временно недоступна. Показываю найденные пункты без обработки модели — '
                'проверьте их самостоятельно или повторите вопрос через минуту.',
}


def letter(ticket):
    """Готовое письмо в службу качества: вопрос, что нашлось, просьба ответить."""
    lines = ['Тема: нет ответа в регламентах — запрос №%d' % ticket['id'], '',
             'Здравствуйте, служба качества!', '',
             'Сотрудник задал вопрос, ответа на который в регламентах не нашлось:', '',
             '«%s»' % ticket['question']]
    if ticket['context']:
        lines += ['', 'Что нашлось по теме:', ticket['context']]
    lines += ['', 'Прошу дать ответ — он будет добавлен в базу знаний агента.']
    return '\n'.join(lines)


def kb_unit(ticket):
    """Запись базы знаний — такой же искомый атом, как пункт регламента."""
    return Unit('%s:%d' % (KNOWLEDGE, ticket['id']), KNOWLEDGE, '', ticket['answer'], 'Ответ службы качества',
                (('Ответ службы качества, %s, %s' % (ticket['answered_by'], (ticket['answered_at'] or '')[:10]), '',
                  SECTION),
                 ('Вопрос сотрудника: %s' % ticket['question'], '', 'label')),
                TEXT)


class Service:
    def __init__(self, engine, llm, store):
        self.engine, self.llm, self.store = engine, llm, store
        self.agent = Agent(engine, llm)
        for ticket in store.tickets('answered'):      # база знаний переживает перезапуск
            self.learn(ticket)

    # ---- база знаний ----

    def learn(self, ticket):
        unit = kb_unit(ticket)
        if self.engine.corpus.get(unit.uid) is None:          # повторный запуск — запись уже в индексе
            self.engine.corpus.add_document(KNOWLEDGE, KB_TITLE)
            self.engine.add(unit)
        return unit

    # ---- чат сотрудника ----

    def ask(self, session_id, text):
        text = ' '.join((text or '').split())
        if not text:
            raise ValueError('Вопрос пустой')
        session_id = self.store.session(session_id)['id']     # новый идентификатор, если сессии не было
        self.store.add_message(session_id, 'user', text)
        question, history = self.thread(session_id, text)
        started = time.perf_counter()
        decision = self.agent.ask(question['question'], [tuple(h) for h in history])
        message = self.respond(session_id, question, history, decision, time.perf_counter() - started)
        self.store.add_message(session_id, 'agent', message.get('text', ''), meta=message)
        return message

    def thread(self, session_id, text):
        """Продолжаем открытый вопрос (это ответ на уточнение) или начинаем новый."""
        opened = self.store.open_question(session_id)
        if opened:
            meta = json.loads(opened['meta'] or '{}')
            if meta.get('awaiting'):
                chosen = next((o for o in meta.get('options', []) if o['text'] == text), None)
                answer = [meta['awaiting'], text] + ([chosen['doc']] if chosen and chosen.get('doc') else [])
                return opened, meta.get('clarifications', []) + [answer]
            self.store.touch_question(opened['id'], route='abandoned')   # уточнение осталось без ответа
        return {'id': self.store.start_question(session_id, text), 'question': text}, []

    def respond(self, session_id, question, history, decision, seconds):
        for reply in decision.replies:
            self.store.log_llm(reply, 'decide', session_id, question['id'])
        route = decision.route
        message = {'role': 'agent', 'route': route, 'tokens': decision.tokens, 'seconds': round(seconds, 1),
                   'clarifications': len(history)}
        common = dict(reply=decision.replies[-1] if decision.replies else None, turns=decision.turns,
                      clarifications=history)
        if route == 'answer':
            message.update(text=decision.why, sources=[self.source(c[0]) for c in decision.cites])
            message['priority'] = any(s['doc'] == KNOWLEDGE for s in message['sources'])
            message['conflict'] = message['priority'] and any(s['doc'] != KNOWLEDGE for s in message['sources'])
            self.store.touch_question(question['id'], route=route, uids=[s['uid'] for s in message['sources']],
                                      awaiting=None, **common)
        elif route == 'clarify':
            message.update(text=decision.question, clarify={
                'question': decision.question,
                'options': [{'text': o, 'doc': decision.option_docs.get(o)} for o in decision.options]})
            self.store.touch_question(question['id'], route=None, awaiting=decision.question,
                                      options=message['clarify']['options'], **common)
        elif route == 'degraded':
            message.update(text=NOTICES[route], notice=decision.error, retry=True,
                           sources=[self.source(c[0]) for c in decision.cites])
            self.store.touch_question(question['id'], route=route, awaiting=None, **common)
        else:
            sources = [self.source(c[0]) for c in decision.cites]
            context = '\n'.join('• %s — %s: «%s»' % (s['doc_title'], s['address'], s['quote']) for s in sources)
            ticket_id = self.store.create_ticket(session_id, question['id'], question['question'], context)
            message.update(text=NOTICES.get(route, NOTICES['not_found']), sources=sources,
                           ticket={'id': ticket_id, 'letter': letter(self.store.ticket(ticket_id))})
            self.store.touch_question(question['id'], route=route, uids=[s['uid'] for s in sources],
                                      ticket_id=ticket_id, awaiting=None, **common)
        return message

    def source(self, unit):
        """Источник так, как его показывает интерфейс."""
        address = unit.address
        if unit.doc == KNOWLEDGE:
            address = address.replace(' → подпункт', ' · ').replace('Вопрос сотрудника', 'Вопрос').rstrip('.')
        return {'uid': unit.uid, 'doc': unit.doc, 'doc_title': self.engine.corpus.title(unit.doc),
                'address': address, 'quote': unit.quote}

    # ---- служба качества ----

    def send(self, ticket_id):
        ticket = self.store.send_ticket(ticket_id)
        self.store.add_message(ticket['session_id'], 'system',
                               'Письмо отправлено в службу качества, запрос №%d.' % ticket['id'],
                               meta={'role': 'system', 'route': 'sent', 'ticket_id': ticket['id']})
        return ticket

    def answer(self, ticket_id, answer, author):
        answer, author = ' '.join((answer or '').split()), ' '.join((author or 'Служба качества').split())
        if not answer:
            raise ValueError('Ответ пустой')
        ticket = self.store.answer_ticket(ticket_id, answer, author)
        unit = self.learn(ticket)
        self.store.add_message(ticket['session_id'], 'qc', answer,
                               meta={'role': 'qc', 'route': 'qc', 'ticket_id': ticket['id'],
                                     'author': author, 'sources': [self.source(unit)]})
        return ticket

    def document(self, doc_id):
        """Регламент для окна документов: заголовки разделов и абзацы в порядке документа."""
        info = self.engine.corpus.docs.get(doc_id)
        if not info:
            raise KeyError(doc_id)
        blocks = [{'uid': u.uid, 'kind': u.kind, 'number': u.number, 'label': u.label,
                   'text': u.title or u.text, 'address': u.address}
                  for u in self.engine.corpus.units if u.doc == doc_id]
        return {'doc': doc_id, 'title': info['title'], 'blocks': blocks}

    # ---- статистика ----

    def analytics(self, days=None):
        return self.store.analytics(days)

    def weekly(self, days=7):
        """Что внести в документацию или обновить в регламенте: частые вопросы без ответа."""
        unanswered = self.store.analytics(days)['unanswered']
        if not unanswered:
            return {'days': days, 'proposals': [], 'text': 'Предложений нет.'}
        lines = ['Вопросы без ответа за %d дн.:' % days]
        lines += ['%d. %s — спросили %d раз(а)' % (i, row['question'], row['n'])
                  for i, row in enumerate(unanswered, 1)]
        return {'days': days, 'proposals': unanswered, 'text': '\n'.join(lines)}
