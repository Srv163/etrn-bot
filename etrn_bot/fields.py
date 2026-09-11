"""
Реестр полей Титула 1 ЭТрН.

Ядро бота. Всё остальное — обвязка вокруг этой таблицы.

Для каждого поля описано:
  - как его добыть (порядок стратегий);
  - можно ли боту подставить значение самому;
  - что делать, если человек сказал «не знаю»;
  - как спросить по-человечески и зачем это поле вообще нужно;
  - куда оно попадает в XML (`xsd`).

СВЕРКА СО СХЕМОЙ ВЫПОЛНЕНА 09.09.2026 против
`schema/ON_TRNACLGROT_1_973_01_05_01_02.xsd` (приказ ФНС ЕД-7-26/1065@,
приложение № 1, формат 5.01, часть 973, титул 1 — информация грузоотправителя).
Пути в `xsd` взяты из схемы, а не из описания формата по смыслу.
Проверка расхождений: `python3 -m etrn_bot.tools.check_registry`.

Соглашения по `xsd`:
  - несколько путей через `|` — одно значение уходит в несколько мест схемы
    (например масса брутто пишется и в описании груза, и в сведениях о погрузке);
  - `not_in_xml=True` — поле в XML не пишется вообще; причина обязательна в `why`.
    Так помечены реквизиты, которых титул 1 не содержит (юридические адреса
    сторон — схема их не требует) и данные для имени файла обмена.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from enum import Enum


class Strategy(str, Enum):
    EXTRACT = "extract"   # распознать из загруженных документов
    LOOKUP = "lookup"     # достать из справочника по ключу
    DERIVE = "derive"     # вычислить из других полей
    DEFAULT = "default"   # подставить политику организации
    ASK = "ask"           # спросить человека
    DEFER = "defer"       # не наше поле: заполнит другая сторона / другой титул


@dataclass(frozen=True)
class Field:
    key: str
    section: str
    title: str
    strategies: tuple[Strategy, ...]
    required: bool = True

    question: str = ""          # как спросить
    why: str = ""               # ответ на «а зачем это?»
    hint: str = ""              # где человеку это найти

    parser: str | None = None   # имя парсера свободной формы
    default: object | None = None
    lookup_by: str | None = None
    derive_from: tuple[str, ...] = ()
    defer_to: str | None = None

    # Критичное поле: боту запрещено подставлять значение самому.
    # «Не знаю» здесь — не повод угадать, а повод остановиться и подсказать, где взять.
    never_guess: bool = False

    xsd: str | None = None
    verify: bool = False        # True — привязка к схеме ещё не подтверждена
    not_in_xml: bool = False    # поле нужно человеку/имени файла, но не телу XML

    # Умолчание не догадка бота, а формулировка, предписанная форматом ФНС
    # (например «Отсутствуют» при отсутствии особых требований). Подтверждать
    # такое у бухгалтера нечего — спрашивать про версию приказа бессмысленно.
    default_by_format: bool = False

    # Расшифровка кодов формата: бухгалтеру нельзя показывать «ТипВлад=3».
    codes: tuple[tuple[str, str], ...] = ()

    # В каких документах поле участвует: "etrn" (Титул 1) и/или "zayavka".
    docs: tuple[str, ...] = ("etrn",)

    # Рутинное поле: значение берётся из вашей коммерческой политики и от
    # рейса к рейсу не меняется (НДС, условия оплаты, нормы простоя).
    # Такое НЕ спрашиваем по одному — показываем блоком в карточке.
    # Иначе бухгалтер восемь раз подряд жмёт «да», и это не помощь, а издевательство.
    routine: bool = False

    # Привязка к схемам других документов: ((тип документа, "путь|путь"), ...).
    # Для накладной путь по-прежнему в `xsd` — так сверка ЭТрН не меняется.
    paths: tuple[tuple[str, str], ...] = ()

    # В каких документах поле обязательно, даже если в целом необязательно:
    # объём груза в ЭТрН не нужен, а в заявке без него схема не пройдёт.
    required_in: tuple[str, ...] = ()

    def never_guess_key_blocked(self) -> bool:
        """
        Запоминать ответ по ключу можно и для never_guess: справочник — это
        данные, введённые человеком, а не догадка бота. Запрещено только
        подставлять значение без источника. Метод оставлен явным, чтобы это
        решение было видно, а не подразумевалось.
        """
        return False

    def xsd_for(self, doc_type: str) -> str | None:
        if doc_type == "etrn":
            return self.xsd
        return dict(self.paths).get(doc_type)

    def required_for(self, doc_type: str) -> bool:
        return self.required or doc_type in self.required_in

    def show(self, value: object) -> str:
        """Как значение выглядит для человека."""
        if self.codes:
            for code, title in self.codes:
                if str(value) == code:
                    return f"{code} — {title}"
        if isinstance(value, tuple):
            return " ".join(str(p) for p in value)
        return str(value)

    def can_self_fill(self) -> bool:
        """Может ли бот закрыть поле без человека."""
        if self.never_guess:
            return False
        return any(
            s in (Strategy.LOOKUP, Strategy.DERIVE, Strategy.DEFAULT, Strategy.DEFER)
            for s in self.strategies
        )


E, L, D, DF, A, DEF = (
    Strategy.EXTRACT, Strategy.LOOKUP, Strategy.DERIVE,
    Strategy.DEFAULT, Strategy.ASK, Strategy.DEFER,
)

# Корень содержания информации грузоотправителя — префикс почти всех путей.
GO = "/Файл/Документ/СодИнфГО"


_ETRN_BASE: tuple[Field, ...] = (

    # ── Служебное ────────────────────────────────────────────────────────────
    Field(
        key="doc_number", section="Служебное", title="Номер документа",
        strategies=(D, A), derive_from=("_counter",),
        question="Номер накладной? (пусто — присвою следующий по журналу)",
        why="Свой сквозной номер документа. Не путать с УИД, его выдаёт ГИС ЭПД.",
        xsd=f"{GO}/@НомерТрН",
    ),
    Field(
        key="doc_date", section="Служебное", title="Дата составления",
        strategies=(D,), derive_from=("_today",),
        why="Дата составления накладной. По умолчанию — сегодня.",
        xsd=f"{GO}/@ДатаТрН",
    ),
    Field(
        key="uid", section="Служебное", title="УИД накладной",
        strategies=(DEF,), required=False, defer_to="ГИС ЭПД",
        why="Уникальный идентификатор присваивает государственная система, не мы. "
            "В титуле 1 такого реквизита нет — он появляется после регистрации в ГИС ЭПД.",
        not_in_xml=True,
    ),

    # ── 1. Грузоотправитель ──────────────────────────────────────────────────
    Field(
        key="shipper_inn", section="1. Грузоотправитель", title="ИНН грузоотправителя",
        strategies=(DF, A), parser="inn", default="__org_inn__", never_guess=True,
        question="ИНН вашей организации-грузоотправителя?",
        why="ИНН идентифицирует отправителя в ГИС ЭПД",
        xsd=f"{GO}/СвГО/РекИдентГО/ИдСв/СвЮЛУч/@ИННЮЛ",
    ),
    Field(key="shipper_name", section="1. Грузоотправитель", title="Наименование грузоотправителя",
          strategies=(L, DF, A), lookup_by="shipper_inn",
          xsd=f"{GO}/СвГО/РекИдентГО/ИдСв/СвЮЛУч/@НаимОрг"),
    Field(key="shipper_kpp", section="1. Грузоотправитель", title="КПП грузоотправителя",
          strategies=(L, DF, A), lookup_by="shipper_inn", required=False,
          xsd=f"{GO}/СвГО/РекИдентГО/ИдСв/СвЮЛУч/@КПП"),
    Field(key="shipper_address", section="1. Грузоотправитель", title="Юрадрес грузоотправителя",
          strategies=(L, DF, A), lookup_by="shipper_inn", required=False,
          why="Юридический адрес отправителя. Схема титула 1 адрес сторон не требует "
              "(элемент Адрес опционален), а адреса погрузки и выгрузки — отдельные поля. "
              "Держим для карточки, которую человек видит перед подписью.",
          not_in_xml=True),
    Field(key="shipper_phone", section="1. Грузоотправитель", title="Телефон грузоотправителя",
          strategies=(DF, L, A), lookup_by="shipper_inn", parser="phone",
          why="Обязателен по схеме: перевозчику нужен контакт отправителя.",
          xsd=f"{GO}/СвГО/РекИдентГО/Контакт/Тлф"),
    Field(key="shipper_contact", section="1. Грузоотправитель", title="Контактное лицо",
          strategies=(DF, A), required=False,
          why="Кому звонить по погрузке. Отдельного реквизита в титуле 1 нет — "
              "показываем человеку, в XML не уходит.",
          not_in_xml=True),
    Field(
        key="shipper_edo_id", section="1. Грузоотправитель", title="Идентификатор участника ЭДО",
        strategies=(L,), lookup_by="shipper_inn",
        hint="Заводится один раз в справочнике организаций — из карточки контрагента в СБИС.",
        why="Идёт в ИМЯ файла обмена (часть O по приказу), а не в тело XML. "
            "Присваивает оператор ЭДО; руками не вводится — если его нет, "
            "занесите организацию в справочник, иначе имя файла не собрать.",
        not_in_xml=True,
    ),
    Field(
        key="shipper_is_expeditor", section="1. Грузоотправитель",
        title="Грузоотправитель — экспедитор?",
        strategies=(DF, A), default="0",
        codes=(("0", "не экспедитор"), ("1", "экспедитор")),
        question="Вы выступаете экспедитором по этой перевозке? (обычно нет)",
        why="Реквизит формата: 0 — не является исполнителем услуг по организации перевозки, "
            "1 — является. Для собственного груза это 0.",
        xsd=f"{GO}/СвГО/@ГОЭксп",
    ),

    # ── 2. Грузополучатель ───────────────────────────────────────────────────
    Field(
        key="consignee_inn", section="2. Грузополучатель", title="ИНН грузополучателя",
        strategies=(E, L, A), parser="inn", never_guess=True,
        question="ИНН грузополучателя?",
        hint="Есть в заявке, счёте или договоре — шапка контрагента.",
        why="без верного ИНН документ не свяжется с получателем в ЭДО",
        xsd=f"{GO}/СвГП/РекИдентГП/ИдСв/СвЮЛУч/@ИННЮЛ",
    ),
    Field(key="consignee_name", section="2. Грузополучатель", title="Наименование грузополучателя",
          strategies=(L, E, A), lookup_by="consignee_inn",
          xsd=f"{GO}/СвГП/РекИдентГП/ИдСв/СвЮЛУч/@НаимОрг"),
    Field(key="consignee_kpp", section="2. Грузополучатель", title="КПП грузополучателя",
          strategies=(L, E, A), lookup_by="consignee_inn", required=False,
          xsd=f"{GO}/СвГП/РекИдентГП/ИдСв/СвЮЛУч/@КПП"),
    Field(key="consignee_address", section="2. Грузополучатель", title="Юрадрес грузополучателя",
          strategies=(L, E, A), lookup_by="consignee_inn", required=False,
          why="Юридический адрес получателя. В XML не пишется: схема требует адрес "
              "доставки груза (поля раздела «7. Сдача груза»), а не юрадрес.",
          not_in_xml=True),
    Field(key="consignee_phone", section="2. Грузополучатель", title="Телефон грузополучателя",
          strategies=(L, E, A), lookup_by="consignee_inn", parser="phone",
          why="Обязателен по схеме: водителю нужен телефон на выгрузке.",
          xsd=f"{GO}/СвГП/РекИдентГП/Контакт/Тлф"),
    Field(key="consignee_edo_id", section="2. Грузополучатель", title="Идентификатор участника ЭДО",
          strategies=(L,), lookup_by="consignee_inn",
          hint="Из карточки контрагента в СБИС; заносится в справочник один раз.",
          why="Идёт в ИМЯ файла обмена (часть E по приказу), а не в тело XML. "
              "Без него файл собрать можно, а отправить — нет.",
          not_in_xml=True),

    # ── 3. Груз ──────────────────────────────────────────────────────────────
    Field(
        key="cargo_name", section="3. Груз", title="Наименование груза",
        strategies=(E, A), never_guess=True,
        question="Что везём? Наименование груза как в заявке.",
        hint="Строка из заявки или спецификации.",
        xsd=f"{GO}/СвГруз/ОпГруз/@НаимГруз",
    ),
    Field(
        key="cargo_places", section="3. Груз", title="Количество мест",
        strategies=(E, A), parser="int", never_guess=True,
        question="Сколько мест?",
        hint="Паллеты, коробки, бочки — сколько физических грузовых мест. "
             "Для насыпных и наливных грузов формат требует 1.",
        why="расхождение по местам — самый частый спор при приёмке",
        xsd=f"{GO}/СвГруз/ОпГруз/@КолМестГр|{GO}/СвПогруз/@КолМестПрием",
    ),
    Field(
        key="cargo_package", section="3. Груз", title="Способ упаковки",
        strategies=(E, L, DF, A), lookup_by="cargo_name", default="без упаковки",
        question="Как упакован груз?",
        why="Текстовое описание упаковки. Код тары — отдельное поле.",
        xsd=f"{GO}/СвГруз/ОпГруз/@СпУпак",
    ),
    Field(
        key="cargo_package_code", section="3. Груз", title="Код вида тары",
        strategies=(E, L, D, A), lookup_by="cargo_name", parser="packcode",
        derive_from=("cargo_package",),
        question="Как упакован груз? Подберу код тары сам.",
        hint="Можно словами — «паллеты», «мешки», «бочки», «навалом»: "
             "подберу код по классификатору и покажу, что выбрал.",
        why="Обязательный реквизит формата, ровно два символа по классификатору "
            "из решения КТС № 378. Подобранный код бот показывает на подтверждение: "
            "неверный код — отказ у оператора.",
        xsd=f"{GO}/СвГруз/ОпГруз/@ВидТар",
    ),
    Field(
        key="cargo_state", section="3. Груз", title="Состояние груза",
        strategies=(E, DF, A), default="В исправном состоянии",
        question="Состояние груза при погрузке?",
        why="Обязательный реквизит. Умолчание бот показывает и просит подтвердить: "
            "состояние груза — факт, а не политика.",
        xsd=f"{GO}/СвГруз/ОпГруз/@СостГруз",
    ),
    Field(
        key="cargo_mass_gross_kg", section="3. Груз", title="Масса брутто, кг",
        strategies=(E, A), parser="mass", never_guess=True,
        question="Масса брутто?",
        hint="Можно писать как удобно: «20 т», «20500 кг», «двадцать тонн».",
        why="по массе считают допустимую нагрузку и расхождения при выдаче",
        xsd=f"{GO}/СвГруз/ОпГруз/ПлМасГруз/@МасБрутЗнач|{GO}/СвПогруз/@МасБрутОтгр",
    ),
    Field(
        key="mass_method", section="3. Груз", title="Метод определения массы",
        strategies=(DF, A), default="01",
        codes=(("01", "взвешивание по общей массе"), ("02", "взвешивание поосно"),
               ("03", "расчётная масса")),
        question="Как определяли массу: 01 — взвешивание по общей массе, "
                 "02 — поосно, 03 — расчётная масса?",
        why="Обязательный код формата. По умолчанию 01 — взвешивание по общей массе.",
        xsd=f"{GO}/СвПогруз/@МетОпрМасс",
    ),
    Field(key="cargo_volume_m3", section="3. Груз", title="Объём, м³",
          strategies=(E, A), parser="volume", required=False,
          xsd=f"{GO}/СвГруз/ОпГруз/@Объем"),
    Field(key="cargo_marking", section="3. Груз", title="Маркировка",
          strategies=(E, A),
          question="Маркировка груза?",
          hint="Номера партий, ярлыки, надписи на местах. Если маркировки нет — так и напишите.",
          why="Схема требует хотя бы одну запись маркировки в описании груза.",
          xsd=f"{GO}/СвГруз/ОпГруз/Марк"),
    Field(key="cargo_declared_value", section="3. Груз", title="Объявленная стоимость",
          strategies=(E, DF, A), parser="money", default=None, required=False,
          why="Если не объявлена — ответственность перевозчика считается по общим правилам.",
          xsd=f"{GO}/СвГруз/ОпГруз/ЦеннГруз/@СтЦеннГр"),
    Field(key="cargo_extra", section="3. Груз", title="Прочие сведения о грузе",
          strategies=(E, DF, A), default="", required=False,
          why="Свободное поле формата: сюда уходят особые условия и температурный режим, "
              "для которых отдельных реквизитов в титуле 1 нет.",
          xsd=f"{GO}/СвГруз/ОпГруз/@ДрОГруз"),

    # ── 4. Сопроводительные документы ────────────────────────────────────────
    Field(key="accompanying_docs", section="4. Сопроводительные документы",
          title="Документы на груз", strategies=(E, DF, A), default=[], required=False,
          question="Какие документы едут с грузом? (счёт, УПД, паспорт качества, сертификат)",
          xsd=f"{GO}/СопрДок/ДокКГр/@НаимДок"),

    # ── 5. Указания грузоотправителя ─────────────────────────────────────────
    Field(key="special_instructions", section="5. Указания грузоотправителя",
          title="Особые условия", strategies=(E, DF, A), default="", required=False,
          why="Уходит в прочие сведения о грузе вместе с температурным режимом: "
              "отдельного реквизита «особые условия» титул 1 не содержит.",
          xsd=f"{GO}/СвГруз/ОпГруз/@ДрОГруз"),
    Field(key="temperature_mode", section="5. Указания грузоотправителя",
          title="Температурный режим", strategies=(E, DF, A), default=None, required=False,
          why="Отдельного реквизита в титуле 1 нет; пишется в прочие сведения о грузе. "
              "Контрольная температура из раздела опасных грузов сюда не годится — "
              "это другой смысл.",
          xsd=f"{GO}/СвГруз/ОпГруз/@ДрОГруз"),
    Field(key="transport_norms", section="5. Указания грузоотправителя",
          title="Указания по нормам перевозки",
          strategies=(DF, E, A), default="Отсутствуют", default_by_format=True,
          question="Особые требования: фитосанитарные, санитарные, карантинные, таможенные?",
          why="Обязательный реквизит. При отсутствии формат прямо предписывает "
              "писать «Отсутствуют» — это не догадка бота, а требование приказа.",
          xsd=f"{GO}/УказГО/@УкНормПрвз"),
    Field(key="redirect_person", section="5. Указания грузоотправителя",
          title="Кто вправе переадресовать", strategies=(DF, A), default="Грузоотправитель",
          question="По чьему указанию возможна переадресовка: грузоотправитель или грузополучатель?",
          why="Обязательный реквизит блока переадресовки.",
          xsd=f"{GO}/УказГО/СвПА/@ЛицоПА"),
    Field(key="redirect_method", section="5. Указания грузоотправителя",
          title="Способ передачи указания о переадресовке",
          strategies=(DF, A), default="Электронное уведомление перевозчика о переадресовке",
          default_by_format=True,
          why="Обязательный реквизит блока переадресовки; формат приводит эту формулировку.",
          xsd=f"{GO}/УказГО/СвПА/@СпосПерУкПА"),
    Field(key="redirect_phone", section="5. Указания грузоотправителя",
          title="Телефон для переадресовки", strategies=(D, DF, A),
          derive_from=("shipper_phone",), parser="phone",
          why="Обязателен по схеме. По умолчанию — телефон грузоотправителя.",
          xsd=f"{GO}/УказГО/СвПА/КонтПА/Тлф"),

    # ── 6. Приём груза ───────────────────────────────────────────────────────
    Field(
        key="pickup_address", section="6. Приём груза", title="Адрес места погрузки",
        strategies=(E, L, A), lookup_by="_route", never_guess=True,
        question="Откуда забираем? Полный адрес погрузки.",
        why="без точного адреса водитель не найдёт место погрузки",
        xsd=f"{GO}/СвПогруз/ФАдресПогр/@АдрКоммент",
    ),
    Field(key="pickup_postcode", section="6. Приём груза", title="Индекс места погрузки",
          strategies=(D, L, E, A), derive_from=("pickup_address",), lookup_by="pickup_address",
          parser="postcode",
          question="Почтовый индекс места погрузки?",
          why="Обязателен по схеме внутри адреса РФ. Бот достаёт его из строки адреса, "
              "если он там есть.",
          xsd=f"{GO}/СвПогруз/ФАдресПогр/АдресРФ/@Индекс"),
    Field(key="pickup_region", section="6. Приём груза", title="Код региона места погрузки",
          strategies=(L, E, A), lookup_by="pickup_address", parser="region",
          question="Код региона места погрузки? (две цифры, как в госномере: 50 — Московская область)",
          why="Обязателен по схеме внутри адреса РФ. Справочника соответствия "
              "«индекс → регион» у бота нет, поэтому спрашиваем, а не угадываем.",
          xsd=f"{GO}/СвПогруз/ФАдресПогр/АдресРФ/@КодРегион"),
    Field(key="pickup_city", section="6. Приём груза", title="Город погрузки",
          strategies=(D, E, A), derive_from=("pickup_address",), required=False,
          xsd=f"{GO}/СвПогруз/ФАдресПогр/АдресРФ/@Город"),
    Field(key="pickup_street", section="6. Приём груза", title="Улица погрузки",
          strategies=(D, E, A), derive_from=("pickup_address",), required=False,
          xsd=f"{GO}/СвПогруз/ФАдресПогр/АдресРФ/@Улица"),
    Field(key="pickup_house", section="6. Приём груза", title="Дом погрузки",
          strategies=(D, E, A), derive_from=("pickup_address",), required=False,
          xsd=f"{GO}/СвПогруз/ФАдресПогр/АдресРФ/@Дом"),
    Field(
        key="pickup_datetime", section="6. Приём груза", title="Дата и время подачи ТС",
        strategies=(E, A), parser="datetime", never_guess=True,
        question="Когда машина под погрузку?",
        hint="Можно так: «завтра к 8», «12.09 14:00», «в понедельник утром».",
        xsd=f"{GO}/СвПогруз/@ЗаявПогр",
    ),
    Field(
        key="pickup_arrival_fact", section="6. Приём груза",
        title="Фактические дата и время прибытия под погрузку",
        strategies=(E, A), parser="datetime", never_guess=True,
        question="Когда машина фактически прибыла на погрузку?",
        hint="Смотрите отметку охраны или журнал въезда.",
        why="схема требует фактическое время: накладная собирается после погрузки, "
            "а не до неё, и факт угадывать нельзя",
        xsd=f"{GO}/СвПогруз/@ФДатВрПриб",
    ),
    Field(
        key="pickup_departure_fact", section="6. Приём груза",
        title="Фактические дата и время убытия",
        strategies=(E, A), parser="datetime", never_guess=True,
        question="Когда машина фактически уехала с погрузки?",
        why="обязательный реквизит титула 1, а факт не подставляется автоматически",
        xsd=f"{GO}/СвПогруз/@ФДатВрУбыт",
    ),
    Field(
        key="loader_is_shipper", section="6. Приём груза",
        title="Погрузку выполняет грузоотправитель?",
        strategies=(DF, A), default="1",
        codes=(("1", "грузили сами"), ("2", "грузило другое лицо")),
        question="Кто грузил: вы сами (1) или другое лицо (2)?",
        why="Обязательный признак формата. Если 2 — схема потребует реквизиты того, "
            "кто грузил; такого сценария бот пока не собирает.",
        xsd=f"{GO}/СвПогруз/СвЛицПогрГр/@СовпГОП",
    ),
    Field(
        key="infra_owner_is_shipper", section="6. Приём груза",
        title="Владелец площадки погрузки — грузоотправитель?",
        strategies=(DF, A), default="1",
        codes=(("1", "площадка наша"), ("2", "площадка чужая")),
        question="Площадка погрузки ваша (1) или чужая (2)?",
        why="Обязательный признак формата.",
        xsd=f"{GO}/СвПогруз/ВладИнфр/@СовпГОВ",
    ),

    # ── 7. Сдача груза ───────────────────────────────────────────────────────
    Field(
        key="delivery_address", section="7. Сдача груза", title="Адрес места выгрузки",
        strategies=(E, L, A), lookup_by="consignee_inn", never_guess=True,
        question="Куда везём? Полный адрес выгрузки.",
        hint="Если совпадает с юридическим адресом получателя — так и напишите.",
        why="без точного адреса выгрузки груз некуда везти",
        xsd=f"{GO}/СвГП/АдресДостГр/@АдрКоммент",
    ),
    Field(key="delivery_postcode", section="7. Сдача груза", title="Индекс места выгрузки",
          strategies=(D, E, L, A), derive_from=("delivery_address",),
          lookup_by="consignee_inn", parser="postcode",
          question="Почтовый индекс места выгрузки?",
          xsd=f"{GO}/СвГП/АдресДостГр/АдресРФ/@Индекс"),
    Field(key="delivery_region", section="7. Сдача груза", title="Код региона места выгрузки",
          strategies=(L, E, A), lookup_by="consignee_inn", parser="region",
          question="Код региона места выгрузки? (две цифры)",
          xsd=f"{GO}/СвГП/АдресДостГр/АдресРФ/@КодРегион"),
    Field(key="delivery_city", section="7. Сдача груза", title="Город выгрузки",
          strategies=(D, E, A), derive_from=("delivery_address",), required=False,
          xsd=f"{GO}/СвГП/АдресДостГр/АдресРФ/@Город"),
    Field(key="delivery_street", section="7. Сдача груза", title="Улица выгрузки",
          strategies=(D, E, A), derive_from=("delivery_address",), required=False,
          xsd=f"{GO}/СвГП/АдресДостГр/АдресРФ/@Улица"),
    Field(key="delivery_house", section="7. Сдача груза", title="Дом выгрузки",
          strategies=(D, E, A), derive_from=("delivery_address",), required=False,
          xsd=f"{GO}/СвГП/АдресДостГр/АдресРФ/@Дом"),
    Field(key="delivery_datetime", section="7. Сдача груза",
          title="Планируемые дата и время доставки",
          strategies=(E, D, A), parser="datetime", derive_from=("pickup_datetime",),
          required=False,
          xsd=f"{GO}/УказГО/@ДатВрДостГр"),
    Field(key="delivery_fact", section="7. Сдача груза", title="Фактическая сдача",
          strategies=(DEF,), defer_to="Титул получателя", required=False,
          why="Фактическую выдачу оформляют получатель и перевозчик своими титулами; "
              "в титуле 1 такого реквизита нет.",
          not_in_xml=True),

    # ── 8. Перевозчик и водитель ─────────────────────────────────────────────
    Field(
        key="carrier_inn", section="8. Перевозчик", title="ИНН перевозчика",
        strategies=(E, L, A), parser="inn", never_guess=True,
        question="ИНН перевозчика?",
        hint="Из договора-заявки на перевозку.",
        xsd=f"{GO}/СвПер/ИдСв/СвЮЛУч/@ИННЮЛ",
    ),
    Field(key="carrier_name", section="8. Перевозчик", title="Наименование перевозчика",
          strategies=(L, E, A), lookup_by="carrier_inn",
          xsd=f"{GO}/СвПер/ИдСв/СвЮЛУч/@НаимОрг"),
    Field(key="carrier_kpp", section="8. Перевозчик", title="КПП перевозчика",
          strategies=(L, E, A), lookup_by="carrier_inn", required=False,
          xsd=f"{GO}/СвПер/ИдСв/СвЮЛУч/@КПП"),
    Field(key="carrier_address", section="8. Перевозчик", title="Юрадрес перевозчика",
          strategies=(L, E, A), lookup_by="carrier_inn", required=False,
          why="Юрадрес перевозчика: схема титула 1 его не требует. Держим для карточки.",
          not_in_xml=True),
    Field(key="carrier_phone", section="8. Перевозчик", title="Телефон перевозчика",
          strategies=(L, E, A), lookup_by="carrier_inn", parser="phone",
          why="Обязателен по схеме.",
          xsd=f"{GO}/СвПер/Контакт/Тлф"),
    Field(key="carrier_edo_id", section="8. Перевозчик", title="Идентификатор участника ЭДО",
          strategies=(L,), lookup_by="carrier_inn",
          hint="Из карточки перевозчика в СБИС; заносится в справочник один раз.",
          why="Идёт в ИМЯ файла обмена (часть A по приказу), а не в тело XML.",
          not_in_xml=True),
    Field(
        key="driver_fio", section="8. Перевозчик", title="ФИО водителя",
        strategies=(E, L, A), lookup_by="driver_license", never_guess=True, parser="fio",
        question="ФИО водителя?",
        hint="Проще прислать фото водительского удостоверения — распознаю сам.",
        xsd=f"{GO}/СвВодит/ФИО/@Фамилия|{GO}/СвВодит/ФИО/@Имя|{GO}/СвВодит/ФИО/@Отчество",
    ),
    Field(
        key="driver_license", section="8. Перевозчик", title="Водительское удостоверение",
        strategies=(E, A), parser="license", never_guess=True,
        question="Серия и номер ВУ?",
        hint="Фото ВУ подойдёт.",
        xsd=f"{GO}/СвВодит/@СерВУ|{GO}/СвВодит/@НомВУ",
    ),
    Field(
        key="driver_phone", section="8. Перевозчик", title="Телефон водителя",
        strategies=(E, L, A), lookup_by="driver_license", parser="phone",
        question="Телефон водителя?",
        why="Обязателен по схеме: без него получатель не найдёт машину.",
        xsd=f"{GO}/СвВодит/Тлф",
    ),

    # ── 8а. Данные водителя для доверенности ─────────────────────────────────
    # В Титул 1 эти поля не идут — там нет паспортных реквизитов. Нужны они для
    # машиночитаемой доверенности, которую водителю выписывают отдельно, и
    # хранятся в справочнике водителей, а не в накладной. Все — never_guess:
    # ошибка в паспортных данных доверенность обнуляет.
    Field(key="driver_birth_date", section="8а. Водитель — для доверенности",
          title="Дата рождения водителя", strategies=(E, L, A), parser="date",
          lookup_by="driver_license", required=False, never_guess=True,
          why="нужна для доверенности, в накладную не идёт",
          not_in_xml=True),
    Field(key="driver_passport", section="8а. Водитель — для доверенности",
          title="Паспорт водителя (серия и номер)", strategies=(E, L, A),
          lookup_by="driver_license", required=False, never_guess=True,
          why="нужен для доверенности; в Титуле 1 паспортных реквизитов нет",
          not_in_xml=True),
    Field(key="driver_passport_issued_by", section="8а. Водитель — для доверенности",
          title="Кем выдан паспорт", strategies=(E, L, A),
          lookup_by="driver_license", required=False, never_guess=True,
          why="реквизит доверенности",
          not_in_xml=True),
    Field(key="driver_passport_date", section="8а. Водитель — для доверенности",
          title="Дата выдачи паспорта", strategies=(E, L, A), parser="date",
          lookup_by="driver_license", required=False, never_guess=True,
          why="реквизит доверенности",
          not_in_xml=True),
    Field(key="driver_passport_subdivision", section="8а. Водитель — для доверенности",
          title="Код подразделения", strategies=(E, L, A),
          lookup_by="driver_license", required=False, never_guess=True,
          why="реквизит доверенности",
          not_in_xml=True),

    # ── 9. Транспортное средство ─────────────────────────────────────────────
    Field(
        key="vehicle_plate", section="9. Транспортное средство", title="Госномер ТС",
        strategies=(E, A), parser="plate", never_guess=True,
        question="Госномер машины?",
        hint="Формат любой: «а123вс77», «А123ВС777».",
        why="по номеру ГИБДД сверяет накладную с машиной на дороге",
        xsd=f"{GO}/СвТС/ТС/@РегНомер",
    ),
    Field(key="vehicle_type", section="9. Транспортное средство", title="Тип ТС",
          strategies=(E, L, DF, A), lookup_by="vehicle_plate", default="грузовой тягач седельный",
          xsd=f"{GO}/СвТС/ТС/ПарТС/@Тип"),
    Field(key="vehicle_brand", section="9. Транспортное средство", title="Марка, модель",
          strategies=(E, L, A), lookup_by="vehicle_plate",
          xsd=f"{GO}/СвТС/ТС/ПарТС/@Марка"),
    Field(key="vehicle_capacity_t", section="9. Транспортное средство",
          title="Грузоподъёмность, т",
          strategies=(E, L, A), lookup_by="vehicle_plate", parser="mass",
          question="Грузоподъёмность машины в тоннах?",
          hint="Из ПТС или СТС. Это не разрешённая максимальная масса — "
               "грузоподъёмность меньше на массу самой машины.",
          why="Обязательный реквизит формата, в тоннах.",
          xsd=f"{GO}/СвТС/ТС/ПарТС/@Грузопод"),
    Field(key="vehicle_volume_m3", section="9. Транспортное средство",
          title="Вместимость, м³",
          strategies=(E, L, A), lookup_by="vehicle_plate", parser="volume",
          question="Вместимость кузова или полуприцепа в кубометрах?",
          why="Обязательный реквизит формата.",
          xsd=f"{GO}/СвТС/ТС/ПарТС/@Вместим"),
    Field(key="vehicle_max_mass", section="9. Транспортное средство",
          title="Разрешённая максимальная масса, кг",
          strategies=(E, L, A), lookup_by="vehicle_plate", parser="mass", required=False,
          why="Титул 1 требует грузоподъёмность и вместимость, а РММ не содержит. "
              "Держим для контроля перегруза при показе карточки.",
          not_in_xml=True),
    Field(
        key="vehicle_ownership", section="9. Транспортное средство", title="Тип владения",
        strategies=(L, DF, A), lookup_by="carrier_inn", default="3",
        codes=(("1", "собственность"), ("2", "совместная собственность супругов"),
               ("3", "аренда"), ("4", "лизинг"), ("5", "безвозмездное пользование")),
        question="ТС у перевозчика: 1 — собственность, 2 — совместная собственность супругов, "
                 "3 — аренда, 4 — лизинг, 5 — безвозмездное пользование?",
        why="Обязательный код формата. По умолчанию 3 (аренда) — самый частый случай "
            "для наёмного транспорта.",
        xsd=f"{GO}/СвТС/ТС/@ТипВлад",
    ),
    Field(key="vehicle_sts", section="9. Транспортное средство", title="Номер СТС",
          strategies=(E, L, A), lookup_by="vehicle_plate", required=False,
          xsd=f"{GO}/СвТС/ТС/@НомСТС"),
    Field(key="trailer_plate", section="9. Транспортное средство", title="Госномер прицепа",
          strategies=(E, A), parser="plate", required=False,
          xsd=f"{GO}/СвТС/Прицеп/@РегНомер"),

    # ── 10. Подписант ────────────────────────────────────────────────────────
    Field(
        key="signer_fio", section="10. Подписант", title="ФИО подписанта",
        strategies=(DF, A), parser="fio",
        question="Кто подписывает накладную? ФИО полностью.",
        why="Схема требует фамилию и имя подписанта. Отчество — при наличии.",
        xsd="/Файл/Документ/Подписант/ФИО/@Фамилия"
            "|/Файл/Документ/Подписант/ФИО/@Имя"
            "|/Файл/Документ/Подписант/ФИО/@Отчество",
    ),
    Field(
        key="signer_status", section="10. Подписант", title="Статус подписанта",
        strategies=(DF, A), default="1",
        codes=(("1", "без доверенности"), ("2", "электронная доверенность"),
               ("3", "бумажная доверенность"), ("4", "погрузчик без доверенности"),
               ("5", "погрузчик, электронная доверенность"),
               ("6", "погрузчик, бумажная доверенность")),
        question="На каком основании подписывает: 1 — без доверенности, "
                 "2 — электронная доверенность, 3 — бумажная доверенность?",
        why="Обязательный код формата. Значения 4–6 — для лица, осуществляющего погрузку.",
        xsd="/Файл/Документ/Подписант/@СтатПодп",
    ),
    Field(key="signer_position", section="10. Подписант", title="Должность подписанта",
          strategies=(DF, A), required=False,
          xsd="/Файл/Документ/Подписант/@Должн"),

    # ── 13. Заказ (заявка) ───────────────────────────────────────────────────
    Field(key="order_number", section="13. Заказ (заявка)", title="Номер заявки",
          strategies=(E, A),
          question="Номер заявки на перевозку?",
          why="Обязательный реквизит титула 1.",
          xsd=f"{GO}/@НомЗак"),
    Field(key="order_date", section="13. Заказ (заявка)", title="Дата заявки",
          strategies=(E, A), parser="datetime",
          question="Дата заявки?",
          why="Обязательный реквизит титула 1.",
          xsd=f"{GO}/@ДатаЗак"),

    # ── 15. Стоимость перевозки ──────────────────────────────────────────────
    Field(key="carriage_cost", section="15. Стоимость перевозки", title="Стоимость услуг",
          strategies=(DEF,), defer_to="Титул перевозчика", required=False,
          why="Этот раздел заполняет перевозчик, не отправитель: стоимости перевозки "
              "в титуле 1 нет вообще.",
          not_in_xml=True),
)


# ─────────────────────────────────────────────────────────────────────────────
# Заявка перевозчику
# ─────────────────────────────────────────────────────────────────────────────
# Заявка — коммерческий документ, госформата у неё нет. Но она идёт ПЕРЕД
# накладной: сначала заказали машину, потом выписали ЭТрН. Поэтому заявка —
# главный источник данных для Титула 1, и если бот сформировал её сам,
# распознавать потом почти нечего.

# Поля, общие для заявки и накладной.
_SHARED = {
    "doc_number", "doc_date",
    "shipper_name", "shipper_inn", "shipper_address", "shipper_phone", "shipper_contact",
    "consignee_name", "consignee_inn", "consignee_address",
    "cargo_name", "cargo_places", "cargo_package", "cargo_mass_gross_kg",
    "cargo_volume_m3", "cargo_declared_value",
    "pickup_address", "pickup_datetime",
    "delivery_address", "delivery_datetime",
    "carrier_inn", "carrier_name", "carrier_address", "carrier_phone",
    "special_instructions", "temperature_mode",
    # Идентификатор ЭДО перевозчика в XML заявки не пишется, но без него не собрать
    # имя файла обмена (часть A). Без этой строки бот его не искал в справочнике,
    # и заявка падала на сборке имени, хотя перевозчик в справочнике был.
    "carrier_edo_id",
}

Z = ("zayavka",)
P = ("poruchenie",)

# Корень содержания заявки по формату ФНС (приказ ЕД-7-26/108@, титул
# грузоотправителя ON_ZAKZVGO, КНД 1110361). Имя корня совпадает с ЭТрН, дети — нет.
ZG = "/Файл/Документ/СодИнфГО"
ZD = "/Файл/Документ"

_ZAYAVKA_ONLY: tuple[Field, ...] = (
    Field(
        key="req_vehicle_type", section="Требования к транспорту", title="Тип кузова",
        strategies=(L, DF, A), lookup_by="consignee_inn", default="тент",
        docs=Z, question="Какой нужен кузов?",
        hint="тент / рефрижератор / изотерм / открытый борт / контейнеровоз",
        why="Перевозчик подбирает машину по типу кузова. Ошибка здесь — сорванная погрузка.",
    ),
    Field(
        key="req_capacity_t", section="Требования к транспорту", title="Грузоподъёмность, т",
        strategies=(D, DF, A), derive_from=("cargo_mass_gross_kg",), default=20,
        docs=Z, parser="mass",
        question="Какая нужна грузоподъёмность?",
        why="Считаю от массы груза с запасом. Можно поправить.",
    ),
    Field(
        key="req_volume_m3", section="Требования к транспорту", title="Объём кузова, м³",
        strategies=(DF, A), default=82, docs=Z, parser="volume", required=False,
        why="82 м³ — стандартная еврофура. Для нестандартного груза укажите своё.",
        routine=True,
    ),
    Field(
        key="loading_type", section="Требования к транспорту", title="Тип загрузки",
        strategies=(L, DF, A), lookup_by="_route", default="задняя",
        docs=Z, required=False,
        question="Как грузим?", hint="задняя / боковая / верхняя / полная растентовка",
        routine=True,
    ),
    Field(
        key="rate", section="Оплата", title="Ставка за перевозку",
        strategies=(A,), parser="money", docs=Z, never_guess=True,
        question="Ставка за рейс?",
        hint="Можно «85 тыс», «85000», «85 т.р.»",
        why="это цена договора — её называет только человек",
    ),
    Field(
        key="rate_vat", section="Оплата", title="НДС",
        strategies=(DF, A), default="без НДС", docs=Z,
        question="Ставка с НДС или без?",
        routine=True,
    ),
    Field(
        key="payment_terms", section="Оплата", title="Условия оплаты",
        strategies=(L, DF, A), lookup_by="carrier_inn",
        default="безналичный расчёт в течение 10 банковских дней "
                "с даты получения оригиналов документов",
        docs=Z,
        why="Стандартная формулировка из вашей политики. Меняется в настройках.",
        routine=True,
    ),
    Field(
        key="pickup_contact", section="Контакты", title="Контакт на погрузке",
        strategies=(DF, L, A), lookup_by="_route", docs=Z, required=False,
        question="Кто встречает на погрузке? ФИО и телефон.",
    ),
    Field(
        key="delivery_contact", section="Контакты", title="Контакт на выгрузке",
        strategies=(L, A), lookup_by="consignee_inn", docs=Z, required=False,
        question="Кто принимает на выгрузке? ФИО и телефон.",
    ),
    Field(
        key="order_kind", section="Заявка: вид документа", title="Заказ или заявка",
        strategies=(L, A), lookup_by="carrier_inn", parser="order_kind", docs=Z,
        codes=(("Заказ", "договора об организации перевозок с перевозчиком нет"),
               ("Заявка", "есть договор об организации перевозок")),
        question="С этим перевозчиком есть договор об организации перевозок? "
                 "(есть — это «Заявка», нет — «Заказ»)",
        why="так по Уставу автотранспорта называется документ, и от этого зависит, "
            "как заключён договор перевозки",
        never_guess=True,
    ),
    Field(
        key="cargo_dims", section="3. Груз", title="Габариты грузового места, м",
        strategies=(E, L, A), lookup_by="cargo_name", parser="dims", docs=Z,
        question="Габариты одного грузового места? Длина × ширина × высота.",
        hint="Как удобно: «120×80×150 см», «1,2 × 0,8 × 1,5». Паллета 120×80 — "
             "длина и ширина, третье число — высота.",
        why="Обязательный реквизит заявки: по нему перевозчик подбирает кузов.",
    ),
    Field(
        key="cargo_distributable", section="3. Груз",
        title="Груз можно распределить по длине платформы", strategies=(L, DF, A),
        default="0", lookup_by="cargo_name", docs=Z, routine=True,
        codes=(("0", "можно распределить"), ("1", "распределить нельзя")),
        why="Обязательный признак заявки. Паллетированный груз обычно распределяется.",
    ),
    Field(
        key="cargo_divisible", section="3. Груз", title="Делимость груза",
        strategies=(L, DF, A), default="1", lookup_by="cargo_name", docs=Z, routine=True,
        codes=(("0", "неделимый"), ("1", "делимый")),
        why="Обязательный признак заявки. Груз из многих мест обычно делимый.",
    ),
    Field(
        key="food_transport", section="Заявка: условия", title="Перевозка пищевых продуктов",
        strategies=(DF, A), default="Отсутствуют", default_by_format=True, docs=Z,
        why="Обязательный реквизит заявки; при отсутствии формат предписывает "
            "писать «Отсутствуют».",
    ),
    Field(
        key="signer_authority", section="10. Подписант",
        title="Способ подтверждения полномочий подписанта", strategies=(DF, A),
        default="1", docs=Z, routine=True,
        codes=(("1", "по данным электронной подписи"),
               ("2", "электронная доверенность в пакете документов"),
               ("3", "электронная доверенность из информационной системы"),
               ("4", "электронная доверенность из информационной системы, по ссылке"),
               ("5", "доверенность на бумаге")),
        why="Обязательный код заявки и поручения. 1 — подписывает руководитель по "
            "своей электронной подписи.",
    ),
    # ── Поручение экспедитору (приказ ФНС ЕД-1-26/277@, титул клиента) ─────
    Field(key="forwarder_inn", section="Экспедитор", title="ИНН экспедитора",
          strategies=(E, L, A), parser="inn", never_guess=True, docs=P,
          question="ИНН экспедитора?", hint="Из договора транспортной экспедиции.",
          why="без верного ИНН поручение не дойдёт до экспедитора в ЭДО"),
    Field(key="forwarder_name", section="Экспедитор", title="Наименование экспедитора",
          strategies=(L, E, A), lookup_by="forwarder_inn", docs=P),
    Field(key="forwarder_kpp", section="Экспедитор", title="КПП экспедитора",
          strategies=(L, E, A), lookup_by="forwarder_inn", required=False, docs=P),
    Field(key="forwarder_edo_id", section="Экспедитор", title="Идентификатор ЭДО экспедитора",
          strategies=(L,), lookup_by="forwarder_inn", docs=P,
          hint="Из карточки экспедитора в Saby; заносится в справочник один раз.",
          why="Идёт в ИМЯ файла обмена поручения (часть A), а не в тело XML."),
    Field(key="forwarder_contract_name", section="Экспедитор",
          title="Договор экспедиции: наименование", strategies=(L, DF, A),
          lookup_by="forwarder_inn", default="Договор транспортной экспедиции",
          docs=P, routine=True,
          why="Обязательный реквизит поручения. Как называется ваш договор с экспедитором."),
    Field(key="forwarder_contract_number", section="Экспедитор",
          title="Договор экспедиции: номер", strategies=(L, E, A),
          lookup_by="forwarder_inn", never_guess=True, docs=P,
          question="Номер договора транспортной экспедиции с этим экспедитором?",
          hint="Если номера нет — напишите «Без номера», так требует формат. "
               "Спрошу один раз, дальше запомню.",
          why="поручение даётся по договору, без его реквизитов формат не принимает файл"),
    Field(key="forwarder_contract_date", section="Экспедитор",
          title="Договор экспедиции: дата", strategies=(L, E, A), parser="date",
          lookup_by="forwarder_inn", never_guess=True, docs=P,
          question="Дата договора транспортной экспедиции?",
          why="обязательный реквизит договора в поручении"),
    Field(key="vehicle_kind", section="Поручение: груз", title="Вид транспорта",
          strategies=(DF, A), default="1", docs=P, routine=True,
          codes=(("1", "автомобильный"), ("2", "внутренний водный"), ("3", "воздушный"),
                 ("4", "железнодорожный"), ("5", "морской")),
          why="Обязательный код поручения."),
    # Без поиска по справочнику: вес у каждого рейса свой. С запоминанием
    # ответов привязка к грузу означала бы тихую подстановку веса прошлого рейса.
    Field(key="cargo_net_kg", section="3. Груз", title="Вес нетто, кг",
          strategies=(E, A), parser="mass", never_guess=True, docs=P,
          question="Вес нетто груза?",
          hint="Без упаковки и тары. Можно «19,5 т», «19500 кг».",
          why="обязательный реквизит поручения, а нетто из брутто не вычислить"),
    Field(key="cargo_origin_code", section="Поручение: груз",
          title="Страна происхождения груза: код", strategies=(L, DF, A),
          lookup_by="cargo_name", default="643", docs=P, routine=True,
          codes=(("643", "Россия"),),
          why="Обязательный реквизит поручения, код по ОКСМ. По умолчанию 643 — Россия."),
    Field(key="cargo_origin_name", section="Поручение: груз",
          title="Страна происхождения груза", strategies=(L, DF, A),
          lookup_by="cargo_name", default="Россия", docs=P, routine=True,
          why="Обязательный реквизит поручения, наименование по ОКСМ."),
    Field(key="cargo_dangerous", section="Поручение: груз", title="Опасный груз",
          strategies=(DF, A), default="0", docs=P, routine=True,
          codes=(("0", "нет"), ("1", "есть опасный груз")),
          why="Обязательный признак поручения."),
    Field(key="cargo_restricted", section="Поручение: груз",
          title="Изъятые или ограниченно оборотоспособные вещества",
          strategies=(DF, A), default="0", docs=P, routine=True,
          codes=(("0", "нет"), ("1", "есть")), why="Обязательный признак поручения."),
    Field(key="cargo_gov_tracked", section="Поручение: груз",
          title="Товар, подлежащий учёту в госсистеме", strategies=(DF, A),
          default="0", docs=P, routine=True,
          codes=(("0", "нет"), ("1", "есть"), ("2", "есть, сведения переданы")),
          why="Обязательный признак поручения."),
    Field(key="cargo_has_tnved", section="Поручение: груз", title="Есть код ТН ВЭД",
          strategies=(DF, A), default="0", docs=P, routine=True,
          codes=(("0", "нет"), ("1", "есть")), why="Обязательный признак поручения."),
    Field(key="notify_parties", section="Поручение: груз",
          title="Уведомить стороны о прибытии груза", strategies=(DF, A),
          default="0", docs=P, routine=True,
          codes=(("0", "не уведомлять"), ("1", "уведомить")),
          why="Обязательный признак поручения."),
    Field(key="container_kind", section="Поручение: груз", title="Контейнер",
          strategies=(DF, A), default="0", docs=P, routine=True,
          codes=(("0", "без контейнера"), ("1", "контейнер гружёный"),
                 ("2", "контейнер порожний как груз")),
          why="Обязательный признак поручения."),
    Field(key="cargo_packed", section="Поручение: груз", title="Наличие упаковки",
          strategies=(D,), derive_from=("cargo_package",), docs=P,
          codes=(("0", "упаковки нет"), ("1", "есть упаковка")),
          why="Вычисляется из способа упаковки."),
    Field(key="cargo_volume_known", section="Поручение: груз", title="Объём груза определён",
          strategies=(D,), derive_from=("cargo_volume_m3",), docs=P,
          codes=(("0", "не определён"), ("1", "определён")),
          why="Вычисляется: есть объём — «1», нет — «0»."),
    Field(key="cargo_docs_present", section="Поручение: груз",
          title="Документы для перевозки груза", strategies=(D,),
          derive_from=("accompanying_docs",), docs=P,
          codes=(("0", "отсутствуют"), ("1", "присутствуют")),
          why="Вычисляется из списка документов на груз."),
    Field(key="ship_from", section="Поручение: груз", title="Груз готов к отгрузке: с",
          strategies=(D, E, A), derive_from=("pickup_datetime",), parser="date", docs=P,
          why="По умолчанию — дата подачи машины."),
    Field(key="ship_to", section="Поручение: груз", title="Груз готов к отгрузке: по",
          strategies=(D, E, A), derive_from=("pickup_datetime",), parser="date", docs=P,
          why="По умолчанию — дата подачи машины."),
    Field(key="pickup_region_name", section="6. Приём груза",
          title="Регион места погрузки (наименование)", strategies=(L, E, A),
          lookup_by="pickup_address", docs=P,
          question="Регион места погрузки словами? Например «Московская область».",
          why="В поручении формат требует и код, и наименование региона. Спрошу один "
              "раз для этого адреса."),
    Field(key="delivery_region_name", section="7. Сдача груза",
          title="Регион места выгрузки (наименование)", strategies=(L, E, A),
          lookup_by="consignee_inn", docs=P,
          question="Регион места выгрузки словами? Например «г. Москва».",
          why="В поручении формат требует и код, и наименование региона."),
    Field(
        key="free_hours", section="Простой", title="Норма погрузки/выгрузки, ч",
        strategies=(DF, A), default=24, docs=Z, parser="int", required=False,
        why="Сколько часов простоя входит в ставку. Дальше — платно.",
        routine=True,
    ),
    Field(
        key="demurrage", section="Простой", title="Стоимость простоя, ₽/сутки",
        strategies=(DF, A), default=5000, docs=Z, parser="money", required=False,
        routine=True,
    ),
)


# Куда поле попадает в XML заявки. Отдельной таблицей, а не в каждом Field:
# так видно весь маппинг формата сразу и сверка идёт по одному месту.
# Поля без строки здесь в XML заявки не идут (грузополучателя в формате
# заявки нет вообще) — они остаются в карточке и черновике Word.
_ZAYAVKA_PATHS: dict[str, str] = {
    "doc_number": f"{ZG}/@НомЗак",
    "doc_date": f"{ZG}/@ДатаЗак",
    "shipper_inn": f"{ZG}/СвГО/ИдСв/СвЮЛУч/@ИННЮЛ",
    "shipper_name": f"{ZG}/СвГО/ИдСв/СвЮЛУч/@НаимОрг",
    "shipper_kpp": f"{ZG}/СвГО/ИдСв/СвЮЛУч/@КПП",
    "shipper_phone": f"{ZG}/СвГО/Конт/Тлф",
    "carrier_inn": f"{ZG}/СвПрв/ИдСв/СвЮЛУч/@ИННЮЛ",
    "carrier_name": f"{ZG}/СвПрв/ИдСв/СвЮЛУч/@НаимОрг",
    "carrier_kpp": f"{ZG}/СвПрв/ИдСв/СвЮЛУч/@КПП",
    "carrier_phone": f"{ZG}/СвПрв/Конт/Тлф",
    "pickup_address": f"{ZG}/ПунктПод/АдрПунктПод/@АдрКоммент",
    "pickup_postcode": f"{ZG}/ПунктПод/АдрПунктПод/Адрес/АдрРФ/@Индекс",
    "pickup_region": f"{ZG}/ПунктПод/АдрПунктПод/Адрес/АдрРФ/@КодРегион",
    "pickup_city": f"{ZG}/ПунктПод/АдрПунктПод/Адрес/АдрРФ/@Город",
    "pickup_street": f"{ZG}/ПунктПод/АдрПунктПод/Адрес/АдрРФ/@Улица",
    "pickup_house": f"{ZG}/ПунктПод/АдрПунктПод/Адрес/АдрРФ/@Дом",
    "pickup_datetime": f"{ZG}/ПунктПод/@ДатВрПод",
    "delivery_address": f"{ZG}/АдрПункт/АдресПункт/@АдрКоммент",
    "delivery_postcode": f"{ZG}/АдрПункт/АдресПункт/Адрес/АдрРФ/@Индекс",
    "delivery_region": f"{ZG}/АдрПункт/АдресПункт/Адрес/АдрРФ/@КодРегион",
    "delivery_city": f"{ZG}/АдрПункт/АдресПункт/Адрес/АдрРФ/@Город",
    "delivery_street": f"{ZG}/АдрПункт/АдресПункт/Адрес/АдрРФ/@Улица",
    "delivery_house": f"{ZG}/АдрПункт/АдресПункт/Адрес/АдрРФ/@Дом",
    "delivery_datetime": f"{ZG}/АдрПункт/@ДатВрОпер",
    "cargo_name": f"{ZG}/ОпГруз/@НаимГруз",
    "cargo_state": f"{ZG}/ОпГруз/@СостГруз",
    "cargo_package_code": f"{ZG}/ОпГруз/@ВидТар",
    "cargo_places": f"{ZG}/ОпГруз/@КолГрМест",
    "mass_method": f"{ZG}/ОпГруз/@МетОпрМасс",
    "cargo_volume_m3": f"{ZG}/ОпГруз/@Объем",
    "cargo_mass_gross_kg": f"{ZG}/ОпГруз/МасГруз/@МасБрутЗнач",
    "cargo_dims": f"{ZG}/ОпГруз/РазмерГрМест/@ДлЗнач|{ZG}/ОпГруз/РазмерГрМест/@ШирЗнач"
                  f"|{ZG}/ОпГруз/РазмерГрМест/@ВысЗнач",
    "cargo_distributable": f"{ZG}/ОпГруз/@РаспрГр",
    "cargo_divisible": f"{ZG}/ОпГруз/@ДелГр",
    "cargo_extra": f"{ZG}/ОпГруз/@ДрОГруз",
    "special_instructions": f"{ZG}/ОпГруз/@ДрОГруз",
    "transport_norms": f"{ZG}/@УкНормПрвз",
    "food_transport": f"{ZG}/@ПрвзПищПрод",
    "req_vehicle_type": f"{ZG}/ПарТСПрвз/@Тип",
    "req_capacity_t": f"{ZG}/ПарТСПрвз/@Грузопод",
    "req_volume_m3": f"{ZG}/ПарТСПрвз/@Вместим",
    "order_kind": f"{ZD}/@Функция",
    "signer_fio": f"{ZD}/ПодпИнфГО/ФИО/@Фамилия|{ZD}/ПодпИнфГО/ФИО/@Имя"
                  f"|{ZD}/ПодпИнфГО/ФИО/@Отчество",
    "signer_position": f"{ZD}/ПодпИнфГО/@Должн",
    "signer_authority": f"{ZD}/ПодпИнфГО/@СпосПодтПолном",
}

# Куда поле попадает в XML поручения экспедитору (титул клиента ON_POREXPKLT).
PK = "/Файл/Документ/СодИнфКлнт"
PG = PK + "/СвГруз"
_PORUCH_PATHS: dict[str, str] = {
    "doc_number": f"{PK}/@НомДок",
    "doc_date": f"{PK}/@ДатаДок",
    "shipper_inn": f"{PK}/СвКлнт/ИдСв/СвЮЛУч/@ИННЮЛ|{PG}/СвГО/ИдСв/СвЮЛУч/@ИННЮЛ",
    "shipper_name": f"{PK}/СвКлнт/ИдСв/СвЮЛУч/@НаимОрг|{PG}/СвГО/ИдСв/СвЮЛУч/@НаимОрг",
    "shipper_kpp": f"{PK}/СвКлнт/ИдСв/СвЮЛУч/@КПП|{PG}/СвГО/ИдСв/СвЮЛУч/@КПП",
    "consignee_inn": f"{PG}/СвГП/ИдСв/СвЮЛУч/@ИННЮЛ",
    "consignee_name": f"{PG}/СвГП/ИдСв/СвЮЛУч/@НаимОрг",
    "consignee_kpp": f"{PG}/СвГП/ИдСв/СвЮЛУч/@КПП",
    "forwarder_inn": f"{PK}/СвЭксп/ИдСв/СвЮЛУч/@ИННЮЛ",
    "forwarder_name": f"{PK}/СвЭксп/ИдСв/СвЮЛУч/@НаимОрг",
    "forwarder_kpp": f"{PK}/СвЭксп/ИдСв/СвЮЛУч/@КПП",
    "forwarder_contract_name": f"{PK}/ДогТрЭксп/@РеквНаимДок",
    "forwarder_contract_number": f"{PK}/ДогТрЭксп/@РеквНомерДок",
    "forwarder_contract_date": f"{PK}/ДогТрЭксп/@РеквДатаДок",
    "cargo_docs_present": f"{PK}/@ДокГруз",
    "vehicle_kind": f"{PG}/СвТС/@ВидТС",
    "cargo_marking": f"{PG}/ОпГруз/Марк",
    "cargo_origin_code": f"{PG}/ОпГруз/СвСтрПроисх/@КодСтр",
    "cargo_origin_name": f"{PG}/ОпГруз/СвСтрПроисх/@НаимСтран",
    "cargo_net_kg": f"{PG}/ОпГруз/ВесГруз/@ВесНетто",
    "cargo_mass_gross_kg": f"{PG}/ОпГруз/ВесГруз/@ВесБрутто",
    "cargo_name": f"{PG}/ОпГруз/@НаимГруз",
    "cargo_places": f"{PG}/ОпГруз/@КолМестГр",
    "cargo_dangerous": f"{PG}/ОпГруз/@ПрзОпаснГруз",
    "cargo_restricted": f"{PG}/ОпГруз/@ПрзИзъятВещ",
    "cargo_volume_known": f"{PG}/ОпГруз/@УказОбъем",
    "cargo_volume_m3": f"{PG}/ОпГруз/@Объем",
    "cargo_gov_tracked": f"{PG}/ОпГруз/@УчГосСист",
    "cargo_packed": f"{PG}/ОпГруз/@НалУпак",
    "cargo_has_tnved": f"{PG}/ОпГруз/@НалКодТовНом",
    "notify_parties": f"{PG}/@ПрУведСторон",
    "ship_from": f"{PG}/@ДатОтгрС",
    "ship_to": f"{PG}/@ДатОтгрПо",
    "container_kind": f"{PG}/@ПрзПрвз",
    "pickup_address": f"{PG}/АдрГруз/АдрПриемГруз/@АдрКоммент",
    "pickup_region": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@КодРегион",
    "pickup_region_name": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@НаимРегион",
    "pickup_postcode": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@Индекс",
    "pickup_city": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@Город",
    "pickup_street": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@Улица",
    "pickup_house": f"{PG}/АдрГруз/АдрПриемГруз/Адрес/АдрРФ/@Дом",
    "delivery_address": f"{PG}/АдрПунктНазн/АдрВыдГруз/@АдрКоммент",
    "delivery_region": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@КодРегион",
    "delivery_region_name": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@НаимРегион",
    "delivery_postcode": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@Индекс",
    "delivery_city": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@Город",
    "delivery_street": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@Улица",
    "delivery_house": f"{PG}/АдрПунктНазн/АдрВыдГруз/Адрес/АдрРФ/@Дом",
    "signer_fio": f"{ZD}/ПодпИнфКлнт/ФИО/@Фамилия|{ZD}/ПодпИнфКлнт/ФИО/@Имя"
                  f"|{ZD}/ПодпИнфКлнт/ФИО/@Отчество",
    "signer_position": f"{ZD}/ПодпИнфКлнт/@Должн",
    "signer_authority": f"{ZD}/ПодпИнфКлнт/@СпосПодтПолном",
}
# Нужны поручению без своего места в XML: источники вычислений и имя файла.
_PORUCH_EXTRA = {"pickup_datetime", "cargo_package", "accompanying_docs",
                 "forwarder_edo_id", "shipper_edo_id"}
_PORUCH_REQUIRED = {"pickup_region", "delivery_region", "signer_fio", "signer_authority"}

# Обязательны в заявке, хотя в накладной — нет.
_ZAYAVKA_REQUIRED = {"cargo_volume_m3", "signer_position", "req_volume_m3",
                     "cargo_state", "cargo_package_code", "mass_method",
                     "pickup_postcode", "pickup_region", "delivery_postcode",
                     "delivery_region", "transport_norms", "signer_fio",
                     "shipper_phone", "carrier_phone"}


def _with_docs(f: Field) -> Field:
    from dataclasses import replace
    docs = list(f.docs)
    if (f.key in _SHARED or f.key in _ZAYAVKA_PATHS) and "zayavka" not in docs:
        docs.append("zayavka")
    if (f.key in _PORUCH_PATHS or f.key in _PORUCH_EXTRA) and "poruchenie" not in docs:
        docs.append("poruchenie")
    paths = dict(f.paths)
    if f.key in _ZAYAVKA_PATHS:
        paths["zayavka"] = _ZAYAVKA_PATHS[f.key]
    if f.key in _PORUCH_PATHS:
        paths["poruchenie"] = _PORUCH_PATHS[f.key]
    required_in = set(f.required_in)
    if f.key in _ZAYAVKA_REQUIRED:
        required_in.add("zayavka")
    if f.key in _PORUCH_REQUIRED:
        required_in.add("poruchenie")
    required_in = tuple(sorted(required_in))
    return replace(f, docs=tuple(docs), paths=tuple(paths.items()), required_in=required_in)


REGISTRY: tuple[Field, ...] = tuple(_with_docs(f) for f in _ETRN_BASE + _ZAYAVKA_ONLY)

ETRN, ZAYAVKA, PORUCHENIE = "etrn", "zayavka", "poruchenie"

DOC_TITLES = {
    ETRN: "Транспортная накладная (Титул 1)",
    ZAYAVKA: "Заявка перевозчику",
    PORUCHENIE: "Поручение экспедитору",
}


def registry_for(doc_type: str) -> tuple[Field, ...]:
    return tuple(f for f in REGISTRY if doc_type in f.docs)


BY_KEY: dict[str, Field] = {f.key: f for f in REGISTRY}


def required_keys(doc_type: str = ETRN) -> list[str]:
    return [f.key for f in registry_for(doc_type) if f.required_for(doc_type)]


def sections(doc_type: str = ETRN) -> list[str]:
    seen: list[str] = []
    for f in registry_for(doc_type):
        if f.section not in seen:
            seen.append(f.section)
    return seen
