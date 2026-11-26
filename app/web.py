# -*- coding: utf-8 -*-
"""Веб-стенд: чат сотрудника, служба качества, документы с подсветкой, аналитика.

Один адрес, три вида: / — полный стенд (чат и правая колонна), /user — только чат,
/qc — только служба качества. Все ресурсы локальные: стенду не нужен интернет.
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import KNOWLEDGE
from .engine import load_engine
from .llm import LLMClient
from .service import EXAMPLES, Service
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / 'static'


def build(with_vectors=True):
    """Собрать службу из того, что задано окружением."""
    llm = LLMClient(config.LLM_BASE_URL, config.LLM_MODEL, config.LLM_API_KEY, config.LLM_FALLBACK_MODEL)
    return Service(load_engine(with_vectors=with_vectors), llm, Store(config.DB_PATH))


class AskIn(BaseModel):
    session_id: str | None = None
    text: str


class AnswerIn(BaseModel):
    answer: str
    author: str = 'Служба качества'


def create_app(service=None, with_vectors=True):
    @asynccontextmanager
    async def lifespan(app):
        if not hasattr(app.state, 'service'):           # на сервере служба собирается при запуске
            app.state.service = build(with_vectors)
        yield

    app = FastAPI(title='Агент ответов по регламентам', lifespan=lifespan, docs_url=None, redoc_url=None)
    if service is not None:                              # в тестах служба передана — доступна сразу
        app.state.service = service
    app.mount('/static', StaticFiles(directory=STATIC), name='static')

    def svc(request: Request) -> Service:
        return request.app.state.service

    @app.get('/robots.txt')
    def robots():
        return FileResponse(STATIC / 'robots.txt', media_type='text/plain')

    @app.get('/api/health')
    def health(request: Request):
        service = svc(request)
        return {'ok': True, 'regulations': len(service.engine.corpus.docs) - (KNOWLEDGE in service.engine.corpus.docs),
                'knowledge': sum(1 for u in service.engine.corpus.units if u.doc == KNOWLEDGE),
                'model': config.LLM_MODEL, 'embeddings': config.EMBED_MODEL,
                'vectors': service.engine.search.vectors is not None, 'examples': EXAMPLES}

    @app.post('/api/session')
    def session(request: Request):
        return {'id': svc(request).new_session()}

    @app.get('/api/session/{session_id}')
    def history(session_id: str, request: Request, after: int = 0):
        return {'id': session_id, 'messages': svc(request).store.messages(session_id, after)}

    @app.post('/api/ask')
    def ask(body: AskIn, request: Request):
        try:
            return svc(request).ask(body.session_id, body.text)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get('/api/tickets')
    def tickets(request: Request, status: str | None = None):
        if status in ('draft', 'open', 'answered'):
            return svc(request).store.tickets(status)
        return [t for t in svc(request).store.tickets() if t['status'] != 'draft']

    @app.post('/api/tickets/{ticket_id}/send')
    def send(ticket_id: int, request: Request):
        service = svc(request)
        ticket = service.store.ticket(ticket_id)
        if ticket is None:
            raise HTTPException(404, 'нет такого запроса')
        if ticket['status'] != 'draft':                 # повторная отправка уже отправленного письма
            raise HTTPException(409, 'запрос уже отправлен или на него уже ответили')
        return service.send(ticket_id)

    @app.post('/api/tickets/{ticket_id}/answer')
    def answer(ticket_id: int, body: AnswerIn, request: Request):
        try:
            return svc(request).answer(ticket_id, body.answer, body.author)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get('/api/document/{doc_id}')
    def document(doc_id: str, request: Request):
        try:
            return svc(request).document(doc_id)
        except KeyError:
            raise HTTPException(404, 'нет такого регламента')

    @app.get('/api/analytics')
    def analytics(request: Request, days: int | None = None):
        return svc(request).analytics(days)

    @app.get('/api/weekly')
    def weekly(request: Request, days: int = 7):
        return svc(request).weekly(days)

    for path in ('/', '/user', '/qc', '/analytics'):
        app.get(path)(lambda: FileResponse(STATIC / 'index.html'))
    return app


app = create_app()
