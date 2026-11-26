# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/app.py" дата="2026-10-09">
# @что: каркас прототипа «ИИкузница» — оркестратор агента + RAG + витрина, 0 зависимостей
# @запуск: python app.py  → http://127.0.0.1:8765
# @точки подключения на хакатоне: llm_answer() (LLM-адаптер), индекс (TF-IDF → эмбеддинги)
# </@паспорт>
"""
Каркас ИИ-агента для бизнес-процессов. Всё на stdlib, чтобы за 48 часов
хакатона тратить время на задачу, а не на окружение.

API:
  GET  /                — витрина (демо-страница)
  POST /api/docs        — {"title": "...", "text": "..."} добавить документ в базу знаний
  GET  /api/docs        — список документов
  POST /api/ask         — {"question": "..."} ответ агента (сейчас: выдержки + черновик, LLM-адаптер подключается в llm_answer)
  GET  /api/health      — статус
"""
import json
import math
import re
import threading
import time
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "http://127.0.0.1:8765"
DOCS = []          # [{"id","title","text","tokens"}]
LOCK = threading.Lock()
LOG = []

STOP = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам сказал ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто человек чего раз тоже себе под жизнь будет ж тогда кто этот говорил того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее кажется сейчас были куда зачем всех можно при об хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между".split())


def tokenize(text):
    return [w for w in re.findall(r"[а-яёa-z0-9]+", text.lower()) if w not in STOP and len(w) > 2]


def add_doc(title, text):
    with LOCK:
        doc = {"id": len(DOCS) + 1, "title": title, "text": text, "tokens": tokenize(text)}
        DOCS.append(doc)
    return doc["id"]


def search(question, top=3):
    q = Counter(tokenize(question))
    if not q:
        return []
    with LOCK:
        docs = list(DOCS)
    scored = []
    for d in docs:
        tf = Counter(d["tokens"])
        score = sum((1 + math.log(tf[w])) * 1.5 for w in q if tf.get(w))
        cover = sum(1 for w in q if tf.get(w)) / len(q)
        scored.append((score * (0.5 + cover), d))
    scored.sort(key=lambda x: -x[0])
    return [d for s, d in scored[:top] if s > 0]


def llm_answer(question, contexts):
    """ТОЧКА ПОДКЛЮЧЕНИЯ LLM. На хакатоне: собрать промпт из contexts и позвать
    модель (у станции есть живой пример адаптера: PROJECTS/tquote/probe.py —
    api.z.ai/api/anthropic, ключ из сейфа). Сейчас — честный черновик без модели."""
    cites = "\n".join(f"[{d['id']}] {d['title']}: {d['text'][:300]}…" for d in contexts)
    return ("Черновик ответа агента (без LLM-адаптера).\n\n"
            f"Вопрос: {question}\n\nОпора на базу знаний:\n{cites or '— база пуста —'}\n\n"
            "Дальше агент: уточняет недостающее → предлагает действие → ждёт подтверждения.")


def page():
    return """<!doctype html><html lang=ru><meta charset=utf-8>
<title>Каркас ИИ-агента</title>
<style>body{font-family:system-ui;max-width:760px;margin:40px auto;padding:0 16px;background:#101416;color:#e9e3d5}
textarea,input,button{width:100%;box-sizing:border-box;margin:6px 0;padding:10px;border-radius:8px;border:1px solid #334;background:#1c2123;color:inherit}
button{background:#12684f;color:#fff;font-weight:600;cursor:pointer;width:auto;padding:10px 22px}
pre{background:#1c2123;padding:12px;border-radius:8px;white-space:pre-wrap}</style>
<h1>ИИ-агент · каркас</h1>
<p>База знаний: <b id=n>?</b> документов. Добавьте документы, спросите — агент ответит с опорой на них.</p>
<h3>1. Документ в базу</h3>
<input id=t placeholder="Название"><textarea id=x rows=4 placeholder="Текст документа"></textarea>
<button onclick="fetch('/api/docs',{method:'POST',body:JSON.stringify({title:t.value,text:x.value})}).then(()=>{x.value='';load()})">Добавить</button>
<h3>2. Вопрос агенту</h3>
<textarea id=q rows=2 placeholder="Например: какие документы нужны для возврата?"></textarea>
<button onclick="ask()">Спросить</button>
<pre id=out>…</pre>
<script>
function load(){fetch('/api/docs').then(r=>r.json()).then(d=>n.textContent=d.count)}
function ask(){out.textContent='…думаю';fetch('/api/ask',{method:'POST',body:JSON.stringify({question:q.value})}).then(r=>r.json()).then(d=>out.textContent=d.answer);load()}
load()</script>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        raw = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            self._send(200, page(), "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._send(200, json.dumps({"ok": True, "docs": len(DOCS), "time": time.strftime("%H:%M:%S")}))
        elif self.path == "/api/docs":
            self._send(200, json.dumps({"count": len(DOCS), "titles": [d["title"] for d in DOCS]}, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "нет пути"}))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return self._send(400, json.dumps({"error": "плохой json"}))
        if self.path == "/api/docs":
            if not payload.get("text"):
                return self._send(400, json.dumps({"error": "пустой текст"}))
            did = add_doc(payload.get("title") or "без названия", payload["text"])
            LOG.append(f"doc#{did} · {payload.get('title','')[:40]}")
            return self._send(200, json.dumps({"ok": True, "id": did}))
        if self.path == "/api/ask":
            q = (payload.get("question") or "").strip()
            if not q:
                return self._send(400, json.dumps({"error": "пустой вопрос"}))
            ctx = search(q)
            answer = None
            try:
                import llm_adapter
                docs_text = chr(10).join(f"[{d['id']}] {d['title']}: {d['text']}" for d in ctx)
                answer = llm_adapter.llm_documents(q, docs_text) if ctx else llm_adapter.llm_documents(q, "база пуста")
            except Exception:
                answer = None
            if not answer:
                answer = llm_answer(q, ctx)
            LOG.append(f"ask · {q[:40]} · опора: {[d['id'] for d in ctx]}")
            return self._send(200, json.dumps({"answer": answer, "sources": [d["id"] for d in ctx]}, ensure_ascii=False))
        self._send(404, json.dumps({"error": "нет пути"}))


if __name__ == "__main__":
    add_doc("Демо-регламент возврата", "Возврат товара возможен в течение 14 дней с чеком. Деньги возвращаются на карту в течение 3 рабочих дней. Обмен без чека — на усмотрение магазина.")
    add_doc("Демо-график доставки", "Доставка по городу: пн-сб 10:00-20:00, слоты по 2 часа. В день заказа — при заказе до 15:00.")
    add_doc("Демо-гарантия", "Гарантия на технику — 12 месяцев, на аксессуары — 30 дней. Гарантийный ремонт: до 14 дней, подменный товар не выдаётся.")
    add_doc("Демо-оплата", "Оплата: карта, СБП, наличные при получении. Предоплата 30% для заказов от 50 000 ₽. Счёт действителен 5 рабочих дней.")
    add_doc("Демо-регистрация юрлиц", "Работаем с юрлицами по договору и счёту. НДС не облагаем. Документы: карточка компании, ИНН, подписанный договор-оферта.")
    print(f"каркас на {BASE} · демо-база: 2 документа · Ctrl+C — стоп")
    ThreadingHTTPServer(("127.0.0.1", 8765), H).serve_forever()
