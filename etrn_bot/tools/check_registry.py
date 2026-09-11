"""
Сверка реестра полей с действующей XSD (задача 1 из CLAUDE.md).

Печатает две дельты:
  1. поля реестра, чей путь `xsd=` не найден в схеме (или не проставлен);
  2. обязательные узлы схемы, не покрытые ни реестром, ни шаблоном.

Пустые обе — маппинг сверен.

Почему нужен второй список отдельно: часть обязательных узлов схемы человек
не заполняет никогда — версия формата, КНД, идентификатор файла, признаки
координации времени. Их закрывает шаблон, и держать их в реестре означало бы
спрашивать бухгалтера про версию формата. Такие узлы перечислены в
TEMPLATE_OWNED — с обоснованием у каждого, иначе список превращается
в место, куда прячут несверенное.

    PYTHONPATH=. python3 -m etrn_bot.tools.check_registry
    PYTHONPATH=. python3 -m etrn_bot.tools.check_registry --schema <файл.xsd>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..fields import ETRN, ZAYAVKA, registry_for

# Со схемой ФНС сверяется только накладная: у заявки госформата нет.
REGISTRY = registry_for(ETRN)
from .xsd_map import Schema, Walker

DEFAULT_SCHEMA = (Path(__file__).resolve().parents[2] / "schema"
                  / "ON_TRNACLGROT_1_973_01_05_01_02.xsd")

# Обязательные узлы схемы, которые заполняет шаблон, а не человек.
TEMPLATE_OWNED: dict[str, str] = {
    "/Файл/@ИдФайл": "повторяет имя файла обмена — собирается в filename()",
    "/Файл/@ВерсФорм": "константа формата 5.01",
    "/Файл/@ВерсПрог": "версия нашего бота",
    "/Файл/Документ": "корневой контейнер",
    "/Файл/Документ/@КНД": "константа 1110339 для титула 1",
    "/Файл/Документ/@ПоФактХЖ": "константа: наименование документа по факту хоз. жизни",
    "/Файл/Документ/@ДатИнфГО": "дата формирования файла — время сборки",
    "/Файл/Документ/@ВрИнфГО": "время формирования файла — время сборки",
    "/Файл/Документ/СодИнфГО": "контейнер содержания",
    "/Файл/Документ/СодИнфГО/@СодОпер": "содержание операции — политика организации",
    "/Файл/Документ/СодИнфГО/СвПогруз/@НалКоорТочВрЗаяв":
        "признак UTC в заявленном времени: пишем локальное время, признак фиксирован",
    "/Файл/Документ/СодИнфГО/СвПогруз/@НалКоорТочВрФПогр": "то же для фактического прибытия",
    "/Файл/Документ/СодИнфГО/СвПогруз/@НалКоорТочВрФУбыт": "то же для фактического убытия",
}


def schema_nodes(path: Path) -> tuple[set[str], list[str]]:
    """→ (все пути схемы, обязательные пути в порядке схемы)."""
    schema = Schema(str(path))
    rows = Walker(schema, max_depth=12, only_required=False).run()
    every: set[str] = set()
    required: list[str] = []
    for raw, req, _typ, _doc in rows:
        p = raw.split(" ")[0]
        every.add(p)
        if req and p not in required:
            required.append(p)
    return every, required


def main() -> int:
    ap = argparse.ArgumentParser(description="сверка реестра полей со схемой")
    ap.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    args = ap.parse_args()

    path = Path(args.schema)
    if not path.exists():
        print(f"схема не найдена: {path}")
        print("положите действующую XSD в schema/ (см. schema/README.md)")
        return 2

    every, required = schema_nodes(path)

    # ── дельта 1: поля реестра без соответствия в схеме ────────────────────
    no_match: list[tuple[str, str]] = []
    for f in REGISTRY:
        if f.not_in_xml:
            # Поле сознательно не пишется в XML. Требуем объяснение: без него
            # not_in_xml становится способом спрятать несверенное поле.
            if not f.why:
                no_match.append((f.key, "not_in_xml без объяснения в why"))
            if f.xsd:
                no_match.append((f.key, "not_in_xml, но путь xsd проставлен"))
            continue
        if not f.xsd:
            no_match.append((f.key, "нет xsd="))
            continue
        for xp in f.xsd.split("|"):
            if xp.strip() not in every:
                no_match.append((f.key, f"путь не найден в схеме: {xp.strip()}"))

    # ── дельта 2: обязательные узлы схемы без покрытия ─────────────────────
    covered: set[str] = set(TEMPLATE_OWNED)
    for f in REGISTRY:
        if f.xsd:
            covered.update(p.strip() for p in f.xsd.split("|"))
    # узел считается покрытым и тогда, когда покрыт его потомок:
    # <СвГруз> закрывается полями внутри него, спрашивать про контейнер нечего
    uncovered = [p for p in required
                 if p not in covered
                 and not any(c.startswith(p + "/") or c.startswith(p + "@")
                             for c in covered)]

    print(f"схема: {path.name}")
    print(f"полей в реестре: {len(REGISTRY)}, узлов в схеме: {len(every)}, "
          f"обязательных: {len(required)}\n")

    print(f"── дельта 1: поля реестра без соответствия в схеме ({len(no_match)}) ──")
    for key, why in no_match:
        print(f"  {key:26} {why}")
    if not no_match:
        print("  пусто")

    print(f"\n── дельта 2: обязательные узлы схемы, не покрытые реестром ({len(uncovered)}) ──")
    for p in uncovered:
        print(f"  {p}")
    if not uncovered:
        print("  пусто")

    ok = not no_match and not uncovered
    print("\nсверено полностью" if ok else "\nсверка НЕ завершена")
    return 0 if ok else 1


# ── заявка (приказ ФНС ЕД-7-26/108@, титул грузоотправителя) ──────────────────

ZAK_SCHEMA = (Path(__file__).resolve().parents[2] / "schema" / "zakaz"
              / "ON_ZAKZVGO_1_969_01_05_01_01.xsd")

ZAK_TEMPLATE_OWNED: dict[str, str] = {
    "/Файл/@ИдФайл": "повторяет имя файла обмена",
    "/Файл/@ВерсФорм": "константа формата 5.01",
    "/Файл/@ВерсПрог": "версия нашего бота",
    "/Файл/Документ": "корневой контейнер",
    "/Файл/Документ/@КНД": "константа 1110361 для титула грузоотправителя",
    "/Файл/Документ/@ДатИнфГО": "дата формирования файла — время сборки",
    "/Файл/Документ/@ВрИнфГО": "время формирования файла — время сборки",
    "/Файл/Документ/@НаимЭкСубСост": "составитель файла — наименование грузоотправителя",
    "/Файл/Документ/СодИнфГО": "контейнер содержания",
    "/Файл/Документ/СодИнфГО/@СодОпер":
        "константа: «Предоставление заказа и заявки на перевозку груза автомобильным транспортом»",
    "/Файл/Документ/СодИнфГО/ПунктПод/@НалКоорТочВрПод":
        "признак UTC: время пишем со смещением, признак фиксирован",
    "/Файл/Документ/СодИнфГО/АдрПункт/@Опер": "константа «Выгрузка» для пункта назначения",
}


def check_zayavka() -> tuple[list[tuple[str, str]], list[str], int, int]:
    """Дельты для заявки: пути полей существуют; обязательные узлы покрыты."""
    every, required = schema_nodes(ZAK_SCHEMA)
    fields = registry_for(ZAYAVKA)
    no_match = [(f.key, f"путь не найден в схеме заявки: {p.strip()}")
                for f in fields if f.xsd_for(ZAYAVKA)
                for p in f.xsd_for(ZAYAVKA).split("|") if p.strip() not in every]
    covered = set(ZAK_TEMPLATE_OWNED)
    for f in fields:
        if f.xsd_for(ZAYAVKA):
            covered.update(p.strip() for p in f.xsd_for(ZAYAVKA).split("|"))
    uncovered = [p for p in required if p not in covered
                 and not any(c.startswith(p + "/") or c.startswith(p + "@") for c in covered)]
    return no_match, uncovered, len(every), len(required)


# ── поручение экспедитору (приказ ФНС ЕД-1-26/277@, титул клиента) ──────────

POR_SCHEMA = (Path(__file__).resolve().parents[2] / "schema" / "eed"
              / "ON_POREXPKLT_1_958_05_05_01_01.xsd")

POR_TEMPLATE_OWNED: dict[str, str] = {
    "/Файл/@ИдФайл": "повторяет имя файла обмена",
    "/Файл/@ВерсФорм": "константа формата 5.01",
    "/Файл/@ВерсПрог": "версия нашего бота",
    "/Файл/Документ": "корневой контейнер",
    "/Файл/Документ/@КНД": "константа 1110486 для титула клиента",
    "/Файл/Документ/@НаимДок": "константа «электронное поручение экспедитору, информация клиента»",
    "/Файл/Документ/@ДатИнфКлнт": "дата формирования файла — время сборки",
    "/Файл/Документ/@ВрИнфКлнт": "время формирования файла — время сборки",
    "/Файл/Документ/СодИнфКлнт": "контейнер содержания",
    "/Файл/Документ/СодИнфКлнт/@СодОпер": "константа «представление электронного поручения экспедитору»",
    "/Файл/Документ/СодИнфКлнт/СвГруз": "контейнер сведений о грузе",
}


def check_doc(doc_type: str, schema: Path, owned: dict[str, str]):
    """Две дельты для любого документа: пути полей есть в схеме; обязательное покрыто."""
    every, required = schema_nodes(schema)
    fields = registry_for(doc_type)
    no_match = [(f.key, f"путь не найден в схеме: {p.strip()}")
                for f in fields if f.xsd_for(doc_type)
                for p in f.xsd_for(doc_type).split("|") if p.strip() not in every]
    covered = set(owned)
    for f in fields:
        if f.xsd_for(doc_type):
            covered.update(p.strip() for p in f.xsd_for(doc_type).split("|"))
    uncovered = [p for p in required if p not in covered
                 and not any(c.startswith(p + "/") or c.startswith(p + "@") for c in covered)]
    return no_match, uncovered, len(every), len(required)


def check_poruchenie():
    return check_doc("poruchenie", POR_SCHEMA, POR_TEMPLATE_OWNED)


if __name__ == "__main__":
    if "--doc" in sys.argv and "poruchenie" in sys.argv:
        nm, un, n_all, n_req = check_poruchenie()
        print(f"схема: {POR_SCHEMA.name}; узлов {n_all}, обязательных {n_req}\n")
        print(f"── дельта 1: пути полей, которых нет в схеме ({len(nm)}) ──")
        for k, why in nm:
            print(f"  {k:26} {why}")
        print(f"\n── дельта 2: обязательные узлы без покрытия ({len(un)}) ──")
        for p in un:
            print(f"  {p}")
        print("\nсверено полностью" if not nm and not un else "\nсверка НЕ завершена")
        sys.exit(0 if not nm and not un else 1)
    if "--doc" in sys.argv and "zayavka" in sys.argv:
        nm, un, n_all, n_req = check_zayavka()
        print(f"схема: {ZAK_SCHEMA.name}; узлов {n_all}, обязательных {n_req}\n")
        print(f"── дельта 1: пути полей, которых нет в схеме ({len(nm)}) ──")
        for k, why in nm:
            print(f"  {k:26} {why}")
        print(f"\n── дельта 2: обязательные узлы без покрытия ({len(un)}) ──")
        for p in un:
            print(f"  {p}")
        print("\nсверено полностью" if not nm and not un else "\nсверка НЕ завершена")
        sys.exit(0 if not nm and not un else 1)
    sys.exit(main())

