# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/pipeline.py" дата="2026-10-22">
# @что: конвейер одного документа: байты PDF → текст по страницам → тип → реквизиты
#       с цитатами → сверка с реестром → черновик → карточка в базу.
#       Скан без текстового слоя — честная эскалация, не фантазия.
# </@паспорт>
import io
import re
import secrets
from datetime import datetime, timezone

from pypdf import PdfReader

from . import classify, drafts, llm_adapter, registry, requisites, store


def extract_pages(data: bytes) -> list[tuple[int, str]]:
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        return []
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append((i, text))
    return pages


def process(data: bytes, filename: str) -> dict:
    pages = extract_pages(data)
    full_text = "\n".join(t for _, t in pages)
    doc_id = "doc_" + secrets.token_hex(4)

    card = {
        "id": doc_id,
        "filename": filename,
        "received_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "status": "новый",
        "llm": llm_adapter.has_key(),
    }

    if not re.search(r"[А-Яа-яёЁA-Za-z]", full_text):
        # нечитаемо: скан без слоя или не документ — эскалируем человеку сразу
        card.update({
            "doc_type": "нечитаемо", "confidence": 0.0, "fields": {},
            "mismatches": [], "draft": {
                "text": "Текст из файла не извлёкся (вероятно, скан без текстового слоя). "
                        "Передаю человеку — распознавать и решать будет сотрудник.",
                "citations": [], "escalate_recommended": True},
        })
        card["status"] = "эскалировано"
        store.save_doc(card)
        store.add_decision(doc_id, "escalate", "авто: нет текстового слоя", actor="агент")
        return card

    doc_type, conf = classify.classify(full_text)
    fields = requisites.extract(pages)
    mismatches = registry.сверка(fields, doc_type)
    draft = drafts.compose(doc_type, fields, mismatches, filename, наш_текст=full_text)

    # LLM полирует тон черновика, если настроен ключ; факты и цитаты остаются машинными
    polished = llm_adapter.llm_answer(draft["text"], " ".join(draft["citations"]))
    if polished:
        draft = {**draft, "text": polished, "llm_polished": True}

    card.update({
        "doc_type": doc_type, "confidence": conf, "fields": fields,
        "mismatches": mismatches, "draft": draft,
    })
    if draft["escalate_recommended"] or mismatches:
        card["status"] = "эскалировано"
        store.add_decision(doc_id, "escalate",
                           "авто: " + ("расхождения в реквизитах" if mismatches
                                       else f"тип «{doc_type}» требует человека"),
                           actor="агент")
    store.save_doc(card)
    return card
