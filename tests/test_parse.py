"""Парсеры: кривые входы не должны превращаться в уверенные значения."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from etrn_bot import parse

TODAY = date(2026, 9, 9)


# ── масса ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kg", [
    ("20 т", 20000), ("20,5 т", 20500), ("18500 кг", 18500), ("тонн двадцать", 20000),
])
def test_mass_ok(text, kg):
    val, conf, _ = parse.run("mass", text)
    assert val == kg and conf >= 0.75


def test_mass_without_unit_is_unsure():
    """«22» — тонны или килограммы? Бот обязан переспросить, а не решить сам."""
    val, conf, shown = parse.run("mass", "22")
    assert val == 22000 and conf < 0.75 and "единица не указана" in shown


@pytest.mark.parametrize("text", ["", "много", "как договаривались", "-"])
def test_mass_garbage(text):
    assert parse.run("mass", text)[0] is None


# ── дата и время ────────────────────────────────────────────────────────────

def test_datetime_relative():
    val, conf, _ = parse.datetime_ru("завтра к 8", today=TODAY)
    assert val == datetime(2026, 9, 10, 8, 0) and conf >= 0.75


def test_datetime_weekday_is_unsure():
    """«в понедельник» — какой именно? Ниже порога, бот подтверждает."""
    val, conf, _ = parse.datetime_ru("в понедельник утром", today=TODAY)
    assert isinstance(val, datetime) and conf < 0.75


@pytest.mark.parametrize("text", ["", "скоро", "32.13.2026"])
def test_datetime_garbage(text):
    assert parse.datetime_ru(text, today=TODAY)[0] is None


# ── ИНН ─────────────────────────────────────────────────────────────────────

def test_inn_checksum_ok():
    val, conf, _ = parse.run("inn", "7707083893")
    assert val == "7707083893" and conf > 0.9


def test_inn_checksum_broken_is_flagged():
    """Контрольная сумма не сошлась — значение отдаём, но с низкой уверенностью."""
    val, conf, shown = parse.run("inn", "1234567890")
    assert val == "1234567890" and conf < 0.75 and "контрольная сумма" in shown


@pytest.mark.parametrize("text", ["77070838", "абв", ""])
def test_inn_garbage(text):
    assert parse.run("inn", text)[0] is None


# ── госномер, ВУ, телефон ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,norm", [
    ("а123вс77", "А123ВС77"), ("A 123 BC 777", "А123ВС777"), ("а123вс-777", "А123ВС777"),
])
def test_plate_ok(text, norm):
    assert parse.run("plate", text)[0] == norm


@pytest.mark.parametrize("text", ["123", "ЁЁ123", ""])
def test_plate_garbage(text):
    assert parse.run("plate", text)[0] is None


def test_license_normalized():
    assert parse.run("license", "99 12 345678")[0] == "9912 345678"


@pytest.mark.parametrize("text", ["99 12 34567", "нет прав", ""])
def test_license_garbage(text):
    assert parse.run("license", text)[0] is None


@pytest.mark.parametrize("text", ["8 916 123 45 67", "+7(916)1234567", "9161234567"])
def test_phone_ok(text):
    assert parse.run("phone", text)[0] == "+7 (916) 123-45-67"


@pytest.mark.parametrize("text", ["123", "телефона нет", ""])
def test_phone_garbage(text):
    assert parse.run("phone", text)[0] is None


# ── новые парсеры под требования схемы ──────────────────────────────────────

def test_postcode_from_address_start_is_confident():
    val, conf, _ = parse.run("postcode", "141400, Химки, Ленина 1")
    assert val == "141400" and conf >= 0.75


def test_postcode_in_middle_is_unsure():
    """Шесть цифр посреди строки бывают номером договора — подтверждаем."""
    val, conf, _ = parse.run("postcode", "Химки, Ленина 141400")
    assert val == "141400" and conf < 0.9


@pytest.mark.parametrize("text", ["Химки", "1414", "", "1414000"])
def test_postcode_garbage(text):
    assert parse.run("postcode", text)[0] is None


@pytest.mark.parametrize("text,code", [("50", "50"), ("5", "05"), ("код 77", "77")])
def test_region_ok(text, code):
    assert parse.run("region", text)[0] == code


@pytest.mark.parametrize("text", ["завтра к 8", "Московская область", "", "500"])
def test_region_garbage(text):
    """«завтра к 8» не должно приезжать кодом региона 08 — так ломался answer_batch."""
    assert parse.run("region", text)[0] is None


def test_fio_full():
    val, conf, _ = parse.run("fio", "петров иван сергеевич")
    assert val == ("Петров", "Иван", "Сергеевич") and conf >= 0.75


def test_fio_initials_are_unsure():
    val, conf, shown = parse.run("fio", "Петров И.С.")
    assert conf < 0.75 and "полностью" in shown


@pytest.mark.parametrize("text", ["Петров", ""])
def test_fio_garbage(text):
    assert parse.run("fio", text)[0] is None


# ── разбор адреса ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("141400, Химки, Ленина 1, склад 3", {"city": "Химки", "street": "Ленина", "house": "1"}),
    ("Москва, ул. Вавилова, д. 19", {"city": "Москва", "street": "Вавилова", "house": "19"}),
    ("г. Химки, Ленина 1", {"city": "Химки", "street": "Ленина", "house": "1"}),
])
def test_address_parts(text, expect):
    assert parse.address_parts(text) == expect


def test_address_parts_partial():
    """Одна «Москва» — только город; выдумывать улицу нельзя."""
    assert parse.address_parts("Москва") == {"city": "Москва"}


def test_address_parts_empty():
    assert parse.address_parts("") == {}
