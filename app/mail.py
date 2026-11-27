# -*- coding: utf-8 -*-
"""Отправка писем по SMTP: только стандартная библиотека, без TLS и без аутентификации.

На стенде письма принимает тестовый сервер mailpit. Письмо к этому моменту уже сохранено в базе:
ошибка отправки не ломает действие, которое её вызвало, — письмо помечается не доставленным и
остаётся в базе.
"""
import logging
import smtplib
from email.message import EmailMessage

LOG = logging.getLogger(__name__)
TIMEOUT = 3          # секунды: тестовый сервер стоит рядом, дольше ждать нечего


def delivered_of(statuses):
    """Общий статус доставки действия: None — SMTP не задан, False — хоть одно письмо не ушло, True — все ушли."""
    if not statuses or all(s is None for s in statuses):
        return None
    return all(bool(s) for s in statuses)


def send(host_port, letter):
    """Отправить сохранённое письмо; True — доставлено, False — ошибка, None — SMTP не задан."""
    if not host_port:
        return None
    host, _, port = host_port.partition(':')
    message = EmailMessage()
    message['From'] = letter['mail_from']
    message['To'] = letter['mail_to']
    if letter['mail_cc']:
        message['Cc'] = letter['mail_cc']
    message['Subject'] = letter['subject']
    message.set_content(letter['text'])
    try:
        with smtplib.SMTP(host, int(port or 25), timeout=TIMEOUT) as smtp:
            smtp.send_message(message)
        return True
    except Exception as e:                     # почта не часть действия: вопрос в службу уже сохранён
        LOG.warning('Письмо №%s не отправлено через %s: %s', letter['id'], host_port, e)
        return False
