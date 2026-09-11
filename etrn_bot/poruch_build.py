"""
Сборка XML электронного поручения экспедитору — информация клиента.

Формат — приказ ФНС от 28.04.2026 № ЕД-1-26/277@, титул клиента ON_POREXPKLT,
КНД 1110486, версия 5.01. С 01.09.2026 экспедиторские документы по закону 87-ФЗ
формируются только в электронной форме.

В отличие от ЭТрН и заявки, описание формата требует кодировку UTF-8:
«Первая строка XML-файла должна иметь вид <?xml version="1.0" encoding="UTF-8"?>».
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from pathlib import Path

from .resolve import Carriage
from .xml_build import TEMPLATE_DIR, BuildError, fmt_date, fmt_decimal, fmt_int, validate

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "eed" / "ON_POREXPKLT_1_958_05_05_01_01.xsd"

ENCODING = "UTF-8"
VERSION_FORMAT = "5.01"
KND = "1110486"
FILE_PREFIX = "ON_POREXPKLT"
DOC_NAME = "электронное поручение экспедитору, информация клиента"
OPERATION = "представление электронного поручения экспедитору"
PROGRAM_VERSION = "etrn-bot 0.2"


def filename(shipper_edo_id: str, forwarder_edo_id: str, when: date | None = None,
             extra_receivers: bool = False) -> str:
    """ON_POREXPKLT_A_O_W_ГГГГММДД_N.xml — A: экспедитор-получатель, O: мы-отправитель."""
    missing = [n for n, v in (("экспедитора", forwarder_edo_id),
                              ("клиента (вашей организации)", shipper_edo_id)) if not v]
    if missing:
        raise BuildError("нет идентификаторов участников ЭДО: " + ", ".join(missing) +
                         ". Их присваивает оператор ЭДО (в Saby — карточка контрагента).")
    day = (when or date.today()).strftime("%Y%m%d")
    return (f"{FILE_PREFIX}_{forwarder_edo_id}_{shipper_edo_id}_"
            f"{'1' if extra_receivers else '0'}_{day}_{uuid.uuid4()}.xml")


def context(c: Carriage, *, file_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    g = c.get

    def need(key: str, title: str):
        v = g(key)
        if v in (None, "", []):
            raise BuildError(f"нет обязательного значения: {title} ({key})")
        return v

    signer = g("signer_fio")
    signer = tuple(p for p in (signer.split() if isinstance(signer, str) else (signer or ())) if p)
    if len(signer) < 2:
        raise BuildError("ФИО подписанта: нужны фамилия и имя")

    def org(prefix: str, title: str) -> dict:
        return {"name": need(f"{prefix}_name", f"наименование {title}"),
                "inn": need(f"{prefix}_inn", f"ИНН {title}"), "kpp": g(f"{prefix}_kpp")}

    def place(prefix: str, title: str) -> dict:
        return {"comment": need(f"{prefix}_address", f"адрес {title}"),
                "region": need(f"{prefix}_region", f"код региона {title}"),
                "region_name": need(f"{prefix}_region_name", f"наименование региона {title}"),
                "postcode": g(f"{prefix}_postcode"), "city": g(f"{prefix}_city"),
                "street": g(f"{prefix}_street"), "house": g(f"{prefix}_house")}

    volume = g("cargo_volume_m3")
    return {
        "file_id": file_id, "version_format": VERSION_FORMAT, "program": PROGRAM_VERSION,
        "knd": KND, "doc_name": DOC_NAME,
        "date_built": now.strftime("%d.%m.%Y"), "time_built": now.strftime("%H:%M:%S"),
        "operation": OPERATION,
        "number": g("doc_number") or "Без номера",
        "date": fmt_date(need("doc_date", "дата поручения")),
        "docs_present": need("cargo_docs_present", "документы для перевозки"),
        "client": org("shipper", "клиента"),
        "forwarder": org("forwarder", "экспедитора"),
        "contract": {"name": need("forwarder_contract_name", "наименование договора экспедиции"),
                     "number": need("forwarder_contract_number", "номер договора экспедиции"),
                     "date": fmt_date(need("forwarder_contract_date", "дата договора экспедиции"))},
        "consignee": org("consignee", "грузополучателя"),
        "shipper": org("shipper", "грузоотправителя"),
        "vehicle_kind": need("vehicle_kind", "вид транспорта"),
        "notify": need("notify_parties", "уведомление сторон"),
        "ship_from": fmt_date(need("ship_from", "готовность к отгрузке: с")),
        "ship_to": fmt_date(need("ship_to", "готовность к отгрузке: по")),
        "container": need("container_kind", "признак перевозки"),
        "cargo": {
            "marking": g("cargo_marking") or "Отсутствует",   # так требует формат
            "origin_code": need("cargo_origin_code", "страна происхождения: код"),
            "origin_name": need("cargo_origin_name", "страна происхождения"),
            "net_kg": fmt_decimal(need("cargo_net_kg", "вес нетто"), 3),
            "gross_kg": fmt_decimal(need("cargo_mass_gross_kg", "вес брутто"), 3),
            "name": need("cargo_name", "наименование груза"),
            "places": fmt_int(need("cargo_places", "количество мест")),
            "dangerous": need("cargo_dangerous", "опасный груз"),
            "restricted": need("cargo_restricted", "изъятые вещества"),
            "volume_known": need("cargo_volume_known", "объём определён"),
            "volume": fmt_decimal(volume, 3) if volume not in (None, "") else None,
            "gov_tracked": need("cargo_gov_tracked", "учёт в госсистеме"),
            "packed": need("cargo_packed", "наличие упаковки"),
            "has_tnved": need("cargo_has_tnved", "код ТН ВЭД"),
        },
        "pickup": place("pickup", "места погрузки"),
        "delivery": place("delivery", "места выгрузки"),
        "signer": {"surname": signer[0], "name": signer[1],
                   "patronymic": signer[2] if len(signer) > 2 else None,
                   "position": g("signer_position"),
                   "authority": need("signer_authority", "способ подтверждения полномочий")},
    }


def build(c: Carriage, *, file_id: str | None = None,
          now: datetime | None = None) -> tuple[bytes, list[str]]:
    """→ (xml, ошибки проверки по XSD). Ошибки непусты — файл не отдаём."""
    from jinja2 import Environment, FileSystemLoader

    ctx = context(c, file_id=file_id or "ON_POREXPKLT_TEST", now=now)
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True,
                      trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)
    body = env.get_template("poruchenie.xml.j2").render(**ctx)
    xml = f'<?xml version="1.0" encoding="{ENCODING}"?>\n'.encode("utf-8")
    xml += body.encode("utf-8")
    return xml, validate(xml, SCHEMA)
