"""
Сценарии бота без сети и без телеграма.

Приёмка задачи 5: меню → документы → «Готово, разбирай» → вопросы → карточка →
файл. Плюс путь заявки: заявка в Word → одной кнопкой накладная на тех же данных.

Хендлеры aiogram — обычные async-функции, поэтому вызываем их напрямую,
подставляя минимальные заглушки объектов телеграма.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from etrn_bot import bot as bot_mod
from etrn_bot import store as store_mod
from etrn_bot import vlm
from etrn_bot.fields import BY_KEY, ETRN, ZAYAVKA
from etrn_bot.orgs import Registry
from etrn_bot.tools import demo


# ── заглушки телеграма ──────────────────────────────────────────────────────

class FakeChat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id


class FakeFile:
    def __init__(self, name: str, mime: str) -> None:
        self.file_id = f"fid_{name}"
        self.file_name = name
        self.mime_type = mime


class FakeMessage:
    def __init__(self, chat_id: int = 1, text: str = "", document=None, photo=None) -> None:
        self.chat = FakeChat(chat_id)
        self.text = text
        self.document = document
        self.photo = photo
        self.sent: list[str] = []
        self.markups: list = []
        self.documents: list[tuple[str, bytes]] = []

    async def answer(self, text: str, reply_markup=None, **_kw) -> "FakeMessage":
        self.sent.append(text)
        self.markups.append(reply_markup)
        return self

    async def edit_text(self, text: str, **_kw) -> None:
        self.sent.append(text)

    async def answer_document(self, file, caption: str = "", **_kw) -> None:
        self.documents.append((file.filename, file.data))
        self.sent.append(caption)

    def buttons(self) -> list[str]:
        out = []
        for m in self.markups:
            if m:
                out += [b.callback_data for row in m.inline_keyboard for b in row]
        return out


class FakeBot:
    async def download(self, _file_id):
        return io.BytesIO(b"\xff\xd8\xff jpeg-stub")


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage) -> None:
        self.data = data
        self.message = message
        self.alerts: list[str] = []

    async def answer(self, text: str = "", **_kw) -> None:
        if text:
            self.alerts.append(text)


class FakeExtractor:
    """Возвращает то, что «распознал», по одному куску на документ."""

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks = chunks
        self.calls = 0

    def extract(self, _docs):
        self.calls += 1
        return self.chunks[min(self.calls - 1, len(self.chunks) - 1)]


# ── окружение ───────────────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path: Path):
    directory = demo.directory()
    directory.orgs.pop("5001000001", None)   # организацию заводим диалогом /orgs
    return {"store": store_mod.MemoryStore(), "registry": Registry(tmp_path / "orgs.json"),
            "directory": directory}


ORG_ANSWERS = ["5001000001", 'ООО "НАША КОМПАНИЯ"', "500101001", "141400, Химки, Ленина 1",
               "+7 (495) 111-22-33", "2BM-5001000001", "Иванова Анна Петровна",
               "главный бухгалтер"]


async def _setup_org(env) -> FakeMessage:
    msg = FakeMessage()
    await bot_mod.cmd_orgs(msg, store=env["store"], registry=env["registry"])
    for answer in ORG_ANSWERS:
        step = FakeMessage(text=answer)
        await bot_mod.on_text(step, store=env["store"], registry=env["registry"],
                              directory=env["directory"])
        msg.sent.extend(step.sent)
        msg.markups.extend(step.markups)
    return msg


async def _start(env, doc_type: str) -> FakeMessage:
    msg = FakeMessage()
    await bot_mod.cb_start(FakeCallback(f"start:{doc_type}", msg),
                           store=env["store"], registry=env["registry"])
    return msg


async def _answer_until_card(env, answers: dict[str, str], limit: int = 25) -> list[str]:
    """
    Отвечаем на вопросы, пока бот не покажет карточку. Если значение уже стоит
    и бот лишь не уверен — «да», как ответил бы человек.
    """
    asked: list[str] = []
    for _ in range(limit):
        session = env["store"].load(1)
        key = session.get("current")
        if not key:
            return asked
        value = session["carriage"].values.get(key)
        # Свой ответ человек даёт один раз; если бот переспросил — отвечает «да».
        text = (answers.get(key) if key not in asked else None) or \
            ("да" if value is not None else None)
        asked.append(key)
        assert text, f"бот спросил то, на что у теста нет ответа: {key}"
        step = FakeMessage(text=text)
        await bot_mod.on_text(step, store=env["store"], registry=env["registry"],
                              directory=env["directory"])
    pytest.fail(f"вопросы не кончились: {asked}")


# ── организация и меню ──────────────────────────────────────────────────────

async def test_org_setup_dialog_ends_with_menu(env):
    msg = await _setup_org(env)
    assert "Записал" in msg.sent[-1]
    assert f"start:{ZAYAVKA}" in msg.buttons() and f"start:{ETRN}" in msg.buttons()
    profile = env["registry"].get("5001000001")
    assert profile.ready and profile.edo_id == "2BM-5001000001"


async def test_start_without_org_asks_to_setup(env):
    msg = FakeMessage()
    await bot_mod.cmd_start(msg, registry=env["registry"])
    assert "/orgs" in msg.sent[-1]


async def test_start_shows_both_documents(env):
    await _setup_org(env)
    msg = FakeMessage()
    await bot_mod.cmd_start(msg, registry=env["registry"])
    assert f"start:{ZAYAVKA}" in msg.buttons() and f"start:{ETRN}" in msg.buttons()


# ── накладная ───────────────────────────────────────────────────────────────

async def test_full_flow_to_xml(env):
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    begin = await _start(env, ETRN)
    assert "parse" in begin.buttons()

    for name in ("zayavka.pdf", "dogovor.pdf", "vu.jpg", "sts.jpg", "vezd.jpg"):
        await bot_mod.on_file(FakeMessage(document=FakeFile(name, "image/jpeg")), FakeBot(),
                              store=store, registry=registry, directory=directory,
                              extractor=FakeExtractor([{}]))
    assert store.load(1)["docs"] == 5

    recognised = dict(demo.EXTRACTION)
    for key in ("pickup_arrival_fact", "pickup_departure_fact", "cargo_package_code"):
        recognised.pop(key)
    extractor = FakeExtractor([recognised] + [{}] * 4)
    parse_msg = FakeMessage()
    await bot_mod.cb_parse(FakeCallback("parse", parse_msg), store=store, registry=registry,
                           directory=directory, extractor=extractor)
    assert extractor.calls == 5
    assert bot_mod.PENDING_DOCS.get(1) is None, "сканы удаляются сразу после разбора"
    assert any("Понял из документов" in t for t in parse_msg.sent)
    assert any("Вопрос 1 из" in t for t in parse_msg.sent), "видно, сколько осталось"

    await _answer_until_card(env, {"cargo_package_code": "паллеты",
                                   "pickup_arrival_fact": "10.09 07:50",
                                   "pickup_departure_fact": "10.09 09:30"})

    build_msg = FakeMessage()
    await bot_mod.cb_build(FakeCallback("build", build_msg), store=store,
                           registry=registry, directory=directory)
    assert build_msg.documents, f"XML не отдан: {build_msg.sent}"
    name, payload = build_msg.documents[0]
    assert name.startswith("ON_TRNACLGROT_")
    assert payload.startswith('<?xml version="1.0" encoding="windows-1251"?>'.encode("cp1251"))
    assert store.load(1) is None, "после сборки начинаем с чистого листа"


async def test_file_during_questions_is_an_answer(env):
    """Бот спросил — человек прислал фото вместо ответа. Разбираем сразу."""
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    await bot_mod.cb_parse(FakeCallback("parse", FakeMessage()), store=store,
                           registry=registry, directory=directory,
                           extractor=FakeExtractor([{}]))
    assert store.load(1)["current"], "бот должен был задать вопрос"
    photo = FakeMessage(document=FakeFile("vu.jpg", "image/jpeg"))
    extractor = FakeExtractor([{"vehicle_plate": ("А123ВС777", 0.95, "фото СТС")}])
    await bot_mod.on_file(photo, FakeBot(), store=store, registry=registry,
                          directory=directory, extractor=extractor)
    assert extractor.calls == 1
    assert store.load(1)["carriage"].get("vehicle_plate") == "А123ВС777"


async def test_provider_failure_does_not_show_traceback(env):
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    await bot_mod.on_file(FakeMessage(document=FakeFile("z.jpg", "image/jpeg")), FakeBot(),
                          store=store, registry=registry, directory=directory,
                          extractor=FakeExtractor([{}]))

    class Broken:
        def extract(self, _docs):
            raise vlm.VLMError("провайдер недоступен: нет сети")

    msg = FakeMessage()
    await bot_mod.cb_parse(FakeCallback("parse", msg), store=store, registry=registry,
                           directory=directory, extractor=Broken())
    text = " ".join(msg.sent)
    assert "спрошу всё сам" in text.lower()
    assert "Traceback" not in text
    assert store.load(1) is not None, "диалог продолжается"


async def test_build_without_edo_id_explains_why(env):
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    session = store.load(1)
    r = bot_mod._resolver(directory, registry, store, session)
    c = demo.build_carriage(r)
    c.values.pop("carrier_edo_id")
    session["carriage"] = c
    store.save(1, session)

    msg = FakeMessage()
    cb = FakeCallback("build", msg)
    await bot_mod.cb_build(cb, store=store, registry=registry, directory=directory)
    assert not msg.documents
    assert any("Идентификатор участника ЭДО" in a for a in cb.alerts), cb.alerts
    assert "справочник" in " ".join(msg.sent)


async def test_fix_edits_a_field_and_returns_to_card(env):
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    session = store.load(1)
    r = bot_mod._resolver(directory, registry, store, session)
    session["carriage"] = demo.build_carriage(r)
    store.save(1, session)

    await bot_mod.cb_edit(FakeCallback("edit:cargo_places", FakeMessage()), store=store)
    step = FakeMessage(text="44")
    await bot_mod.on_text(step, store=store, registry=registry, directory=directory)
    assert store.load(1)["carriage"].get("cargo_places") == 44
    assert any("Проверьте" in t for t in step.sent), "после правки — снова карточка"


# ── заявка ──────────────────────────────────────────────────────────────────

ZAYAVKA_RECOGNISED = {
    "consignee_inn": ("7707083893", 0.99, "переписка"),
    "carrier_inn": ("7736050003", 0.99, "переписка"),
    "cargo_name": ("Сера гранулированная", 0.93, "переписка"),
    "cargo_mass_gross_kg": (20000, 0.9, "переписка"),
    "pickup_address": ("141400, Химки, Ленина 1, склад 3", 0.9, "переписка"),
    "delivery_address": ("117997, Москва, Вавилова 19", 0.9, "переписка"),
    "pickup_datetime": (demo.NOW.replace(hour=8, minute=0), 0.9, "переписка"),
    "cargo_places": (40, 0.9, "переписка"),
}


async def test_zayavka_to_docx_then_etrn_in_one_button(env):
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ZAYAVKA)
    await bot_mod.on_file(FakeMessage(document=FakeFile("perepiska.png", "image/png")),
                          FakeBot(), store=store, registry=registry, directory=directory,
                          extractor=FakeExtractor([{}]))
    await bot_mod.cb_parse(FakeCallback("parse", FakeMessage()), store=store,
                           registry=registry, directory=directory,
                           extractor=FakeExtractor([ZAYAVKA_RECOGNISED]))

    asked = await _answer_until_card(env, {
        "rate": "85 тыс", "order_kind": "есть договор", "cargo_dims": "120x80x150",
        "cargo_volume_m3": "64", "pickup_region": "50", "pickup_postcode": "141400",
        "delivery_region": "77", "delivery_postcode": "117997"})
    routine = {k for k in asked if BY_KEY[k].routine}
    assert not routine, f"рутину спросили по одному: {routine}"
    assert "rate" in asked, "ставку угадывать нельзя — её обязаны спросить"

    build_msg = FakeMessage()
    await bot_mod.cb_build(FakeCallback("build", build_msg), store=store,
                           registry=registry, directory=directory)
    assert build_msg.documents, f"заявка не отдана: {build_msg.sent}"
    name, xml = build_msg.documents[0]
    assert name.startswith("ON_ZAKZVGO_") and name.endswith(".xml"), "документ — XML по формату ФНС"
    assert xml.startswith('<?xml version="1.0" encoding="windows-1251"?>'.encode("cp1251"))
    drafts = [(n, d) for n, d in build_msg.documents[1:] if n.endswith(".docx")]
    assert drafts and drafts[0][1][:2] == b"PK", "Word — черновиком рядом"
    assert store.list_issued("5001000001"), "выписанная заявка сохранена для ЭТрН"
    assert any("<pre>" in t and "Ставка" in t for t in build_msg.sent), "и текстом для мессенджера"
    base = [b for b in build_msg.buttons() if b.startswith("base:")]
    assert base, "после заявки — кнопка «Накладная по этому документу»"

    zayavka_number = store.load(1)["carriage"].get("doc_number")
    move = FakeMessage()
    await bot_mod.cb_base(FakeCallback(base[0], move), store=store, registry=registry,
                          directory=directory)
    session = store.load(1)
    assert session["doc_type"] == ETRN
    c = session["carriage"]
    assert c.get("consignee_inn") == "7707083893" and c.get("cargo_places") == 40
    assert c.get("rate") is None, "ставка из заявки в накладную не идёт"
    assert c.get("doc_number") != zayavka_number, "у накладной свой номер"
    assert any("Перенёс" in t for t in move.sent)



async def test_unsure_answer_asks_to_confirm_instead_of_tick(env):
    """
    «паллеты» → бот подобрал код тары, но это догадка. Галочка «✓» тут вводила
    в заблуждение: вопрос повторялся, человек писал то же самое по кругу.
    """
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    session = store.load(1)
    session["current"] = "cargo_package_code"
    store.save(1, session)

    step = FakeMessage(text="паллеты")
    await bot_mod.on_text(step, store=store, registry=registry, directory=directory)
    assert "Верно?" in step.sent[-1] and "PX" in step.sent[-1]
    assert not any(t.startswith("✓") for t in step.sent)

    yes = FakeMessage(text="да")
    await bot_mod.on_text(yes, store=store, registry=registry, directory=directory)
    assert any(t.startswith("✓") for t in yes.sent)
    assert not store.load(1)["carriage"].values["cargo_package_code"].needs_confirm



async def test_missing_validator_is_named_honestly(env, monkeypatch):
    """
    Нет xmllint — это «проверить нечем», а не «файл не прошёл проверку».
    Файл при этом всё равно не отдаём: непроверенный XML наружу не уходит.
    Найдено прогоном на Hermes, где xmllint не установлен.
    """
    from etrn_bot import xml_build
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    await _start(env, ETRN)
    session = store.load(1)
    r = bot_mod._resolver(directory, registry, store, session)
    session["carriage"] = demo.build_carriage(r)
    store.save(1, session)

    def no_xmllint(*_a, **_kw):
        raise FileNotFoundError("xmllint")
    monkeypatch.setattr(xml_build.subprocess, "run", no_xmllint)

    msg = FakeMessage()
    await bot_mod.cb_build(FakeCallback("build", msg), store=store, registry=registry,
                           directory=directory)
    assert not msg.documents, "непроверенный файл не отдаём"
    text = " ".join(msg.sent)
    assert "проверить его по схеме ФНС сейчас нечем" in text
    assert "не прошёл проверку" not in text



# ── поручение экспедитору и ЭТрН «на основании» ─────────────────────────────

FORWARDER = "7702070139"


async def test_poruchenie_to_xml_then_etrn_later(env):
    """
    Поручение → XML по формату ФНС; через «день» — новая сессия ЭТрН: бот сам
    предлагает выписанное поручение как основание и переносит маршрут и груз.
    """
    await _setup_org(env)
    store, registry, directory = env["store"], env["registry"], env["directory"]
    directory.orgs[FORWARDER] = {"forwarder_name": 'ООО "ЭКСПЕДИТОР"',
                                 "forwarder_edo_id": "2BM-7702070139"}
    menu = FakeMessage()
    await bot_mod.cmd_start(menu, registry=registry)
    assert "start:poruchenie" in menu.buttons(), "поручение — в меню"

    await _start(env, "poruchenie")
    await bot_mod.on_file(FakeMessage(document=FakeFile("zayavka.png", "image/png")), FakeBot(),
                          store=store, registry=registry, directory=directory,
                          extractor=FakeExtractor([{}]))
    recognised = dict(demo.EXTRACTION)
    recognised["forwarder_inn"] = (FORWARDER, 0.99, "договор экспедиции")
    await bot_mod.cb_parse(FakeCallback("parse", FakeMessage()), store=store, registry=registry,
                           directory=directory, extractor=FakeExtractor([recognised]))
    await _answer_until_card(env, {
        "forwarder_contract_number": "ТЭ-17", "forwarder_contract_date": "15.01.2026",
        "cargo_net_kg": "19500 кг", "pickup_region_name": "Московская область",
        "delivery_region_name": "г. Москва"})

    build_msg = FakeMessage()
    await bot_mod.cb_build(FakeCallback("build", build_msg), store=store, registry=registry,
                           directory=directory)
    assert build_msg.documents, f"поручение не отдано: {build_msg.sent}"
    name, xml = build_msg.documents[0]
    assert name.startswith("ON_POREXPKLT_") and xml.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    assert store.list_issued("5001000001")[0]["doc_type"] == "poruchenie"

    # «через день»: сессия кончилась, новая ЭТрН
    store.drop(1)
    start = await _start(env, ETRN)
    base = [b for b in start.buttons() if b.startswith("base:poruchenie:")]
    assert base and "nobase" in start.buttons(), "бот предлагает выбрать основание"
    move = FakeMessage()
    await bot_mod.cb_base(FakeCallback(base[0], move), store=store, registry=registry,
                          directory=directory)
    c = store.load(1)["carriage"]
    assert store.load(1)["doc_type"] == ETRN
    assert c.get("cargo_name") == "Сера гранулированная" and c.get("consignee_inn") == "7707083893"
    assert c.get("forwarder_inn") is None, "экспедитор в ЭТрН не переносится — он не грузоотправитель"
    assert any("Перенёс" in t for t in move.sent)


async def test_etrn_without_issued_documents_asks_for_files(env):
    await _setup_org(env)
    start = await _start(env, ETRN)
    assert "parse" in start.buttons() and not [b for b in start.buttons() if b.startswith("base:")]


async def test_etrn_nobase_goes_to_files(env):
    await _setup_org(env)
    store = env["store"]
    store.save_issued({"id": "zayavka:00001", "org_inn": "5001000001", "doc_type": "zayavka",
                       "number": "00001", "date": "09.09.2026", "counterparty": "",
                       "route": "Химки → Москва", "cargo": "",
                       "carriage": store_mod.dumps(bot_mod.Carriage(doc_type="zayavka"))})
    start = await _start(env, ETRN)
    assert "nobase" in start.buttons()
    msg = FakeMessage()
    await bot_mod.cb_nobase(FakeCallback("nobase", msg), store=store)
    assert "parse" in msg.buttons()
