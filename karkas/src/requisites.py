# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/src/requisites.py" дата="2026-10-22">
# @что: извлечение реквизитов по регуляркам, каждое поле — с провенансом
#       {значение, страница (с 1), цитата (строка из PDF)}. Ничего не выдумываем:
#       нет совпадения — поля в карточке нет.
# </@паспорт>
import re

# (имя поля, регэксп, номер группы значения)
_PATTERNS: list[tuple[str, str, int]] = [
    ("ИНН", r"\bИНН[:\s№]*(\d{10}(?:\d{2})?)\b", 1),
    ("КПП", r"\bКПП[:\s]*(\d{9})\b", 1),
    ("ОГРН", r"\bОГРН(?:ИП)?[:\s]*(\d{13}(?:\d{2})?)\b", 1),
    ("р/с", r"(?:р/с|расч\.?\s*сч[её]т|расчетный\s+счет|расчётный\s+счет)[:\s№]*(\d{20})", 1),
    ("к/с", r"(?:к/с|кор\.?\s*сч[её]т)[:\s№]*(\d{20})", 1),
    ("БИК", r"\bБИК[:\s]*(\d{9})\b", 1),
    # банк: сначала строка «р/с <20 цифр> в <Банк>, …», потом «Банк: Имя»
    ("Банк", r"(?:р/с|расч[.а-яё\s]*сч[а-яё]*)\s*\d{20}\s+в\s+([^,\n]{3,60})", 1),
    ("Банк", r"Банк[:\s]+([А-ЯЁA-Z][^«»\n:,;]{2,60})", 1),
    ("Дата документа", r"\bот\s*(\d{2}\.\d{2}\.\d{4})", 1),
    ("Срок оплаты", r"(?:оплат[а-яё]*\s+до|срок\s+оплаты\s*[:\-]?)\s*(\d{2}\.\d{2}\.\d{4})", 1),
    ("Номер", r"№\s*([0-9А-ЯA-Z][0-9А-ЯA-Z\-/]{0,20})", 1),
    ("Подписант", r"(?:Генеральный\s+директор|Директор)\s+([А-ЯЁ][а-яё\-]+(?:\s+[А-ЯЁ][а-яё\-]+)?)", 1),
]

_КОНТРАГЕНТ = re.compile(
    r"(?:Поставщик|Исполнитель|Отправитель|Продавец)\s*[:：]\s*(.+)", re.IGNORECASE)
_ДЕНЬГИ = re.compile(r"(\d[\d\s]{2,11}[.,]\d{2})\s*(?:руб|₽|rub)", re.IGNORECASE)
_НДС = re.compile(r"НДС[^0-9\n]{0,25}(\d[\d\s]*[.,]\d{2})")
_ИТОГ_МЕТКИ = ("итого", "всего", "к оплате", "сумма к оплате")


def _цитата(line: str) -> str:
    line = " ".join(line.split())
    return line[:140] + ("…" if len(line) > 140 else "")


def _чист(v: str) -> str:
    """Декоративные кавычки-огрызки наружу: «Северный» → Северный,
    но ПАО «Северный» не трогаем (парные внешние снимаем только целиком)."""
    v = v.strip()
    if len(v) >= 2 and v[0] in "«\"" and v[-1] in "»\"":
        v = v[1:-1]
    return v.strip()


def _деньги_число(raw: str) -> float:
    return float(raw.replace(" ", "").replace("\u00a0", "").replace(",", "."))


def _формат_денег(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} ₽"


def extract(pages: list[tuple[int, str]]) -> dict[str, dict]:
    """pages: [(номер с 1, текст страницы)] → {поле: {value, страница, цитата}}"""
    fields: dict[str, dict] = {}

    def put(имя: str, value, page: int, line: str):
        if имя not in fields:
            fields[имя] = {"value": value, "страница": page, "цитата": _цитата(line)}

    # простые регэксп-поля: первое совпадение в документе
    for page_no, text in pages:
        for line in text.splitlines():
            for имя, pattern, grp in _PATTERNS:
                if имя in fields:
                    continue
                m = re.search(pattern, line, re.IGNORECASE)
                if m:
                    put(имя, _чист(m.group(grp)), page_no, line)

    # контрагент: строка «Поставщик: …», обрезаем по ИНН
    for page_no, text in pages:
        for line in text.splitlines():
            m = _КОНТРАГЕНТ.search(line)
            if m:
                name = m.group(1).split("ИНН")[0].strip(" .,;—-")
                if name:
                    put("Контрагент", name, page_no, line)
            if "Контрагент" in fields:
                break
        if "Контрагент" in fields:
            break

    # сумма: сначала строка с меткой «итого/всего/к оплате», иначе максимум
    деньги: list[tuple[float, int, str]] = []
    for page_no, text in pages:
        for line in text.splitlines():
            for m in _ДЕНЬГИ.finditer(line):
                деньги.append((_деньги_число(m.group(1)), page_no, line))
            m = _НДС.search(line)
            if m:
                put("НДС", _формат_денег(_деньги_число(m.group(1))), page_no, line)
    if деньги:
        метка = next((d for d in деньги
                      if any(k in d[2].lower() for k in _ИТОГ_МЕТКИ)), None)
        value, page, line = метка or max(деньги, key=lambda d: d[0])
        put("Сумма", _формат_денег(value), page, line)
        fields["Сумма"]["value_num"] = value

    return fields
