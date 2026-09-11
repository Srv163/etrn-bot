"""
Сборка XML Титула 1.

Три железных правила:
  1. XML собирает шаблон, а не языковая модель. Модель извлекает значения —
     на этом её роль заканчивается.
  2. Ни один файл не уходит человеку без валидации по действующей XSD.
     Не прошло схему — это баг у нас, а не проблема бухгалтерии.
  3. Пути и константы берутся из схемы и приказа, а не из головы. Маппинг
     живёт в реестре полей (`fields.py`, поле `xsd`), сверка — `tools/check_registry.py`.

Валидация идёт через системный `xmllint`: `lxml` в окружении нет, а libxml2
на macOS есть из коробки и схему ФНС разбирает. Проверено 09.09.2026.
ВАЖНО: xmllint проверяет только XSD. Правила Schematron (`usch:`), которых
в схемах ФНС много, он не проверяет — зелёная валидация не гарантирует, что
оператор ЭДО примет файл.

Кодировка файла обмена — windows-1251, так предписывает приказ.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from datetime import date, datetime
from pathlib import Path

from .resolve import Carriage

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "ON_TRNACLGROT_1_973_01_05_01_02.xsd"
TEMPLATE_DIR = ROOT / "templates"

ENCODING = "windows-1251"
VERSION_FORMAT = "5.01"
KND = "1110339"
DOC_KIND = "Транспортная накладная, информация грузоотправителя"
OPERATION = ("Лицом, осуществляющим погрузку груза, при указанных обстоятельствах "
             "передан водителю груз с указанными характеристиками")
FILE_PREFIX = "ON_TRNACLGROT"
PROGRAM_VERSION = "etrn-bot 0.1"

# Смещение локального времени по умолчанию. Формат ДатаВремяВЗТип требует
# смещение обязательно, поэтому признак «UTC указан» всегда 1.
DEFAULT_TZ = "+03:00"
UTC_FLAG = "1"


class BuildError(Exception):
    """Собрать нельзя: не хватает обязательных значений."""


# Текст ошибки, когда проверять нечем. Отдельной константой: бот обязан отличать
# «файл плохой» от «валидатора нет» — это разные причины и разные действия.
NO_VALIDATOR = "не найден xmllint — без него проверить файл по схеме нечем"


def validator_missing(errors: list[str]) -> bool:
    return any(NO_VALIDATOR in e for e in errors)


# ── форматирование значений под типы схемы ──────────────────────────────────

def fmt_date(v: object) -> str:
    """→ ДД.ММ.ГГГГ (ДатаТип, ровно 10 символов)."""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y")
    if isinstance(v, date):
        return v.strftime("%d.%m.%Y")
    s = str(v).strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", s):
        return s
    raise BuildError(f"не дата в формате ДД.ММ.ГГГГ: {v!r}")


def fmt_datetime(v: object, tz: str = DEFAULT_TZ) -> str:
    """→ ДД.ММ.ГГГГTЧЧ:ММ:СС±ЧЧ:ММ (ДатаВремяВЗТип, ровно 25 символов)."""
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%YT%H:%M:%S") + tz
    s = str(v).strip()
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", s):
        return s
    raise BuildError(f"не дата-время формата ЭТрН: {v!r}")


def fmt_decimal(v: object, digits: int) -> str:
    """Число с фиксированной дробной частью; запятую приводим к точке."""
    if v is None:
        raise BuildError("пустое число")
    s = str(v).replace(",", ".").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        raise BuildError(f"не число: {v!r}")
    return f"{float(m.group(0)):.{digits}f}"


def fmt_int(v: object) -> str:
    s = str(v).strip()
    m = re.search(r"\d+", s)
    if not m:
        raise BuildError(f"не целое: {v!r}")
    return m.group(0)


def kg_to_t(v: object) -> str:
    """Грузоподъёмность в схеме — тонны, а парсер массы отдаёт килограммы."""
    kg = float(str(v).replace(",", ".").strip().split()[0])
    return f"{kg / 1000:.2f}" if kg > 1000 else f"{kg:.2f}"


# ── имя файла обмена ────────────────────────────────────────────────────────

def filename(shipper_edo_id: str, carrier_edo_id: str, consignee_edo_id: str,
             when: date | None = None, extra_receivers: bool = False) -> str:
    """
    Имя файла обмена по приказу ЕД-7-26/1065@, раздел II, пункт 4:

        ON_TRNACLGROT_A_E_O_W_ГГГГММДД_N.xml

    A — идентификатор перевозчика-получателя, E — грузополучателя-получателя,
    O — грузоотправителя-отправителя (наш), W — признак дополнительных
    получателей, N — GUID из 36 символов.

    Идентификаторы участников присваивает оператор ЭДО (три символа кода
    оператора + GUID участника, не более 46 символов каждый). Бот их не
    генерирует: пустой идентификатор — это отказ собирать имя, а не повод
    подставить заглушку.
    """
    ids = {"перевозчика": carrier_edo_id, "грузополучателя": consignee_edo_id,
           "грузоотправителя": shipper_edo_id}
    missing = [name for name, v in ids.items() if not v]
    if missing:
        raise BuildError(
            "нет идентификаторов участников ЭДО: " + ", ".join(missing) +
            ". Их присваивает оператор ЭДО (в СБИС — карточка контрагента); "
            "без них имя файла обмена собрать нельзя."
        )
    for name, v in ids.items():
        if len(v) > 46:
            raise BuildError(f"идентификатор {name} длиннее 46 символов: {v!r}")
    day = (when or date.today()).strftime("%Y%m%d")
    return (f"{FILE_PREFIX}_{carrier_edo_id}_{consignee_edo_id}_{shipper_edo_id}_"
            f"{'1' if extra_receivers else '0'}_{day}_{uuid.uuid4()}.xml")


# ── контекст шаблона ────────────────────────────────────────────────────────

def xml_context(c: Carriage, *, file_id: str, now: datetime | None = None,
                tz: str = DEFAULT_TZ) -> dict:
    """
    Готовит значения к печати: приводит форматы, считает служебные константы.
    Шаблон не форматирует ничего — он только расставляет разметку.
    """
    now = now or datetime.now()
    g = c.get

    def need(key: str, title: str):
        v = g(key)
        if v in (None, "", []):
            raise BuildError(f"нет обязательного значения: {title} ({key})")
        return v

    fio = g("driver_fio")
    if isinstance(fio, str):
        fio = tuple(p for p in fio.split() if p)
    signer = g("signer_fio")
    if isinstance(signer, str):
        signer = tuple(p for p in signer.split() if p)
    if not fio or len(fio) < 2:
        raise BuildError("ФИО водителя: нужны фамилия и имя")
    if not signer or len(signer) < 2:
        raise BuildError("ФИО подписанта: нужны фамилия и имя")

    ctx = {
        "file_id": file_id,
        "version_format": VERSION_FORMAT,
        "program": PROGRAM_VERSION,
        "knd": KND,
        "doc_kind": DOC_KIND,
        "date_built": now.strftime("%d.%m.%Y"),
        "time_built": now.strftime("%H:%M:%S"),
        "operation": OPERATION,
        "utc_flag": UTC_FLAG,

        "doc_number": need("doc_number", "номер накладной"),
        "doc_date": fmt_date(need("doc_date", "дата составления")),
        "order_number": need("order_number", "номер заявки"),
        "order_date": fmt_date(need("order_date", "дата заявки")),

        "shipper": {
            "inn": need("shipper_inn", "ИНН грузоотправителя"),
            "name": need("shipper_name", "наименование грузоотправителя"),
            "kpp": g("shipper_kpp"),
            "phone": need("shipper_phone", "телефон грузоотправителя"),
            "expeditor": need("shipper_is_expeditor", "статус экспедитора"),
        },
        "consignee": {
            "inn": need("consignee_inn", "ИНН грузополучателя"),
            "name": need("consignee_name", "наименование грузополучателя"),
            "kpp": g("consignee_kpp"),
            "phone": need("consignee_phone", "телефон грузополучателя"),
            "address_comment": need("delivery_address", "адрес выгрузки"),
            "postcode": need("delivery_postcode", "индекс места выгрузки"),
            "region": need("delivery_region", "код региона места выгрузки"),
            "city": g("delivery_city"),
            "street": g("delivery_street"),
            "house": g("delivery_house"),
        },
        "carrier": {
            "inn": need("carrier_inn", "ИНН перевозчика"),
            "name": need("carrier_name", "наименование перевозчика"),
            "kpp": g("carrier_kpp"),
            "phone": need("carrier_phone", "телефон перевозчика"),
        },
        "cargo": {
            "name": need("cargo_name", "наименование груза"),
            "marking": need("cargo_marking", "маркировка"),
            "state": need("cargo_state", "состояние груза"),
            "package": need("cargo_package", "способ упаковки"),
            "package_code": need("cargo_package_code", "код вида тары"),
            "places": fmt_int(need("cargo_places", "количество мест")),
            "mass_kg": fmt_decimal(need("cargo_mass_gross_kg", "масса брутто"), 3),
            "volume": fmt_decimal(g("cargo_volume_m3"), 3) if g("cargo_volume_m3") else None,
            "value": fmt_decimal(g("cargo_declared_value"), 2) if g("cargo_declared_value") else None,
            "extra": " ".join(str(x) for x in (g("cargo_extra"), g("special_instructions"),
                                               g("temperature_mode")) if x) or None,
        },
        "instructions": {
            "norms": need("transport_norms", "указания по нормам перевозки"),
            "redirect_person": need("redirect_person", "кто вправе переадресовать"),
            "redirect_method": need("redirect_method", "способ передачи указания"),
            "redirect_phone": need("redirect_phone", "телефон для переадресовки"),
            "delivery_datetime": (fmt_datetime(g("delivery_datetime"), tz)
                                  if g("delivery_datetime") else None),
        },
        "driver": {
            "surname": fio[0], "name": fio[1],
            "patronymic": fio[2] if len(fio) > 2 else None,
            "phone": need("driver_phone", "телефон водителя"),
            "license_series": str(need("driver_license", "ВУ")).split()[0],
            "license_number": str(need("driver_license", "ВУ")).split()[-1],
        },
        "vehicle": {
            "plate": need("vehicle_plate", "госномер"),
            "type": need("vehicle_type", "тип ТС"),
            "brand": need("vehicle_brand", "марка ТС"),
            "capacity_t": kg_to_t(need("vehicle_capacity_t", "грузоподъёмность")),
            "volume_m3": fmt_decimal(need("vehicle_volume_m3", "вместимость"), 2),
            "ownership": need("vehicle_ownership", "тип владения"),
            "sts": g("vehicle_sts"),
            "trailer": g("trailer_plate"),
        },
        "loading": {
            "address_comment": need("pickup_address", "адрес погрузки"),
            "postcode": need("pickup_postcode", "индекс места погрузки"),
            "region": need("pickup_region", "код региона места погрузки"),
            "city": g("pickup_city"),
            "street": g("pickup_street"),
            "house": g("pickup_house"),
            "planned": fmt_datetime(need("pickup_datetime", "дата подачи ТС"), tz),
            "arrived": fmt_datetime(need("pickup_arrival_fact", "фактическое прибытие"), tz),
            "departed": fmt_datetime(need("pickup_departure_fact", "фактическое убытие"), tz),
            "loader_is_shipper": need("loader_is_shipper", "кто грузил"),
            "infra_is_shipper": need("infra_owner_is_shipper", "чья площадка"),
            "mass_text": fmt_int(need("cargo_mass_gross_kg", "масса брутто")),
            "mass_method": need("mass_method", "метод определения массы"),
        },
        "signer": {
            "surname": signer[0], "name": signer[1],
            "patronymic": signer[2] if len(signer) > 2 else None,
            "status": need("signer_status", "статус подписанта"),
            "position": g("signer_position"),
        },
        "docs": g("accompanying_docs") or [],
    }
    return ctx


# ── сборка и валидация ──────────────────────────────────────────────────────

def build_title1(c: Carriage, *, file_id: str | None = None,
                 now: datetime | None = None) -> tuple[bytes, list[str]]:
    """→ (xml, список ошибок валидации). Ошибки непусты — файл не отдаём."""
    from jinja2 import Environment, FileSystemLoader

    ctx = xml_context(c, file_id=file_id or "ON_TRNACLGROT_TEST", now=now)
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=True, trim_blocks=True, lstrip_blocks=True,
                      keep_trailing_newline=True)
    body = env.get_template("title1.xml.j2").render(**ctx)
    xml = f'<?xml version="1.0" encoding="{ENCODING}"?>\n'.encode(ENCODING)
    xml += body.encode(ENCODING, errors="strict")
    return xml, validate(xml)


def validate(xml: bytes, schema: Path | None = None) -> list[str]:
    """Проверка по XSD через xmllint. Пустой список — схема пройдена."""
    schema = schema or SCHEMA
    if not schema.exists():
        return [f"XSD-схема не найдена: {schema}. См. schema/README.md."]
    try:
        proc = subprocess.run(
            ["xmllint", "--noout", "--schema", str(schema), "-"],
            input=xml, capture_output=True, timeout=30,
        )
    except FileNotFoundError:
        return [NO_VALIDATOR]
    if proc.returncode == 0:
        return []
    text = proc.stderr.decode("utf-8", errors="replace")
    errors = [ln.strip() for ln in text.splitlines()
              if ln.strip() and not ln.strip().endswith("fails to validate")]
    return errors or [f"xmllint вернул код {proc.returncode}"]
