# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/bot.py" дата="2026-10-09">
# @что: заготовка TG-интерфейса агента — длинный опрос, пересылка вопросов в каркас
# @запуск: APP=http://127.0.0.1:8765 TG_TOKEN=<токен> python bot.py
# @токен: брать у владельца на хакатоне (BotFather), в файл не писать
# </@паспорт>
import json
import os
import time
import urllib.parse
import urllib.request

APP = os.environ.get("APP", "http://127.0.0.1:8765")
TG = os.environ.get("TG_TOKEN", "")
API = f"https://api.telegram.org/bot{TG}"


def tg(method, **params):
    req = urllib.request.Request(f"{API}/{method}", method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=json.dumps(params).encode(), timeout=40) as r:
        return json.loads(r.read().decode())


def ask(question):
    req = urllib.request.Request(APP + "/api/ask", method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=json.dumps({"question": question}).encode(), timeout=30) as r:
        return json.loads(r.read().decode())


offset = 0
print("бот ждёт сообщения…")
while True:
    try:
        updates = tg("getUpdates", offset=offset, timeout=30).get("result", [])
    except Exception as e:
        print("сеть моргнула:", e)
        time.sleep(5)
        continue
    for u in updates:
        offset = u["update_id"] + 1
        msg = u.get("message") or {}
        text = msg.get("text") or ""
        chat = msg.get("chat", {}).get("id")
        if text and chat:
            try:
                answer = ask(text)["answer"]
            except Exception as e:
                answer = f"агент не ответил: {e}"
            tg("sendMessage", chat_id=chat, text=answer[:4000])
