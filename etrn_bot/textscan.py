"""
Разбор текста документа по подписям полей.

Зачем это есть, если есть модель. Маленькая vision-модель на процессоре не
держит промпт «найди 78 полей и верни JSON» — проверено на Hermes: 11 минут и
ноль полей. Зато она умеет то, что от неё и нужно, — переписать текст с фото.
Дальше разбор делает детерминированный код: подписи в перевозочных документах
стандартные, а значения проверяются теми же парсерами, что и ответы человека
(ИНН — по контрольной сумме, госномер — по формату, масса — с единицами).

Этот же разбор работает, когда бухгалтер присылает не фото, а текст письма.

Уверенность здесь никогда не 1.0: подпись в документе может стоять над чужим
значением, поэтому всё, что нашли, идёт человеку на подтверждение.
"""

from __future__ import annotations

import re

from . import parse

# Ключ реестра → как это подписано в заявках и договорах-заявках.
# Порядок важен: более узкие подписи идут раньше общих («телефон водителя»
# раньше «телефон»), иначе общая подпись перехватит строку.
LABELS: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("order_number", ("заявка №", "заявка no", "заявка N", "номер заявки"), None),
    ("order_date", ("дата заявки",), "datetime"),
    ("pickup_address", ("адрес погрузки", "место погрузки", "погрузка по адресу",
                        "откуда"), None),
    ("delivery_address", ("адрес выгрузки", "адрес доставки", "место выгрузки",
                          "место доставки", "куда"), None),
    ("cargo_name", ("наименование груза", "груз:", "наименование товара"), None),
    ("cargo_mass_gross_kg", ("масса брутто", "вес брутто", "масса груза", "вес груза"),
     "mass"),
    ("cargo_places", ("количество мест", "кол-во мест", "число мест", "мест:"), "int"),
    ("cargo_package", ("упаковка", "вид упаковки", "способ упаковки", "тара"), None),
    ("cargo_marking", ("маркировка",), None),
    ("cargo_volume_m3", ("объем груза", "объём груза"), "volume"),
    ("driver_phone", ("телефон водителя", "тел. водителя"), "phone"),
    ("driver_license", ("удостоверение", "в/у", "ву:", "права"), "license"),
    ("driver_fio", ("водитель",), "fio"),
    ("vehicle_plate", ("гос. номер", "госномер", "гос номер", "номер тс",
                       "регистрационный знак"), "plate"),
    ("vehicle_brand", ("автомобиль", "марка тс", "марка, модель", "тягач"), None),
    ("trailer_plate", ("прицеп", "полуприцеп"), "plate"),
    ("pickup_datetime", ("подачи тс", "подача тс", "дата погрузки",
                         "время погрузки"), "datetime"),
    ("pickup_arrival_fact", ("факт. прибытие", "фактическое прибытие", "прибытие"),
     "datetime"),
    ("pickup_departure_fact", ("факт. убытие", "фактическое убытие", "убытие"),
     "datetime"),
    ("delivery_datetime", ("дата доставки", "срок доставки", "дата выгрузки"), "datetime"),
    ("carriage_cost", ("стоимость перевозки", "ставка", "фрахт"), "money"),
)

# Роли сторон: из одной строки достаём и ИНН, и наименование.
ROLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("shipper_inn", "shipper_name", ("грузоотправитель", "отправитель")),
    ("consignee_inn", "consignee_name", ("грузополучатель", "получатель")),
    ("carrier_inn", "carrier_name", ("перевозчик", "исполнитель")),
)

_INN = re.compile(r"\bИНН[\s:]*([0-9]{10}|[0-9]{12})\b", re.I)
# Госномер без подписи: буква, три цифры, две буквы, регион. Латиница и
# кириллица вперемешку — OCR путает двойников, нормализует их парсер.
_PLATE_ANYWHERE = re.compile(
    r"(?<![\w])([A-ZА-ЯЁ]\s?\d{3}\s?[A-ZА-ЯЁ]{2}\s?\d{2,3})(?![\w])", re.I)
# «ЗАЯВКА № 418 на перевозку груза» → номер это «418», а не весь хвост строки.
_DOC_NUMBER = re.compile(r"^[№N\s:]*([A-Za-zА-Яа-я0-9][A-Za-zА-Яа-я0-9/\-]*)")
# «от 08 сентября 2026 г.» под заголовком заявки — это её дата.
_DATE_LINE = re.compile(r"^от\s+(.{6,40}?)\s*(?:г\.?|года)?\s*$", re.I)


def _after_label(line: str, label: str) -> str:
    """Часть строки после подписи, без двоеточий и лишних пробелов."""
    idx = line.lower().find(label)
    tail = line[idx + len(label):]
    return tail.lstrip(" :–—-\t").strip()


def _org_name(tail: str) -> str:
    """«ООО "ВЕЗЁМ", ИНН 7736050003» → «ООО "ВЕЗЁМ"»."""
    cut = _INN.split(tail)[0]
    return cut.rstrip(" ,;").strip()


# ── документы-удостоверения ─────────────────────────────────────────────────
#
# У прав и СТС нет подписей вида «Водитель:» — там нумерованные пункты и
# двуязычные заголовки. Разбираем их отдельными правилами, по признаку самого
# документа. Паспорт сознательно не разбираем: в Титуле 1 нет ни одного поля с
# паспортными данными, а значит незачем их и доставать — меньше ПД в системе.

_VU_MARKERS = ("водительское удостоверение", "driving licence", "permis de conduire")
_STS_MARKERS = ("свидетельство о регистрации", "certificat d", "регистрационный знак")

_VU_NUMBER = re.compile(r"\b(\d{2}\s?\d{2})\s?(\d{6})\b")
_DATE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
_UPPER_WORDS = re.compile(r"^[\dA-Za-z.)\s]*([А-ЯЁ][А-ЯЁ\-]{2,}(?:\s+[А-ЯЁ][А-ЯЁ\-]{2,})*)\s*$")


def _scan_license(text: str, source: str) -> dict[str, tuple[object, float, str]]:
    """Водительское удостоверение: номер и ФИО."""
    found: dict[str, tuple[object, float, str]] = {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    m = _VU_NUMBER.search(text)
    if m:
        val, conf, _shown = parse.run("license", f"{m.group(1)}{m.group(2)}")
        if val is not None:
            # OCR на пластике ошибается в цифрах чаще, чем на бумаге: ниже порога,
            # чтобы номер прав человек обязательно подтвердил глазами.
            found["driver_license"] = (val, 0.6, f"{source}: водительское удостоверение")

    # ФИО: в правах фамилия и имя-отчество идут отдельными строками заглавными,
    # первая латинская строка — их транслитерация, она нам не нужна.
    # Заголовки бланка тоже написаны заглавными, поэтому их отсекаем по словарю:
    # без этого «ВОДИТЕЛЬСКОЕ УДОСТОВЕРЕНИЕ» приезжает фамилией водителя.
    stop = ("УДОСТОВЕРЕНИЕ", "ВОДИТЕЛЬСК", "РОССИЙСК", "ФЕДЕРАЦ", "ГИБДД",
            "ОБЛАСТЬ", "ОБЛ", "КРАЙ", "РАЙОН", "ГОРОД", "РЕСПУБЛИК", "МВД",
            "СВИДЕТЕЛЬСТВО", "РЕГИСТРАЦИИ", "КАТЕГОРИИ", "ОСОБЫЕ", "ОТМЕТКИ")
    caps: list[str] = []
    for line in lines:
        m2 = _UPPER_WORDS.match(line)
        if not m2:
            continue
        value = m2.group(1)
        if any(word in value for word in stop):
            continue
        caps.append(value)
        if len(caps) >= 2:
            break
    if len(caps) >= 2:
        parts = (caps[0] + " " + caps[1]).split()
        if 2 <= len(parts) <= 3:
            fio = tuple(p.capitalize() for p in parts)
            found["driver_fio"] = (fio, 0.6, f"{source}: водительское удостоверение")
    return found


def _scan_vehicle_certificate(text: str, source: str) -> dict[str, tuple[object, float, str]]:
    """СТС: госномер, марка, тип, массы. Бланк с защитной сеткой — OCR тут слаб."""
    found: dict[str, tuple[object, float, str]] = {}
    labels = (
        ("vehicle_plate", "регистрационный знак", "plate"),
        ("vehicle_brand", "марка", None),
        ("vehicle_type", "тип тс", None),
        ("vehicle_max_mass", "допустимая", "mass"),
        ("vehicle_sts", "птс", None),
    )
    for raw in text.splitlines():
        low = raw.lower()
        for key, label, parser in labels:
            if key in found or label not in low:
                continue
            tail = _after_label(raw, label)
            tail = re.sub(r"[|_=~]+", " ", tail).strip(" .,:;-")
            if not tail:
                continue
            if parser:
                val, conf, _shown = parse.run(parser, tail)
                if val is None:
                    continue
                found[key] = (val, min(conf, 0.6), f"{source}: свидетельство о регистрации")
            else:
                found[key] = (tail[:60], 0.5, f"{source}: свидетельство о регистрации")
    return found


# Карточка водителя — вордовский файл, который логисты пересылают друг другу:
# ФИО с датой рождения, паспорт, телефон, машина и прицеп одной строкой.
# Подписей «Водитель:» там нет, поэтому опознаём по составу: дата рождения
# рядом с ФИО и слова «Паспорт РФ».
_CARD_MARKERS = ("г.р", "паспорт рф", "код подразделения")
_FIO_BIRTH = re.compile(
    r"^([А-ЯЁ][а-яё\-]+)\s+([А-ЯЁ][а-яё\-]+)(?:\s+([А-ЯЁ][а-яё\-]+))?\s+"
    r"\d{2}[.,]\d{2}[.,]\d{4}\s*г")
# В карточках пишут как придётся: «Е 361 ВР 250», «ЕО 9379  50» — пробелы
# внутри номера произвольные, поэтому \s* везде, а нормализует парсер.
_CAR_LINE = re.compile(
    r"а/м\s+(.+?)\s+([АВЕКМНОРСТУХ]\s*\d{3}\s*[АВЕКМНОРСТУХ]{2}\s*\d{2,3})", re.I)
_TRAILER_LINE = re.compile(r"п/п\s+([АВЕКМНОРСТУХ]{2}\s*\d{4}\s*\d{2,3})", re.I)


def _scan_driver_card(text: str, source: str) -> dict[str, tuple[object, float, str]]:
    """Карточка водителя: ФИО, телефон, машина, прицеп."""
    found: dict[str, tuple[object, float, str]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _FIO_BIRTH.match(line)
        if m and "driver_fio" not in found:
            parts = tuple(p for p in m.groups() if p)
            found["driver_fio"] = (parts, 0.8, f"{source}: «{line[:50]}»")
        if "телефон" in line.lower() and "driver_phone" not in found:
            val, conf, _shown = parse.run("phone", line)
            if val is not None:
                found["driver_phone"] = (val, min(conf, 0.8), f"{source}: «{line[:50]}»")
        car = _CAR_LINE.search(line)
        if car:
            brand = car.group(1).strip(" .,")
            plate, conf, _shown = parse.run("plate", car.group(2))
            if plate is not None and "vehicle_plate" not in found:
                found["vehicle_plate"] = (plate, 0.8, f"{source}: «{line[:50]}»")
            if brand and "vehicle_brand" not in found:
                found["vehicle_brand"] = (brand.title(), 0.7, f"{source}: «{line[:50]}»")
        trailer = _TRAILER_LINE.search(line)
        if trailer and "trailer_plate" not in found:
            plate, conf, _shown = parse.run("plate", trailer.group(1))
            if plate is not None:
                found["trailer_plate"] = (plate, 0.7, f"{source}: «{line[:50]}»")
    return found


# Именованные поля, которые возвращают шаблонные модели Yandex Vision OCR.
# Значение → (ключ реестра, парсер). Чего нет в этой таблице, мы не берём:
# лишние ПД в системе не нужны.
ENTITY_MAP: dict[str, tuple[str, str | None]] = {
    # водительское удостоверение
    "number": ("driver_license", "license"),
    "birth_date": ("driver_birth_date", "date"),
    # паспорт (для доверенности, не для накладной)
    "issued_by": ("driver_passport_issued_by", None),
    "issue_date": ("driver_passport_date", "date"),
    "subdivision": ("driver_passport_subdivision", None),
    # СТС
    "stsfront_car_number": ("vehicle_plate", "plate"),
    "stsfront_car_brand": ("vehicle_brand", None),
    "stsfront_sts_number": ("vehicle_sts", None),
    # stsfront_car_trailer_number сознательно не берём: у Yandex Vision это
    # «номер кузова, кабины или прицепа» из бланка — заводской номер, а не
    # госномер прицепа. Маппинг на trailer_plate давал неверный номер.
}

# Номера-идентификаторы из сервиса ниже порога 0.75 — человек подтверждает
# каждый. Проверено 10.09.2026 на реальных сканах СТС: сервис вернул
# «к556мн99» вместо К555МН199 и «ав12399» вместо АВ1234 99 — путает цифры и
# теряет разряды. Имена и даты он читает заметно надёжнее.
_RISKY_NUMBERS = {"driver_license", "driver_passport", "vehicle_plate",
                  "trailer_plate", "vehicle_sts"}
_RISKY_CONF = 0.6

# Госномер прицепа отличается форматом: две буквы, четыре цифры, регион
# («АВ1234 99»). По нему и раскладываем СТС: тягач или прицеп.
_TRAILER_PLATE = re.compile(r"^[АВЕКМНОРСТУХ]{2}\d{4}\d{2,3}$")


def from_entities(fields: dict[str, str], source: str,
                  kind: str = "") -> dict[str, tuple[object, float, str]]:
    """
    Именованные поля шаблонного документа → поля реестра.

    Уверенность 0.8: сервис читает бланки надёжнее нашего OCR, но реквизиты
    водителя и машины человек всё равно подтверждает — цена ошибки в номере
    прав выше, чем неудобство одного вопроса.
    """
    found: dict[str, tuple[object, float, str]] = {}
    fio_parts = [fields.get("surname"), fields.get("name"), fields.get("middle_name")]
    fio = tuple(p.strip() for p in fio_parts if p and p.strip())
    if len(fio) >= 2:
        found["driver_fio"] = (fio, 0.8, f"{source}: {kind or 'документ'}")

    # Паспорт: серия и номер приходят одним полем number — в накладную они не
    # идут, но нужны для доверенности, поэтому кладём в паспортное поле.
    if kind == "passport" and fields.get("number"):
        found["driver_passport"] = (fields["number"].strip(), 0.8,
                                    f"{source}: паспорт")

    for name, value in fields.items():
        if name in ("surname", "name", "middle_name"):
            continue
        if kind == "passport" and name == "number":
            continue
        target = ENTITY_MAP.get(name)
        if not target:
            continue
        key, parser = target
        if key == "vehicle_plate" and parser == "plate":
            val, _c, _s = parse.run("plate", value)
            if val is not None and _TRAILER_PLATE.match(val):
                # СТС прицепа: его номер — в прицеп, марка и номер СТС —
                # не про тягач, их не берём, чтобы не перетереть тягач.
                found.setdefault("trailer_plate", (val, _RISKY_CONF, f"{source}: СТС прицепа"))
                return {k: v for k, v in found.items()
                        if k not in ("vehicle_brand", "vehicle_sts")}
        if key in found:
            continue
        cap = _RISKY_CONF if key in _RISKY_NUMBERS else 0.8
        if parser:
            val, conf, _shown = parse.run(parser, value)
            if val is None:
                continue
            found[key] = (val, min(conf, cap), f"{source}: {kind or 'документ'}")
        else:
            found[key] = (value.strip(), cap, f"{source}: {kind or 'документ'}")
    if "driver_passport" in found:
        v, _c, src = found["driver_passport"]
        found["driver_passport"] = (v, _RISKY_CONF, src)
    return found


def scan(text: str, source: str = "документ") -> dict[str, tuple[object, float, str]]:
    """
    text → {ключ реестра: (значение, уверенность, откуда)}.

    Значение прогоняется через парсер поля: если парсер не понял, поле не
    возвращается вовсе. Пустой результат — законный ответ: значит в тексте
    этого нет, и бот спросит человека.
    """
    found: dict[str, tuple[object, float, str]] = {}
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]

    for raw in lines:
        low = raw.lower()

        # стороны: ИНН + наименование из одной строки
        for inn_key, name_key, labels in ROLES:
            if any(lb in low for lb in labels):
                m = _INN.search(raw)
                if m and inn_key not in found:
                    val, conf, _shown = parse.run("inn", m.group(1))
                    if val is not None:
                        found[inn_key] = (val, min(conf, 0.85), f"{source}: «{raw[:60]}»")
                label = next(lb for lb in labels if lb in low)
                name = _org_name(_after_label(raw, label))
                if name and name_key not in found and len(name) > 3:
                    found[name_key] = (name, 0.8, f"{source}: «{raw[:60]}»")

        # обычные подписи
        for key, labels, parser in LABELS:
            if key in found:
                continue
            label = next((lb for lb in labels if lb in low), None)
            if label is None:
                continue
            tail = _after_label(raw, label)
            if not tail:
                continue
            # В строке может стоять сразу два значения («прибытие … убытие …») —
            # обрезаем по следующей подписи, иначе парсер возьмёт чужое.
            for other_key, other_labels, _p in LABELS:
                if other_key == key:
                    continue
                for other in other_labels:
                    pos = tail.lower().find(other)
                    if pos > 0:
                        tail = tail[:pos].rstrip(" ,;")
            if key == "order_number":
                m = _DOC_NUMBER.match(tail)
                if not m:
                    continue
                found[key] = (m.group(1), 0.85, f"{source}: «{raw[:60]}»")
                continue
            if parser:
                val, conf, _shown = parse.run(parser, tail)
                if val is None:
                    continue
                found[key] = (val, min(conf, 0.85), f"{source}: «{raw[:60]}»")
            else:
                found[key] = (tail, 0.8, f"{source}: «{raw[:60]}»")

    # Номер машины без подписи. tesseract 4.1 (стоит на Hermes) читает подпись
    # «гос. номер» латиницей — «roc. Homep», — и разбор по подписи номер
    # теряет, хотя сам номер прочитан верно. Берём его, только если в тексте он
    # один: два разных номера — это тягач и прицеп или чужая машина, гадать нельзя.
    if "vehicle_plate" not in found:
        plates = set()
        for m in _PLATE_ANYWHERE.finditer(text):
            val, _c, _s = parse.run("plate", m.group(1))
            if val is not None and val != (found.get("trailer_plate") or (None,))[0]:
                plates.add(val)
        if len(plates) == 1:
            found["vehicle_plate"] = (plates.pop(), 0.6, f"{source}: номер найден без подписи")

    low_all = text.lower()
    if sum(m in low_all for m in _CARD_MARKERS) >= 2:
        for key, value in _scan_driver_card(text, source).items():
            found.setdefault(key, value)
    if any(m in low_all for m in _VU_MARKERS):
        for key, value in _scan_license(text, source).items():
            found.setdefault(key, value)
    if any(m in low_all for m in _STS_MARKERS):
        for key, value in _scan_vehicle_certificate(text, source).items():
            found.setdefault(key, value)

    # Дата документа обычно стоит отдельной строкой «от 08 сентября 2026 г.»
    # сразу под заголовком — подписи «дата заявки» в документах почти не бывает.
    if "order_date" not in found:
        for raw in lines[:5]:
            m = _DATE_LINE.match(raw)
            if not m:
                continue
            val, conf, _shown = parse.run("datetime", m.group(1))
            if val is not None:
                found["order_date"] = (val, min(conf, 0.8), f"{source}: «{raw[:60]}»")
                break

    return found
