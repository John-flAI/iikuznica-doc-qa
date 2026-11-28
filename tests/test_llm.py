# -*- coding: utf-8 -*-
"""Клиент модели: разбор JSON, повторы при перегрузке, обрыв ответа, честный счёт токенов."""
import json
import unittest
from unittest import mock

from app.llm import PAUSES, PAUSES_WITH_FALLBACK, LLMClient, Reply, Usage, parse_json


def overloaded():
    return Reply(error='HTTP 429', prompt_tokens=100, completion_tokens=0), True


def fine(text='{"action": "answer"}', model='m'):
    return Reply(text=text, ok=True, model=model, prompt_tokens=100, completion_tokens=20), False


def slow_overload(seconds):
    return Reply(error='HTTP 429', prompt_tokens=100, completion_tokens=0, seconds=seconds), True


def slow_fine(seconds, model='m'):
    return Reply(text='{"action": "answer"}', ok=True, model=model, prompt_tokens=100, completion_tokens=20,
                 seconds=seconds), False


class FakeResponse:
    """Ответ сервера без сети: read() отдаёт заготовленное тело."""

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def served(*bodies):
    """Подменяет urllib.request.urlopen: сервер отвечает заготовленными телами по очереди."""
    queue = [json.dumps(body).encode('utf-8') for body in bodies]

    def urlopen(request, timeout=None):
        return FakeResponse(queue.pop(0))
    return urlopen


def server_reply(text, finish_reason='stop'):
    return {'model': 'm', 'choices': [{'message': {'content': text}, 'finish_reason': finish_reason}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}


class ParseJson(unittest.TestCase):
    def test_plain_object(self):
        self.assertEqual(parse_json('{"a": 1}'), {'a': 1})

    def test_inside_code_fence_and_words(self):
        self.assertEqual(parse_json('Вот ответ:\n```json\n{"a": {"b": "x}y"}}\n```'), {'a': {'b': 'x}y'}})

    def test_broken_answer_raises(self):
        for text in ('{"a": ', 'нет json', '[1, 2]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_json(text)


class Client(unittest.TestCase):
    def client(self, replies, **kwargs):
        client = LLMClient('http://server/v1', 'main', **kwargs)
        client.log = []
        queued = list(replies)

        def call(model, messages, max_tokens, temperature):
            client.log.append((model, max_tokens))
            return queued.pop(0)
        client._call = call
        return client

    def test_overload_is_retried_then_succeeds(self):
        client = self.client([overloaded(), overloaded(), fine()])
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertTrue(reply.ok)
        self.assertEqual(reply.attempts, 3)

    def test_failed_attempts_are_still_paid_for(self):
        client = self.client([overloaded(), overloaded(), fine()])
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        usage = Usage()
        usage.add(reply, 'decide')
        self.assertEqual((usage.calls, usage.prompt_tokens, usage.completion_tokens), (1, 300, 20))

    def test_seconds_cover_the_whole_retry_series(self):
        """Считаем, сколько шли сами попытки; паузы между повторами в секунды не включаем."""
        client = self.client([slow_overload(1.5), slow_overload(2), slow_fine(3)])
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertTrue(reply.ok)
        self.assertEqual(reply.seconds, 6.5)
        usage = Usage()
        usage.add(reply, 'decide')
        self.assertEqual(usage.seconds, 6.5)

    def test_seconds_include_the_fallback_model_attempts(self):
        client = self.client([slow_overload(1)] * len(PAUSES_WITH_FALLBACK) + [slow_fine(2, model='reserve')],
                             fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertEqual((reply.ok, reply.model), (True, 'reserve'))
        self.assertEqual(reply.seconds, len(PAUSES_WITH_FALLBACK) + 2)

    def test_total_failure_counts_tokens_and_seconds_once(self):
        """Серия кончилась отказом: последняя попытка лежит и в reply, и в потерянных — считаем её один раз."""
        attempts = 2 * len(PAUSES_WITH_FALLBACK)
        client = self.client([slow_overload(1)] * attempts, fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertFalse(reply.ok)
        usage = Usage()
        usage.add(reply, 'decide')
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens, usage.seconds),
                         (attempts * 100, 0, float(attempts)))

    def test_fallback_model_takes_over_when_main_is_overloaded(self):
        client = self.client([overloaded()] * len(PAUSES_WITH_FALLBACK) + [fine(model='reserve')], fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertEqual((reply.ok, reply.model, reply.attempts), (True, 'reserve', len(PAUSES_WITH_FALLBACK) + 1))

    def test_both_models_down_is_an_error_not_a_crash(self):
        client = self.client([overloaded()] * (2 * len(PAUSES_WITH_FALLBACK)), fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertFalse(reply.ok)
        self.assertEqual(reply.attempts, 2 * len(PAUSES_WITH_FALLBACK))

    def test_truncated_answer_is_retried_with_a_bigger_budget(self):
        cut = Reply(text='{"action": "ans', ok=True, truncated=True, prompt_tokens=100, completion_tokens=600), False
        client = self.client([cut, fine()])
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([], max_tokens=600)
        self.assertEqual([budget for _, budget in client.log], [600, 1200])
        self.assertTrue(reply.ok)

    def test_a_long_overload_streak_is_outlasted(self):
        """Замер 27.11: у бесплатной Flash бывают семь отказов подряд — клиент должен пересидеть серию."""
        client = self.client([overloaded()] * 7 + [fine()])
        with mock.patch('app.llm.time.sleep') as sleep:
            reply = client.chat([])
        self.assertTrue(reply.ok)
        self.assertEqual(reply.attempts, 8)
        self.assertEqual(sleep.call_count, 7)              # после последней попытки уже не спим

    def test_a_hanging_server_does_not_hold_the_question_for_half_an_hour(self):
        """Попытки по 50 секунд: после второй сумма вышла за дедлайн серии — не девять попыток по 180 секунд."""
        client = self.client([slow_overload(50)] * 9)
        with mock.patch('app.llm.time.sleep') as sleep:
            reply = client.chat([])
        self.assertFalse(reply.ok)
        self.assertEqual((reply.attempts, sleep.call_count, reply.seconds), (2, 1, 100.0))
        self.assertEqual(len(client.log), 2)                 # девятая заготовка так и не понадобилась

    def test_client_error_is_not_retried(self):
        client = self.client([(Reply(error='HTTP 400'), False)])
        reply = client.chat([])
        self.assertEqual((reply.ok, reply.attempts), (False, 1))

    def test_thinking_is_switched_off_for_both_kinds_of_server(self):
        client = LLMClient('http://server/v1', 'm')
        self.assertEqual(client.extra_body, {'reasoning_effort': 'none', 'thinking': {'type': 'disabled'}})


class ServerReply(unittest.TestCase):
    """Разбор сырого ответа сервера: finish_reason «length» виден только на настоящем теле ответа."""

    def test_answer_cut_by_length_limit_is_marked_truncated(self):
        client = LLMClient('http://server/v1', 'm')
        body = server_reply('{"action": "ans')
        body['choices'][0]['finish_reason'] = 'length'
        with mock.patch('urllib.request.urlopen', served(body)):
            reply, _ = client._call('m', [], 600, 0)
        self.assertTrue(reply.truncated)
        self.assertEqual((reply.ok, reply.prompt_tokens, reply.completion_tokens), (True, 100, 20))

    def test_truncated_server_answer_is_retried_with_a_bigger_budget(self):
        client = LLMClient('http://server/v1', 'main')
        cut = server_reply('{"action": "ans')
        cut['choices'][0]['finish_reason'] = 'length'
        with mock.patch('urllib.request.urlopen', served(cut, server_reply('{"action": "answer"}'))), \
                mock.patch('app.llm.time.sleep'):
            reply = client.chat([], max_tokens=600)
        self.assertTrue(reply.ok)
        self.assertFalse(reply.truncated)
        usage = Usage()
        usage.add(reply, 'decide')
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (200, 40))   # оборванная попытка не потеряна


if __name__ == '__main__':
    unittest.main()
