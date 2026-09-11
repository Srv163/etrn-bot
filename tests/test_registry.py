"""
Реестр как единственный источник правды.

Эти тесты ловят не «код упал», а расхождение реестра со схемой ФНС и
с правилами из CLAUDE.md — то, что иначе всплывёт отказом оператора ЭДО.
"""

from __future__ import annotations

import pytest

from etrn_bot.fields import BY_KEY, REGISTRY, Field, Strategy
from etrn_bot.resolve import Carriage, Resolver
from etrn_bot.tools.check_registry import DEFAULT_SCHEMA, TEMPLATE_OWNED, schema_nodes


@pytest.fixture(scope="module")
def nodes():
    if not DEFAULT_SCHEMA.exists():
        pytest.skip(f"нет схемы {DEFAULT_SCHEMA.name}")
    return schema_nodes(DEFAULT_SCHEMA)


def test_every_xsd_path_exists_in_schema(nodes):
    """Дельта 1 из задачи 1: путей, которых нет в схеме, быть не должно."""
    every, _required = nodes
    bad = [(f.key, p) for f in REGISTRY if f.xsd
           for p in (x.strip() for x in f.xsd.split("|")) if p not in every]
    assert bad == []


def test_every_required_schema_node_is_covered(nodes):
    """Дельта 2: обязательные узлы схемы закрыты реестром или шаблоном."""
    _every, required = nodes
    covered = set(TEMPLATE_OWNED)
    for f in REGISTRY:
        if f.xsd:
            covered.update(p.strip() for p in f.xsd.split("|"))
    uncovered = [p for p in required
                 if p not in covered
                 and not any(c.startswith(p + "/") or c.startswith(p + "@") for c in covered)]
    assert uncovered == []


def test_not_in_xml_fields_explain_themselves():
    """not_in_xml не должен стать местом, куда прячут несверенные поля."""
    for f in REGISTRY:
        if f.not_in_xml:
            assert f.why, f.key
            assert f.xsd is None, f.key


def test_no_field_left_unverified():
    """verify=True означает «привязка не подтверждена» — таких быть не должно."""
    assert [f.key for f in REGISTRY if f.verify] == []


def test_keys_are_unique():
    keys = [f.key for f in REGISTRY]
    assert len(keys) == len(set(keys))


def test_asked_fields_have_human_question():
    """Если поле спрашивают у человека — вопрос должен быть написан по-людски."""
    for f in REGISTRY:
        if Strategy.ASK in f.strategies and f.required and not f.can_self_fill():
            assert f.question or f.title, f.key
            if f.question:
                assert not f.question.startswith("Поле"), f.key


def test_defer_fields_name_the_owner():
    for f in REGISTRY:
        if Strategy.DEFER in f.strategies:
            assert f.defer_to, f.key


# ── добавление поля не ломает существующее поведение (приёмка задачи 3) ─────

NEW_FIELD = Field(
    key="_test_extra", section="Служебное", title="Тестовое поле",
    strategies=(Strategy.DEFAULT,), default="значение", required=False,
    why="поле из теста: проверяем, что расширение реестра ничего не рушит",
    not_in_xml=True,
)


def test_registry_can_grow(monkeypatch, directory, org):
    before = Resolver(directory, dict(org), today=None)
    c1 = Carriage()
    before.resolve(c1)
    baseline = {k: v.value for k, v in c1.values.items()}

    # Движок берёт поля через registry_for, а она читает реестр модуля fields.
    monkeypatch.setattr("etrn_bot.fields.REGISTRY", REGISTRY + (NEW_FIELD,))
    monkeypatch.setitem(BY_KEY, NEW_FIELD.key, NEW_FIELD)

    after = Resolver(directory, dict(org), today=None)
    c2 = Carriage()
    after.resolve(c2)

    # старые поля закрылись так же, новое добавилось
    for key, val in baseline.items():
        assert c2.get(key) == val, key
    assert c2.get("_test_extra") == "значение"
