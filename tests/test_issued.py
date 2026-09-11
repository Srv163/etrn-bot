"""
Выписанные заявки и поручения хранятся: ЭТрН по ним собирают, когда машина
пришла, — через день-два, в другой сессии. Раньше это работало только сразу
после заявки, в той же сессии.
"""

from __future__ import annotations

from datetime import date

from etrn_bot import store as store_mod
from etrn_bot.fields import ZAYAVKA
from etrn_bot.resolve import Carriage, Resolver
from etrn_bot.tools import demo


def _zayavka(number_from: store_mod.MemoryStore) -> Carriage:
    settings = dict(demo.org_settings(), next_number=number_from.next_number)
    r = Resolver(demo.directory(), settings, today=date(2026, 9, 9), doc_type=ZAYAVKA)
    c = Carriage(doc_type=ZAYAVKA)
    r.apply_extraction(c, demo.EXTRACTION)
    r.resolve(c)
    r.answer(c, "rate", "85 тыс")
    return c


def test_issued_document_survives_the_session():
    s = store_mod.MemoryStore()
    c = _zayavka(s)
    s.save_issued(store_mod.issued_record(c, "5001000001", ZAYAVKA))
    s.drop(1)                                   # сессия закончилась
    got = s.list_issued("5001000001")
    assert len(got) == 1
    back = store_mod.loads(got[0]["carriage"])
    assert back.get("consignee_inn") == c.get("consignee_inn")
    assert back.doc_type == ZAYAVKA


def test_list_shows_what_people_recognise():
    """В списке — номер, дата, перевозчик, маршрут, груз: по ним узнают свой рейс."""
    s = store_mod.MemoryStore()
    rec = store_mod.issued_record(_zayavka(s), "5001000001", ZAYAVKA)
    assert rec["number"] and rec["date"]
    assert "ВЕЗЁМ" in rec["counterparty"]
    assert "Химки" in rec["route"] and "Москва" in rec["route"]
    assert rec["cargo"] == "Сера гранулированная"


def test_newest_first_and_no_duplicates():
    s = store_mod.MemoryStore()
    a = _zayavka(s)
    b = _zayavka(s)
    for c in (a, b, a):                         # a пересобрали — не дублируем
        s.save_issued(store_mod.issued_record(c, "5001000001", ZAYAVKA))
    ids = [r["id"] for r in s.list_issued("5001000001")]
    assert ids == [f"zayavka:{a.get('doc_number')}", f"zayavka:{b.get('doc_number')}"]


def test_documents_are_per_organisation():
    s = store_mod.MemoryStore()
    s.save_issued(store_mod.issued_record(_zayavka(s), "5001000001", ZAYAVKA))
    assert s.list_issued("7700000000") == []


def test_get_by_id():
    s = store_mod.MemoryStore()
    c = _zayavka(s)
    rec = store_mod.issued_record(c, "5001000001", ZAYAVKA)
    s.save_issued(rec)
    assert s.get_issued("5001000001", rec["id"])["number"] == rec["number"]
    assert s.get_issued("5001000001", "zayavka:нет") is None
