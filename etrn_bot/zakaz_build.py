"""
Сборка XML электронной заявки (заказа) перевозчику.

Формат — приказ ФНС от 13.02.2023 № ЕД-7-26/108@ (срок действия снят приказом
ЕД-1-26/494@), титул грузоотправителя ON_ZAKZVGO, КНД 1110361, версия 5.01.
С 01.09.2026 заявка по Уставу автотранспорта формируется только в электронном
виде, поэтому Word остаётся черновиком, а документом является этот XML.

Те же правила, что у накладной: разметку собирает шаблон, не модель; файл
не отдаётся без проверки по XSD; пути полей — в реестре (`fields._ZAYAVKA_PATHS`),
сверка — `tools/check_registry.py --doc zayavka`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

from .resolve import Carriage
from .xml_build import (DEFAULT_TZ, TEMPLATE_DIR, BuildError, fmt_date, fmt_datetime,
                        fmt_decimal, fmt_int, kg_to_t, validate)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "zakaz" / "ON_ZAKZVGO_1_969_01_05_01_01.xsd"

ENCODING = "windows-1251"          # «Первая строка XML-файла должна иметь вид … windows-1251»
VERSION_FORMAT = "5.01"
KND = "1110361"
FILE_PREFIX = "ON_ZAKZVGO"
OPERATION = "Предоставление заказа и заявки на перевозку груза автомобильным транспортом"
PROGRAM_VERSION = "etrn-bot 0.2"
UTC_FLAG = "1"


def filename(shipper_edo_id: str, carrier_edo_id: str, when: date | None = None,
             extra_receivers: bool = False) -> str:
    """
    ON_ZAKZVGO_A_O_W_ГГГГММДД_N.xml — A: перевозчик-получатель, O: мы-отправитель.
    Идентификаторы присваивает оператор ЭДО; без них имя не собирается.
    """
    missing = [n for n, v in (("перевозчика", carrier_edo_id),
                              ("грузоотправителя", shipper_edo_id)) if not v]
    if missing:
        raise BuildError("нет идентификаторов участников ЭДО: " + ", ".join(missing) +
                         ". Их присваивает оператор ЭДО (в Saby — карточка контрагента).")
    day = (when or date.today()).strftime("%Y%m%d")
    return (f"{FILE_PREFIX}_{carrier_edo_id}_{shipper_edo_id}_"
            f"{'1' if extra_receivers else '0'}_{day}_{uuid.uuid4()}.xml")


def _fio(value, who: str) -> tuple[str, ...]:
    parts = tuple(p for p in (value.split() if isinstance(value, str) else (value or ())) if p)
    if len(parts) < 2:
        raise BuildError(f"{who}: нужны фамилия и имя")
    return parts


def context(c: Carriage, *, file_id: str, now: datetime | None = None,
            tz: str = DEFAULT_TZ) -> dict:
    now = now or datetime.now()
    g = c.get

    def need(key: str, title: str):
        v = g(key)
        if v in (None, "", []):
            raise BuildError(f"нет обязательного значения: {title} ({key})")
        return v

    dims = need("cargo_dims", "габариты грузового места")
    if not (isinstance(dims, (tuple, list)) and len(dims) == 3):
        raise BuildError("габариты грузового места: нужны три числа — длина, ширина, высота")
    length, width, height = dims
    signer = _fio(g("signer_fio"), "ФИО подписанта")

    def party(prefix: str, title: str) -> dict:
        return {"name": need(f"{prefix}_name", f"наименование {title}"),
                "inn": need(f"{prefix}_inn", f"ИНН {title}"),
                "kpp": g(f"{prefix}_kpp"),
                "phone": need(f"{prefix}_phone", f"телефон {title}")}

    def place(prefix: str, title: str) -> dict:
        return {"comment": need(f"{prefix}_address", f"адрес {title}"),
                "postcode": need(f"{prefix}_postcode", f"индекс {title}"),
                "region": need(f"{prefix}_region", f"код региона {title}"),
                "city": g(f"{prefix}_city"), "street": g(f"{prefix}_street"),
                "house": g(f"{prefix}_house")}

    return {
        "file_id": file_id, "version_format": VERSION_FORMAT, "program": PROGRAM_VERSION,
        "knd": KND, "function": need("order_kind", "заказ или заявка"),
        "date_built": now.strftime("%d.%m.%Y"), "time_built": now.strftime("%H:%M:%S"),
        "composer": need("shipper_name", "наименование грузоотправителя"),
        "operation": OPERATION, "utc_flag": UTC_FLAG,
        # «При отсутствии указывается: „Без номера“» — из описания формата
        "number": g("doc_number") or "Без номера",
        "date": fmt_date(need("doc_date", "дата заявки")),
        "norms": need("transport_norms", "указания по нормам перевозки"),
        "food": need("food_transport", "перевозка пищевых продуктов"),
        "shipper": party("shipper", "грузоотправителя"),
        "carrier": party("carrier", "перевозчика"),
        "pickup": {**place("pickup", "места погрузки"),
                   "when": fmt_datetime(need("pickup_datetime", "дата подачи ТС"), tz)},
        "delivery": {**place("delivery", "места выгрузки"),
                     "when": (fmt_datetime(g("delivery_datetime"), tz)
                              if isinstance(g("delivery_datetime"), datetime) else None)},
        "cargo": {
            "name": need("cargo_name", "наименование груза"),
            "state": need("cargo_state", "состояние груза"),
            "package_code": need("cargo_package_code", "код вида тары"),
            "extra": " ".join(str(x) for x in (g("cargo_extra"), g("special_instructions"),
                                               g("temperature_mode")) if x) or None,
            "places": fmt_int(need("cargo_places", "количество мест")),
            "mass_method": need("mass_method", "метод определения массы"),
            "volume": fmt_decimal(need("cargo_volume_m3", "объём груза"), 2),
            "distributable": need("cargo_distributable", "распределение по платформе"),
            "divisible": need("cargo_divisible", "делимость груза"),
            "mass_kg": fmt_decimal(need("cargo_mass_gross_kg", "масса брутто"), 3),
            "length": fmt_decimal(length, 3), "width": fmt_decimal(width, 3),
            "height": fmt_decimal(height, 3),
        },
        "vehicle": {
            "type": need("req_vehicle_type", "тип кузова"),
            "capacity_t": kg_to_t(need("req_capacity_t", "грузоподъёмность")),
            "volume_m3": fmt_decimal(need("req_volume_m3", "объём кузова"), 2),
        },
        "signer": {"surname": signer[0], "name": signer[1],
                   "patronymic": signer[2] if len(signer) > 2 else None,
                   "position": need("signer_position", "должность подписанта"),
                   "authority": need("signer_authority", "способ подтверждения полномочий")},
    }


def build(c: Carriage, *, file_id: str | None = None,
          now: datetime | None = None) -> tuple[bytes, list[str]]:
    """→ (xml, ошибки проверки по XSD). Ошибки непусты — файл не отдаём."""
    from jinja2 import Environment, FileSystemLoader

    ctx = context(c, file_id=file_id or "ON_ZAKZVGO_TEST", now=now)
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    body = env.get_template("zakaz.xml.j2").render(**ctx)
    xml = f'<?xml version="1.0" encoding="{ENCODING}"?>\n'.encode(ENCODING)
    xml += body.encode(ENCODING, errors="strict")
    return xml, validate(xml, SCHEMA)
