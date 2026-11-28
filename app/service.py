# -*- coding: utf-8 -*-
"""Служба: склеивает агента, хранилище и базу знаний — то, что видят чат и служба качества.

Диалог: вопрос → агент → ответ, уточнение или запрос в службу качества. Если ответа нет,
в чате появляется готовое письмо и кнопка «Отправить». Сотрудник службы качества отвечает —
ответ попадает в чат сотрудника и в базу знаний. Регламент — утверждённый документ, он главнее;
ответ службы качества — консультация о том, как применять его нормы (позиция заказчика, Q&A 27.11).
"""
import json
import time

from . import config, mail
from .agent import KNOWLEDGE, Agent, Decision
from .ingest import SECTION, TEXT, Unit
from .smalltalk import ROUTE as SMALLTALK
from .smalltalk import smalltalk_kind, smalltalk_reply

KB_TITLE = 'База знаний: ответы службы качества'
CHATTER_LIMIT = 2            # столько раз подряд отвечаем на «не по регламентам» вежливо, дальше — коротко возвращаем к теме

EXAMPLES = [
    'Сколько заявок может быть у одного ПА?',
    'Как часто надо обновлять реестр рисков?',
    'Может ли РП начать проект, если договор ещё не подписан?',
    'Кто готовит ТКП, если заказчик уже выбрал продукт?',
]

NOTICES = {
    'not_found': 'В регламентах нет ответа на этот вопрос, поэтому я его не выдумываю. '
                 'Оформлю запрос в службу качества.',
    'unresolved': 'Дать однозначный ответ не получилось: в регламентах несколько вариантов, '
                  'и уточнения не сняли различие. Оформлю запрос в службу качества.',
    'degraded': 'Модель временно недоступна. Показываю найденные пункты без обработки модели — '
                'проверьте их самостоятельно или повторите вопрос через минуту.',
    'unavailable': 'Модель временно недоступна, поэтому связного ответа нет. Показываю найденные пункты '
                   'дословно — проверьте их самостоятельно или повторите вопрос через минуту.',
    'verbatim': 'Связный текст не прошёл проверку на опору в регламенте, поэтому привожу пункты дословно.',
    'quality': 'Служба качества уже отвечала на этот вопрос — привожу её ответ дословно.',
}
NO_KEY = ('Стенд не получил доступа к модели: ключ не задан или не принят. Показываю найденные пункты '
          'без обработки модели — проверьте их самостоятельно.')

# Адреса фиксированных ролей — из ACCOUNTS, строками не дублируем. Агент — почтовый посредник:
# вопрос службе качества и уведомление сотруднику приходят от агента (question, notice),
# ответ службы качества агенту агент передаёт сотруднику (reply, answer).
MAILBOX = {'question': (config.ACCOUNTS['agent'][1], config.ACCOUNTS['quality'][1]),
           'notice': (config.ACCOUNTS['agent'][1], config.ACCOUNTS['client'][1]),
           'reply': (config.ACCOUNTS['quality'][1], config.ACCOUNTS['agent'][1]),
           'answer': (config.ACCOUNTS['agent'][1], config.ACCOUNTS['client'][1])}


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


def notice_letter(ticket):
    """Письмо сотруднику: его вопрос принят, передан службе качества, ответ придёт на почту и в чат."""
    return '\n'.join(['Тема: ваш вопрос передан службе качества — запрос №%d' % ticket['id'], '',
                      'Здравствуйте!', '',
                      'Ваш вопрос принят и передан в службу качества:', '',
                      '«%s»' % ticket['question'], '',
                      'Ответ придёт на эту почту и в чат.'])


def answer_letter(ticket):
    """Текст ответа: вопрос сотрудника и ответ службы качества дословно (агент передаёт его сотруднику)."""
    return '\n'.join(['Тема: ответ службы качества — запрос №%d' % ticket['id'], '',
                      'Здравствуйте!', '',
                      'Служба качества ответила на ваш вопрос.', '',
                      'Ваш вопрос:', '«%s»' % ticket['question'], '',
                      'Ответ:', ticket['answer'], '',
                      'Ответ также добавлен в базу знаний агента.'])


def kb_unit(ticket):
    """Запись базы знаний — такой же искомый атом, как пункт регламента."""
    author = (ticket['answered_by'] or '').strip()          # «Служба качества» в адресе уже названа — не двоим
    return Unit('%s:%d' % (KNOWLEDGE, ticket['id']), KNOWLEDGE, '', ticket['answer'], 'Ответ службы качества',
                (('', 'Ответ службы качества%s, %s. Вопрос сотрудника: %s'
                  % (', ' + author if author and author.casefold() != 'служба качества' else '',
                     (ticket['answered_at'] or '')[:10], ticket['question']), SECTION),),
                TEXT)


class Service:
    def __init__(self, engine, llm, store, llms=None, catalog=()):
        self.engine, self.llm, self.store = engine, llm, store
        self.catalog = [dict(item) for item in catalog]      # модели на выбор: [{id, label, model}]; первая — по умолчанию
        self.agents = {key: Agent(engine, client) for key, client in (llms or {}).items()}   # своя модель — свой агент
        self.agent = self.agents.get(self.catalog[0]['id']) if self.catalog and self.agents else Agent(engine, llm)
        for ticket in store.tickets('answered'):      # база знаний переживает перезапуск
            self.learn(ticket)

    # ---- база знаний ----

    def learn(self, ticket):
        unit = kb_unit(ticket)
        if self.engine.corpus.get(unit.uid) is None:          # повторный запуск — запись уже в индексе
            self.engine.corpus.add_document(KNOWLEDGE, KB_TITLE)
            self.engine.add(unit)
        return unit

    def knowledge_of(self, owner):
        """Какие записи базы знаний видны сотруднику: только ответы на его собственные запросы.

        None — владелец не важен (прямая работа со службой, тесты): видны все.
        """
        if owner is None:
            return None
        return {'%s:%d' % (KNOWLEDGE, t['id']) for t in self.store.tickets('answered', owner=owner)}

    # ---- чат сотрудника ----

    def new_session(self, mode='full', model=None, owner=None):
        return self.store.session(mode=mode, model=model if model in self.agents else None, owner=owner)['id']

    def set_model(self, session_id, model):
        """Переключить модель диалога (выбор в шапке чата); неизвестная модель — ошибка 400."""
        if model not in self.agents:
            raise ValueError('Такой модели на стенде нет')
        self.store.set_model(session_id, model)
        return {'id': session_id, 'model': model}

    def models(self):
        return [{'id': item['id'], 'label': item['label'], 'model': item['model']} for item in self.catalog]

    def agent_for(self, session):
        """Агент на той модели, что выбрана в сессии; сессия без выбора — модель по умолчанию."""
        return self.agents.get(session.get('model') or None, self.agent)

    def ask(self, session_id, text, owner=None):
        text = ' '.join((text or '').split())
        if not text:
            raise ValueError('Вопрос пустой')
        session = self.store.session(session_id, owner=owner)    # чужой идентификатор — своя чистая сессия
        session_id = session['id']
        agent = self.agent_for(session)                        # модель выбрана в сессии — переключатель в шапке чата
        kb = self.knowledge_of(owner)                  # ответы службы качества видны только по своим запросам
        chatter = self.chatter(session_id)
        self.store.add_message(session_id, 'user', text)
        started = time.perf_counter()
        kind = smalltalk_kind(text)
        if kind:                     # приветствие или благодарность — не вопрос: модель не нужна, письмо не готовится
            question, history = {'id': self.store.start_question(session_id, text), 'question': text}, []
            steady = chatter >= CHATTER_LIMIT
            decision = Decision(route=SMALLTALK, text=smalltalk_reply('steady' if steady else kind, EXAMPLES[0]))
        else:
            question, history = self.thread(session_id, text)
            decision = agent.ask(question['question'], [tuple(h) for h in history], kb=kb)
            if decision.route == SMALLTALK and (chatter >= CHATTER_LIMIT or not decision.text):
                decision.text = smalltalk_reply('steady', EXAMPLES[0])   # разговор ушёл от темы — возвращаем к теме
        message = self.respond(session_id, question, history, decision, time.perf_counter() - started)
        message['id'] = self.store.add_message(session_id, 'agent', message.get('text', ''), meta=message)
        message['session_id'] = session_id
        return message

    def chatter(self, session_id):
        """Сколько раз подряд агент отвечал на «не по регламентам» (последнее сообщение — новое, сотрудника)."""
        streak = 0
        for message in reversed(self.store.messages(session_id)):
            if message['role'] == 'user':
                continue
            try:
                meta = json.loads(message['meta'] or '{}')
            except ValueError:
                meta = {}
            if message['role'] == 'agent' and meta.get('route') == SMALLTALK:
                streak += 1
            else:
                break
        return streak

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
        for step, reply in decision.usage.steps:      # журнал вызовов: шаг и модель — какие были на деле
            self.store.log_llm(reply, step, session_id, question['id'])
        route = decision.route
        message = {'role': 'agent', 'route': route, 'tokens': decision.tokens, 'seconds': round(seconds, 1),
                   'clarifications': len(history), 'models': decision.usage.as_dict()['models']}
        common = dict(turns=decision.turns, clarifications=history)
        if route == 'answer':
            message.update(text=decision.text, checks=decision.checks,
                           sources=[self.source(c[0]) for c in decision.cites])
            if not decision.text:        # пункты дословно — скажем почему: модель не ответила, текст не прошёл проверку или отвечала служба качества
                message['notice'] = NOTICES.get(decision.checks.get('judge'), NOTICES['verbatim'])
            # priority — в ответе есть ответ службы качества (запись базы знаний); conflict — рядом пункт регламента.
            # Интерфейс поясняет: регламент главнее, ответ службы качества — консультация по применению.
            message['priority'] = any(s['doc'] == KNOWLEDGE for s in message['sources'])
            message['conflict'] = message['priority'] and any(s['doc'] != KNOWLEDGE for s in message['sources'])
            self.store.touch_question(question['id'], route=route, uids=[s['uid'] for s in message['sources']],
                                      awaiting=None, **common)
        elif route == SMALLTALK:
            message.update(text=decision.text)
            self.store.touch_question(question['id'], route=route, awaiting=None, **common)
        elif route == 'clarify':
            message.update(text=decision.question, clarify={
                'question': decision.question,
                'options': [{'text': o, 'doc': decision.option_docs.get(o)} for o in decision.options]})
            self.store.touch_question(question['id'], route=None, awaiting=decision.question,
                                      options=message['clarify']['options'], **common)
        elif route == 'degraded':
            # 401/403 — не перегрузка: повтор через минуту не поможет, это ключ доступа к модели
            no_key = str(decision.error or '').startswith(('HTTP 401', 'HTTP 403'))
            message.update(text=NO_KEY if no_key else NOTICES[route], notice=decision.error, retry=not no_key,
                           sources=[self.source(c[0]) for c in decision.cites])
            self.store.touch_question(question['id'], route=route, awaiting=None, **common)
        else:
            sources = [self.source(c[0]) for c in decision.cites]
            context = '\n'.join('• %s — %s: «%s»' % (s['doc_title'], s['address'], s['quote']) for s in sources)
            ticket_id = self.store.create_ticket(session_id, question['id'], question['question'], context)
            message.update(text=NOTICES.get(route, NOTICES['not_found']), sources=sources,
                           ticket={'id': ticket_id, 'question': question['question'],   # дословно — карточка в чате
                                   'letter': letter(self.store.ticket(ticket_id))})
            self.store.touch_question(question['id'], route=route, uids=[s['uid'] for s in sources],
                                      ticket_id=ticket_id, awaiting=None, **common)
        for reply in decision.replies:          # токены всех вызовов раунда: решение, текст ответа, судья
            self.store.touch_question(question['id'], reply=reply)
        return message

    def source(self, unit):
        """Источник так, как его показывает интерфейс."""
        address = unit.address
        if unit.doc == KNOWLEDGE:
            address = '%s · запрос №%s' % (unit.crumbs[0][1], unit.uid.split(':')[1])
        return {'uid': unit.uid, 'doc': unit.doc, 'doc_title': self.engine.corpus.title(unit.doc),
                'address': address, 'quote': unit.quote}

    def deliver(self, kind, subject, text, ticket_id, owner):
        """Сохранить письмо в базе, затем попытаться отправить по SMTP; ошибка отправки не ломает действие."""
        sender, recipient = MAILBOX[kind]
        letter_id = self.store.add_letter(owner=owner, kind=kind, mail_from=sender, mail_to=recipient,
                                          subject=subject, text=text, ticket_id=ticket_id)
        delivered = mail.send(config.MAIL_SMTP, self.store.letter(letter_id))
        self.store.set_delivered(letter_id, delivered)
        return delivered

    # ---- служба качества ----

    def send(self, ticket_id):
        draft = self.store.ticket(ticket_id)               # письма в службу — разовые: шлём при первой отправке
        ticket = self.store.send_ticket(ticket_id)
        if draft and draft['status'] == 'draft':           # повторный вызов письма второй раз не шлёт
            owner, number = self.store.ticket_owner(ticket['id']), ticket['id']
            statuses = [self.deliver('question', 'Вопрос в службу качества №%d' % number,
                                     letter(ticket), number, owner),
                        self.deliver('notice', 'Ваш вопрос передан в службу качества, №%d' % number,
                                     notice_letter(ticket), number, owner)]
            ticket['letter_delivered'] = mail.delivered_of(statuses)
        self.store.add_message(ticket['session_id'], 'system',
                               'Запрос №%d отправлен в службу качества. Ожидайте ответа — он появится в этом чате.' % ticket['id'],
                               meta={'role': 'system', 'route': 'sent', 'ticket_id': ticket['id']})
        return ticket

    def answer(self, ticket_id, answer, author):
        answer, author = ' '.join((answer or '').split()), ' '.join((author or 'Служба качества').split())
        if not answer:
            raise ValueError('Ответ пустой')
        was = self.store.ticket(ticket_id)                 # письма сотруднику — при первом ответе
        ticket = self.store.answer_ticket(ticket_id, answer, author)
        unit = self.learn(ticket)
        if was and was['status'] != 'answered':            # повторный ответ письма второй раз не шлёт
            owner, number = self.store.ticket_owner(ticket['id']), ticket['id']
            statuses = [self.deliver('reply', 'Ответ службы качества №%d' % number,
                                     answer_letter(ticket), number, owner),
                        self.deliver('answer', 'Ответ на ваш вопрос №%d' % number,
                                     answer_letter(ticket), number, owner)]
            ticket['letter_delivered'] = mail.delivered_of(statuses)
        self.store.add_message(ticket['session_id'], 'qc', answer,
                               meta={'role': 'qc', 'route': 'qc', 'ticket_id': ticket['id'],
                                     'author': author, 'sources': [self.source(unit)]})
        return ticket

    def document(self, doc_id, owner=None):
        """Регламент для окна документов: заголовки разделов и абзацы в порядке документа.

        База знаний — не общий документ: с владельцем видны только ответы на его собственные запросы.
        """
        info = self.engine.corpus.docs.get(doc_id)
        if not info:
            raise KeyError(doc_id)
        allowed = self.knowledge_of(owner) if doc_id == KNOWLEDGE else None
        blocks = [{'uid': u.uid, 'kind': u.kind, 'number': u.number, 'label': u.label,
                   'text': u.title or u.text, 'address': u.address}
                  for u in self.engine.corpus.units if u.doc == doc_id and (allowed is None or u.uid in allowed)]
        return {'doc': doc_id, 'title': info['title'], 'blocks': blocks}

    # ---- статистика ----

    def analytics(self, days=None, owner=None):
        return self.store.analytics(days, owner)

    def weekly(self, days=7, owner=None):
        """Что внести в документацию или обновить в регламенте: частые вопросы без ответа (свои)."""
        unanswered = self.store.analytics(days, owner)['unanswered']
        if not unanswered:
            return {'days': days, 'proposals': [], 'text': 'Предложений нет.'}
        lines = ['Вопросы без ответа за %d дн.:' % days]
        lines += ['%d. %s — спросили %d раз(а)' % (i, row['question'], row['n'])
                  for i, row in enumerate(unanswered, 1)]
        return {'days': days, 'proposals': unanswered, 'text': '\n'.join(lines)}
