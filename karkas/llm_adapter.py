# -*- coding: utf-8 -*-
# <@паспорт файл="karkas/llm_adapter.py" дата="2026-10-11">
# @что: LLM-адаптер тревел-агента — anthropic-совместимый запрос к GLM Flash
# @ключ: только из переменной окружения LLM_API_KEY; в файле и репозитории ключей нет
# @запуск: из app.py (recommend) как llm_answer(query, picks_text)
# </@паспорт>
import json
import os
import urllib.error
import urllib.request

URL = "https://api.z.ai/api/anthropic/v1/messages"
MODEL = os.environ.get("LLM_MODEL", "GLM-5.3-Flash")
TIMEOUT = 60

def _key():
    k = os.environ.get("LLM_API_KEY")
    if k:
        return k
    return None  # ключ берётся только из переменной окружения LLM_API_KEY

SYSTEM = ("Ты — «Канцелярист», ИИ-помощник офиса. Тебе дают вопрос клиента и "
          "выдержки из базы знаний компании (регламенты, документы). Отвечай "
          "СТРОГО по выдержкам: если ответа в них нет — честно скажи «этого нет "
          "в базе» и предложи связать с человеком. Цитируй документ-источник в "
          "скобках. Отвечай по-русски, коротко и делово.")

def llm_documents(question, docs_text):
    """Ответ строго по документам базы знаний (для каркаса ИИкузницы)."""
    return llm_answer(question, "Документы базы знаний:" + chr(10) + docs_text)

def llm_answer(query, picks_text):
    key = _key()
    if not key:
        return None  # вызывающий падает на детерминированный черновик
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 700,
        "temperature": 0.4,
        "stream": False,
        "thinking": {"type": "disabled"},
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"Запрос: {query}\n\nПодборка:\n{picks_text}"}],
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
