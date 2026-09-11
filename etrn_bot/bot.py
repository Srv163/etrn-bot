"""
Телеграм-бот. Тонкий слой поверх resolve.py.

Два документа:
  • Заявка перевозчику — идёт первой, заказывает машину;
  • Транспортная накладная (Титул 1) — после подтверждения перевозчиком.

Сценарий:
    /orgs  → завести организации (один раз)
    /start → что оформляем: заявка или накладная → от какой организации
           → кидай что есть → «Готово, разбирай» → вопросы → карточка → файл.

Принципы диалога:
  • первое сообщение объясняет, что делать, — без чтения инструкций;
  • один вопрос за раз, но видно, сколько осталось;
  • документы можно прислать в любой момент, в том числе вместо ответа;
  • на каждом вопросе есть «Не знаю» и «Зачем?»;
  • любое поле можно поправить до сборки файла;
  • рутина (НДС, оплата, простой) не спрашивается по одному — блоком в карточке;
  • ничего не спрашиваем дважды и ничего не подставляем молча.

Устройство:
  • зависимости приходят в хендлеры через workflow_data диспетчера;
  • состояние диалога живёт в общем хранилище с TTL — перезапуск не теряет рейс;
  • номер документа выдаёт хранилище атомарно;
  • распознавание — в отдельном потоке с таймаутом, сбой — человеческой фразой;
  • сканы держим в памяти только до разбора (правило 6).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import date, datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery,
                           InlineKeyboardButton, InlineKeyboardMarkup, Message)

from . import store as store_mod
from . import poruch_build, vlm, zakaz_build, zayavka
from .extract import Document, Extractor, merge
from .fields import BY_KEY, DOC_TITLES, ETRN, PORUCHENIE, ZAYAVKA
from .orgs import SETUP_FIELDS, Profile, Registry
from .resolve import Ask, Carriage, Directory, Resolver
from .vlm import VLMError
from .xml_build import BuildError, build_title1, filename, validator_missing

log = logging.getLogger(__name__)

# Пачка сканов в PDF распознаётся минутами — лучше подождать, чем оборвать разбор.
EXTRACT_TIMEOUT = int(os.environ.get("ETRN_EXTRACT_TIMEOUT", 600))

dp = Dispatcher()

# Загруженные файлы — только в памяти процесса и только до разбора: в хранилище
# сканы не уезжают (правило 6 — исходники не хранятся ни на диске, ни в логах).
PENDING_DOCS: dict[int, list[Document]] = {}


# ── вспомогательное ─────────────────────────────────────────────────────────

def esc(v: object) -> str:
    return html.escape(str(v))


def _fmt(v: object) -> str:
    if isinstance(v, datetime):
        return v.strftime("%d.%m.%Y %H:%M")
    if isinstance(v, tuple):
        return " ".join(str(p) for p in v)
    return str(v)


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


# Порядок — как работает бухгалтерия: сначала поручение экспедитору или заявка
# перевозчику, ЭТрН — на их основании; бывает и ЭТрН сама по себе.
MENU = _kb([
    [("📋 Поручение экспедитору", f"start:{PORUCHENIE}")],
    [("📄 Заявка перевозчику", f"start:{ZAYAVKA}")],
    [("🚚 Накладная ЭТрН", f"start:{ETRN}")],
])


def _question_kb(f) -> InlineKeyboardMarkup:
    row = [("Не знаю", f"dk:{f.key}")]
    if f.why:
        row.append(("Зачем?", f"why:{f.key}"))
    return _kb([row, [("Как в прошлый раз", "last"), ("Отменить", "cancel")]])


def _resolver(directory: Directory, registry: Registry, store, session: dict) -> Resolver:
    profile = registry.get(session["org_inn"])
    if profile is None:
        raise LookupError(session["org_inn"])
    # Реквизиты организации — в справочник: резолвер тянет их той же стратегией
    # LOOKUP, что и данные контрагентов, отдельной ветки не нужно.
    directory.orgs.setdefault(profile.inn, {}).update(profile.directory_card())
    return Resolver(directory, profile.org_settings(next_number=store.next_number),
                    today=date.today(), doc_type=session.get("doc_type", ETRN))


def _session(store, chat_id: int) -> dict | None:
    s = store.load(chat_id)
    return s if s and "org_inn" in s else None


# ── организации ─────────────────────────────────────────────────────────────

@dp.message(Command("start", "help"))
async def cmd_start(msg: Message, registry: Registry) -> None:
    if not registry.list():
        await msg.answer(
            "Здравствуйте! Помогу оформить документы на перевозку: заявку "
            "перевозчику и транспортную накладную для Saby.\n\n"
            "Сначала заведём вашу организацию — это один раз. Наберите /orgs.")
        return
    await msg.answer(
        "Здравствуйте! Помогу оформить документы на перевозку.\n\n"
        "Работает так: вы кидаете сюда всё, что есть по рейсу — заявку, счёт, "
        "фото прав и техпаспорта, хоть скриншот переписки. Что смогу — прочитаю сам, "
        "чего не хватит — спрошу.\n\n<b>Что делаем?</b>",
        parse_mode="HTML", reply_markup=MENU)


@dp.message(Command("orgs"))
async def cmd_orgs(msg: Message, store, registry: Registry) -> None:
    profiles = registry.list()
    lines = [f"  • {p.title()}" + ("" if p.ready else f" — не хватает: {', '.join(p.missing())}")
             for p in profiles]
    listing = "Организации:\n" + "\n".join(lines) if profiles else "Организаций пока нет."
    store.save(msg.chat.id, {"carriage": Carriage(), "setup": {"step": 0, "draft": {}}})
    _key, title, hint = SETUP_FIELDS[0]
    await msg.answer(
        f"{esc(listing)}\n\nДобавим организацию. Отвечайте по одному сообщению, "
        f"«-» если пропустить.\n\n<b>{esc(title)}?</b>\n<i>{esc(hint)}</i>",
        parse_mode="HTML")


async def _setup_step(msg: Message, store, registry: Registry, session: dict) -> None:
    setup = session["setup"]
    key, _title, _hint = SETUP_FIELDS[setup["step"]]
    answer = msg.text.strip()
    setup["draft"][key] = "" if answer == "-" else answer
    setup["step"] += 1
    if setup["step"] < len(SETUP_FIELDS):
        store.save(msg.chat.id, session)
        _nkey, ntitle, nhint = SETUP_FIELDS[setup["step"]]
        await msg.answer(f"<b>{esc(ntitle)}?</b>\n<i>{esc(nhint)}</i>", parse_mode="HTML")
        return
    draft = setup["draft"]
    store.drop(msg.chat.id)
    if not draft.get("inn"):
        await msg.answer("Без ИНН организацию не заведу. Начнём заново: /orgs")
        return
    profile = Profile(**draft)
    registry.add(profile)
    missing = profile.missing()
    tail = f"\nНе хватает: {', '.join(missing)} — допишу, когда пришлёте." if missing else ""
    await msg.answer(f"Записал: {esc(profile.title())}.{esc(tail)}\n\n<b>Что делаем?</b>",
                     parse_mode="HTML", reply_markup=MENU)


# ── начало документа ────────────────────────────────────────────────────────

@dp.message(Command("new", "cancel"))
async def cmd_new(msg: Message, store, registry: Registry) -> None:
    store.drop(msg.chat.id)
    PENDING_DOCS.pop(msg.chat.id, None)
    if not registry.list():
        await msg.answer("Сначала заведём организацию: /orgs")
        return
    await msg.answer("Что оформляем?", reply_markup=MENU)


@dp.callback_query(F.data.startswith("start:"))
async def cb_start(cb: CallbackQuery, store, registry: Registry) -> None:
    doc_type = cb.data.split(":", 1)[1]
    await cb.answer()
    profiles = registry.list()
    if not profiles:
        await cb.message.answer("Сначала заведём организацию: /orgs")
        return
    if len(profiles) == 1:
        await _begin(cb.message, store, profiles[0].inn, doc_type)
        return
    await cb.message.answer(
        "От какой организации?",
        reply_markup=_kb([[(p.title(), f"org:{doc_type}:{p.inn}")] for p in profiles]))


@dp.callback_query(F.data == "nobase")
async def cb_nobase(cb: CallbackQuery, store) -> None:
    await cb.answer()
    await _docs_prompt(cb.message, ETRN)


def _etrn_from(record: dict) -> tuple[Carriage, int]:
    """ЭТрН на данных выписанного документа. Номер, дата и ставка у неё свои."""
    old = store_mod.loads(record["carriage"])
    new = Carriage(doc_type=ETRN)
    for k, v in old.values.items():
        if ETRN in BY_KEY[k].docs and k not in ("doc_number", "doc_date"):
            new.values[k] = v
    return new, len(new.values)


@dp.callback_query(F.data.startswith("base:"))
async def cb_base(cb: CallbackQuery, store, registry: Registry,
                  directory: Directory) -> None:
    """ЭТрН «на основании» — в любой момент, хоть через два дня после заявки."""
    chat_id = cb.message.chat.id
    session = _session(store, chat_id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    record = store.get_issued(session["org_inn"], cb.data.split(":", 1)[1])
    if record is None:
        await cb.answer("Этот документ уже не найден — выберите другой", show_alert=True)
        return
    new, moved = _etrn_from(record)
    session.update({"doc_type": ETRN, "carriage": new, "current": None, "editing": False,
                    "docs": 0, "based_on": record["id"]})
    r = _resolver(directory, registry, store, session)
    r.resolve(new)
    session["carriage"] = new
    store.save(chat_id, session)
    left = len(r.open_questions(new))
    await cb.answer()
    await cb.message.answer(
        f"Перенёс {moved} реквизит(ов) из документа «{esc(_issued_label(record))}». "
        f"{'Осталось уточнить ' + str(left) + '.' if left else 'Всё на месте.'}\n\n"
        "Пришлите фото прав и СТС — водителя и машину прочитаю сам.",
        parse_mode="HTML",
        reply_markup=_kb([[("Готово, разбирай", "parse")], [("Ничего нет", "parse")]]))


@dp.callback_query(F.data.startswith("org:"))
async def cb_org(cb: CallbackQuery, store) -> None:
    _, doc_type, inn = cb.data.split(":", 2)
    await cb.answer()
    await _begin(cb.message, store, inn, doc_type)


def _issued_label(rec: dict) -> str:
    kind = {ZAYAVKA: "Заявка", PORUCHENIE: "Поручение"}.get(rec["doc_type"], rec["doc_type"])
    route = rec.get("route", "")
    return f"{kind} №{rec['number']} от {rec['date']} · {route}"[:60]


async def _begin(msg: Message, store, org_inn: str, doc_type: str) -> None:
    PENDING_DOCS.pop(msg.chat.id, None)
    store.save(msg.chat.id, {"carriage": Carriage(doc_type=doc_type), "org_inn": org_inn,
                             "doc_type": doc_type, "current": None, "editing": False,
                             "docs": 0})
    # ЭТрН обычно строится на основании заявки или поручения — предлагаем выбрать.
    issued = store.list_issued(org_inn, 5) if doc_type == ETRN else []
    if issued:
        await msg.answer(
            "<b>Накладная ЭТрН</b>\n\nНа основании какого документа? Маршрут, груз и "
            "стороны перенесу — останется водитель и машина.",
            parse_mode="HTML",
            reply_markup=_kb([[(_issued_label(r), f"base:{r['id']}")] for r in issued]
                             + [[("Без основания — соберу с нуля", "nobase")]]))
        return
    await _docs_prompt(msg, doc_type)


async def _docs_prompt(msg: Message, doc_type: str) -> None:
    await msg.answer(
        f"<b>{DOC_TITLES[doc_type]}</b>\n\n"
        "Присылайте документы — по одному или пачкой, в любом порядке.\n"
        "Когда закончите, нажмите кнопку ниже.\n\n"
        "<i>Если документов нет — нажмите «Ничего нет», спрошу всё сам.</i>",
        parse_mode="HTML",
        reply_markup=_kb([[("Готово, разбирай", "parse")], [("Ничего нет", "parse")]]))


# ── приём файлов: работает на любом этапе ───────────────────────────────────

@dp.message(F.document | F.photo)
async def on_file(msg: Message, bot: Bot, store, registry: Registry,
                  directory: Directory, extractor: Extractor) -> None:
    session = _session(store, msg.chat.id)
    if not session:
        await msg.answer("Сначала выберите, что оформляем:", reply_markup=MENU)
        return
    file_id = msg.document.file_id if msg.document else msg.photo[-1].file_id
    name = msg.document.file_name if msg.document else f"photo_{file_id[:8]}.jpg"
    mime = msg.document.mime_type if msg.document else "image/jpeg"
    buf = await bot.download(file_id)
    doc = Document(name, mime, buf.read())

    # Файл пришёл посреди вопросов — значит это ответ на вопрос. Разбираем сразу.
    if session.get("current"):
        await _digest(msg, [doc], store=store, registry=registry,
                      directory=directory, extractor=extractor)
        return

    docs = PENDING_DOCS.setdefault(msg.chat.id, [])
    docs.append(doc)
    session["docs"] = len(docs)
    store.save(msg.chat.id, session)
    await msg.answer(f"Принял: {esc(name)} — всего {len(docs)}", parse_mode="HTML",
                     reply_markup=_kb([[("Готово, разбирай", "parse")]]))


@dp.message(F.text.lower().in_({"готово", "все", "всё", "done"}))
async def on_done_text(msg: Message, store, registry: Registry, directory: Directory,
                       extractor: Extractor) -> None:
    """«готово» текстом — то же, что кнопка: люди пишут, а не жмут."""
    if not _session(store, msg.chat.id):
        await msg.answer("Сначала выберите, что оформляем:", reply_markup=MENU)
        return
    docs = PENDING_DOCS.pop(msg.chat.id, [])
    await _digest(msg, docs, store=store, registry=registry, directory=directory,
                  extractor=extractor)


@dp.callback_query(F.data == "parse")
async def cb_parse(cb: CallbackQuery, store, registry: Registry, directory: Directory,
                   extractor: Extractor) -> None:
    await cb.answer()
    if not _session(store, cb.message.chat.id):
        await cb.message.answer("Сначала выберите, что оформляем:", reply_markup=MENU)
        return
    docs = PENDING_DOCS.pop(cb.message.chat.id, [])
    await _digest(cb.message, docs, store=store, registry=registry, directory=directory,
                  extractor=extractor)


async def _digest(msg: Message, docs: list[Document], *, store, registry: Registry,
                  directory: Directory, extractor: Extractor) -> None:
    """Разобрать документы, показать по-человечески, что понял, и идти дальше."""
    chat_id = msg.chat.id
    session = _session(store, chat_id)
    if session is None:
        await msg.answer("Сессия истекла. Начнём заново: /new")
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)

    if docs:
        note = await msg.answer(
            f"Читаю {len(docs)} документ{'' if len(docs) == 1 else 'а'}…")
        try:
            parts = await asyncio.wait_for(
                asyncio.to_thread(lambda: [extractor.extract([d]) for d in docs]),
                timeout=EXTRACT_TIMEOUT)
        except asyncio.TimeoutError:
            await note.edit_text(f"Распознавание не ответило за {EXTRACT_TIMEOUT} с. "
                                 "Спрошу всё сам — данные не потеряются.")
        except (VLMError, RuntimeError) as e:
            log.warning("распознавание: %s", e)
            await note.edit_text("Не смог прочитать документы — спрошу всё сам.\n"
                                 f"Причина: {e}")
        except Exception:                      # noqa: BLE001 — бухгалтер не читает трейсбеки
            log.exception("сбой при извлечении")
            await note.edit_text("Что-то сломалось на моей стороне. "
                                 "Спрошу всё сам — данные не потеряются.")
        else:
            before = set(c.values)
            r.apply_extraction(c, merge(*parts))
            r.resolve(c)
            new = [k for k in c.values if k not in before]
            text = _recognised(c, new) or "Из этих документов ничего не вытянул."
            try:
                await note.edit_text(text, parse_mode="HTML")
            except Exception:                  # noqa: BLE001
                await msg.answer(text, parse_mode="HTML")
        finally:
            docs.clear()                       # исходники с ПД дальше не живут

    r.resolve(c)
    session["carriage"] = c
    session["docs"] = 0
    store.save(chat_id, session)
    if session.get("editing"):
        session["editing"] = False
        store.save(chat_id, session)
        await _show_card(msg, chat_id, store=store, registry=registry, directory=directory)
        return
    await _ask_next(msg, chat_id, store=store, registry=registry, directory=directory)


def _recognised(c: Carriage, keys: list[str]) -> str:
    """Человеческая сводка: что именно понял, а не «заполнено 31 поле»."""
    show = [k for k in ("consignee_name", "carrier_name", "cargo_name",
                        "cargo_mass_gross_kg", "pickup_address", "delivery_address",
                        "driver_fio", "vehicle_plate", "rate") if k in keys]
    if not show:
        return ""
    lines = ["<b>Понял из документов:</b>"]
    for k in show:
        v = c.values[k]
        mark = " ⚠️" if v.needs_confirm else ""
        lines.append(f"• {esc(BY_KEY[k].title)}: {esc(BY_KEY[k].show(v.value))}{mark}")
    extra = len(keys) - len(show)
    if extra > 0:
        lines.append(f"<i>…и ещё {extra} реквизит(ов)</i>")
    return "\n".join(lines)


# ── вопросы ─────────────────────────────────────────────────────────────────

async def _ask_next(msg: Message, chat_id: int, *, store, registry: Registry,
                    directory: Directory) -> None:
    session = _session(store, chat_id)
    if session is None:
        await msg.answer("Сессия истекла. Начнём заново: /new")
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    ask: Ask | None = r.next_question(c)

    if ask is None:
        session["current"] = None
        store.save(chat_id, session)
        await _show_card(msg, chat_id, store=store, registry=registry, directory=directory)
        return

    session["current"] = ask.field.key
    store.save(chat_id, session)
    f = ask.field
    n, total = r.progress(c)
    head = f"<i>Вопрос {n} из {total}</i>"
    if "не уверен" in ask.reason:
        body = f"{esc(ask.reason).capitalize()}.\n<b>{esc(f.question or f.title)}</b>"
    else:
        body = f"<b>{esc(f.question or f.title + '?')}</b>"
    # Подсказки из движка: где взять, что сейчас стоит, коды тары по описанию.
    extra = r.question_text(ask, c).splitlines()[1:]
    tail = "".join(f"\n<i>{esc(line.strip('_'))}</i>" for line in extra if line.strip())
    await msg.answer(f"{head}\n\n{body}{tail}", parse_mode="HTML",
                     reply_markup=_question_kb(f))


@dp.message(F.text)
async def on_text(msg: Message, store, registry: Registry, directory: Directory) -> None:
    session = store.load(msg.chat.id)
    if session and "setup" in session:
        await _setup_step(msg, store, registry, session)
        return
    session = _session(store, msg.chat.id)
    if not session:
        await msg.answer("Сначала выберите, что оформляем:", reply_markup=MENU)
        return

    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    text = msg.text

    # Человек мог ответить сразу на несколько вопросов одной строкой:
    # «20 тонн, 40 мест, завтра к 8».
    listed = "," in text or ";" in text
    if listed or len(text.split()) > 3:
        filled = r.answer_batch(c, text)
        if filled and (listed or len(filled) > 1):
            session["carriage"] = c
            store.save(msg.chat.id, session)
            shown = "\n".join(f"• {esc(BY_KEY[k].title)}: {esc(v)}" for k, v in filled)
            await msg.answer(f"Записал:\n{shown}", parse_mode="HTML")
            await _ask_next(msg, msg.chat.id, store=store, registry=registry,
                            directory=directory)
            return

    key = session.get("current")
    if not key:
        await msg.answer("Не понял, к чему это. Нажмите /new, чтобы начать заново.")
        return

    ok, shown = r.answer(c, key, text)
    session["carriage"] = c
    store.save(msg.chat.id, session)
    f = BY_KEY[key]
    if not ok:
        await msg.answer(esc(shown) if shown else
                         f"Не разобрал «{esc(text)}». Напишите иначе или пришлите документ.",
                         parse_mode="HTML", reply_markup=_question_kb(f))
        return

    stored = c.values.get(key)
    if stored is not None and stored.needs_confirm:
        # Ответ разобран, но это догадка бота (например код тары по слову
        # «паллеты»). Галочка здесь врёт: она выглядит как «принято», а вопрос
        # тут же повторяется — человек пишет то же самое снова, и так по кругу.
        await msg.answer(f"Понял как «{esc(f.show(stored.value))}». Верно? "
                         "Ответьте «да» или напишите иначе.",
                         parse_mode="HTML", reply_markup=_question_kb(f))
        return
    await msg.answer(f"✓ {esc(f.title)}: {esc(shown)}", parse_mode="HTML")
    if session.get("editing"):
        session["editing"] = False
        store.save(msg.chat.id, session)
        await _show_card(msg, msg.chat.id, store=store, registry=registry, directory=directory)
        return
    await _ask_next(msg, msg.chat.id, store=store, registry=registry, directory=directory)


# ── кнопки вопроса ──────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("dk:"))
async def cb_dont_know(cb: CallbackQuery, store, registry: Registry,
                       directory: Directory) -> None:
    session = _session(store, cb.message.chat.id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    ok, text = r.dont_know(c, key)
    session["carriage"] = c
    store.save(cb.message.chat.id, session)
    await cb.answer()
    if ok:
        await cb.message.answer(f"✓ {esc(text)}", parse_mode="HTML")
        await _ask_next(cb.message, cb.message.chat.id, store=store, registry=registry,
                        directory=directory)
    else:
        await cb.message.answer(esc(text), parse_mode="HTML",
                                reply_markup=_question_kb(BY_KEY[key]))


@dp.callback_query(F.data.startswith("why:"))
async def cb_why(cb: CallbackQuery) -> None:
    f = BY_KEY[cb.data.split(":", 1)[1]]
    await cb.answer(f.why or f.title, show_alert=True)


@dp.callback_query(F.data == "last")
async def cb_last(cb: CallbackQuery, store, registry: Registry,
                  directory: Directory) -> None:
    session = _session(store, cb.message.chat.id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    applied = r.like_last_time(c)
    if not applied:
        await cb.answer("По этому контрагенту прошлых рейсов нет", show_alert=True)
        return
    session["carriage"] = c
    store.save(cb.message.chat.id, session)
    await cb.answer()
    await cb.message.answer(
        "Подтянул из прошлого рейса:\n" + "\n".join(
            f"• {esc(BY_KEY[k].title)}: {esc(_fmt(c.get(k)))}" for k in applied),
        parse_mode="HTML")
    await _ask_next(cb.message, cb.message.chat.id, store=store, registry=registry,
                    directory=directory)


@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, store) -> None:
    store.drop(cb.message.chat.id)
    PENDING_DOCS.pop(cb.message.chat.id, None)
    await cb.answer()
    await cb.message.answer("Отменил. Что оформляем?", reply_markup=MENU)


# ── карточка и правка ───────────────────────────────────────────────────────

async def _show_card(msg: Message, chat_id: int, *, store, registry: Registry,
                     directory: Directory, full: bool = False) -> None:
    session = _session(store, chat_id)
    if session is None:
        await msg.answer("Сессия истекла. Начнём заново: /new")
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    doc_type = session.get("doc_type", ETRN)

    text = r.card(c) if full else _short_card(c, doc_type)
    routine = r.routine_block(c)
    if not full and routine:
        text += ("\n\n<i>Плюс стандартные условия ("
                 + esc(", ".join(t.lower() for t, _ in routine[:3]))
                 + " и др.) — по вашей политике. Поправить можно кнопкой ниже.</i>")
    label = {ZAYAVKA: "собрать заявку", PORUCHENIE: "собрать поручение"}.get(doc_type, "собрать XML")
    rows = [[(f"✅ Всё верно — {label}", "build")], [("✏️ Поправить", "fix")]]
    if not full:
        rows.append([("Показать все реквизиты", "cardfull")])
    await msg.answer(
        f"<b>{DOC_TITLES[doc_type]}</b>\nПроверьте — и подтвердите:\n\n{text}",
        parse_mode="HTML", reply_markup=_kb(rows))


def _short_card(c: Carriage, doc_type: str) -> str:
    keys = {ZAYAVKA: ["carrier_name", "pickup_address", "pickup_datetime", "delivery_address",
                      "cargo_name", "cargo_mass_gross_kg", "req_vehicle_type", "rate"],
            PORUCHENIE: ["forwarder_name", "forwarder_contract_number", "pickup_address",
                         "pickup_datetime", "delivery_address", "cargo_name",
                         "cargo_mass_gross_kg", "cargo_net_kg"],
            }.get(doc_type, ["consignee_name", "carrier_name", "pickup_address", "pickup_datetime",
                             "delivery_address", "cargo_name", "cargo_mass_gross_kg",
                             "cargo_places", "driver_fio", "vehicle_plate"])
    lines = []
    for k in keys:
        v = c.values.get(k)
        if not v:
            continue
        mark = " ⚠️" if v.needs_confirm else ""
        lines.append(f"• {esc(BY_KEY[k].title)}: <b>{esc(BY_KEY[k].show(v.value))}</b>{mark}")
    return "\n".join(lines)


@dp.callback_query(F.data == "cardfull")
async def cb_cardfull(cb: CallbackQuery, store, registry: Registry,
                      directory: Directory) -> None:
    await cb.answer()
    await _show_card(cb.message, cb.message.chat.id, store=store, registry=registry,
                     directory=directory, full=True)


@dp.callback_query(F.data == "fix")
async def cb_fix(cb: CallbackQuery, store, registry: Registry, directory: Directory) -> None:
    session = _session(store, cb.message.chat.id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)
    keys = [f.key for f in r.fields if f.key in c.values][:40]
    rows = [[(BY_KEY[k].title, f"edit:{k}")] for k in keys]
    rows.append([("← Назад", "back")])
    await cb.answer()
    await cb.message.answer("Что поправить?", reply_markup=_kb(rows))


@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(cb: CallbackQuery, store) -> None:
    session = _session(store, cb.message.chat.id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    session["current"], session["editing"] = key, True
    store.save(cb.message.chat.id, session)
    f = BY_KEY[key]
    await cb.answer()
    await cb.message.answer(
        f"Сейчас: <b>{esc(f.show(session['carriage'].get(key)))}</b>\n\n"
        f"{esc(f.question or f.title + '?')}",
        parse_mode="HTML", reply_markup=_question_kb(f))


@dp.callback_query(F.data == "back")
async def cb_back(cb: CallbackQuery, store, registry: Registry, directory: Directory) -> None:
    await cb.answer()
    await _show_card(cb.message, cb.message.chat.id, store=store, registry=registry,
                     directory=directory)


# ── сборка файла ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "build")
async def cb_build(cb: CallbackQuery, store, registry: Registry,
                   directory: Directory) -> None:
    chat_id = cb.message.chat.id
    session = _session(store, chat_id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    c: Carriage = session["carriage"]
    r = _resolver(directory, registry, store, session)

    blockers = r.blockers(c)
    if blockers:
        first = blockers[0].field
        await cb.answer(f"Ещё не хватает: {first.title}", show_alert=True)
        await cb.message.answer(
            f"Без «{esc(first.title)}» документ не собрать.\n"
            + esc(first.hint or "Пришлите документ, из которого это видно, — распознаю сам."),
            parse_mode="HTML")
        await _ask_next(cb.message, chat_id, store=store, registry=registry,
                        directory=directory)
        return
    await cb.answer()

    profile = registry.get(session["org_inn"])
    doc_type = session.get("doc_type")
    if doc_type in (ZAYAVKA, PORUCHENIE):
        built = await (_build_zayavka(cb.message, c, profile) if doc_type == ZAYAVKA
                       else _build_poruchenie(cb.message, c, profile))
        if built:
            _remember(directory, c)
            record = store_mod.issued_record(c, session["org_inn"], doc_type)
            store.save_issued(record)
            who = "перевозчик" if doc_type == ZAYAVKA else "экспедитор"
            await cb.message.answer(
                f"Когда {who} подтвердит и назовёт водителя с машиной — сделаю накладную "
                "на этих же данных. Можно и позже: при старте ЭТрН этот документ будет в списке.",
                reply_markup=_kb([[("🚚 Накладная по этому документу", f"base:{record['id']}")]]))
        return

    try:
        name = filename(shipper_edo_id=profile.edo_id if profile else "",
                        carrier_edo_id=str(c.get("carrier_edo_id") or ""),
                        consignee_edo_id=str(c.get("consignee_edo_id") or ""))
        xml, errors = build_title1(c, file_id=name[:-4])
    except BuildError as e:
        await cb.message.answer(f"Собрать пока нельзя: {esc(e)}", parse_mode="HTML")
        return
    if errors:
        # Непроверенный файл наружу не уходит в обоих случаях, но причина разная:
        # «файл плохой» — наш баг в данных или шаблоне, «валидатора нет» — сервер
        # не донастроен. Называть второе первым — сообщать неправду.
        if validator_missing(errors):
            log.error("нет xmllint — XML нечем проверить")
            await cb.message.answer(
                "Файл собран, но проверить его по схеме ФНС сейчас нечем — "
                "на сервере не установлен валидатор. Непроверенный файл не отдаю.\n"
                "Накладную пока выпишите в Saby вручную, я сообщу разработчику.")
            return
        log.error("XML не прошёл XSD: %s", errors)
        await cb.message.answer(
            "Файл не прошёл проверку по схеме ФНС — это моя ошибка, не ваша.\n"
            "Накладную пока выпишите в Saby вручную, я сообщу разработчику.")
        return
    await cb.message.answer_document(
        BufferedInputFile(xml, filename=name),
        caption="Титул 1 готов. Загрузите в Saby и подпишите своей подписью.")
    _remember(directory, c)
    store.drop(chat_id)


async def _build_zayavka(msg: Message, c: Carriage, profile: Profile | None) -> bool:
    """
    Документ — XML по формату ФНС: с 01.09.2026 заявка по Уставу автотранспорта
    бывает только электронной. Word и текст — черновик для глаз и мессенджера.
    Возвращает, отдан ли XML: без него заявки как документа нет.
    """
    try:
        name = zakaz_build.filename(shipper_edo_id=profile.edo_id if profile else "",
                                    carrier_edo_id=str(c.get("carrier_edo_id") or ""))
        xml, errors = await asyncio.to_thread(zakaz_build.build, c, file_id=name[:-4])
    except BuildError as e:
        await msg.answer(f"Собрать заявку пока нельзя: {esc(e)}", parse_mode="HTML")
        return False
    if errors:
        if validator_missing(errors):
            log.error("нет xmllint — XML заявки нечем проверить")
            await msg.answer("Заявка собрана, но проверить её по схеме ФНС сейчас нечем — "
                             "на сервере не установлен валидатор. Непроверенный файл не отдаю.")
        else:
            log.error("XML заявки не прошёл XSD: %s", errors)
            await msg.answer("Файл заявки не прошёл проверку по схеме ФНС — это моя ошибка, "
                             "не ваша. Я сообщу разработчику.")
        return False
    await msg.answer_document(
        BufferedInputFile(xml, filename=name),
        caption="Заявка — электронный документ. Загрузите в Saby, подпишите и отправьте "
                "перевозчику.")

    # Черновики: Word — распечатать и сверить глазами, текст — кинуть в мессенджер.
    terms = profile.terms() if profile else None
    try:
        data = await asyncio.to_thread(zayavka.as_docx, c, terms)
    except Exception:                          # noqa: BLE001 — черновик не критичен
        log.exception("черновик Word не собрался")
    else:
        await msg.answer_document(
            BufferedInputFile(data, filename=f"zayavka_{c.get('doc_number')}_черновик.docx"),
            caption="Черновик в Word — для проверки глазами. Документом он не является.")
    await msg.answer("Текстом — если удобнее кинуть в мессенджер:\n\n"
                     f"<pre>{esc(zayavka.as_text(c))}</pre>", parse_mode="HTML")
    return True
    await msg.answer(
        "Когда перевозчик подтвердит и назовёт водителя с машиной — "
        "сделаю накладную на этих же данных.",
        reply_markup=_kb([[("🚚 Накладная по этой заявке", "toetrn")]]))


async def _build_poruchenie(msg: Message, c: Carriage, profile: Profile | None) -> bool:
    """
    Поручение экспедитору — только XML по формату ФНС: с 01.09.2026 экспедиторские
    документы электронные. Возвращает, отдан ли файл.
    """
    try:
        name = poruch_build.filename(shipper_edo_id=profile.edo_id if profile else "",
                                     forwarder_edo_id=str(c.get("forwarder_edo_id") or ""))
        xml, errors = await asyncio.to_thread(poruch_build.build, c, file_id=name[:-4])
    except BuildError as e:
        await msg.answer(f"Собрать поручение пока нельзя: {esc(e)}", parse_mode="HTML")
        return False
    if errors:
        if validator_missing(errors):
            await msg.answer("Поручение собрано, но проверить его по схеме ФНС сейчас нечем — "
                             "на сервере не установлен валидатор. Непроверенный файл не отдаю.")
        else:
            log.error("XML поручения не прошёл XSD: %s", errors)
            await msg.answer("Файл поручения не прошёл проверку по схеме ФНС — это моя "
                             "ошибка, не ваша. Я сообщу разработчику.")
        return False
    await msg.answer_document(
        BufferedInputFile(xml, filename=name),
        caption="Поручение экспедитору — электронный документ. Загрузите в Saby, подпишите "
                "и отправьте экспедитору.")
    return True


@dp.callback_query(F.data == "toetrn")
async def cb_to_etrn(cb: CallbackQuery, store, registry: Registry,
                     directory: Directory) -> None:
    """Накладная на данных заявки: маршрут и груз не перезаполняем."""
    chat_id = cb.message.chat.id
    session = _session(store, chat_id)
    if not session:
        await cb.answer("Сессия истекла, начните /new", show_alert=True)
        return
    old: Carriage = session["carriage"]
    new = Carriage(doc_type=ETRN)
    for k, v in old.values.items():
        # Номер и дата у накладной свои — заявка их не передаёт.
        if ETRN in BY_KEY[k].docs and k not in ("doc_number", "doc_date"):
            new.values[k] = v
    session.update({"doc_type": ETRN, "carriage": new, "current": None,
                    "editing": False, "docs": 0})
    r = _resolver(directory, registry, store, session)
    r.resolve(new)
    session["carriage"] = new
    store.save(chat_id, session)
    left = len(r.open_questions(new))
    await cb.answer()
    moved = len([k for k in old.values if ETRN in BY_KEY[k].docs
                 and k not in ("doc_number", "doc_date")])
    await cb.message.answer(
        f"Перенёс {moved} реквизит(ов) из заявки. "
        f"{'Осталось уточнить ' + str(left) + '.' if left else 'Всё на месте.'}\n\n"
        "Пришлите фото прав и техпаспорта — водителя и машину прочитаю сам.")
    await _ask_next(cb.message, chat_id, store=store, registry=registry, directory=directory)


def _remember(directory: Directory, c: Carriage) -> None:
    # «Как в прошлый раз». В проде — Postgres (задача 6), сейчас память процесса.
    inn = c.get("consignee_inn") or c.get("carrier_inn")
    if inn:
        directory.last_by_consignee[str(inn)] = {k: v.value for k, v in c.values.items()}


# ── запуск ──────────────────────────────────────────────────────────────────

def build_dispatcher(*, store=None, registry: Registry | None = None,
                     directory: Directory | None = None,
                     extractor: Extractor | None = None) -> Dispatcher:
    """Зависимости — в workflow_data: так они доезжают до хендлеров аргументами."""
    dp["store"] = store or store_mod.from_env()
    dp["registry"] = registry or Registry()
    dp["directory"] = directory or Directory()
    dp["extractor"] = extractor or Extractor(vlm.from_env(), vlm.model_from_env(),
                                             mode=vlm.mode_from_env())
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("нет BOT_TOKEN: токен бота выдаёт @BotFather, "
                         "кладём его в .env, а не в код")
    build_dispatcher()
    await dp.start_polling(Bot(token))


if __name__ == "__main__":
    asyncio.run(main())
