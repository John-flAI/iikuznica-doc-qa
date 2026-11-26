# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/store.py" дата="2026-10-22">
# @что: SQLite-хранилище карточек документов и журнала решений человека.
#       Файл: karkas/data/канцелярист.db (создаётся сам).
# </@паспорт>
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# KANC_DB — путь к базе (для чистого демо без затирания боевой журнальной базы)
_DB = Path(os.environ.get("KANC_DB")
           or Path(__file__).resolve().parent.parent / "data" / "канцелярист.db")


def _conn() -> sqlite3.Connection:
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS docs(
        id TEXT PRIMARY KEY, received_at TEXT, filename TEXT,
        doc_type TEXT, confidence REAL, payload TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS decisions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT, ts TEXT,
        action TEXT, note TEXT, actor TEXT DEFAULT 'человек')""")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save_doc(card: dict) -> None:
    with _conn() as c:
        c.execute("INSERT OR REPLACE INTO docs VALUES(?,?,?,?,?,?)",
                  (card["id"], card["received_at"], card["filename"],
                   card["doc_type"], card["confidence"],
                   json.dumps(card, ensure_ascii=False)))


def get_doc(doc_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT payload FROM docs WHERE id=?", (doc_id,)).fetchone()
    return json.loads(row[0]) if row else None


def list_docs() -> list[dict]:
    """Краткая лента для витрины: новые сверху."""
    with _conn() as c:
        rows = c.execute("SELECT payload FROM docs ORDER BY received_at DESC").fetchall()
    out = []
    for (payload,) in rows:
        card = json.loads(payload)
        out.append({"id": card["id"], "filename": card["filename"],
                    "doc_type": card["doc_type"], "confidence": card["confidence"],
                    "status": card["status"], "received_at": card["received_at"],
                    "сумма": (card.get("fields") or {}).get("Сумма", {}).get("value"),
                    "контрагент": (card.get("fields") or {}).get("Контрагент", {}).get("value"),
                    "расхождений": len(card.get("mismatches", []))})
    return out


def add_decision(doc_id: str, action: str, note: str, actor: str = "человек") -> dict | None:
    """Запись в журнал + смена статуса карточки. → обновлённая карточка."""
    with _conn() as c:
        row = c.execute("SELECT payload FROM docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return None
        card = json.loads(row[0])
        c.execute("INSERT INTO decisions(doc_id, ts, action, note, actor) VALUES(?,?,?,?,?)",
                  (doc_id, _now(), action, note, actor))
        card["status"] = {"approve": "согласован", "correct": "исправлен",
                          "escalate": "эскалировано"}.get(action, card["status"])
        c.execute("UPDATE docs SET payload=? WHERE id=?",
                  (json.dumps(card, ensure_ascii=False), doc_id))
    return card


def decisions_of(doc_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT ts, action, note, actor FROM decisions "
                         "WHERE doc_id=? ORDER BY id DESC", (doc_id,)).fetchall()
    return [{"ts": ts, "action": a, "note": n, "actor": actor} for ts, a, n, actor in rows]
