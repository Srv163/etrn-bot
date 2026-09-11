"""
Парсеры свободной формы.

Принцип: бухгалтер пишет как ей удобно — «тонн двадцать», «завтра к 8»,
«а123вс77». Бот приводит к каноническому виду и ПОКАЗЫВАЕТ, что понял.
Если не уверен — переспрашивает, а не догадывается молча.

Каждый парсер возвращает (значение, уверенность 0..1, что_понял_текстом).
Уверенность < 0.75 → бот обязан переспросить подтверждением.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

Parsed = tuple[object | None, float, str]

FAIL: Parsed = (None, 0.0, "")

# ── числительные ─────────────────────────────────────────────────────────────

_ONES = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19,
}
_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
}


def _words_to_number(text: str) -> float | None:
    """«двадцать пять» → 25. Достаточно для 0..999, дальше пишут цифрами."""
    total, found = 0, False
    for w in re.findall(r"[а-яё]+", text.lower()):
        if w in _TENS:
            total += _TENS[w]
            found = True
        elif w in _ONES:
            total += _ONES[w]
            found = True
    return float(total) if found else None


def _any_number(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)", text.replace(" ", ""))
    if m:
        return float(m.group(1).replace(",", "."))
    return _words_to_number(text)


# ── масса ────────────────────────────────────────────────────────────────────

# Слова, по которым видно, что число — это НЕ количество мест: масса, объём,
# деньги, время. Без этой проверки «завтра к 8» и «20 тонн» приезжали
# количеством мест — парсер целого хватал любое число с уверенностью 0.95.
_NOT_A_COUNT = re.compile(
    r"(кг|килограмм|тонн|\bт\b|м3|м³|куб|литр|₽|руб|\bр\b|"
    r"час|\bч\b|мин|утр|вечер|завтра|сегодня|послезавтра|понедельник|вторник|"
    r"сред|четверг|пятниц|суббот|воскресень|[:.]\d)", re.I)
_COUNT_WORDS = re.compile(r"(мест|шт|штук|паллет|поддон|коробк|бочк|мешк|рулон|ящик)", re.I)


def mass(text: str) -> Parsed:
    """→ масса в килограммах (int)."""
    t = text.lower().strip()
    n = _any_number(t)
    if n is None:
        return FAIL

    # «40 мест» — это количество, а не масса. Без такой проверки фрагмент ответа
    # уезжал в массу брутто, когда количество мест уже было заполнено.
    if _COUNT_WORDS.search(t) and not re.search(r"\bт\b|тонн|тн\b|\bкг\b|килог", t):
        return FAIL

    if re.search(r"\bт\b|тонн|тн\b|\bt\b", t):
        kg, unit = n * 1000, "т"
    elif re.search(r"\bкг\b|килог", t):
        kg, unit = n, "кг"
    else:
        # Единица не названа. Догадка по величине: до 200 — почти наверняка тонны.
        if n <= 200:
            kg, unit = n * 1000, "т (единица не указана)"
            return int(kg), 0.6, f"{n:g} {unit} = {int(kg)} кг"
        kg, unit = n, "кг (единица не указана)"
        return int(kg), 0.6, f"{int(kg)} {unit}"

    conf = 0.95 if re.fullmatch(r"[\d\s.,]+[а-яё]*", t) else 0.85
    return int(kg), conf, f"{n:g} {unit} = {int(kg)} кг"


def volume(text: str) -> Parsed:
    n = _any_number(text)
    if n is None:
        return FAIL
    return round(n, 3), 0.9, f"{n:g} м³"


def money(text: str) -> Parsed:
    n = _any_number(text)
    if n is None:
        return FAIL
    if re.search(r"млн|миллион", text.lower()):
        n *= 1_000_000
    elif re.search(r"\bтыс|\bк\b", text.lower()):
        n *= 1000
    return round(n, 2), 0.85, f"{n:,.2f} ₽".replace(",", " ")


def integer(text: str) -> Parsed:
    """
    Количество мест. Число само по себе («40») или с единицей счёта («40 мест»).
    Если рядом стоит масса, объём, деньги или время — это не количество мест,
    и лучше отказаться, чем увести чужой ответ в это поле.
    """
    n = _any_number(text)
    if n is None or n != int(n):
        return FAIL
    if _COUNT_WORDS.search(text):
        return int(n), 0.95, str(int(n))
    if _NOT_A_COUNT.search(text):
        return FAIL
    return int(n), 0.9, str(int(n))


# ── дата и время ─────────────────────────────────────────────────────────────

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}
_DAYPARTS = {"утр": 8, "обед": 13, "день": 13, "вечер": 18, "ноч": 23}


def datetime_ru(text: str, today: date | None = None) -> Parsed:
    """«завтра к 8», «12.09 14:00», «в понедельник утром» → datetime."""
    t = text.lower().strip()
    today = today or date.today()
    d: date | None = None
    conf = 0.9

    if "послезавтра" in t:
        d = today + timedelta(days=2)
    elif "завтра" in t:
        d = today + timedelta(days=1)
    elif "сегодня" in t:
        d = today

    if d is None:
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", t)
        if m:
            day, mon = int(m.group(1)), int(m.group(2))
            year = int(m.group(3) or today.year)
            if year < 100:
                year += 2000
            try:
                d = date(year, mon, day)
            except ValueError:
                return FAIL

    if d is None:
        m = re.search(r"(\d{1,2})\s*([а-яё]{3,})", t)
        if m and m.group(2)[:3] in _MONTHS:
            try:
                d = date(today.year, _MONTHS[m.group(2)[:3]], int(m.group(1)))
            except ValueError:
                return FAIL

    if d is None:
        for name, idx in _WEEKDAYS.items():
            if name in t:
                delta = (idx - today.weekday()) % 7 or 7
                d = today + timedelta(days=delta)
                conf = 0.75
                break

    if d is None:
        return FAIL

    # Вырезаем распознанную дату, чтобы «12.09» не читалось как время.
    t = re.sub(r"\d{1,2}[.\-/]\d{1,2}(?:[.\-/]\d{2,4})?", " ", t)

    hh, mm = None, 0
    m = re.search(r"(\d{1,2})\s*[:]\s*(\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"(?:к|в|до|на)\s*(\d{1,2})(?:\s*(?:час|ч)\b)?", t)
        if m and int(m.group(1)) <= 23:
            hh = int(m.group(1))
        else:
            for part, hour in _DAYPARTS.items():
                if part in t:
                    hh, conf = hour, min(conf, 0.65)
                    break

    if hh is None:
        hh, conf = 9, min(conf, 0.5)  # время не названо — ставим утро, но переспросим

    dt = datetime(d.year, d.month, d.day, hh, mm)
    return dt, conf, dt.strftime("%d.%m.%Y %H:%M")


# ── реквизиты ────────────────────────────────────────────────────────────────

def _inn_valid(v: str) -> bool:
    def check(digits, weights):
        return sum(int(d) * w for d, w in zip(digits, weights)) % 11 % 10
    if len(v) == 10:
        return check(v[:9], [2, 4, 10, 3, 5, 9, 4, 6, 8]) == int(v[9])
    if len(v) == 12:
        w1 = [7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        w2 = [3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]
        return check(v[:10], w1) == int(v[10]) and check(v[:11], w2) == int(v[11])
    return False


def inn(text: str) -> Parsed:
    for cand in re.findall(r"\d{10,12}", text.replace(" ", "")):
        if len(cand) in (10, 12) and _inn_valid(cand):
            return cand, 0.99, cand
    m = re.search(r"\d{10,12}", text.replace(" ", ""))
    if m:
        # Похоже на ИНН, но контрольная сумма не сходится — почти всегда опечатка.
        return m.group(0), 0.3, f"{m.group(0)} — контрольная сумма не сходится"
    return FAIL


_LAT2CYR = str.maketrans("ABEKMHOPCTYXabekmhopctyx", "АВЕКМНОРСТУХавекмнорстух")


def plate(text: str) -> Parsed:
    """Госномер → канонический вид А123ВС777. Латиница-двойник приводится к кириллице."""
    t = text.upper().translate(_LAT2CYR).replace(" ", "").replace("-", "")
    m = re.search(r"([АВЕКМНОРСТУХ])(\d{3})([АВЕКМНОРСТУХ]{2})(\d{2,3})", t)
    if m:
        norm = "".join(m.groups())
        return norm, 0.95, norm
    m = re.search(r"([АВЕКМНОРСТУХ]{2})(\d{4})(\d{2,3})", t)  # прицепы
    if m:
        norm = "".join(m.groups())
        return norm, 0.9, norm
    return FAIL


def license_no(text: str) -> Parsed:
    t = re.sub(r"[^\dA-Za-zА-Яа-я]", "", text)
    m = re.search(r"(\d{2})(\d{2})(\d{6})", t)
    if m:
        norm = f"{m.group(1)}{m.group(2)} {m.group(3)}"
        return norm, 0.9, norm
    return FAIL


def phone(text: str) -> Parsed:
    d = re.sub(r"\D", "", text)
    if len(d) == 11 and d[0] in "78":
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    else:
        return FAIL
    norm = f"+{d[0]} ({d[1:4]}) {d[4:7]}-{d[7:9]}-{d[9:]}"
    return norm, 0.95, norm


def postcode(text: str) -> Parsed:
    """
    Почтовый индекс: шесть цифр. Схема требует его внутри адреса РФ.

    Берём именно шестизначную группу, а не любое число: в строке адреса рядом
    стоят номера домов, корпусов и телефоны, и «дом 123456» индексом не является
    только по позиции — поэтому индекс ищем как отдельное слово.
    """
    m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if not m:
        return FAIL
    norm = m.group(1)
    # Индекс в начале строки адреса — почти наверняка индекс; в середине бывает
    # номером договора, поэтому уверенность ниже и бот переспросит.
    conf = 0.95 if m.start() <= 2 else 0.8
    return norm, conf, norm


def region_code(text: str) -> Parsed:
    """
    Код региона: две цифры (01..99). Название региона в код не переводим —
    справочника субъектов у бота нет, а угадывать реквизит нельзя.
    """
    # Только чистые одна-две цифры целиком: иначе «завтра к 8» приезжает
    # кодом региона 08 и уводит ответ в чужое поле.
    cleaned = re.sub(r"\b(?:код|регион|региона|обл\.?)\b", "", text, flags=re.I)
    m = re.fullmatch(r"(\d{1,2})", cleaned.strip(" .,-"))
    if not m:
        return FAIL
    n = int(m.group(1))
    if not 1 <= n <= 99:
        return FAIL
    norm = f"{n:02d}"
    return norm, 0.9, norm


def fio(text: str) -> Parsed:
    """
    ФИО → «Фамилия Имя Отчество». Схема хранит части раздельно, поэтому
    значение возвращаем кортежем: шаблон не должен делить строку сам.
    Отчество — при наличии: формат допускает его отсутствие.
    """
    parts = [p for p in re.split(r"[\s.]+", text.strip()) if p]
    if len(parts) < 2:
        return FAIL
    parts = [p.capitalize() if p.isupper() or p.islower() else p for p in parts[:3]]
    norm = " ".join(parts)
    conf = 0.95 if len(parts) == 3 else 0.85
    # «Петров И.С.» — не ФИО: схема требует имя, а не инициал. Переспрашиваем.
    if any(len(p) < 2 for p in parts):
        conf = 0.5
        norm += " (нужно имя полностью, не инициалы)"
    return tuple(parts), conf, norm


def address_parts(text: str) -> dict[str, str]:
    """
    Грубый разбор строки адреса на город / улицу / дом.

    Это ЭВРИСТИКА, а не разбор по ФИАС: схема требует индекс и код региона
    (они спрашиваются отдельными полями), а город, улица и дом по формату
    необязательны — если разобрать не удалось, лучше не писать ничего, чем
    написать неверно. Поэтому уверенность у производных значений низкая и бот
    показывает их человеку.

    «141400, Химки, Ленина 1, склад 3» → {city: Химки, street: Ленина, house: 1}
    «Москва, ул. Вавилова, д. 19»      → {city: Москва, street: Вавилова, house: 19}
    """
    _STREET_PREFIX = r"(?:ул\.?|улица|пр-т|просп\.?|проспект|пер\.?|переулок|ш\.?|шоссе|бул\.?|бульвар)"
    _HOUSE_PREFIX = r"(?:д\.?|дом|стр\.?|строение|влд\.?)"

    chunks = [c.strip() for c in text.split(",") if c.strip()]
    chunks = [c for c in chunks if not re.fullmatch(r"\d{6}", c)]
    out: dict[str, str] = {}
    if not chunks:
        return out

    city = re.sub(r"^(?:г\.|город|г)\s+", "", chunks[0], flags=re.I).strip(" .")
    if city:
        out["city"] = city

    def clean_street(raw: str) -> str:
        return re.sub(rf"^{_STREET_PREFIX}\s*", "", raw.strip(), flags=re.I).strip(" .")

    for i, chunk in enumerate(chunks[1:], start=1):
        # «д. 19» отдельным куском — улица осталась в предыдущем
        only_house = re.fullmatch(rf"{_HOUSE_PREFIX}?\s*(\d+[А-Яа-яA-Za-z]?)", chunk, flags=re.I)
        if only_house and i >= 2:
            out["street"] = clean_street(chunks[i - 1])
            out["house"] = only_house.group(1)
            break
        # «ул. Ленина 1» одним куском
        both = re.fullmatch(rf"{_STREET_PREFIX}?\s*([А-Яа-яЁё\w .-]+?)\s+{_HOUSE_PREFIX}?\s*"
                            rf"(\d+[А-Яа-яA-Za-z]?)", chunk, flags=re.I)
        if both:
            out["street"] = clean_street(both.group(1))
            out["house"] = both.group(2)
            break
    return out


def packaging_code(text: str) -> Parsed:
    """
    Код вида тары. Принимаем либо готовый код из классификатора, либо описание
    упаковки словами — тогда подбираем код и показываем, что выбрали.
    Кода, которого нет в классификаторе, не принимаем вовсе: оператор отобьёт
    файл, а бухгалтер об этом узнает последним.
    """
    from . import packaging

    raw = (text or "").strip()
    if len(raw) == 2 and packaging.is_valid(raw):
        code = raw.upper()
        return code, 0.95, f"{code} — {packaging.title(code)}"

    found = packaging.search(raw)
    if not found:
        return FAIL
    code, name = found[0]
    # Подобранный по описанию код — догадка: человек обязан её увидеть.
    conf = 0.7 if len(found) > 1 else 0.85
    return code, conf, f"{code} — {name}"


def date_only(text: str) -> Parsed:
    """
    Дата без времени: рождение, выдача документа. Отдельно от datetime_ru,
    потому что тот честно ставит 0.5, когда времени нет, — для даты подачи
    машины это правильно, а для даты рождения превращается в лишний вопрос.
    """
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text or "")
    if not m:
        return FAIL
    try:
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return FAIL
    return d, 0.95, d.strftime("%d.%m.%Y")


def dims(text: str) -> Parsed:
    """
    Габариты грузового места → (длина, ширина, высота) в метрах.

    Порядок «длина × ширина × высота» — так говорят в логистике («паллета 120×80,
    высота 150»). Единицы: сантиметры, если любое число больше 10, миллиметры —
    если больше 1000; иначе метры. Показываем расшифровку — порядок чисел
    человек мог иметь в виду другой, поэтому уверенность ниже 0.9.
    """
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", text or "")]
    if len(nums) != 3 or any(n <= 0 for n in nums):
        return FAIL
    low = (text or "").lower()
    if "мм" in low or max(nums) > 1000:
        k = 0.001
    elif "см" in low or max(nums) > 10:
        k = 0.01
    else:
        k = 1.0
    l, w, h = (round(n * k, 3) for n in nums)
    return (l, w, h), 0.85, f"длина {l:g} м, ширина {w:g} м, высота {h:g} м"


def order_kind(text: str) -> Parsed:
    """«Заказ» (договора об организации перевозок нет) или «Заявка» (есть)."""
    t = (text or "").strip().lower()
    if re.search(r"\bнет\b|заказ|без договор", t):
        return "Заказ", 0.95, "Заказ — договора об организации перевозок нет"
    if re.search(r"\bда\b|\bесть\b|заявк|договор", t):
        return "Заявка", 0.95, "Заявка — есть договор об организации перевозок"
    return FAIL


PARSERS = {
    "date": date_only,
    "dims": dims,
    "order_kind": order_kind,
    "mass": mass,
    "volume": volume,
    "money": money,
    "int": integer,
    "datetime": datetime_ru,
    "inn": inn,
    "plate": plate,
    "license": license_no,
    "phone": phone,
    "postcode": postcode,
    "region": region_code,
    "fio": fio,
    "packcode": packaging_code,
}


def run(name: str | None, text: str, today: date | None = None) -> Parsed:
    """
    Прогнать текст через парсер поля.

    `today` — опорная дата для относительных ответов («завтра к 8»). Её задаёт
    вызывающий: у накладной своя дата составления, и «завтра» должно считаться
    от неё, а не от системных часов, иначе ответ, данный в 00:05, уедет на сутки.
    """
    if not name:
        s = text.strip()
        return (s, 0.9, s) if s else FAIL
    fn = PARSERS.get(name)
    if not fn:
        s = text.strip()
        return (s, 0.9, s) if s else FAIL
    try:
        if fn is datetime_ru:
            return fn(text, today=today)
        return fn(text)
    except Exception:
        return FAIL
