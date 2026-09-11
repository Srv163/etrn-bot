"""Извлечение из документов: промпт из реестра и склейка результатов."""

from __future__ import annotations

from etrn_bot import extract
from etrn_bot.extract import Document, Extractor, merge
from etrn_bot.fields import REGISTRY, Strategy


class FakeClient:
    """Подставной клиент VLM: ядро тестируется без сети и без провайдера."""

    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def complete(self, **kw) -> str:
        self.calls.append(kw)
        return self.payload


def test_prompt_is_built_from_registry():
    prompt = extract.build_schema_prompt()
    extractable = [f for f in REGISTRY if Strategy.EXTRACT in f.strategies]
    assert extractable, "в реестре должны быть поля, которые ищутся в документах"
    for f in extractable:
        assert f.key in prompt, f.key
    # Чужие поля модель искать не должна — их заполняет другая сторона.
    assert "carriage_cost" not in prompt


def test_extract_parses_confidence_and_source():
    payload = ('{"cargo_name": {"value": "Сера", "confidence": 0.93, '
               '"source": "заявка №418, строка Груз"}}')
    ex = Extractor(FakeClient(payload), model="test")
    out = ex.extract([Document("z.jpg", "image/jpeg", b"")])
    assert out["cargo_name"] == ("Сера", 0.93, "заявка №418, строка Груз")


def test_extract_on_garbage_returns_empty():
    """Модель ответила не-JSON — это отказ, а не повод придумать значения."""
    ex = Extractor(FakeClient("извините, не смог"), model="test")
    assert ex.extract([]) == {}


def test_extract_skips_fields_without_value():
    payload = '{"cargo_name": {"confidence": 0.9}, "cargo_places": {"value": 40}}'
    ex = Extractor(FakeClient(payload), model="test")
    out = ex.extract([])
    assert "cargo_name" not in out
    assert out["cargo_places"][0] == 40


# ── склейка нескольких документов ───────────────────────────────────────────

def test_merge_prefers_higher_confidence():
    a = {"cargo_mass_gross_kg": (20000, 0.9, "заявка")}
    b = {"cargo_mass_gross_kg": (20000, 0.6, "фото весов")}
    assert merge(a, b)["cargo_mass_gross_kg"] == (20000, 0.9, "заявка")


def test_merge_flags_conflict_between_confident_documents():
    """
    Два документа уверенно говорят разное — молча выбирать нельзя:
    уверенность падает до 0.5, источник называет оба документа,
    значит поле уедет человеку на подтверждение.
    """
    a = {"cargo_mass_gross_kg": (20000, 0.95, "заявка №418")}
    b = {"cargo_mass_gross_kg": (18000, 0.9, "договор-заявка")}
    val, conf, src = merge(a, b)["cargo_mass_gross_kg"]
    assert val == 20000              # взяли более уверенный
    assert conf == 0.5               # но ниже порога 0.75 — будет вопрос
    assert "расхождение" in src
    assert "заявка №418" in src and "договор-заявка" in src


def test_merge_conflict_reaches_the_human(resolver):
    """Расхождение доезжает до карточки как открытый вопрос."""
    from etrn_bot.resolve import Carriage
    merged = merge({"cargo_places": (40, 0.95, "заявка")},
                   {"cargo_places": (44, 0.9, "спецификация")})
    c = Carriage()
    resolver.apply_extraction(c, merged)
    resolver.resolve(c)
    assert any(a.field.key == "cargo_places" for a in resolver.open_questions(c))
