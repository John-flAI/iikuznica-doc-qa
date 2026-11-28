# -*- coding: utf-8 -*-
"""Веб-стенд: чат сотрудника, служба качества, документы с подсветкой, аналитика.

Один адрес, три вида: / — полный стенд (чат и правая колонна), /user — только чат,
/qc — только служба качества. Все ресурсы локальные: стенду не нужен интернет.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import config
from .agent import KNOWLEDGE
from .engine import load_engine
from .llm import LLMClient
from .ratelimit import RateLimiter, address_key, client_ip
from .service import EXAMPLES, Service
from .store import Store

STATIC = Path(__file__).resolve().parent.parent / 'static'
LIMIT_NOTICE = ('Демонстрационный стенд: не более %d вопросов в час с одного адреса — так мы бережём бюджет модели. '
                'Ближайшее место освободится через %d мин.')


def build(with_vectors=True):
    """Собрать службу из того, что задано окружением."""
    llm = LLMClient(config.LLM_BASE_URL, config.LLM_MODEL, config.LLM_API_KEY, config.LLM_FALLBACK_MODEL)
    catalog = config.models()                 # модели на выбор — из LLM_MODELS; каждая со своим ключом из окружения
    clients = {item['id']: LLMClient(item['base_url'], item['model'], item['key']) for item in catalog}
    return Service(load_engine(with_vectors=with_vectors), llm, Store(config.DB_PATH), llms=clients,
                   catalog=[{k: item[k] for k in ('id', 'label', 'model')} for item in catalog])


SID = Annotated[str, PathParam(min_length=1, max_length=128)]     # идентификатор сессии в пути — как выдаёт /api/session
ROW = Annotated[int, PathParam(ge=1, le=2 ** 31)]                 # номер запроса или письма: без гигантских чисел
QUESTION_LIMIT = 500        # знаков: слишком длинный вопрос не доходит до модели
ANSWER_LIMIT = 2000         # знаков в ответе службы качества
AUTHOR_LIMIT = 100          # знаков в имени автора
NAME_LIMIT = 100            # знаков в имени чата на вкладке


class AskIn(BaseModel):
    session_id: str | None = None
    text: str = Field(max_length=QUESTION_LIMIT)

    @field_validator('text', mode='before')
    @classmethod
    def russian_length_error(cls, value):
        if isinstance(value, str) and len(value) > QUESTION_LIMIT:
            raise ValueError('Вопрос слишком длинный: не более %d знаков' % QUESTION_LIMIT)
        return value


class SessionIn(BaseModel):
    model: str | None = Field(None, max_length=64)      # id модели из списка в /api/health; нет — модель по умолчанию


class ResetIn(BaseModel):
    confirm: str = Field('', max_length=20)             # кнопка присылает ровно «СБРОС» — защита от случайного клика


class ModelIn(BaseModel):
    model: str = Field(max_length=64)


class NameIn(BaseModel):
    name: str = Field('', max_length=1000)              # имя чата на вкладке; пустое — имя по умолчанию (лимит 100 — ниже)


class LoginIn(BaseModel):
    role: str = Field(max_length=20)                    # фиксированная роль из /api/health: 'client' или 'quality'


def mail_delivered(value):
    """Статус доставки письма в JSON: NULL → null, 0 → false, 1 → true."""
    return None if value is None else bool(value)


def mail_view(row, address):
    """Письмо для списка ящика: адрес роли решает, исходящее это или входящее."""
    return {'id': row['id'], 'created': row['created'], 'kind': row['kind'],
            'outgoing': row['mail_from'] == address,
            'from': row['mail_from'], 'to': row['mail_to'], 'cc': row['mail_cc'] or '',
            'subject': row['subject'], 'snippet': (row['text'] or '')[:160],
            'ticket_id': row['ticket_id'], 'delivered': mail_delivered(row['delivered'])}


def mail_full(row):
    """Письмо целиком для просмотра."""
    return {'id': row['id'], 'created': row['created'], 'kind': row['kind'],
            'from': row['mail_from'], 'to': row['mail_to'], 'cc': row['mail_cc'] or '',
            'subject': row['subject'], 'text': row['text'], 'ticket_id': row['ticket_id'],
            'delivered': mail_delivered(row['delivered'])}


class AnswerIn(BaseModel):
    answer: str = Field(max_length=ANSWER_LIMIT)
    author: str = Field('Служба качества', max_length=AUTHOR_LIMIT)

    @field_validator('answer', mode='before')
    @classmethod
    def russian_answer_error(cls, value):
        if isinstance(value, str) and len(value) > ANSWER_LIMIT:
            raise ValueError('Ответ слишком длинный: не более %d знаков' % ANSWER_LIMIT)
        return value

    @field_validator('author', mode='before')
    @classmethod
    def russian_author_error(cls, value):
        if isinstance(value, str) and len(value) > AUTHOR_LIMIT:
            raise ValueError('Имя автора слишком длинное: не более %d знаков' % AUTHOR_LIMIT)
        return value


def create_app(service=None, with_vectors=True, ask_limit=None):
    limiter = RateLimiter(config.ASK_LIMIT if ask_limit is None else ask_limit)

    def owner_of(request: Request) -> str:
        """Владелец запроса — тот же адрес, что у лимита вопросов: аккаунт создаётся по адресу.

        Считаем один раз на запрос и передаём вниз: чужая история, чужие запросы и чужие ответы
        службы качества этому владельцу не видны.
        """
        return address_key(client_ip(request.headers.get('x-forwarded-for'),
                                     request.client.host if request.client else None))

    @asynccontextmanager
    async def lifespan(app):
        if not hasattr(app.state, 'service'):           # на сервере служба собирается при запуске
            app.state.service = build(with_vectors)
        yield

    # страница документации API и схема наружу не отдаются: стенд — для сотрудников и жюри
    app = FastAPI(title='Агент ответов по регламентам', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if service is not None:                              # в тестах служба передана — доступна сразу
        app.state.service = service
    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    PAGES = ('/', '/user', '/qc', '/analytics')

    @app.middleware('http')
    async def no_stale_client(request, call_next):
        """Страницы и статика — без эвристического кэша браузера: после обновления стенда новая страница не должна
        смешаться со старым app.js. Браузер каждый раз сверяет ETag — файл не менялся, ответ 304 без тела.
        Заодно — заголовки безопасности: стенду не нужен интернет, всё своё, поэтому CSP строгий ('self';
        'unsafe-inline' в script — только для маленького загрузчика входа в index.html, style — для подстраховки)."""
        response = await call_next(request)
        if request.url.path.startswith('/static/') or request.url.path in PAGES:
            response.headers['cache-control'] = 'no-cache'
        response.headers['content-security-policy'] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                       "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                                       "connect-src 'self'; font-src 'self'; base-uri 'self'; "
                                                       "form-action 'self'; frame-ancestors 'none'")
        response.headers['x-content-type-options'] = 'nosniff'
        response.headers['x-frame-options'] = 'DENY'
        response.headers['referrer-policy'] = 'strict-origin-when-cross-origin'
        response.headers['permissions-policy'] = 'camera=(), microphone=(), geolocation=(), interest-cohort=()'
        return response

    def svc(request: Request) -> Service:
        return request.app.state.service

    @app.get('/robots.txt')
    def robots():
        return FileResponse(STATIC / 'robots.txt', media_type='text/plain')

    @app.get('/favicon.ico')
    def favicon():
        return FileResponse(STATIC / 'favicon.ico', media_type='image/x-icon')

    @app.get('/api/health')
    def health(request: Request):
        service = svc(request)
        visible = service.knowledge_of(owner_of(request))          # база знаний — только свои записи
        return {'ok': True, 'regulations': len(service.engine.corpus.docs) - (KNOWLEDGE in service.engine.corpus.docs),
                'knowledge': sum(1 for u in service.engine.corpus.units
                                 if u.doc == KNOWLEDGE and (visible is None or u.uid in visible)),
                'model': service.catalog[0]['model'] if service.catalog else config.LLM_MODEL,   # по умолчанию — первая в каталоге
                'model_served': service.store.served_model(),     # что реально ответило на последний запрос
                'vectors': getattr(service.engine.search, 'semantic', service.engine.search.vectors is not None),
                'examples': EXAMPLES,
                'ask_limit': limiter.limit,     # вопросов в час с одного адреса; клиент пишет это на стенде
                'models': service.models(), 'model_default': service.catalog[0]['id'] if service.catalog else None,
                'accounts': {role: {'name': name, 'email': email} for role, (name, email) in config.ACCOUNTS.items()},
                'mail': {'smtp': bool(config.MAIL_SMTP)}}   # письмо уходит по SMTP, только если сервер задан

    @app.post('/api/session')
    def session(request: Request, body: SessionIn | None = None):
        return {'id': svc(request).new_session(model=body.model if body else None, owner=owner_of(request))}

    @app.get('/api/sessions')
    def sessions(request: Request):
        return svc(request).store.sessions_of(owner_of(request))

    @app.post('/api/session/{session_id}/name')
    def session_name(session_id: SID, body: NameIn, request: Request):
        store = svc(request).store
        if store.session_for(session_id, owner_of(request)) is None:    # чужая, закрытая или нет такой
            raise HTTPException(404, 'нет такой сессии')
        name = (body.name or '').strip()                # по краям обрезаем; пустое — имя по умолчанию
        if len(name) > NAME_LIMIT:
            raise HTTPException(400, 'Имя слишком длинное: не более %d знаков' % NAME_LIMIT)
        renamed = store.rename_session(session_id, name)
        return {'id': session_id, 'name': renamed['name']}

    @app.delete('/api/session/{session_id}')
    def session_close(session_id: SID, request: Request):
        if svc(request).store.close_session(session_id, owner_of(request)) is None:   # чужая, закрытая или нет такой
            raise HTTPException(404, 'нет такой сессии')
        return {'id': session_id, 'closed': True}

    @app.post('/api/session/{session_id}/model')
    def session_model(session_id: SID, body: ModelIn, request: Request):
        if svc(request).store.session_for(session_id, owner_of(request)) is None:    # чужая сессия — не существует
            raise HTTPException(404, 'нет такой сессии')
        try:
            return svc(request).set_model(session_id, body.model)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get('/api/session/{session_id}')
    def history(session_id: SID, request: Request,
                after: int = Query(0, ge=0, le=2 ** 31)):          # «после такого сообщения»: без гигантских чисел
        found = svc(request).store.session_for(session_id, owner_of(request))
        if found is None:                     # чужая история для этого владельца не существует — не подсказываем
            raise HTTPException(404, 'нет такой сессии')
        return {'id': session_id, 'messages': svc(request).store.messages(session_id, after)}

    @app.post('/api/ask')
    def ask(body: AskIn, request: Request):
        text = ' '.join((body.text or '').split())
        if not text:
            # пустой вопрос не тратит лимит: проверяем до списания квоты (сервис ответил бы тем же)
            raise HTTPException(400, 'Вопрос пустой')
        owner = owner_of(request)
        # адрес — из заголовка прокси (стенд слушает только локальный порт): он же владелец сессии и запросов
        allowed, wait = limiter.allow(owner)
        if not allowed:
            raise HTTPException(429, LIMIT_NOTICE % (limiter.limit, -(-wait // 60)),
                                headers={'Retry-After': str(wait)})
        try:
            return svc(request).ask(body.session_id, text, owner)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post('/api/login')
    def login(body: LoginIn):
        """Вход фиктивный: роль сверяется со списком, пароля нет, сервер ничего не запоминает."""
        account = config.ACCOUNTS.get(body.role)
        if account is None:
            raise HTTPException(400, 'Нет такой роли: клиент, служба качества или агент')
        name, email = account
        return {'role': body.role, 'name': name, 'email': email}

    @app.get('/api/letters')
    def letters(request: Request, role: str | None = Query(None, max_length=20)):
        """Ящик роли: письма владельца адреса, где адрес роли стоит во from, to или cc; новые сверху."""
        account = config.ACCOUNTS.get(role)
        if account is None:
            raise HTTPException(400, 'Нет такой роли: клиент, служба качества или агент')
        store = svc(request).store
        return [mail_view(row, account[1]) for row in store.letters(owner_of(request), account[1])]

    @app.get('/api/letter/{letter_id}')
    def letter(letter_id: ROW, request: Request):
        found = svc(request).store.letter(letter_id, owner=owner_of(request))
        if found is None:                     # чужое письмо — как «нет такого», номер не подсказываем
            raise HTTPException(404, 'нет такого письма')
        return mail_full(found)

    @app.get('/api/tickets')
    def tickets(request: Request, status: str | None = None):
        owner = owner_of(request)
        store = svc(request).store
        result = store.tickets(status, owner=owner) if status in ('draft', 'open', 'answered') \
            else [t for t in store.tickets(owner=owner) if t['status'] != 'draft']
        for ticket in result:                 # почта запроса: доставлены ли все её письма
            ticket['letter_delivered'] = store.letter_delivered(ticket['id'])
        return result

    @app.post('/api/tickets/{ticket_id}/send')
    def send(ticket_id: ROW, request: Request):
        service = svc(request)
        ticket = service.store.ticket(ticket_id, owner=owner_of(request))
        if ticket is None:                              # чужой запрос — как «нет такого», номер не подсказываем
            raise HTTPException(404, 'нет такого запроса')
        if ticket['status'] != 'draft':                 # повторная отправка уже отправленного письма
            raise HTTPException(409, 'запрос уже отправлен или на него уже ответили')
        return service.send(ticket_id)

    @app.post('/api/tickets/{ticket_id}/answer')
    def answer(ticket_id: ROW, body: AnswerIn, request: Request):
        service = svc(request)
        ticket = service.store.ticket(ticket_id, owner=owner_of(request))
        if ticket is None:
            raise HTTPException(404, 'нет такого запроса')
        if ticket['status'] == 'answered':              # повторный ответ перезаписывал ответ и не обновлял базу знаний
            raise HTTPException(409, 'на этот запрос уже ответили — повторный ответ не нужен')
        try:
            return service.answer(ticket_id, body.answer, body.author)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post('/api/stand/reset')
    def stand_reset(body: ResetIn, request: Request):
        """Кнопка «Настройки → Стенд и API → Сбросить стенд»: то же, что прибор stand_reset.py —
        база диалогов/запросов/писем переносится в бэкап в томе, тестовый ящик очищается, процесс
        завершается — контейнер (restart=unless-stopped) поднимается с пустой базой. Регламенты,
        векторный индекс и конфиг не трогаются: сбрасываются только данные демо."""
        try:
            svc(request).stand_reset(body.confirm)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {'ok': True, 'text': 'Стенд сброшен: база в бэкапе, ящик очищен, стенд перезапускается — страница обновится сама.'}

    @app.get('/api/document/{doc_id}')
    def document(doc_id: str, request: Request):
        try:
            return svc(request).document(doc_id, owner_of(request) if doc_id == KNOWLEDGE else None)   # база знаний — своя
        except KeyError:
            raise HTTPException(404, 'нет такого регламента')

    @app.get('/api/analytics')
    def analytics(request: Request, days: int | None = Query(None, ge=1, le=3660)):   # период — от дня до десяти лет
        return svc(request).analytics(days, owner=owner_of(request))

    @app.get('/api/weekly')
    def weekly(request: Request, days: int = Query(7, ge=1, le=3660)):
        return svc(request).weekly(days, owner=owner_of(request))

    @app.exception_handler(404)
    async def page_404(request: Request, exc):
        """Неизвестная страница ведёт на стенд (жюри не видит сырой JSON); API и статика отвечают JSON-ом как раньше."""
        if request.url.path.startswith(('/api/', '/static/')):
            return JSONResponse({'detail': getattr(exc, 'detail', 'Not Found')}, status_code=404)
        return RedirectResponse('/', status_code=302)

    for path in PAGES:
        app.api_route(path, methods=['GET', 'HEAD'])(lambda: FileResponse(STATIC / 'index.html'))
    return app


app = create_app()
