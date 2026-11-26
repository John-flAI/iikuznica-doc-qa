# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/main.py" дата="2026-10-22">
# @что: FastAPI «Канцеляриста» — приём PDF, карточки, решения человека, витрина.
#       Один сервис (порт 8002): витрина в main, логика в src/*.
# </@паспорт>
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import llm_adapter, pipeline, store, vitrina

app = FastAPI(title="Канцелярист · агент входящих документов", version="0.1.0")


@app.get("/", response_class=HTMLResponse)
def root():
    return vitrina.PAGE


@app.get("/health")
def health():
    return {"status": "ok", "service": "kancelyarist",
            "llm": "готов" if llm_adapter.has_key() else "детерминированный режим"}


@app.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    """Загрузка PDF → карточка: тип, реквизиты с цитатами, расхождения, черновик."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "пустой файл")
    if not data[:5] == b"%PDF-":
        raise HTTPException(415, "пока принимаем только PDF (сканы и DOCX — на хакатоне)")
    return pipeline.process(data, file.filename or "без_имени.pdf")


@app.get("/api/docs")
def docs() -> list[dict]:
    return store.list_docs()


@app.get("/api/docs/{doc_id}")
def doc(doc_id: str) -> dict:
    card = store.get_doc(doc_id)
    if not card:
        raise HTTPException(404, "карточка не найдена")
    card["decisions"] = store.decisions_of(doc_id)
    return card


class DecisionIn(BaseModel):
    action: str          # approve | correct | escalate
    note: str = ""


@app.post("/api/docs/{doc_id}/decision")
def decision(doc_id: str, d: DecisionIn) -> dict:
    if d.action not in ("approve", "correct", "escalate"):
        raise HTTPException(400, "action: approve|correct|escalate")
    card = store.add_decision(doc_id, d.action, d.note)
    if not card:
        raise HTTPException(404, "карточка не найдена")
    return card
