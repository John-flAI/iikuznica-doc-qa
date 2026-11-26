# -*- coding: utf-8 -*-
"""Клиент модели: разбор JSON, повторы при перегрузке, обрыв ответа, честный счёт токенов."""
import unittest
from unittest import mock

from app.llm import LLMClient, Reply, Usage, parse_json


def overloaded():
    return Reply(error='HTTP 429', prompt_tokens=100, completion_tokens=0), True


def fine(text='{"action": "answer"}', model='m'):
    return Reply(text=text, ok=True, model=model, prompt_tokens=100, completion_tokens=20), False


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

    def test_fallback_model_takes_over_when_main_is_overloaded(self):
        client = self.client([overloaded()] * 4 + [fine(model='reserve')], fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertEqual((reply.ok, reply.model, reply.attempts), (True, 'reserve', 5))

    def test_both_models_down_is_an_error_not_a_crash(self):
        client = self.client([overloaded()] * 8, fallback_model='reserve')
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([])
        self.assertFalse(reply.ok)
        self.assertEqual(reply.attempts, 8)

    def test_truncated_answer_is_retried_with_a_bigger_budget(self):
        cut = Reply(text='{"action": "ans', ok=True, truncated=True, prompt_tokens=100, completion_tokens=600), False
        client = self.client([cut, fine()])
        with mock.patch('app.llm.time.sleep'):
            reply = client.chat([], max_tokens=600)
        self.assertEqual([budget for _, budget in client.log], [600, 1200])
        self.assertTrue(reply.ok)

    def test_client_error_is_not_retried(self):
        client = self.client([(Reply(error='HTTP 400'), False)])
        reply = client.chat([])
        self.assertEqual((reply.ok, reply.attempts), (False, 1))

    def test_thinking_is_switched_off_for_both_kinds_of_server(self):
        client = LLMClient('http://server/v1', 'm')
        self.assertEqual(client.extra_body, {'reasoning_effort': 'none', 'thinking': {'type': 'disabled'}})


if __name__ == '__main__':
    unittest.main()
