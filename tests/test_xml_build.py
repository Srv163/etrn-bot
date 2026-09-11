"""Сборка и валидация XML: файл либо проходит XSD, либо не отдаётся."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from etrn_bot import xml_build
from etrn_bot.xml_build import BuildError


NOW = datetime(2026, 9, 10, 10, 0, 0)


def test_full_passes_xsd(carriage_full):
    xml, errors = xml_build.build_title1(carriage_full, file_id="ON_TRNACLGROT_TEST", now=NOW)
    assert errors == [], errors
    assert xml.startswith('<?xml version="1.0" encoding="windows-1251"?>'.encode("cp1251"))


def test_answered_passes_xsd(carriage_answered):
    """Тонкий комплект, добранный ответами человека, тоже даёт валидный файл."""
    xml, errors = xml_build.build_title1(carriage_answered, file_id="ON_TRNACLGROT_TEST", now=NOW)
    assert errors == [], errors
    assert "без маркировки".encode("cp1251") in xml


def test_conflict_passes_xsd_but_flags_mass(carriage_conflict):
    # Расхождение по массе не мешает собрать файл, но значение помечено
    # неуверенным — человек это видел перед подписью (в тесте подтверждено).
    xml, errors = xml_build.build_title1(carriage_conflict, file_id="ON_TRNACLGROT_TEST", now=NOW)
    assert errors == [], errors


def test_thin_refuses_to_build(carriage_thin):
    """Нет фактов погрузки — собирать нечего, и это ошибка, а не пустой атрибут."""
    with pytest.raises(BuildError) as e:
        xml_build.build_title1(carriage_thin, file_id="ON_TRNACLGROT_TEST", now=NOW)
    # Сообщение называет конкретное поле — бухгалтеру видно, чего не хватает.
    missing = str(e.value)
    assert missing.startswith("нет обязательного значения:")
    assert any(k in missing for k in ("маркировка", "код вида тары",
                                      "состояние груза", "прибытие", "убытие"))


def test_broken_xml_reported_not_swallowed():
    errors = xml_build.validate('<?xml version="1.0" encoding="windows-1251"?><Файл/>'.encode("cp1251"))
    assert errors and any("ИдФайл" in e for e in errors)


# ── имя файла обмена ────────────────────────────────────────────────────────

def test_filename_matches_order():
    name = xml_build.filename("2BM-500", "2BM-773", "2BM-770", when=date(2026, 9, 10))
    assert name.startswith("ON_TRNACLGROT_2BM-773_2BM-770_2BM-500_0_20260910_")
    assert name.endswith(".xml")
    guid = name[:-4].split("_")[-1]
    assert len(guid) == 36


def test_filename_without_edo_ids_refuses():
    with pytest.raises(BuildError) as e:
        xml_build.filename("2BM-500", "", "2BM-770")
    assert "оператор ЭДО" in str(e.value)


def test_filename_rejects_too_long_id():
    with pytest.raises(BuildError):
        xml_build.filename("2BM-500", "2BM-" + "x" * 60, "2BM-770")


# ── форматирование под типы схемы ───────────────────────────────────────────

def test_fmt_datetime_is_25_chars():
    s = xml_build.fmt_datetime(datetime(2026, 9, 10, 8, 0, 0))
    assert s == "10.09.2026T08:00:00+03:00"
    assert len(s) == 25


def test_fmt_date_rejects_garbage():
    with pytest.raises(BuildError):
        xml_build.fmt_date("завтра")


def test_kg_to_tonnes():
    assert xml_build.kg_to_t(20000) == "20.00"
    assert xml_build.kg_to_t(20) == "20.00"
