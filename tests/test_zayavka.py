"""
Заявка перевозчику: второй документ на том же движке.

Контрольные числа — сценарий владельца (smoke.py): 8 полей из переписки.
До 11.09.2026 заявка была Word-документом: 30 полей, 3 вопроса. С сентября заявка
по закону электронная, формат ФНС требует больше реквизитов — стало 10 вопросов
на первом рейсе. На втором рейсе с тем же перевозчиком, грузом и адресом бот
берёт запомненное — 3 вопроса. Рост при том же реестре — регрессия.
"""

from __future__ import annotations

import io
from datetime import date, datetime

import pytest

from etrn_bot import store as store_mod
from etrn_bot import zayavka
from etrn_bot.fields import BY_KEY, ETRN, ZAYAVKA, registry_for
from etrn_bot.orgs import Profile
from etrn_bot.resolve import Carriage, Resolver
from etrn_bot.tools import demo

FROM_CHAT = {
    "consignee_inn": ("7707083893", 0.99, "переписка"),
    "carrier_inn": ("7736050003", 0.99, "переписка"),
    "cargo_name": ("Сера гранулированная", 0.93, "переписка"),
    "cargo_mass_gross_kg": (20000, 0.9, "переписка"),
    "pickup_address": ("Химки, Ленина 1, склад 3", 0.9, "переписка"),
    "delivery_address": ("Москва, Вавилова 19", 0.9, "переписка"),
    "pickup_datetime": (datetime(2026, 9, 11, 8, 0), 0.9, "переписка"),
    "cargo_places": (40, 0.9, "переписка"),
}


@pytest.fixture
def rz() -> Resolver:
    return Resolver(demo.directory(), demo.org_settings(), today=date(2026, 9, 9),
                    doc_type=ZAYAVKA)


@pytest.fixture
def cz(rz) -> Carriage:
    c = Carriage(doc_type=ZAYAVKA)
    rz.apply_extraction(c, FROM_CHAT)
    rz.resolve(c)
    return c


FIRST_TRIP = ["cargo_package", "cargo_package_code", "cargo_state", "cargo_volume_m3",
              "pickup_postcode", "pickup_region", "req_vehicle_type", "rate",
              "order_kind", "cargo_dims"]

ANSWERS = {"cargo_package": "паллеты", "cargo_package_code": "да", "cargo_state": "да",
           "cargo_volume_m3": "64", "pickup_postcode": "141400", "pickup_region": "50",
           "req_vehicle_type": "да", "rate": "85 тыс", "order_kind": "есть договор",
           "cargo_dims": "120x80x150"}


def _answer_all(r: Resolver, c: Carriage) -> None:
    for _ in range(30):
        qs = r.open_questions(c)
        if not qs:
            return
        ok, shown = r.answer(c, qs[0].field.key, ANSWERS[qs[0].field.key])
        assert ok, shown
    raise AssertionError("вопросы не кончились")


def test_control_numbers(rz, cz):
    """Первый рейс: 52 поля само, 10 вопросов — все требует формат ФНС."""
    assert len(cz.values) == 52
    assert [a.field.key for a in rz.open_questions(cz)] == FIRST_TRIP


def test_second_trip_asks_only_what_changes():
    """
    Второй рейс с тем же перевозчиком, грузом и складом: договор с перевозчиком,
    габариты паллеты, индекс и регион склада бот помнит. Спрашивает то, что
    меняется от рейса к рейсу, — ставку, объём, состояние груза.
    """
    d = demo.directory()
    for trip, expected in ((1, FIRST_TRIP), (2, ["cargo_state", "cargo_volume_m3", "rate"])):
        r = Resolver(d, demo.org_settings(), today=date(2026, 9, 9), doc_type=ZAYAVKA)
        c = Carriage(doc_type=ZAYAVKA)
        r.apply_extraction(c, FROM_CHAT)
        r.resolve(c)
        assert [a.field.key for a in r.open_questions(c)] == expected, trip
        _answer_all(r, c)
    assert d.lookup("carrier_inn", "7736050003")["order_kind"] == "Заявка"
    assert d.lookup("cargo_name", "Сера гранулированная")["cargo_dims"] == (1.2, 0.8, 1.5)


def test_code_is_recomputed_when_package_is_corrected():
    """
    Код тары вычислен из упаковки по умолчанию («без упаковки» → NE). Человек
    ответил «паллеты» — код обязан пересчитаться, а не остаться NE. Раньше бот
    держал NE и запоминал его для груза.
    """
    d = demo.directory()
    r = Resolver(d, demo.org_settings(), today=date(2026, 9, 9), doc_type=ZAYAVKA)
    c = Carriage(doc_type=ZAYAVKA)
    r.apply_extraction(c, FROM_CHAT)
    r.resolve(c)
    assert c.get("cargo_package_code") == "NE"
    r.answer(c, "cargo_package", "паллеты")
    assert c.get("cargo_package_code") == "PX"
    r.answer(c, "cargo_package_code", "да")
    assert d.lookup("cargo_name", "Сера гранулированная")["cargo_package_code"] == "PX"


def test_human_answer_is_never_recomputed():
    d = demo.directory()
    r = Resolver(d, demo.org_settings(), today=date(2026, 9, 9), doc_type=ZAYAVKA)
    c = Carriage(doc_type=ZAYAVKA)
    r.apply_extraction(c, FROM_CHAT)
    r.resolve(c)
    r.answer(c, "cargo_package_code", "BG")     # человек сам назвал код
    r.answer(c, "cargo_package", "паллеты")
    assert c.get("cargo_package_code") == "BG"


def test_yes_without_value_is_an_answer():
    """«Договор есть? — да»: значения ещё нет, значит это ответ, а не подтверждение."""
    r = Resolver(demo.directory(), demo.org_settings(), today=date(2026, 9, 9), doc_type=ZAYAVKA)
    c = Carriage(doc_type=ZAYAVKA)
    ok, shown = r.answer(c, "order_kind", "да")
    assert ok and c.get("order_kind") == "Заявка"


def test_xml_passes_fns_schema(rz, cz):
    from etrn_bot import zakaz_build
    _answer_all(rz, cz)
    xml, errors = zakaz_build.build(cz, now=datetime(2026, 9, 10, 10, 0))
    assert errors == [], errors
    assert xml.startswith('<?xml version="1.0" encoding="windows-1251"?>'.encode("cp1251"))
    text = xml.decode("cp1251")
    assert 'Функция="Заявка"' in text and 'КНД="1110361"' in text
    assert 'ДлЗнач="1.200"' in text and 'ВысЗнач="1.500"' in text


def test_xml_without_order_kind_refuses(rz, cz):
    from etrn_bot import zakaz_build
    from etrn_bot.xml_build import BuildError
    with pytest.raises(BuildError) as e:
        zakaz_build.build(cz)
    assert "заказ или заявка" in str(e.value) or "габариты" in str(e.value)


def test_zakaz_filename_follows_format():
    from etrn_bot import zakaz_build
    name = zakaz_build.filename("2BM-500", "2BM-773", when=date(2026, 9, 10))
    assert name.startswith("ON_ZAKZVGO_2BM-773_2BM-500_0_20260910_") and name.endswith(".xml")
    assert len(name[:-4].split("_")[-1]) == 36


def test_registry_covers_zayavka_schema():
    from etrn_bot.tools.check_registry import check_zayavka
    no_match, uncovered, _all, _req = check_zayavka()
    assert no_match == [] and uncovered == []


@pytest.mark.parametrize("text,dims", [
    ("120x80x150", (1.2, 0.8, 1.5)), ("1,2 × 0,8 × 1,5", (1.2, 0.8, 1.5)),
    ("1200*800*1500 мм", (1.2, 0.8, 1.5)),
])
def test_dims_parser(text, dims):
    from etrn_bot import parse
    assert parse.run("dims", text)[0] == dims


@pytest.mark.parametrize("text", ["2 места", "", "120x80"])
def test_dims_parser_refuses(text):
    from etrn_bot import parse
    assert parse.run("dims", text)[0] is None


def test_routine_is_never_a_question(rz, cz):
    asked = {a.field.key for a in rz.open_questions(cz)}
    assert not {f.key for f in rz.fields if f.routine} & asked
    titles = [t for t, _ in rz.routine_block(cz)]
    assert "НДС" in titles and "Условия оплаты" in titles


def test_rate_is_never_guessed(rz, cz):
    ok, msg = rz.dont_know(cz, "rate")
    assert ok is False and "угадывать нельзя" in msg
    assert cz.get("rate") is None


def test_capacity_from_mass(rz):
    for kg, tonnes in ((20000, 20), (4000, 5), (900, 1.5), (26000, 26)):
        c = Carriage(doc_type=ZAYAVKA)
        c.set("cargo_mass_gross_kg", kg, "заявка")
        rz.resolve(c)
        assert c.get("req_capacity_t") == tonnes, kg


def test_progress_counts_answers(rz, cz):
    assert rz.progress(cz) == (1, 10)
    rz.answer(cz, "rate", "85 тыс")
    assert rz.progress(cz) == (2, 10)


def test_card_is_html_with_routine_block(rz, cz):
    rz.answer(cz, "rate", "85 тыс")
    card = rz.card(cz)
    assert "<b>Стандартные условия</b>" in card
    assert "&quot;ВЕЗЁМ&quot;" in card or "&#x27;" in card or "ВЕЗЁМ" in card
    assert "<i>(" in card, "у каждого значения виден источник"
    # рутина — только блоком, в секциях её нет: строка «НДС: …» ровно одна
    assert card.count("• НДС:") == 1
    head, _, block = card.partition("<b>Стандартные условия</b>")
    assert "• НДС:" in block and "• НДС:" not in head


def test_etrn_is_not_polluted_by_zayavka_fields():
    etrn = {f.key for f in registry_for(ETRN)}
    for key in ("rate", "rate_vat", "payment_terms", "req_vehicle_type", "demurrage"):
        assert key not in etrn, key


def test_shared_fields_are_in_both_documents():
    both = {f.key for f in registry_for(ETRN)} & {f.key for f in registry_for(ZAYAVKA)}
    for key in ("consignee_inn", "carrier_inn", "cargo_name", "pickup_address", "doc_number"):
        assert key in both, key


def test_docx_opens_and_carries_rate_and_terms(rz, cz):
    docx = pytest.importorskip("docx")
    rz.answer(cz, "rate", "85 тыс")
    data = zayavka.as_docx(cz)
    doc = docx.Document(io.BytesIO(data))
    cells = [cell.text for t in doc.tables for row in t.rows for cell in row.cells]
    text = "\n".join(p.text for p in doc.paragraphs)
    assert doc.paragraphs[0].text.startswith("ЗАЯВКА №")
    assert any("85" in x for x in cells)
    assert "Условия" in text and zayavka.DEFAULT_TERMS[0][:30] in text


def test_org_terms_replace_defaults(rz, cz):
    """Условия заявки — юридический текст организации, правится без разработчика."""
    docx = pytest.importorskip("docx")
    profile = Profile(inn="5001000001", zayavka_terms=["Особое условие нашей компании."])
    doc = docx.Document(io.BytesIO(zayavka.as_docx(cz, profile.terms())))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Особое условие нашей компании." in text
    assert zayavka.DEFAULT_TERMS[0][:30] not in text
    assert Profile(inn="1").terms() == zayavka.DEFAULT_TERMS


def test_text_version_for_messenger(rz, cz):
    rz.answer(cz, "rate", "85 тыс")
    text = zayavka.as_text(cz)
    assert text.startswith("ЗАЯВКА №")
    assert "Ставка: 85 000" in text
    assert "Перевозчик: ООО \"ВЕЗЁМ\"" in text


def test_store_keeps_document_type_and_progress(rz, cz):
    """Перезапуск бота не должен превращать заявку в накладную и сбрасывать «Вопрос 2 из 3»."""
    rz.answer(cz, "rate", "85 тыс")
    back = store_mod.loads(store_mod.dumps(cz))
    assert back.doc_type == ZAYAVKA
    assert back.answered_count == cz.answered_count == 1
