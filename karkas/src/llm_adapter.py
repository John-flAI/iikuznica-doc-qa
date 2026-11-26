# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/llm_adapter.py" дата="2026-10-22">
# @что: LLM-слой (перенос паттерна каркаса Островка): полировка черновика ПОВЕРХ фактов.
# @ключ: только env LLM_API_KEY; без ключа/сети — None, работаем детерминированно.
#        Цитаты и факты LLM не порождает — только тон и связность.
# </@паспорт>
import json
import os
import urllib.request

URL = "https://api.z.ai/api/anthropic/v1/messages"
MODEL = os.environ.get("LLM_MODEL", "GLM-5.3-Flash")
TIMEOUT = 60

SYSTEM = ("Ты — «Канцелярист», агент входящих документов малого бизнеса. Тебе дают черновик "
          "делового ответа и факты с цитатами. Перепиши черновик вежливым деловым письмом "
          "(2-5 предложений). Факты, суммы, реквизиты и предупреждения о расхождениях не меняй "
          "и не добавляй ничего от себя: то, чего нет в фактах, писать запрещено. Отвечай по-русски.")


def has_key() -> bool:
    return bool(os.environ.get("LLM_API_KEY"))


def llm_answer(черновик: str, факты: str) -> str | None:
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 700,
        "temperature": 0.3,
        "stream": False,
        "thinking": {"type": "disabled"},
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"Черновик:\n{черновик}\n\nФакты с цитатами:\n{факты}"}],
    }).encode("utf-8")
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "authorization": "Bearer " + key,
        "anthropic-version": "2023-06-01",
        "user-agent": "aized-kancelyarist/1",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in data.get("content", []))
    except Exception:
        return None
