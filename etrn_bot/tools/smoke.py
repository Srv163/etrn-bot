"""
Быстрый визуальный прогон ядра: парсеры, диалог, «не знаю», карточка.

Не заменяет тесты (`pytest tests/`) — нужен, чтобы глазами увидеть, как бот
разговаривает. Данные — из `demo`, чтобы не расходились с фикстурами.

    PYTHONPATH=. python3 -m etrn_bot.tools.smoke
"""

from __future__ import annotations

from etrn_bot import parse
from etrn_bot.resolve import Resolver
from etrn_bot.tools import demo

print("=== ПАРСЕРЫ ===")
CASES = [
    ("mass", "тонн двадцать"), ("mass", "20,5 т"), ("mass", "18500 кг"), ("mass", "22"),
    ("mass", "40 мест"),
    ("datetime", "завтра к 8"), ("datetime", "12.09 14:00"), ("datetime", "в понедельник утром"),
    ("plate", "а123вс77"), ("plate", "A 123 BC 777"),
    ("inn", "7707083893"), ("inn", "1234567890"),
    ("int", "сорок мест"), ("int", "завтра к 8"),
    ("phone", "8 916 123 45 67"), ("license", "99 12 345678"),
    ("postcode", "141400, Химки, Ленина 1"), ("region", "50"), ("region", "завтра к 8"),
    ("fio", "петров иван сергеевич"), ("fio", "Петров И.С."),
]
for field, text in CASES:
    val, conf, shown = parse.run(field, text)
    mark = " " if conf >= 0.75 else "?"
    print(f" {mark} {field:9} {text!r:30} -> {shown!r:44} conf={conf}")

print("\n=== ДИАЛОГ: тонкий комплект документов ===")
r = Resolver(demo.directory(), demo.org_settings(), today=demo.TODAY)
thin = {k: v for k, v in demo.EXTRACTION.items()
        if k not in ("cargo_package_code", "cargo_marking", "cargo_state",
                     "pickup_arrival_fact", "pickup_departure_fact", "cargo_places")}
c = demo.build_carriage(r, thin, confirm_all=False)
print(f"распознано полей: {len(thin)} → заполнено само: {len(c.values)}, "
      f"отложено как чужие: {len(c.deferred)}")
questions = r.open_questions(c)
print(f"вопросов человеку: {len(questions)}")
for a in questions:
    print(f"   • {a.field.title} — {a.reason}")

print("\n-- отвечаем пачкой: «40 мест, PX, 10.09 07:50» --")
for key, shown in r.answer_batch(c, "40 мест, 10.09 07:50"):
    print(f"   {key} = {shown}")

print("\n-- «не знаю» на разных полях --")
for key in ("vehicle_ownership", "carriage_cost", "cargo_places", "delivery_address",
            "cargo_volume_m3"):
    if key in c.values:
        c.values.pop(key)
    ok, msg = r.dont_know(c, key)
    print(f"   [{key}] ok={ok}: {msg.splitlines()[0]}")

print("\n-- что осталось --")
for a in r.open_questions(c):
    print(f"   • {a.field.title}: {r.question_text(a).splitlines()[0]}")
print("\nблокеры:", [b.field.title for b in r.blockers(c)])

print("\n\n=== ЗАЯВКА ПЕРЕВОЗЧИКУ ===")
from datetime import date, datetime  # noqa: E402

from etrn_bot import zayavka as Z  # noqa: E402
from etrn_bot.fields import ZAYAVKA  # noqa: E402
from etrn_bot.resolve import Carriage  # noqa: E402

rz = Resolver(demo.directory(), demo.org_settings(), today=date(2026, 9, 9), doc_type=ZAYAVKA)
cz = Carriage(doc_type=ZAYAVKA)
rz.apply_extraction(cz, {
    "consignee_inn": ("7707083893", 0.99, "переписка"),
    "carrier_inn": ("7736050003", 0.99, "переписка"),
    "cargo_name": ("Сера гранулированная", 0.93, "переписка"),
    "cargo_mass_gross_kg": (20000, 0.9, "переписка"),
    "pickup_address": ("Химки, Ленина 1, склад 3", 0.9, "переписка"),
    "delivery_address": ("Москва, Вавилова 19", 0.9, "переписка"),
    "pickup_datetime": (datetime(2026, 9, 11, 8, 0), 0.9, "переписка"),
    "cargo_places": (40, 0.9, "переписка"),
})
rz.resolve(cz)
print(f"заполнено само: {len(cz.values)}, вопросов: {len(rz.open_questions(cz))}")
for a in rz.open_questions(cz):
    print(f"   • {a.field.title} — {a.reason}")
print("рутина блоком:", ", ".join(t for t, _ in rz.routine_block(cz)))

print("\n-- ставку угадывать нельзя --")
print("  ", rz.dont_know(cz, "rate")[1].splitlines()[0])
rz.answer(cz, "rate", "85 тыс")
print("\n-- остаток --", [a.field.title for a in rz.open_questions(cz)])
for a in list(rz.open_questions(cz)):
    rz.dont_know(cz, a.field.key)
print("блокеры:", [b.field.title for b in rz.blockers(cz)])

print("\n" + "=" * 46)
print(Z.as_text(cz))
