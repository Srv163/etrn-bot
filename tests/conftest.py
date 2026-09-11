"""
Фикстуры: три рейса с разной полнотой документов.

Рейсы отличаются не «объёмом данных», а тем, что именно боту приходится
доспрашивать: полный комплект документов, тонкий комплект, добранный ответами
человека, и рейс с расхождением между двумя документами.

Справочники и политика организации берутся из `etrn_bot.tools.demo` — один
источник для тестов и для ручного прогона, иначе они разъедутся.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from etrn_bot.resolve import Carriage, Directory, Resolver
from etrn_bot.tools import demo


@pytest.fixture
def directory() -> Directory:
    return demo.directory()


@pytest.fixture
def org() -> dict:
    return demo.org_settings()


@pytest.fixture
def resolver(directory, org) -> Resolver:
    return Resolver(directory, org, today=demo.TODAY)


FULL_EXTRACTION = demo.EXTRACTION


@pytest.fixture
def carriage_full(resolver) -> Carriage:
    """Рейс 1: пришли заявка, договор, фото ВУ и СТС, журнал въезда."""
    return demo.build_carriage(resolver)


@pytest.fixture
def carriage_answered(resolver) -> Carriage:
    """
    Рейс 2: в документах не было упаковки, маркировки, состояния груза и
    фактических времён — человек ответил в диалоге. Так выглядит обычный рейс.
    """
    thin = {k: v for k, v in FULL_EXTRACTION.items()
            if k not in ("cargo_package_code", "cargo_marking", "cargo_state",
                         "pickup_arrival_fact", "pickup_departure_fact")}
    c = demo.build_carriage(resolver, thin, confirm_all=False)
    answers = {
        "cargo_package_code": "PX",
        "cargo_marking": "без маркировки",
        "cargo_state": "В исправном состоянии",
        "pickup_arrival_fact": "10.09 07:50",
        "pickup_departure_fact": "10.09 09:30",
    }
    for key, text in answers.items():
        ok, shown = resolver.answer(c, key, text)
        assert ok, f"{key}: {shown}"
    for ask in list(resolver.open_questions(c)):
        v = c.values.get(ask.field.key)
        if v is not None:
            c.set(ask.field.key, v.value, "вы сказали", 1.0)
    return c


@pytest.fixture
def carriage_thin(resolver) -> Carriage:
    """Рейс 3: тонкий комплект без доспроса — собирать нечего."""
    thin = {k: v for k, v in FULL_EXTRACTION.items()
            if k not in ("cargo_package_code", "cargo_marking", "cargo_state",
                         "pickup_arrival_fact", "pickup_departure_fact")}
    return demo.build_carriage(resolver, thin)


@pytest.fixture
def carriage_conflict(resolver) -> Carriage:
    """
    Рейс 4: заявка и договор-заявка расходятся по массе.
    Проверяет, что расхождение доезжает до человека, а не тонет.
    """
    conflict = dict(FULL_EXTRACTION)
    conflict["cargo_mass_gross_kg"] = (18000, 0.5,
                                       "расхождение: заявка №418 / договор-заявка")
    return demo.build_carriage(resolver, conflict)
