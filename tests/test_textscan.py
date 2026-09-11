"""
Разбор текста документа по подписям.

Это основной путь распознавания при слабой модели: она переписывает текст,
реквизиты достаёт этот код. Поэтому тесты смотрят не только «нашлось ли», но и
что НЕ нашлось лишнего и что уверенность честная.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from etrn_bot.fields import BY_KEY
from etrn_bot.textscan import scan

ZAYAVKA = """ЗАЯВКА № 418 на перевозку груза
от 08 сентября 2026 г.
Грузоотправитель: ООО "НАША КОМПАНИЯ", ИНН 5001000001
Адрес погрузки: 141400, Химки, ул. Ленина, д. 1, склад 3
Грузополучатель: ПАО СБЕРБАНК, ИНН 7707083893
Адрес выгрузки: 117997, Москва, ул. Вавилова, д. 19
Перевозчик: ООО "ВЕЗЁМ", ИНН 7736050003
СВЕДЕНИЯ О ГРУЗЕ
Наименование груза: Сера гранулированная
Масса брутто: 20 000 кг
Количество мест: 40
Упаковка: паллеты
Маркировка: партия 12/09
ТРАНСПОРТ
Автомобиль: Volvo FH, гос. номер А123ВС777
Водитель: Петров Иван Сергеевич
Вод. удостоверение: 9912 345678
Телефон водителя: +7 916 000-11-22
Дата и время подачи ТС под погрузку: 10.09.2026 08:00
Факт. прибытие: 10.09.2026 07:50   Факт. убытие: 10.09.2026 09:30"""


@pytest.fixture(scope="module")
def found():
    return scan(ZAYAVKA, "заявка №418")


def test_all_keys_belong_to_registry(found):
    """Разбор не имеет права придумать поле, которого нет в реестре."""
    unknown = [k for k in found if k not in BY_KEY]
    assert unknown == []


@pytest.mark.parametrize("key,expected", [
    ("order_number", "418"),
    ("cargo_name", "Сера гранулированная"),
    ("cargo_mass_gross_kg", 20000),
    ("cargo_places", 40),
    ("cargo_package", "паллеты"),
    ("cargo_marking", "партия 12/09"),
    ("consignee_inn", "7707083893"),
    ("carrier_inn", "7736050003"),
    ("vehicle_plate", "А123ВС777"),
    ("driver_license", "9912 345678"),
    ("driver_phone", "+7 (916) 000-11-22"),
    ("driver_fio", ("Петров", "Иван", "Сергеевич")),
])
def test_values_from_labels(found, key, expected):
    assert key in found, f"не нашли {key}"
    assert found[key][0] == expected


def test_two_values_in_one_line_do_not_mix(found):
    """«Факт. прибытие: … Факт. убытие: …» — одна строка, два разных поля."""
    assert found["pickup_arrival_fact"][0] == datetime(2026, 9, 10, 7, 50)
    assert found["pickup_departure_fact"][0] == datetime(2026, 9, 10, 9, 30)


def test_org_names_cut_before_inn(found):
    assert found["consignee_name"][0] == "ПАО СБЕРБАНК"
    assert found["carrier_name"][0] == 'ООО "ВЕЗЁМ"'


def test_addresses_are_full_strings(found):
    assert found["pickup_address"][0].startswith("141400, Химки")
    assert "Вавилова" in found["delivery_address"][0]


def test_nothing_is_certain(found):
    """
    Всё, что достали из документа, идёт человеку на подтверждение: подпись в
    документе может стоять над чужим значением.
    """
    assert all(conf <= 0.85 for _v, conf, _s in found.values())


def test_source_names_document_and_line(found):
    value, _conf, source = found["cargo_name"]
    assert source.startswith("заявка №418")
    assert "Наименование груза" in source


def test_bad_inn_keeps_low_confidence():
    """ИНН с несходящейся контрольной суммой не должен выглядеть надёжным."""
    out = scan("Грузополучатель: ООО РОМАШКА, ИНН 1234567890")
    assert out["consignee_inn"][1] < 0.75


def test_empty_and_noise_give_nothing():
    assert scan("") == {}
    assert scan("Добрый день! Отправьте, пожалуйста, машину.") == {}


def test_order_date_from_header_line():
    out = scan("ЗАЯВКА № 7\nот 08 сентября 2026 г.")
    assert out["order_date"][0].date() == datetime(2026, 9, 8).date()
    assert out["order_number"][0] == "7"


def test_extractor_ocr_mode_uses_textscan():
    """Режим ocr: модель отдаёт текст, реквизиты достаёт разбор."""
    from etrn_bot.extract import Document, Extractor

    class OcrClient:
        def __init__(self):
            self.system = ""

        def complete(self, **kw):
            self.system = kw["system"]
            return "Грузополучатель: ПАО СБЕРБАНК, ИНН 7707083893\nКоличество мест: 40"

    client = OcrClient()
    out = Extractor(client, "m", mode="ocr").extract(
        [Document("zayavka.png", "image/png", b"")])
    assert "переписываешь текст" in client.system
    assert out["consignee_inn"][0] == "7707083893"
    assert out["cargo_places"][0] == 40
    assert "zayavka.png" in out["cargo_places"][2]


def test_extractor_ocr_mode_on_empty_page():
    from etrn_bot.extract import Extractor

    class Blank:
        def complete(self, **_kw):
            return "   "

    assert Extractor(Blank(), "m", mode="ocr").extract([]) == {}


# ── документы-удостоверения ─────────────────────────────────────────────────

VU_TEXT = """ВОДИТЕЛЬСКОЕ УДОСТОВЕРЕНИЕ
PERMIS DE CONDUIRE
DRIVING LICENCE
1. ИВАНОВ
2. ПЁТР СЕРГЕЕВИЧ
IVANOV
PETR SERGEEVICH
3. 03.04.1980 ТУЛЬСКАЯ ОБЛ.
4a) 01.02.2021 4b) 01.02.2031
4c) ГИБДД 7701
5. 9912 345678"""

STS_TEXT = """СВИДЕТЕЛЬСТВО О РЕГИСТРАЦИИ ТС
CERTIFICAT D'IMMATRICULATION
Регистрационный знак К555МН199
Идентификационный номер (VIN) XL962000010002647
Марка, модель ГАЗ 3302
Тип ТС Грузовой фургон
Технически допустимая max масса, кг 3500"""

PASSPORT_TEXT = """ПАСПОРТ
РОССИЙСКАЯ ФЕДЕРАЦИЯ
ИВАНОВ
ПЁТР СЕРГЕЕВИЧ
Дата рождения 03.04.1980
Серия 4507 № 112233"""


def test_license_gives_number_and_fio():
    found = scan(VU_TEXT, "vu.pdf")
    assert found["driver_license"][0] == "9912 345678"
    assert found["driver_fio"][0] == ("Иванов", "Пётр", "Сергеевич")


def test_license_values_need_confirmation():
    """OCR на пластике путает цифры — номер прав человек обязан сверить глазами."""
    found = scan(VU_TEXT, "vu.pdf")
    assert found["driver_license"][1] < 0.75
    assert found["driver_fio"][1] < 0.75


def test_license_headers_are_not_a_surname():
    """«ВОДИТЕЛЬСКОЕ УДОСТОВЕРЕНИЕ» — заголовок бланка, а не фамилия."""
    fio = scan(VU_TEXT, "vu.pdf")["driver_fio"][0]
    assert "Удостоверение" not in fio and "Водительское" not in fio


def test_vehicle_certificate_fields():
    found = scan(STS_TEXT, "sts.pdf")
    assert found["vehicle_plate"][0] == "К555МН199"
    assert found["vehicle_brand"][0].startswith("ГАЗ")
    assert "фургон" in found["vehicle_type"][0].lower()


def test_passport_gives_nothing():
    """
    В Титуле 1 нет ни одного поля с паспортными данными, поэтому из паспорта
    мы не достаём ничего: чем меньше ПД в системе, тем лучше.
    """
    found = scan(PASSPORT_TEXT, "passport.pdf")
    assert "driver_license" not in found
    assert found == {} or all(k in BY_KEY for k in found)


CARD_TEXT = """Иванов Пётр Сергеевич 03.04.1980 г.р
Паспорт РФ 4507  112233 Выдан ГУ МВД России по Ростовской области   05.06.2015г
Код подразделения 770-077
Телефон: +7 900 111-22-33
а/м  СКАНИЯ К 555 МН 199 п/п  АВ 1234  99"""


def test_driver_card_from_docx_text():
    """
    Карточка водителя, которую логисты пересылают вордом: подписей «Водитель:»
    в ней нет, опознаём по составу — дата рождения рядом с ФИО и «Паспорт РФ».
    """
    found = scan(CARD_TEXT, "card.docx")
    assert found["driver_fio"][0] == ("Иванов", "Пётр", "Сергеевич")
    assert found["driver_phone"][0] == "+7 (900) 111-22-33"
    assert found["vehicle_plate"][0] == "К555МН199"
    assert found["trailer_plate"][0] == "АВ123499"
    assert "Скания" in found["vehicle_brand"][0] or "СКАНИЯ" in found["vehicle_brand"][0].upper()


def test_driver_card_passport_is_not_taken():
    """Паспортные данные из карточки не берём: в Титуле 1 таких полей нет."""
    found = scan(CARD_TEXT, "card.docx")
    assert all("passport" not in k for k in found)
    assert all("4507" not in str(v) for v, _c, _s in found.values())



def test_plate_without_label_as_tesseract_4_reads_it():
    """
    Строка ровно в том виде, как её прочитал tesseract 4.1.1 на Hermes: подпись
    «гос. номер» ушла в латиницу («roc. Homep»), сам номер цел.
    """
    found = scan("Автомобиль: Volvo FH, roc. Homep A123BC777", "z.png")
    assert found["vehicle_plate"][0] == "А123ВС777"
    assert found["vehicle_plate"][1] < 0.75, "номер без подписи — только на подтверждение"


def test_two_unlabeled_plates_are_not_guessed():
    found = scan("машины А123ВС777 и К555МН199 на погрузке", "z.png")
    assert "vehicle_plate" not in found
