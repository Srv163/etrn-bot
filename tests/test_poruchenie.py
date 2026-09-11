"""
Электронное поручение экспедитору — титул клиента (приказ ФНС ЕД-1-26/277@).

С 01.09.2026 экспедиторские документы по закону 87-ФЗ только электронные.
Владелец: экспедитор — не грузоотправитель; договор экспедиции бот спрашивает
сам и запоминает по экспедитору.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from etrn_bot import poruch_build
from etrn_bot.fields import BY_KEY, ETRN, PORUCHENIE, registry_for
from etrn_bot.resolve import Carriage, Resolver
from etrn_bot.tools import demo
from etrn_bot.xml_build import BuildError

FORWARDER = "7702070139"
ANSWERS = {"forwarder_contract_number": "ТЭ-17", "forwarder_contract_date": "15.01.2026",
           "cargo_net_kg": "19500 кг", "pickup_region_name": "Московская область",
           "delivery_region_name": "г. Москва"}
FIRST_TRIP = ["forwarder_contract_number", "forwarder_contract_date", "cargo_net_kg",
              "pickup_region_name", "delivery_region_name"]


def _directory():
    d = demo.directory()
    d.orgs[FORWARDER] = {"forwarder_name": 'ООО "ЭКСПЕДИТОР"', "forwarder_kpp": "770201001",
                         "forwarder_edo_id": "2BM-7702070139"}
    return d


def _trip(d):
    r = Resolver(d, demo.org_settings(), today=date(2026, 9, 9), doc_type=PORUCHENIE)
    c = Carriage(doc_type=PORUCHENIE)
    ext = dict(demo.EXTRACTION)
    ext["forwarder_inn"] = (FORWARDER, 0.99, "договор экспедиции")
    r.apply_extraction(c, ext)
    r.resolve(c)
    return r, c


def _answer_all(r, c):
    for _ in range(30):
        qs = r.open_questions(c)
        if not qs:
            return
        k = qs[0].field.key
        v = c.values.get(k)
        ok, shown = r.answer(c, k, ANSWERS.get(k) or ("да" if v is not None else "?"))
        assert ok, (k, shown)
    raise AssertionError("вопросы не кончились")


def test_first_trip_questions():
    r, c = _trip(_directory())
    assert [a.field.key for a in r.open_questions(c)] == FIRST_TRIP


def test_second_trip_remembers_contract_but_not_weight():
    """
    Договор экспедиции и регионы — один раз на экспедитора и адрес. Вес нетто —
    каждый рейс заново: он свой у каждого рейса, подставлять прошлый нельзя.
    """
    d = _directory()
    r, c = _trip(d)
    _answer_all(r, c)
    r2, c2 = _trip(d)
    assert [a.field.key for a in r2.open_questions(c2)] == ["cargo_net_kg"]
    assert c2.get("forwarder_contract_number") == "ТЭ-17"


def test_xml_passes_fns_schema():
    r, c = _trip(_directory())
    _answer_all(r, c)
    xml, errors = poruch_build.build(c, now=datetime(2026, 9, 10, 10, 0))
    assert errors == [], errors
    assert xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>'), "формат требует UTF-8"
    text = xml.decode("utf-8")
    assert 'КНД="1110486"' in text
    assert 'РеквНомерДок="ТЭ-17"' in text and 'РеквДатаДок="15.01.2026"' in text
    assert 'ВесНетто="19500.000"' in text and 'НаимРегион="Московская область"' in text


def test_derived_flags():
    r, c = _trip(_directory())
    assert c.get("cargo_packed") == "1", "паллеты — упаковка есть"
    assert c.get("ship_from") == date(2026, 9, 10) == c.get("ship_to")
    assert c.get("cargo_docs_present") == "0"


def test_forwarder_is_not_the_shipper_in_etrn():
    """Решение владельца: экспедитор — не грузоотправитель; в ЭТрН его полей нет."""
    etrn = {f.key for f in registry_for(ETRN)}
    assert not {k for k in etrn if k.startswith("forwarder_")}
    assert BY_KEY["shipper_is_expeditor"].default == "0"


def test_missing_contract_refuses():
    r, c = _trip(_directory())
    with pytest.raises(BuildError) as e:
        poruch_build.build(c)
    assert "договора экспедиции" in str(e.value) or "вес нетто" in str(e.value)


def test_filename_follows_format():
    name = poruch_build.filename("2BM-500", "2BM-770", when=date(2026, 9, 10))
    assert name.startswith("ON_POREXPKLT_2BM-770_2BM-500_0_20260910_") and name.endswith(".xml")


def test_registry_covers_schema():
    from etrn_bot.tools.check_registry import check_poruchenie
    no_match, uncovered, _a, _r = check_poruchenie()
    assert no_match == [] and uncovered == []
