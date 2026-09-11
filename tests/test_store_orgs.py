"""Хранилище состояния, профили организаций, классификатор тары, клиент модели."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from etrn_bot import packaging, store, vlm
from etrn_bot.orgs import Profile, Registry
from etrn_bot.resolve import Carriage, Resolver


# ── сериализация состояния ──────────────────────────────────────────────────

def test_carriage_survives_round_trip(carriage_full):
    back = store.loads(store.dumps(carriage_full))
    assert len(back.values) == len(carriage_full.values)
    for key, v in carriage_full.values.items():
        assert back.get(key) == v.value, key
        assert back.values[key].source == v.source
        assert back.values[key].confidence == v.confidence
    assert back.deferred == carriage_full.deferred


def test_datetime_and_tuples_survive():
    """Перезапуск бота не должен превращать дату в строку, а ФИО — в мусор."""
    c = Carriage()
    c.set("pickup_datetime", datetime(2026, 9, 10, 8, 0), "заявка")
    c.set("driver_fio", ("Петров", "Иван", "Сергеевич"), "фото ВУ")
    c.set("doc_date", date(2026, 9, 9), "рассчитал сам")
    back = store.loads(store.dumps(c))
    assert back.get("pickup_datetime") == datetime(2026, 9, 10, 8, 0)
    assert back.get("driver_fio") == ("Петров", "Иван", "Сергеевич")
    assert back.get("doc_date") == date(2026, 9, 9)


def test_memory_store_keeps_session_meta(carriage_full):
    s = store.MemoryStore()
    s.save(7, {"carriage": carriage_full, "org_inn": "5001000001", "current": "cargo_name"})
    session = s.load(7)
    assert session["org_inn"] == "5001000001"
    assert session["current"] == "cargo_name"
    s.drop(7)
    assert s.load(7) is None


def test_counter_is_per_organization():
    """Номера накладных не должны пересекаться между юрлицами и повторяться."""
    s = store.MemoryStore()
    assert [s.next_number("111"), s.next_number("111"), s.next_number("222")] == [1, 2, 1]


def test_redis_url_that_does_not_answer_fails_loudly(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6390/0")
    with pytest.raises(RuntimeError) as e:
        store.from_env()
    assert "не отвечает" in str(e.value)


# ── профили организаций ─────────────────────────────────────────────────────

def test_profile_missing_lists_what_to_ask():
    p = Profile(inn="5001000001", name='ООО "РОМАШКА"')
    missing = p.missing()
    assert "адрес" in missing and "телефон" in missing
    assert not p.ready


def test_registry_saves_and_reads(tmp_path: Path):
    reg = Registry(tmp_path / "orgs.json")
    reg.add(Profile(inn="5001000001", name='ООО "ОДИН"'))
    reg.add(Profile(inn="7700000000", name='ООО "ДВА"'))
    # список идёт по названию — так его читает человек в меню выбора
    assert [p.name for p in reg.list()] == ['ООО "ДВА"', 'ООО "ОДИН"']
    assert reg.get("7700000000").name == 'ООО "ДВА"'
    # файл только для владельца: там реквизиты организации
    assert oct((tmp_path / "orgs.json").stat().st_mode)[-3:] == "600"


def test_profile_settings_feed_resolver(directory, tmp_path):
    p = Profile(inn="5001000001", name='ООО "НАША КОМПАНИЯ"',
                address="141400, Химки, Ленина 1", phone="+7 (495) 111-22-33",
                edo_id="2BM-5001000001", signer_fio="Иванова Анна Петровна",
                signer_position="главный бухгалтер")
    s = store.MemoryStore()
    directory.orgs.setdefault(p.inn, {}).update(p.directory_card())
    r = Resolver(directory, p.org_settings(next_number=s.next_number), today=date(2026, 9, 9))
    c = Carriage()
    r.resolve(c)
    assert c.get("doc_number") == "00001"
    assert c.get("shipper_phone") == "+7 (495) 111-22-33"
    assert c.get("signer_fio") == "Иванова Анна Петровна"
    # политика организации не требует подтверждения
    assert not c.values["signer_status"].needs_confirm


def test_numbers_do_not_repeat_across_sessions(directory):
    """Две параллельные накладные не имеют права получить один номер."""
    p = Profile(inn="5001000001", name="ООО", address="а", phone="т", signer_fio="И И")
    s = store.MemoryStore()
    settings = p.org_settings(next_number=s.next_number)
    r1 = Resolver(directory, settings, today=date(2026, 9, 9))
    r2 = Resolver(directory, settings, today=date(2026, 9, 9))
    c1, c2 = Carriage(), Carriage()
    r1.resolve(c1)
    r2.resolve(c2)
    assert c1.get("doc_number") != c2.get("doc_number")


# ── классификатор тары ──────────────────────────────────────────────────────

def test_packaging_classifier_loaded():
    assert len(packaging.codes()) > 300
    assert packaging.title("PX") == "Поддон"


@pytest.mark.parametrize("text,code", [
    ("паллеты", "PX"), ("мешки", "BG"), ("бочки", "CK"), ("ящики", "CS"),
    ("коробки", "BX"), ("наливом", "VL"), ("без упаковки", "NE"),
])
def test_packaging_search_finds_common_kinds(text, code):
    found = packaging.search(text)
    assert found and found[0][0] == code, found[:3]


def test_packaging_search_refuses_nonsense():
    assert packaging.search("чего-то непонятное") == []
    assert packaging.search("") == []


def test_invalid_code_is_rejected():
    from etrn_bot import parse
    assert parse.run("packcode", "ЯЯ")[0] is None
    assert parse.run("packcode", "99")[0] is None


def test_code_from_words_is_marked_as_guess():
    from etrn_bot import parse
    code, conf, shown = parse.run("packcode", "паллеты")
    assert code == "PX" and conf < 0.75 and "Поддон" in shown


# ── клиент модели ───────────────────────────────────────────────────────────

def test_without_model_falls_back_to_ocr(monkeypatch):
    """Модель не настроена — распознаём тем, что есть локально."""
    from etrn_bot import ocr
    monkeypatch.delenv("ETRN_VLM_BASE_URL", raising=False)
    client = vlm.from_env()
    if ocr.available() and "rus" in ocr.languages():
        assert isinstance(client, ocr.TesseractClient)
    else:
        assert isinstance(client, vlm.NoVLMClient)


def test_without_anything_bot_still_works(monkeypatch):
    """Ни модели, ни tesseract — бот не падает, просто спрашивает всё сам."""
    from etrn_bot import ocr
    monkeypatch.delenv("ETRN_VLM_BASE_URL", raising=False)
    monkeypatch.setattr(ocr, "available", lambda: False)
    client = vlm.from_env()
    assert isinstance(client, vlm.NoVLMClient)
    assert client.complete() == "{}"


def test_http_client_refuses_pdf():
    from etrn_bot.extract import Document
    client = vlm.HttpVLMClient(base_url="https://example.invalid/v1", api_key="x")
    with pytest.raises(vlm.VLMError) as e:
        client.complete(model="m", system="s",
                        documents=[Document("a.pdf", "application/pdf", b"%PDF")])
    assert "фотографиями" in str(e.value)


def test_extractor_retries_then_gives_up():
    from etrn_bot.extract import Extractor

    class Client:
        def __init__(self, answers):
            self.answers, self.n = answers, 0

        def complete(self, **_kw):
            a = self.answers[min(self.n, len(self.answers) - 1)]
            self.n += 1
            return a

    ok = Extractor(Client(["не json", '{"cargo_name": {"value": "Сера"}}']), "m")
    assert ok.extract([])["cargo_name"][0] == "Сера"

    fenced = Extractor(Client(['```json\n{"cargo_places": {"value": 40}}\n```']), "m")
    assert fenced.extract([])["cargo_places"][0] == 40

    hopeless = Client(["нет"] * 5)
    assert Extractor(hopeless, "m").extract([]) == {}
    assert hopeless.n == 3, "две попытки после первой, потом отказ"
