# -*- coding: utf-8 -*-
# <@паспорт файл="PROJECTS/zarabotok/iikuznica/karkas/demo/проверка.py" дата="2026-10-22">
# @что: самопроверка конвейера на демо-пакете (без сервера): каждый PDF → конвейер.
#       Ожидание: 10/10 разобрано, типы верны, ровно 2 ловушки найдены (04 р/с, 05 ИНН).
#       Запуск из karkas/: python demo/проверка.py
# </@паспорт>
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import pipeline  # noqa: E402

ОЖИДАНИЯ = {
    "01_schet_kanc_127.pdf": "счёт",
    "02_schet_kanc_128.pdf": "счёт",
    "03_schet_romashka_045.pdf": "счёт",
    "04_schet_romashka_046.pdf": "счёт",     # ловушка: подменён р/с
    "05_schet_kanc_129.pdf": "счёт",         # ловушка: ИНН на цифру отличается
    "06_dogovor_D2026-14.pdf": "договор",
    "07_akt_uslug_12.pdf": "акт",
    "08_pismo_kozlov_44.pdf": "письмо-запрос",
    "09_pismo_romashka_45.pdf": "письмо-запрос",
    "10_reklama_kofe.pdf": "прочее",
}


def main() -> int:
    пакет = Path(__file__).resolve().parent / "пакет"
    файлы = sorted(p for p in пакет.glob("*.pdf"))
    if not файлы:
        print("нет PDF — сначала: python demo/make_demo_pdfs.py")
        return 2

    t0, поля, расхождения_доков, типы_факт = time.time(), 0, [], {}
    print(f"{'файл':34} {'тип':14} {'полей':5} {'расхождения'}")
    for p in файлы:
        card = pipeline.process(p.read_bytes(), p.name)
        типы_факт[p.name] = card["doc_type"]
        mis = card["mismatches"]
        поля += len(card["fields"])
        if mis:
            расхождения_доков.append(p.name)
        флаг = " ".join(f"⚠{m['поле']}({m['важность']})" for m in mis) or "—"
        print(f"{p.name:34} {card['doc_type']:14} {len(card['fields']):5} {флаг}")

    сек = time.time() - t0
    типы_ок = all(типы_факт.get(n) == t for n, t in ОЖИДАНИЯ.items())
    print(f"\nМетрики: {len(файлы)}/{len(ОЖИДАНИЯ)} разобрано · {поля} полей извлечено "
          f"(в среднем {поля / len(файлы):.1f}/док) · {сек:.1f} с ({сек / len(файлы) * 1000:.0f} мс/док)")

    лов = [n for n in расхождения_доков if "04_" in n or "05_" in n]
    лишние = [n for n in расхождения_доков if n not in ("04_schet_romashka_046.pdf",
                                                        "05_schet_kanc_129.pdf")]
    ok = (len(файлы) == 10 and типы_ок and len(лов) == 2 and not лишние)
    print(f"Типы: {'OK' if типы_ок else 'ЕСТЬ ПРОМАХИ'} · "
          f"ловушки {len(лов)}/2 {'OK' if len(лов) == 2 else 'НЕ НАЙДЕНЫ'}"
          + (f" · лишние расхождения: {лишние}" if лишние else ""))
    print("ИТОГ:", "ПАКЕТ ПРОШЁЛ" if ok else "ПАКЕТ НЕ ПРОШЁЛ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
