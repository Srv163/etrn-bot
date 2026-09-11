"""
Движок заполнения накладной.

Правило приоритета: человек > документ > справочник > вычисление > умолчание.
Правило вопроса: спрашиваем только то, что не смогли добыть сами.
Правило «не знаю»: бот подставляет сам ВЕЗДЕ, кроме полей never_guess —
там угадывание запрещено, вместо этого бот объясняет, где значение взять.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from datetime import date, datetime, timedelta

from . import parse
from .fields import BY_KEY, ETRN, REGISTRY, Field, Strategy, registry_for


@dataclass
class Value:
    value: object
    source: str               # откуда взято — показывается человеку
    confidence: float = 1.0

    @property
    def needs_confirm(self) -> bool:
        return self.confidence < 0.75


@dataclass
class Ask:
    field: Field
    reason: str = ""


@dataclass
class Blocker:
    field: Field
    message: str


class Directory:
    """Справочники. В проде — Postgres, здесь — интерфейс + память."""

    def __init__(self) -> None:
        self.orgs: dict[str, dict] = {}       # ИНН → карточка
        self.vehicles: dict[str, dict] = {}   # госномер → карточка
        self.drivers: dict[str, dict] = {}    # ВУ → карточка
        self.routes: dict[str, dict] = {}     # ключ маршрута → адреса
        self.last_by_consignee: dict[str, dict] = {}  # ИНН → прошлая накладная
        self.cargo: dict[str, dict] = {}      # наименование груза → его свойства
        self.addresses: dict[str, dict] = {}  # адрес → индекс, регион

    @staticmethod
    def _norm(value: object) -> str:
        """Ключ адреса или груза без регистра и лишних пробелов: «Химки, Ленина 1»
        и «химки,  ленина 1» — одно и то же место."""
        return " ".join(str(value).lower().replace(",", " ").split())

    def _table(self, key: str) -> dict[str, dict] | None:
        if key in ("shipper_inn", "consignee_inn", "carrier_inn", "forwarder_inn"):
            return self.orgs
        return {"vehicle_plate": self.vehicles, "driver_license": self.drivers,
                "_route": self.routes, "cargo_name": self.cargo,
                "pickup_address": self.addresses,
                "delivery_address": self.addresses}.get(key)

    def _id(self, key: str, value: object) -> str:
        return self._norm(value) if key in ("cargo_name", "pickup_address",
                                            "delivery_address") else str(value)

    def lookup(self, key: str, value: object) -> dict | None:
        table = self._table(key)
        return table.get(self._id(key, value)) if table is not None else None

    def remember(self, key: str, value: object, field_key: str, field_value: object) -> None:
        """
        Запомнить ответ человека в карточке справочника: спросили про договор с
        перевозчиком или габариты паллеты один раз — в следующем рейсе с тем же
        перевозчиком или грузом бот возьмёт это сам. В проде — Postgres (задача 6).
        """
        table = self._table(key)
        if table is None or value in (None, ""):
            return
        table.setdefault(self._id(key, value), {})[field_key] = field_value


@dataclass
class Carriage:
    """Состояние одной накладной."""
    values: dict[str, Value] = dc_field(default_factory=dict)
    deferred: set[str] = dc_field(default_factory=set)
    skipped: set[str] = dc_field(default_factory=set)
    corrections: list[tuple[str, object, object]] = dc_field(default_factory=list)
    answered_count: int = 0
    doc_type: str = "etrn"

    def get(self, key: str):
        v = self.values.get(key)
        return v.value if v else None

    def set(self, key: str, value: object, source: str, confidence: float = 1.0) -> None:
        old = self.values.get(key)
        if old is not None and old.value != value and source == "вы сказали":
            self.corrections.append((key, old.value, value))
        self.values[key] = Value(value, source, confidence)


class Resolver:
    def __init__(self, directory: Directory, org_settings: dict,
                 today: date | None = None, doc_type: str = ETRN) -> None:
        self.dir = directory
        self.org = org_settings
        self.today = today or date.today()
        # Один движок обслуживает и заявку, и накладную: меняется только
        # набор полей. Логика «спросить / достать / не угадывать» общая.
        self.doc_type = doc_type
        self.fields = registry_for(doc_type)

    # ── основной проход ─────────────────────────────────────────────────────

    def apply_extraction(self, c: Carriage, extracted: dict[str, tuple[object, float, str]]) -> None:
        """extracted: key → (значение, уверенность, из какого документа)."""
        for key, (val, conf, src) in extracted.items():
            if key in BY_KEY:
                c.set(key, val, src, conf)

    def resolve(self, c: Carriage) -> None:
        """Прогон до стабилизации: справочники могут открыть новые справочники."""
        for _ in range(4):
            changed = False
            for f in self.fields:
                if f.key in c.values or f.key in c.deferred:
                    continue
                if self._try_fill(c, f):
                    changed = True
            if not changed:
                break

    def _try_fill(self, c: Carriage, f: Field) -> bool:
        for s in f.strategies:
            if s is Strategy.LOOKUP and f.lookup_by:
                key_val = c.get(f.lookup_by)
                if key_val is not None:
                    card = self.dir.lookup(f.lookup_by, key_val)
                    if card and f.key in card:
                        c.set(f.key, card[f.key], "справочник")
                        return True
            elif s is Strategy.DERIVE:
                got = self._derive(c, f)
                if got is not None:
                    # Вычисление бывает точным (номер, дата) и приблизительным
                    # (разбор адреса на части). Второе человек обязан увидеть,
                    # поэтому _derive может вернуть (значение, уверенность).
                    val, conf = got if isinstance(got, tuple) else (got, 1.0)
                    src = ("разобрал из адреса" if f.key in self._ADDRESS_DERIVED
                           else "рассчитал сам")
                    c.set(f.key, val, src, conf)
                    return True
            elif s is Strategy.DEFAULT:
                # never_guess не закрывается умолчанием. Единственное исключение —
                # реквизит самой организации из её настроек (__org_inn__): его
                # ввёл человек при настройке, это не догадка бота.
                if f.never_guess and f.default != "__org_inn__":
                    continue
                val = self._default(f)
                if val is not None:
                    # Политика организации — осознанное решение, подтверждать нечего.
                    # Умолчание из реестра — догадка бота, её человек должен увидеть.
                    # Пустое умолчание ([], "") означает «ничего нет» — это не догадка.
                    from_policy = (f.key in self.org.get("defaults", {})
                                   or f.default == "__org_inn__"
                                   or f.default_by_format
                                   or not val)
                    c.set(f.key, val, "по умолчанию",
                          1.0 if from_policy else 0.7)
                    return True
            elif s is Strategy.DEFER:
                c.deferred.add(f.key)
                return True
        return False

    def _default(self, f: Field):
        if f.default == "__org_inn__":
            return self.org.get("inn")
        if f.key in self.org.get("defaults", {}):
            return self.org["defaults"][f.key]
        return f.default

    # Какое поле-адрес питает какую производную часть.
    _ADDRESS_DERIVED = {
        "pickup_postcode": ("pickup_address", "postcode"),
        "pickup_city": ("pickup_address", "city"),
        "pickup_street": ("pickup_address", "street"),
        "pickup_house": ("pickup_address", "house"),
        "delivery_postcode": ("delivery_address", "postcode"),
        "delivery_city": ("delivery_address", "city"),
        "delivery_street": ("delivery_address", "street"),
        "delivery_house": ("delivery_address", "house"),
    }

    def _derive(self, c: Carriage, f: Field):
        if f.key == "doc_date":
            return self.today.strftime("%d.%m.%Y")
        if f.key == "doc_number":
            # Номер обязан быть уникальным между сессиями, поэтому в бою его
            # выдаёт общее хранилище (Redis INCR). Локальный счётчик остаётся
            # для тестов и одиночного прогона.
            nxt = self.org.get("next_number")
            if callable(nxt):
                return f"{int(nxt(self.org['inn'])):05d}"
            n = self.org.get("counter", 0) + 1
            self.org["counter"] = n
            return f"{n:05d}"
        if f.key == "delivery_datetime":
            p = c.get("pickup_datetime")
            if isinstance(p, datetime):
                return p + timedelta(days=self.org.get("default_transit_days", 1))
        if f.key == "req_capacity_t":
            m = c.get("cargo_mass_gross_kg")
            if isinstance(m, (int, float)):
                # Ближайшая стандартная грузоподъёмность не ниже массы груза.
                for cap in (1.5, 3, 5, 10, 20):
                    if m <= cap * 1000:
                        return cap
                return round(m / 1000 + 0.5)
        if f.key == "cargo_package_code":
            # Код тары выводим из того, как человек описал упаковку словами.
            # Это догадка, поэтому уверенность ниже порога — увидит и подтвердит.
            described = c.get("cargo_package")
            if described:
                code, conf, _shown = parse.run("packcode", str(described))
                if code is not None:
                    return code, min(conf, 0.7)
        # ── поручение экспедитору: признаки, которые следуют из других полей
        if f.key == "cargo_packed":
            pkg = c.get("cargo_package")
            if pkg is not None:
                return "0" if str(pkg).strip().lower() in ("без упаковки", "нет", "навалом",
                                                            "наливом") else "1"
        if f.key == "cargo_volume_known":
            return "1" if c.get("cargo_volume_m3") not in (None, "") else "0"
        if f.key == "cargo_docs_present":
            return "1" if c.get("accompanying_docs") else "0"
        if f.key in ("ship_from", "ship_to"):
            p = c.get("pickup_datetime")
            if isinstance(p, datetime):
                return p.date()
        if f.key == "redirect_phone":
            phone = c.get("shipper_phone")
            if phone:
                return phone
        if f.key in self._ADDRESS_DERIVED:
            src_key, part = self._ADDRESS_DERIVED[f.key]
            raw = c.get(src_key)
            if not raw:
                return None
            if part == "postcode":
                val, conf, _shown = parse.run("postcode", str(raw))
                return (val, conf) if val is not None else None
            parts = parse.address_parts(str(raw))
            if part in parts:
                # Сам адрес — never_guess, его подтвердил человек. Разбор на
                # части ничего нового не утверждает, поэтому отдельный вопрос
                # на каждую часть не задаём: они видны в карточке с источником.
                return parts[part], 1.0
        return None

    # ── вопросы ─────────────────────────────────────────────────────────────

    def open_questions(self, c: Carriage) -> list[Ask]:
        out: list[Ask] = []
        for f in self.fields:
            if f.key in c.deferred or f.key in c.skipped:
                continue
            v = c.values.get(f.key)
            if f.routine and v is not None:
                continue          # рутина живёт в карточке, а не в опроснике
            if v is None:
                if f.required_for(self.doc_type):
                    out.append(Ask(f, "не нашёл в документах"))
            elif v.needs_confirm:
                out.append(Ask(f, f"распознал как «{f.show(v.value)}», но не уверен"))
        return out

    # Источники производных, которые не записаны в derive_from поля явно.
    _DERIVED_FROM = {"pickup_address": ("pickup_postcode", "pickup_city", "pickup_street",
                                        "pickup_house"),
                     "delivery_address": ("delivery_postcode", "delivery_city",
                                          "delivery_street", "delivery_house")}

    def _forget_derived(self, c: Carriage, key: str) -> None:
        """
        Человек поправил источник — производные от него пересчитываются.
        Без этого бот держал код тары NE («без упаковки»), вычисленный из прежнего
        значения, хотя человек уже ответил «паллеты», — и запоминал его для груза.
        Сбрасываются только значения, которые бот вычислил сам: ответ человека
        не трогаем никогда.
        """
        dependents = set(self._DERIVED_FROM.get(key, ()))
        dependents |= {f.key for f in self.fields if key in f.derive_from}
        for dep in dependents:
            v = c.values.get(dep)
            if v is not None and v.source in ("рассчитал сам", "разобрал из адреса"):
                del c.values[dep]

    def _learn(self, c: Carriage, f: Field, value: object) -> None:
        """Подтверждённое человеком — в справочник по ключу поиска поля."""
        if f.lookup_by and not f.never_guess_key_blocked():
            key_value = c.get(f.lookup_by)
            if key_value is not None:
                self.dir.remember(f.lookup_by, key_value, f.key, value)

    def progress(self, c: Carriage) -> tuple[int, int]:
        """(номер текущего вопроса, всего вопросов) — чтобы человек видел конец."""
        left = len(self.open_questions(c))
        asked = c.answered_count
        return asked + 1, asked + left

    def next_question(self, c: Carriage) -> Ask | None:
        qs = self.open_questions(c)
        return qs[0] if qs else None

    def question_text(self, a: Ask, c: Carriage | None = None) -> str:
        f = a.field
        head = f.question or f"{f.title}?"
        parts = [head]
        if f.hint:
            parts.append(f"_{f.hint}_")
        # Если значение уже есть и бот в нём не уверен — человеку нужно знать,
        # что достаточно ответить «да».
        if c is not None:
            v = c.values.get(f.key)
            if v is not None and v.needs_confirm:
                parts.append(f"Сейчас стоит: {f.show(v.value)} — если верно, ответьте «да».")
        # Код тары — единственное поле, где подсказка зависит от уже собранных
        # данных: по описанию упаковки показываем подходящие коды.
        if c is not None and f.key == "cargo_package_code":
            from . import packaging
            described = c.get("cargo_package")
            if described:
                parts.append(packaging.hint(str(described)))
        return "\n".join(parts)

    # ── ответы человека ─────────────────────────────────────────────────────

    # Короткие ответы, которыми человек подтверждает то, что бот уже показал.
    CONFIRMATIONS = {"да", "верно", "ок", "ok", "всё верно", "все верно",
                     "так и есть", "подтверждаю", "+", "угу", "ага"}

    def answer(self, c: Carriage, key: str, text: str) -> tuple[bool, str]:
        f = BY_KEY[key]

        # «да» на переспрос — это подтверждение показанного значения, а не новое
        # значение. Без этого бот спрашивал одно и то же по кругу: он показывал
        # «PX — Поддон», человек отвечал «да», парсер не понимал.
        v = c.values.get(key)
        # «да» на вопрос, где значения ещё нет, — это ответ по существу («есть ли
        # договор? — да»), его разбирает парсер поля, а не подтверждение.
        if text.strip().lower() in self.CONFIRMATIONS and v is not None:
            c.set(key, v.value, "вы подтвердили", 1.0)
            self._learn(c, f, v.value)
            c.answered_count += 1
            self.resolve(c)
            return True, f.show(v.value)

        val, conf, shown = parse.run(f.parser, text, today=self.today)
        if val is None:
            return False, f"Не разобрал «{text}». {f.hint or 'Попробуйте иначе.'}"
        if conf >= 0.75:
            self._learn(c, f, val)
        self._forget_derived(c, key)
        c.set(key, val, "вы сказали", max(conf, 0.9) if conf >= 0.75 else conf)
        c.answered_count += 1
        self.resolve(c)
        return True, shown

    def answer_batch(self, c: Carriage, text: str) -> list[tuple[str, str]]:
        """
        Человек ответил пачкой: «20 тонн, 40 мест, паллеты».
        Раскидываем по открытым вопросам: каждый фрагмент — тому полю,
        чей парсер разобрал его увереннее всех.
        """
        chunks = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
        # Поля без парсера (свободный текст) в пачке не участвуют: они
        # проглатывают любой фрагмент и портят раскладку. Их спрашиваем адресно.
        open_keys = [a.field.key for a in self.open_questions(c)
                     if BY_KEY[a.field.key].parser]
        filled: list[tuple[str, str]] = []
        for chunk in chunks:
            best_key, best_conf, best_shown = None, 0.0, ""
            for key in open_keys:
                if key in [k for k, _ in filled]:
                    continue
                val, conf, shown = parse.run(BY_KEY[key].parser, chunk,
                                               today=self.today)
                if val is not None and conf > best_conf:
                    best_key, best_conf, best_shown = key, conf, shown
            if best_key and best_conf >= 0.6:
                self.answer(c, best_key, chunk)
                filled.append((best_key, best_shown))
        return filled

    def dont_know(self, c: Carriage, key: str) -> tuple[bool, str]:
        """Человек нажал «не знаю». Бот пытается закрыть поле сам."""
        f = BY_KEY[key]

        if f.defer_to:
            c.deferred.add(key)
            return True, f"Это и не ваше поле — его заполнит {f.defer_to}. Пропускаю."

        if f.never_guess:
            return False, (
                f"Тут угадывать нельзя — {f.why or 'это ключевой реквизит накладной'}.\n"
                f"Где взять: {f.hint or 'заявка, договор или документы водителя'}.\n"
                f"Пришлите документ, из которого это видно, — распознаю сам."
            )

        if f.lookup_by:
            kv = c.get(f.lookup_by)
            card = self.dir.lookup(f.lookup_by, kv) if kv is not None else None
            if card and key in card:
                c.set(key, card[key], "справочник")
                return True, f"Нашёл в справочнике: {f.show(card[key])}"

        val = self._default(f)
        if val is not None:
            c.set(key, val, "по умолчанию")
            return True, f"Ставлю по умолчанию: {f.show(val)}. Если не так — скажите."

        if not f.required:
            c.skipped.add(key)
            return True, "Поле необязательное, оставляю пустым."

        return False, f"Без этого накладную не собрать. {f.hint or ''}".strip()

    def like_last_time(self, c: Carriage) -> list[str]:
        """«Как в прошлый раз» — тянем повторяющиеся поля из последней накладной."""
        inn = c.get("consignee_inn")
        prev = self.dir.last_by_consignee.get(str(inn)) if inn else None
        if not prev:
            return []
        reusable = (
            "consignee_address", "delivery_address", "cargo_name", "cargo_package",
            "carrier_inn", "vehicle_ownership", "special_instructions",
        )
        applied = []
        for key in reusable:
            if key in prev and key not in c.values:
                c.set(key, prev[key], "как в прошлый раз")
                applied.append(key)
        self.resolve(c)
        return applied

    # ── итог ────────────────────────────────────────────────────────────────

    def blockers(self, c: Carriage) -> list[Blocker]:
        out = []
        for f in self.fields:
            if not f.required_for(self.doc_type) or f.key in c.deferred or f.key in c.skipped:
                continue
            if f.key not in c.values:
                out.append(Blocker(f, f.hint or "нужно значение"))
        return out

    def ready(self, c: Carriage) -> bool:
        return not self.blockers(c) and not any(
            v.needs_confirm for v in c.values.values()
        )

    def routine_block(self, c: Carriage) -> list[tuple[str, object]]:
        """Стандартные условия — одним блоком, одной строкой на пункт."""
        return [(f.title, c.get(f.key)) for f in self.fields
                if f.routine and c.get(f.key) is not None]

    def card(self, c: Carriage) -> str:
        """
        Карточка на подтверждение. HTML — бот шлёт её с parse_mode="HTML".
        Источник у каждого значения обязателен: человек должен видеть,
        откуда оно взялось, иначе проверка превращается в вычитку вслепую.
        """
        from html import escape as _e
        lines: list[str] = []
        section = None
        for f in self.fields:
            v = c.values.get(f.key)
            # Пустые значения в карточке — шум: человек ищет глазами, что
            # проверить, а видит «Особые условия: —».
            if v is None or v.value in (None, "", [], {}) or f.routine:
                continue
            if f.section != section:
                section = f.section
                lines.append(f"\n<b>{_e(section)}</b>")
            mark = " ⚠️" if v.needs_confirm else ""
            lines.append(
                f"• {_e(f.title)}: {_e(f.show(v.value))} "
                f"<i>({_e(v.source)})</i>{mark}")
        routine = self.routine_block(c)
        if routine:
            lines.append("\n<b>Стандартные условия</b>")
            for title, val in routine:
                lines.append(f"• {_e(title)}: {_e(str(val))}")
        if c.deferred:
            titles = ", ".join(BY_KEY[k].title for k in sorted(c.deferred))
            lines.append(f"\n<i>Заполнят другие стороны: {_e(titles)}</i>")
        return "\n".join(lines).strip()
