# -*- coding: utf-8 -*-
"""Лимит вопросов с одного адреса: стенд демонстрационный, и бюджет модели — не безлимитный."""
import unittest

from app.ratelimit import HOUR, RateLimiter, address_key, client_ip


def limiter(limit, period=HOUR):
    clock = [0.0]
    return RateLimiter(limit, period, clock=lambda: clock[0]), clock


class Address(unittest.TestCase):
    def test_proxy_address_is_the_last_in_forwarded_for(self):
        # всё левее последнего адреса мог прислать сам клиент — верим только тому, что дописал прокси
        self.assertEqual(client_ip('203.0.113.9, 198.51.100.7', '10.0.0.2'), '198.51.100.7')
        self.assertEqual(client_ip('', '10.0.0.2'), '10.0.0.2')
        self.assertEqual(client_ip(None, None), 'unknown')
        self.assertEqual(client_ip('мусор, [2001:db8::1]:443', '10.0.0.2'), '2001:db8::1')

    def test_ipv6_is_limited_per_network(self):
        self.assertEqual(address_key('2001:db8:1:2:3:4:5:6'), address_key('2001:db8:1:2::7'))
        self.assertNotEqual(address_key('2001:db8:1:2::'), address_key('2001:db8:1:3::'))
        self.assertEqual(address_key('198.51.100.7'), '198.51.100.7')
        self.assertNotEqual(address_key('198.51.100.7'), address_key('198.51.100.8'))
        self.assertEqual(address_key('не адрес'), 'не адрес')


class Window(unittest.TestCase):
    def test_limit_then_retry_after(self):
        rate, clock = limiter(3, period=100)
        self.assertEqual([rate.allow('a')[0] for _ in range(3)], [True, True, True])
        self.assertEqual(rate.allow('a'), (False, 101))
        self.assertTrue(rate.allow('b')[0])                 # другой адрес — свой счёт
        clock[0] = 60.0
        self.assertEqual(rate.allow('a'), (False, 41))
        clock[0] = 100.0
        self.assertEqual(rate.allow('a'), (True, 0))        # окно первого вопроса истекло — место освободилось

    def test_zero_means_no_limit(self):
        rate, _ = limiter(0)
        self.assertEqual([rate.allow('a') for _ in range(50)], [(True, 0)] * 50)


if __name__ == '__main__':
    unittest.main()
