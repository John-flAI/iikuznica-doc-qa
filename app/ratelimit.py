# -*- coding: utf-8 -*-
"""Лимит вопросов с одного адреса: стенд демонстрационный, и каждый вопрос — расход бюджета модели.

Скользящее окно в памяти процесса — перезапуск стенда обнуляет счётчик, для демо это допустимо.
Адрес — тот, что записал reverse proxy (последний в X-Forwarded-For: всё левее мог прислать сам клиент).
"""
import ipaddress
import threading
import time

HOUR = 3600


def client_ip(forwarded_for, peer):
    """Адрес сотрудника: последний в X-Forwarded-For, иначе — адрес собеседника."""
    candidates = ([forwarded_for.split(',')[-1]] if forwarded_for else []) + [peer]
    for candidate in candidates:
        text = (candidate or '').strip()
        if text.startswith('[') and ']' in text:             # «[2001:db8::1]:443»
            text = text[1:text.index(']')]
        elif text.count(':') == 1 and text.rsplit(':', 1)[1].isdigit():   # «1.2.3.4:5678» — IPv4 с портом прокси
            text = text.rsplit(':', 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        return str(getattr(address, 'ipv4_mapped', None) or address)     # «::ffff:1.2.3.4» — это IPv4
    return 'unknown'


def address_key(ip):
    """Ключ лимита: IPv4 — целиком, IPv6 — сеть /64 (у клиента адреса вращаются внутри неё)."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if address.version == 6:
        return str(ipaddress.ip_network('%s/64' % address, strict=False))
    return str(address)


class RateLimiter:
    """Не более limit вопросов за period секунд с одного адреса; limit = 0 — лимита нет."""

    def __init__(self, limit, period=HOUR, clock=time.monotonic):
        self.limit, self.period, self.clock = limit, period, clock
        self.seen, self.lock = {}, threading.Lock()       # ключ адреса → моменты вопросов

    def allow(self, key):
        """(можно ли задать вопрос, сколько секунд до ближайшего свободного места)."""
        if not self.limit:
            return True, 0
        with self.lock:
            now = self.clock()
            stamps = [t for t in self.seen.get(key, []) if now - t < self.period]
            self.seen[key] = stamps
            if len(stamps) < self.limit:
                stamps.append(now)
                self.sweep(now)
                return True, 0
            return False, max(1, int(self.period - (now - stamps[0])) + 1)

    def sweep(self, now):
        """Адреса с истёкшим окном убираем из памяти — иначе словарь растёт бесконечно."""
        if len(self.seen) < 1000:
            return
        for key, stamps in list(self.seen.items()):
            fresh = [t for t in stamps if now - t < self.period]
            if fresh:
                self.seen[key] = fresh
            else:
                del self.seen[key]
