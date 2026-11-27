# -*- coding: utf-8 -*-
"""Почта и фиктивный вход: агент-посредник, три адреса, четыре письма, ящики по ролям, изоляция.

SMTP-сервер — минимальная подделка в потоке на 127.0.0.1: наружу тесты не выходят.
"""
import email
import socket
import tempfile
import threading
import unittest
from email import policy
from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.engine import load_engine
from app.service import Service
from app.store import Store
from app.web import create_app

try:                                    # python -m unittest tests.test_mail
    from .test_agent import FakeLLM, ok
    from .test_isolation import FIRST, SECOND, decide_all
except ImportError:                     # python -m unittest discover -s tests
    from test_agent import FakeLLM, ok
    from test_isolation import FIRST, SECOND, decide_all

DATA = Path(__file__).resolve().parents[1] / 'data'
QUESTION = 'Кто имеет право подписывать документы от лица компании?'
ANSWER = 'Право подписи документов имеет генеральный директор и сотрудники по доверенности.'
CLIENT = config.ACCOUNTS['client'][1]           # client1@docii.aized.ru
QUALITY = config.ACCOUNTS['quality'][1]         # s.kachestva@docii.aized.ru
AGENT = config.ACCOUNTS['agent'][1]             # agent@docii.aized.ru
OWNER = FIRST['x-forwarded-for']                # владелец = адрес сотрудника, как его видит хранилище


class FakeSMTP(threading.Thread):
    """Минимальный SMTP-сервер: приветствие 220, команды, ответы 250 (DATA — 354), в конце 221.

    fail_after — после стольких принятых писем DATA отвечает отказом (проверка частичной доставки).
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.messages = []              # принятые письма: (mail from, rcpt to, текст DATA)
        self.fail_after = None

    def run(self):
        try:
            while True:                 # соединений несколько: каждое письмо — новое
                conn, _ = self.listener.accept()
                try:
                    self.serve(conn)
                except (ConnectionError, OSError):
                    pass
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
        except OSError:                 # слушатель закрыт — сервер закончил работу
            pass

    def serve(self, conn):
        buf = b''

        def line():
            nonlocal buf
            while b'\r\n' not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    raise ConnectionError('соединение закрыто')
                buf += chunk
            raw, _, buf = buf.partition(b'\r\n')
            return raw.decode('utf-8', 'replace')

        conn.sendall(b'220 fake ready\r\n')
        sender = recipient = None
        while True:
            text = line()
            upper = text.upper()
            if upper.startswith(('EHLO', 'HELO')):
                # многострочный ответ: продолжения «250-…», последняя строка «250 …» — разбор на ней кончается
                conn.sendall(b'250-fake greets\r\n250 8BITMIME\r\n')
            elif upper.startswith('MAIL FROM'):
                sender = text.split(':', 1)[1].strip().strip('<>')
                conn.sendall(b'250 OK\r\n')
            elif upper.startswith('RCPT TO'):
                recipient = text.split(':', 1)[1].strip().strip('<>')
                conn.sendall(b'250 OK\r\n')
            elif upper == 'DATA':
                conn.sendall(b'354 end with <CRLF>.<CRLF>\r\n')
                body = []
                while True:
                    raw = line()
                    if raw == '.':
                        break
                    body.append(raw[1:] if raw.startswith('.') else raw)    # снять экранирование точки
                if self.fail_after is not None and len(self.messages) >= self.fail_after:
                    conn.sendall(b'554 rejected\r\n')
                    continue
                self.messages.append((sender, recipient, '\r\n'.join(body)))
                conn.sendall(b'250 OK\r\n')
            elif upper == 'QUIT':
                conn.sendall(b'221 bye\r\n')
                return
            else:
                conn.sendall(b'250 OK\r\n')


def received(server):
    """Принятые письма как разобранные сообщения: [(from, to, сообщение)]."""
    return [(sender, recipient, email.message_from_bytes(raw.encode('utf-8'), policy=policy.default))
            for sender, recipient, raw in server.messages]


def stand(decide=None):
    """Стенд на поддельной модели (вопросы уходят в «нет ответа»): клиент, служба, хранилище."""
    llm = FakeLLM(ok('{}'))
    llm.chat = decide_all(decide or (lambda messages: ok('{"action": "not_found"}')))
    store = Store(Path(tempfile.mkdtemp()) / 'state.db')
    service = Service(load_engine(DATA, with_vectors=False), llm, store)
    return TestClient(create_app(service, with_vectors=False, ask_limit=0)), service, store


class MailFlow(unittest.TestCase):
    def setUp(self):
        self.old = config.MAIL_SMTP
        self.server = FakeSMTP()
        self.server.start()
        config.MAIL_SMTP = '127.0.0.1:%d' % self.server.port

    def tearDown(self):
        config.MAIL_SMTP = self.old
        self.server.listener.close()

    def ticket(self, client, headers=FIRST):
        """Запрос в черновиках: вопрос без ответа в регламентах."""
        return client.post('/api/ask', json={'text': QUESTION}, headers=headers).json()['ticket']['id']

    def test_send_mails_two_letters(self):
        client, _, store = stand()
        tid = self.ticket(client)
        response = client.post('/api/tickets/%d/send' % tid, headers=FIRST).json()
        self.assertIs(response['letter_delivered'], True)              # оба письма ушли
        letters = received(self.server)
        self.assertEqual([(s, r) for s, r, _ in letters],
                         [(AGENT, QUALITY), (AGENT, CLIENT)])          # агент — посредник обоих
        self.assertEqual([m['Subject'] for _, _, m in letters],
                         ['Вопрос в службу качества №%d' % tid,
                          'Ваш вопрос передан в службу качества, №%d' % tid])
        self.assertIn('«%s»' % QUESTION, letters[0][2].get_content())   # вопрос сотрудника дословно
        self.assertIn(QUESTION, letters[1][2].get_content())
        self.assertIn('Ответ придёт на эту почту и в чат', letters[1][2].get_content())
        rows = store.letters(OWNER, AGENT)                             # в базе — у своего владельца
        self.assertEqual([row['kind'] for row in rows], ['notice', 'question'])   # новые сверху
        self.assertEqual([row['delivered'] for row in rows], [1, 1])

    def test_answer_mails_two_letters(self):
        client, _, _ = stand()
        tid = self.ticket(client)
        client.post('/api/tickets/%d/send' % tid, headers=FIRST)
        response = client.post('/api/tickets/%d/answer' % tid, json={'answer': ANSWER}, headers=FIRST).json()
        self.assertIs(response['letter_delivered'], True)
        letters = received(self.server)                                # порядок: вопрос, уведомление, ответ…
        self.assertEqual([m['Subject'] for _, _, m in letters],
                         ['Вопрос в службу качества №%d' % tid,
                          'Ваш вопрос передан в службу качества, №%d' % tid,
                          'Ответ службы качества №%d' % tid,
                          'Ответ на ваш вопрос №%d' % tid])
        self.assertEqual([(s, r) for s, r, _ in letters],
                         [(AGENT, QUALITY), (AGENT, CLIENT), (QUALITY, AGENT), (AGENT, CLIENT)])
        for _, _, message in letters[-2:]:                             # вопрос и ответ — дословно в обоих
            content = message.get_content()
            self.assertIn('«%s»' % QUESTION, content)
            self.assertIn(ANSWER, content)

    def test_smtp_failure_does_not_break_the_api(self):
        dead = socket.socket()
        dead.bind(('127.0.0.1', 0))
        port = dead.getsockname()[1]                                    # порт без слушателя — отказ соединения
        dead.close()
        config.MAIL_SMTP = '127.0.0.1:%d' % port
        client, _, store = stand()
        tid = self.ticket(client)
        response = client.post('/api/tickets/%d/send' % tid, headers=FIRST)
        self.assertEqual(response.status_code, 200)                    # действие не сломано
        self.assertIs(response.json()['letter_delivered'], False)      # хоть одно не доставлено
        rows = store.letters(OWNER, AGENT)
        self.assertEqual([row['delivered'] for row in rows], [0, 0])   # письма в базе, не доставлены
        self.assertEqual(self.server.messages, [])

    def test_partial_failure_is_reported_as_not_delivered(self):
        self.server.fail_after = 1                                     # первое примем, второе отклоним
        client, _, store = stand()
        tid = self.ticket(client)
        response = client.post('/api/tickets/%d/send' % tid, headers=FIRST).json()
        self.assertIs(response['letter_delivered'], False)
        rows = store.letters(OWNER, AGENT)
        self.assertEqual([row['delivered'] for row in rows], [0, 1])   # новые сверху: уведомление отклонено
        self.assertIsNone(store.letter_delivered(tid + 100))           # чужой запрос — писем нет

    def test_without_smtp_letters_are_saved_without_sending(self):
        config.MAIL_SMTP = ''
        client, _, store = stand()
        tid = self.ticket(client)
        sent = client.post('/api/tickets/%d/send' % tid, headers=FIRST).json()
        self.assertIsNone(sent['letter_delivered'])                    # SMTP не задан — статус неизвестен
        client.post('/api/tickets/%d/answer' % tid, json={'answer': ANSWER}, headers=FIRST)
        self.assertEqual([row['subject'] for row in store.letters(OWNER, AGENT)],
                         ['Ответ на ваш вопрос №%d' % tid, 'Ответ службы качества №%d' % tid,
                          'Ваш вопрос передан в службу качества, №%d' % tid,
                          'Вопрос в службу качества №%d' % tid])
        self.assertEqual([row['delivered'] for row in store.letters(OWNER, AGENT)], [None] * 4)
        self.assertEqual(self.server.messages, [])                     # соединений с сервером не было

    def test_repeated_send_does_not_mail_twice(self):
        client, service, store = stand()
        tid = self.ticket(client)
        client.post('/api/tickets/%d/send' % tid, headers=FIRST)
        service.send(tid)                                              # повтор на уровне службы
        self.assertEqual(client.post('/api/tickets/%d/send' % tid, headers=FIRST).status_code, 409)
        self.assertEqual(len(self.server.messages), 2)                 # письма — два, по одному на адрес
        self.assertEqual(len(store._all('SELECT * FROM letters WHERE ticket_id=?', tid)), 2)

    def test_mailboxes_are_isolated_by_owner(self):
        client, _, _ = stand()
        tid = self.ticket(client)
        client.post('/api/tickets/%d/send' % tid, headers=FIRST)
        client.post('/api/tickets/%d/answer' % tid, json={'answer': ANSWER}, headers=FIRST)
        mine = client.get('/api/letters?role=client', headers=FIRST).json()
        self.assertEqual(sorted(mine[0]), ['cc', 'created', 'delivered', 'from', 'id', 'kind',
                                           'outgoing', 'snippet', 'subject', 'ticket_id', 'to'])
        self.assertEqual([(m['kind'], m['outgoing']) for m in mine],
                         [('answer', False), ('notice', False)])       # новые сверху; всё от агента — входящее
        self.assertEqual(mine[0]['from'], AGENT)
        self.assertIs(mine[0]['delivered'], True)
        quality = client.get('/api/letters?role=quality', headers=FIRST).json()
        self.assertEqual([(m['kind'], m['outgoing']) for m in quality],
                         [('reply', True), ('question', False)])       # службе — её вопрос и её ответ агенту
        agent = client.get('/api/letters?role=agent', headers=FIRST).json()
        self.assertEqual(len(agent), 4)                                # посредник видит всю переписку
        theirs = client.get('/api/letters?role=client', headers=SECOND).json()
        self.assertEqual(theirs, [])                                   # чужой ящик пуст
        self.assertEqual(client.get('/api/letter/%d' % mine[0]['id'], headers=SECOND).status_code, 404)
        self.assertEqual(client.get('/api/letter/%d' % mine[0]['id'], headers=FIRST).status_code, 200)
        self.assertEqual(client.get('/api/letter/9999', headers=FIRST).status_code, 404)   # нет такого номера
        self.assertEqual([client.get('/api/letters?role=%s' % role, headers=FIRST).status_code
                          for role in ('admin', 'hacker')], [400, 400])
        self.assertEqual(client.get('/api/letters', headers=FIRST).status_code, 400)   # роль не названа
        self.assertIs(client.get('/api/tickets', headers=FIRST).json()[0]['letter_delivered'], True)

    def test_login_returns_the_fixed_accounts(self):
        client, _, _ = stand()
        self.assertEqual(client.post('/api/login', json={'role': 'client'}).json(),
                         {'role': 'client', 'name': 'Сотрудник', 'email': CLIENT})
        self.assertEqual(client.post('/api/login', json={'role': 'quality'}).json(),
                         {'role': 'quality', 'name': 'Служба качества', 'email': QUALITY})
        self.assertEqual(client.post('/api/login', json={'role': 'agent'}).json(),
                         {'role': 'agent', 'name': 'ДокИИ', 'email': AGENT})
        self.assertEqual(client.post('/api/login', json={'role': 'admin'}).status_code, 400)

    def test_health_reports_accounts_and_mail(self):
        client, _, _ = stand()
        health = client.get('/api/health', headers=FIRST).json()
        self.assertEqual(health['accounts'], {'client': {'name': 'Сотрудник', 'email': CLIENT},
                                              'quality': {'name': 'Служба качества', 'email': QUALITY},
                                              'agent': {'name': 'ДокИИ', 'email': AGENT}})
        self.assertIs(health['mail']['smtp'], True)                    # тестовый сервер задан
        config.MAIL_SMTP = ''
        self.assertIs(client.get('/api/health', headers=FIRST).json()['mail']['smtp'], False)


if __name__ == '__main__':
    unittest.main()
