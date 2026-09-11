"""
Демонстрационный рейс: справочники, политика организации и «распознанные» поля.

Один источник данных для двух потребителей: тестов (`tests/conftest.py`) и
ручного прогона (`python3 -m etrn_bot.tools.demo`). Данные вымышленные —
никаких настоящих ПД в репозитории.

    PYTHONPATH=. python3 -m etrn_bot.tools.demo            # собрать и проверить
    PYTHONPATH=. python3 -m etrn_bot.tools.demo --print    # ещё и напечатать XML
"""

from __future__ import annotations

import sys
from datetime import date, datetime

from ..resolve import Carriage, Directory, Resolver

TODAY = date(2026, 9, 9)
NOW = datetime(2026, 9, 10, 10, 0, 0)


def directory() -> Directory:
    d = Directory()
    d.orgs["5001000001"] = {
        "shipper_name": 'ООО "НАША КОМПАНИЯ"', "shipper_kpp": "500101001",
        "shipper_address": "141400, Химки, Ленина 1",
        "shipper_edo_id": "2BM-5001000001",
    }
    d.orgs["7707083893"] = {
        "consignee_name": "ПАО СБЕРБАНК", "consignee_kpp": "770701001",
        "consignee_address": "117997, Москва, Вавилова 19",
        "consignee_phone": "+7 (495) 500-55-50",
        "consignee_edo_id": "2BM-7707083893",
        "delivery_postcode": "117997", "delivery_region": "77",
    }
    d.orgs["7736050003"] = {
        "carrier_name": 'ООО "ВЕЗЁМ"', "carrier_kpp": "773601001",
        "carrier_phone": "+7 (495) 719-19-19",
        "carrier_edo_id": "2BM-7736050003",
        "vehicle_ownership": "3",
    }
    d.vehicles["А123ВС777"] = {
        "vehicle_brand": "Volvo FH", "vehicle_type": "грузовой тягач седельный",
        "vehicle_capacity_t": 20000, "vehicle_volume_m3": 86,
        "vehicle_sts": "9954321098",
    }
    d.drivers["9912 345678"] = {"driver_phone": "+7 (916) 000-11-22"}
    d.routes["Химки-склад-3"] = {"pickup_region": "50", "pickup_postcode": "141400"}
    return d


def org_settings() -> dict:
    """Политика организации: настраивается один раз, у бухгалтера не спрашивается."""
    return {
        "inn": "5001000001", "counter": 41, "default_transit_days": 2,
        "defaults": {
            "shipper_phone": "+7 (495) 111-22-33",
            "shipper_contact": "Иванова А.П.",
            "signer_fio": "Иванова Анна Петровна",
            "signer_position": "главный бухгалтер",
            "signer_status": "1",
            "shipper_is_expeditor": "0",
            "loader_is_shipper": "1",
            "infra_owner_is_shipper": "1",
            "mass_method": "01",
            "redirect_person": "Грузоотправитель",
        },
    }


# Что дал VLM по пачке документов рейса: заявка, договор-заявка, фото ВУ и СТС,
# журнал въезда на склад.
EXTRACTION: dict[str, tuple[object, float, str]] = {
    "consignee_inn": ("7707083893", 0.99, "заявка №418"),
    "carrier_inn": ("7736050003", 0.99, "договор-заявка"),
    "cargo_name": ("Сера гранулированная", 0.93, "заявка №418"),
    "cargo_mass_gross_kg": (20000, 0.88, "заявка №418"),
    "cargo_places": (40, 0.9, "заявка №418"),
    "cargo_package": ("паллеты", 0.9, "заявка №418"),
    "cargo_package_code": ("PX", 0.8, "заявка №418"),
    "cargo_marking": ("партия 12/09", 0.8, "заявка №418"),
    "cargo_state": ("В исправном состоянии", 0.9, "акт погрузки"),
    "vehicle_plate": ("А123ВС777", 0.95, "фото СТС"),
    "driver_fio": (("Петров", "Иван", "Сергеевич"), 0.94, "фото ВУ"),
    "driver_license": ("9912 345678", 0.9, "фото ВУ"),
    "pickup_address": ("141400, Химки, Ленина 1, склад 3", 0.9, "заявка №418"),
    "delivery_address": ("117997, Москва, Вавилова 19", 0.9, "заявка №418"),
    "pickup_datetime": (datetime(2026, 9, 10, 8, 0), 0.9, "заявка №418"),
    "pickup_arrival_fact": (datetime(2026, 9, 10, 7, 50), 0.9, "журнал въезда"),
    "pickup_departure_fact": (datetime(2026, 9, 10, 9, 30), 0.9, "журнал въезда"),
    "order_number": ("418", 0.95, "заявка №418"),
    "order_date": (datetime(2026, 9, 8), 0.95, "заявка №418"),
    "pickup_region": ("50", 0.9, "заявка №418"),
}


def build_carriage(resolver: Resolver, extraction: dict | None = None,
                   confirm_all: bool = True) -> Carriage:
    """
    Прогон рейса. confirm_all имитирует человека, который просмотрел карточку
    и подтвердил неуверенные значения: без подтверждения XML собирать нельзя,
    и это правило, а не удобство теста.
    """
    c = Carriage()
    resolver.apply_extraction(c, extraction if extraction is not None else EXTRACTION)
    resolver.resolve(c)
    if confirm_all:
        for ask in list(resolver.open_questions(c)):
            v = c.values.get(ask.field.key)
            if v is not None:
                c.set(ask.field.key, v.value, "вы сказали", 1.0)
    return c


def main() -> int:
    from .. import xml_build

    r = Resolver(directory(), org_settings(), today=TODAY)
    c = build_carriage(r)
    open_q = r.open_questions(c)
    print(f"заполнено: {len(c.values)}, отложено: {len(c.deferred)}, "
          f"осталось вопросов: {len(open_q)}")
    for a in open_q:
        print("   •", a.field.title, "—", a.reason)

    xml, errors = xml_build.build_title1(c, file_id="ON_TRNACLGROT_DEMO", now=NOW)
    print(f"\nXML: {len(xml)} байт, ошибок валидации по XSD: {len(errors)}")
    for e in errors:
        print("   ", e)
    if "--print" in sys.argv:
        print()
        print(xml.decode("cp1251"))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
