"""Движок заполнения: приоритеты, «не знаю», never_guess, ответ пачкой."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from etrn_bot.fields import BY_KEY, REGISTRY, Strategy
from etrn_bot.resolve import Carriage, Resolver


# ── никаких молчаливых подстановок в критичные поля ─────────────────────────

NEVER_GUESS = [f.key for f in REGISTRY if f.never_guess]


def test_never_guess_stay_empty_on_bare_run(resolver):
    """
    Пустой прогон без документов и справочников: ни одно never_guess поле
    не должно оказаться заполненным. Исключение одно — ИНН самой организации
    из её настроек: это ввод человека при настройке, а не догадка бота.
    """
    c = Carriage()
    resolver.resolve(c)
    filled = [k for k in NEVER_GUESS if k in c.values]
    assert filled == ["shipper_inn"], filled
    assert c.values["shipper_inn"].value == "5001000001"


def test_never_guess_not_closed_by_dont_know(resolver):
    c = Carriage()
    resolver.resolve(c)
    for key in ("cargo_name", "cargo_places", "pickup_address", "delivery_address",
                "vehicle_plate", "driver_license", "pickup_datetime"):
        ok, msg = resolver.dont_know(c, key)
        assert ok is False, key
        assert "угадывать нельзя" in msg
        assert key not in c.values


def test_never_guess_registry_has_no_default(resolver):
    """Страховка от «понизить поле, чтобы бот меньше спрашивал»."""
    for f in REGISTRY:
        if f.never_guess and Strategy.DEFAULT in f.strategies:
            assert f.default == "__org_inn__", f.key


# ── пять исходов «не знаю» ──────────────────────────────────────────────────

def test_dont_know_foreign_field(resolver):
    c = Carriage()
    ok, msg = resolver.dont_know(c, "carriage_cost")
    assert ok and "заполнит Титул перевозчика" in msg
    assert "carriage_cost" in c.deferred


def test_dont_know_second_lookup(resolver, directory):
    """Второй заход в справочник: значение там есть, просто раньше не спросили."""
    c = Carriage()
    c.set("carrier_inn", "7736050003", "заявка")
    ok, msg = resolver.dont_know(c, "vehicle_ownership")
    assert ok and "справочник" in msg.lower()
    assert c.get("vehicle_ownership") == "3"


def test_dont_know_default_is_shown(resolver):
    c = Carriage()
    ok, msg = resolver.dont_know(c, "cargo_package")
    assert ok and c.get("cargo_package") == "без упаковки"
    # Умолчание не подставляется молча — человек видит, что именно поставлено.
    assert "без упаковки" in msg


def test_dont_know_optional_left_empty(resolver):
    c = Carriage()
    ok, msg = resolver.dont_know(c, "cargo_volume_m3")
    assert ok
    assert "cargo_volume_m3" in c.skipped or c.get("cargo_volume_m3") in (None, "")


def test_dont_know_never_guess_explains_where_to_look(resolver):
    c = Carriage()
    ok, msg = resolver.dont_know(c, "cargo_mass_gross_kg")
    assert ok is False
    assert "Где взять" in msg


# ── ответ пачкой ────────────────────────────────────────────────────────────

def test_answer_batch_splits_by_parser(resolver):
    c = Carriage()
    resolver.apply_extraction(c, {"cargo_name": ("Сера", 0.9, "заявка")})
    resolver.resolve(c)
    filled = dict(resolver.answer_batch(c, "40 мест, завтра к 8, 20 тонн"))
    assert filled.get("cargo_places") == "40"
    assert "10.09.2026" in filled.get("pickup_datetime", "")
    assert c.get("cargo_mass_gross_kg") == 20000


def test_answer_batch_does_not_leak_into_region(resolver):
    """
    Регрессия: «завтра к 8» уезжало в код региона (08), потому что парсер
    региона хватал любую цифру из строки.
    """
    c = Carriage()
    resolver.resolve(c)
    dict(resolver.answer_batch(c, "завтра к 8"))
    assert c.get("pickup_region") is None
    assert isinstance(c.get("pickup_datetime"), datetime)


# ── приоритет источников ────────────────────────────────────────────────────

def test_human_answer_beats_extraction(resolver):
    c = Carriage()
    resolver.apply_extraction(c, {"cargo_places": (40, 0.9, "заявка")})
    resolver.answer(c, "cargo_places", "42")
    assert c.get("cargo_places") == 42
    assert c.values["cargo_places"].source == "вы сказали"
    assert c.corrections == [("cargo_places", 40, 42)]


def test_low_confidence_becomes_question(resolver):
    c = Carriage()
    resolver.apply_extraction(c, {"vehicle_plate": ("А123ВС777", 0.71, "фото СТС")})
    resolver.resolve(c)
    asks = {a.field.key: a.reason for a in resolver.open_questions(c)}
    assert "vehicle_plate" in asks and "не уверен" in asks["vehicle_plate"]


def test_derived_address_parts_carry_their_source(resolver):
    """
    Части адреса выводятся из строки, которую человек уже подтвердил, поэтому
    отдельный вопрос по каждой не задаётся — но источник виден в карточке.
    """
    c = Carriage()
    resolver.apply_extraction(c, {"pickup_address": ("141400, Химки, Ленина 1", 0.9, "заявка")})
    resolver.resolve(c)
    assert c.get("pickup_city") == "Химки"
    assert c.values["pickup_city"].source == "разобрал из адреса"
    assert not c.values["pickup_city"].needs_confirm
    assert c.get("pickup_postcode") == "141400"
    assert "Химки" in resolver.card(c)


def test_codes_are_shown_in_words(resolver):
    """Бухгалтеру нельзя показывать «ТипВлад=3» — только «3 — аренда»."""
    c = Carriage()
    c.set("carrier_inn", "7736050003", "заявка")
    ok, msg = resolver.dont_know(c, "vehicle_ownership")
    assert ok and "аренда" in msg


# ── карточка и блокеры ──────────────────────────────────────────────────────

def test_blockers_list_only_unfilled_required(resolver):
    """Блокер — обязательное поле без значения. Чужие и пропущенные не блокируют."""
    c = Carriage()
    resolver.resolve(c)
    keys = {b.field.key for b in resolver.blockers(c)}
    assert keys, "на пустом прогоне блокеры обязаны быть"
    assert keys & set(NEVER_GUESS), "критичные поля должны попадать в блокеры"
    assert not (keys & c.deferred), "чужие поля не блокируют"
    for key in keys:
        assert BY_KEY[key].required and key not in c.values


def test_ready_requires_no_unconfirmed(carriage_full, resolver):
    """Готовность = нет блокеров И нет неподтверждённых значений."""
    assert resolver.ready(carriage_full)
    carriage_full.set("cargo_places", 40, "фото накладной", 0.5)
    assert not resolver.ready(carriage_full)


def test_full_carriage_has_no_open_questions(carriage_full, resolver):
    assert resolver.open_questions(carriage_full) == []
