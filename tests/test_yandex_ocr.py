"""
Клиент Yandex Vision OCR: выбор модели по документу и разбор именованных полей.

Сеть не трогаем — подставляем ответ сервиса. Проверяем то, что действительно
может сломаться: какую модель просим для какого файла, что делаем с полями,
и что ошибки сервиса доходят человеческой фразой.
"""

from __future__ import annotations

import json

import pytest

from etrn_bot import yandex_ocr as yv
from etrn_bot.extract import Document, Extractor
from etrn_bot.fields import BY_KEY
from etrn_bot.textscan import from_entities


@pytest.mark.parametrize("filename,kind", [
    ("Вод удост.pdf", "driver_license_front"),
    ("права Иванова.jpg", "driver_license_front"),
    ("Паспорт-1.pdf", "passport"),
    ("стс АВ1234_99.pdf", "vehicle_front"),
    ("СТС К555МН199.pdf", "vehicle_front"),
    ("заявка 418.png", "page"),
])
def test_model_is_picked_by_filename(filename, kind):
    """Имя файла — самая дешёвая подсказка о типе документа, и она работает."""
    assert yv.guess_kind(filename) == kind
    assert yv.MODELS[kind]


def test_mime_types():
    assert yv.mime_of("a.pdf") == "PDF"
    assert yv.mime_of("b.JPG") == "JPEG"
    assert yv.mime_of("c.png") == "PNG"


# ── разбор именованных полей ────────────────────────────────────────────────

VU_ENTITIES = {
    "surname": "ИВАНОВ", "name": "ПЁТР", "middle_name": "СЕРГЕЕВИЧ",
    "number": "9912 345678", "birth_date": "03.04.1980",
    "issue_date": "01.02.2021", "expiration_date": "01.02.2031",
}

PASSPORT_ENTITIES = {
    "surname": "ИВАНОВ", "name": "ПЁТР", "middle_name": "СЕРГЕЕВИЧ",
    "number": "4507 112233", "issued_by": "ГУ МВД России по Тульской области",
    "issue_date": "05.06.2015", "subdivision": "770-077",
    "birth_date": "03.04.1980", "birth_place": "г. Тула",
}

STS_ENTITIES = {
    "stsfront_car_number": "К555МН199", "stsfront_car_brand": "СКАНИЯ",
    "stsfront_vin_number": "XL962000010002647", "stsfront_sts_number": "9954321098",
    "stsfront_car_year": "2001",
}


def test_license_entities_to_registry():
    found = from_entities(VU_ENTITIES, "vu.pdf", "driver_license_front")
    assert found["driver_fio"][0] == ("ИВАНОВ", "ПЁТР", "СЕРГЕЕВИЧ")
    assert found["driver_license"][0] == "9912 345678"
    assert found["driver_birth_date"][0].strftime("%d.%m.%Y") == "03.04.1980"


def test_passport_goes_to_power_of_attorney_fields():
    """Паспорт нужен для доверенности: поля есть, но в XML накладной не идут."""
    found = from_entities(PASSPORT_ENTITIES, "passport.pdf", "passport")
    assert found["driver_passport"][0] == "4507 112233"
    assert "Тульской" in found["driver_passport_issued_by"][0]
    assert found["driver_passport_subdivision"][0] == "770-077"
    for key in found:
        assert key in BY_KEY, key
    assert all(BY_KEY[k].not_in_xml for k in found if k.startswith("driver_passport"))


def test_vehicle_entities_to_registry():
    found = from_entities(STS_ENTITIES, "sts.pdf", "vehicle_front")
    assert found["vehicle_plate"][0] == "К555МН199"
    assert found["vehicle_brand"][0] == "СКАНИЯ"
    assert found["vehicle_sts"][0] == "9954321098"


def test_unknown_entities_are_ignored():
    """Лишние ПД в систему не тащим: чего нет в таблице — не берём."""
    found = from_entities({"birth_place": "г. Тула", "gender": "МУЖ."}, "p.pdf", "passport")
    assert found == {}


def test_entities_need_confirmation():
    found = from_entities(VU_ENTITIES, "vu.pdf", "driver_license_front")
    assert all(conf <= 0.8 for _v, conf, _s in found.values())


def test_numbers_from_service_are_always_asked():
    """
    На реальных СТС сервис путал цифры госномера («к556мн99» вместо К555МН199),
    поэтому любой номер из него идёт человеку на подтверждение.
    """
    found = {**from_entities(VU_ENTITIES, "vu.pdf", "driver_license_front"),
             **from_entities(STS_ENTITIES, "sts.pdf", "vehicle_front"),
             **from_entities(PASSPORT_ENTITIES, "p.pdf", "passport")}
    for key in ("driver_license", "vehicle_plate", "vehicle_sts", "driver_passport"):
        assert found[key][1] < 0.75, key


# ── работа клиента ──────────────────────────────────────────────────────────

class FakeClient(yv.YandexVisionClient):
    """Подменяем сеть: запоминаем, какие модели просили, и отдаём заготовку."""

    def __init__(self, answers: dict[str, dict]) -> None:
        super().__init__(api_key="k", folder_id="f")
        self.answers = answers
        self.asked: list[str] = []

    def recognize(self, data: bytes, filename: str, model: str) -> dict:
        self.asked.append(model)
        entities = self.answers.get(model, {})
        return {"result": {"textAnnotation": {
            "fullText": "текст страницы",
            "entities": [{"name": k, "text": v} for k, v in entities.items()],
        }}}


def test_one_request_per_document():
    """Запросы платные постранично: на документ — ровно один вызов сервиса."""
    client = FakeClient({"driver-license-front": VU_ENTITIES})
    Extractor(client, "yandex", mode="ocr").extract(
        [Document("Вод удост.jpg", "image/jpeg", b"x")])
    assert client.asked == ["driver-license-front"]


def test_extractor_uses_document_fields():
    client = FakeClient({"driver-license-front": VU_ENTITIES,
                         "vehicle-registration-front": STS_ENTITIES})
    docs = [Document("Вод удост.jpg", "image/jpeg", b"x"),
            Document("СТС К555МН199.jpg", "image/jpeg", b"x")]
    found = Extractor(client, "yandex", mode="ocr").extract(docs)
    assert client.asked == ["driver-license-front", "vehicle-registration-front"]
    assert found["driver_license"][0] == "9912 345678"
    assert found["vehicle_plate"][0] == "К555МН199"


def test_plain_document_falls_back_to_text():
    """У заявки именованных полей нет — читаем текстом и разбираем по подписям."""
    class TextOnly(FakeClient):
        def recognize(self, data, filename, model):
            self.asked.append(model)
            return {"result": {"textAnnotation": {
                "fullText": "Наименование груза: Сера гранулированная\nКоличество мест: 40",
                "entities": [],
            }}}

    client = TextOnly({})
    found = Extractor(client, "yandex", mode="ocr").extract(
        [Document("zayavka.png", "image/png", b"x")])
    assert client.asked == ["page"]
    assert found["cargo_places"][0] == 40


def test_missing_keys_say_what_to_set(monkeypatch):
    monkeypatch.delenv("ETRN_YANDEX_API_KEY", raising=False)
    monkeypatch.delenv("ETRN_YANDEX_FOLDER_ID", raising=False)
    assert not yv.available()
    with pytest.raises(yv.YandexOCRError) as e:
        yv.YandexVisionClient.from_env()
    assert "ETRN_YANDEX_API_KEY" in str(e.value)


def test_folder_is_optional(monkeypatch):
    """Ключ из AI Studio уже привязан к каталогу — без folder id клиент создаётся."""
    monkeypatch.setenv("ETRN_YANDEX_API_KEY", "k")
    monkeypatch.delenv("ETRN_YANDEX_FOLDER_ID", raising=False)
    assert yv.available()
    assert yv.YandexVisionClient.from_env().folder_id == ""


def test_too_big_file_refused():
    client = yv.YandexVisionClient(api_key="k", folder_id="f")
    with pytest.raises(yv.YandexOCRError) as e:
        client.recognize(b"x" * (11 * 1024 * 1024), "big.pdf", "page")
    assert "10 МБ" in str(e.value)



# ── постраничный PDF, прицеп, даты ──────────────────────────────────────────

def test_pdf_pages_go_front_then_back(monkeypatch):
    """Права одним PDF: первая страница — лицевая модель, вторая — оборот."""
    monkeypatch.setattr(yv, "pdf_pages", lambda data, dpi=300: [b"p1", b"p2"])
    client = FakeClient({"driver-license-front": VU_ENTITIES,
                         "driver-license-back": {"experience_from": "2005"}})
    kind, fields, _text = client.read(Document("Вод удост.pdf", "application/pdf", b"%PDF"))
    assert client.asked == ["driver-license-front", "driver-license-back"]
    assert fields["surname"] == "ИВАНОВ" and fields["experience_from"] == "2005"


def test_broken_pdf_is_a_sentence_not_a_traceback():
    with pytest.raises(yv.YandexOCRError) as e:
        yv.pdf_pages(b"not a pdf")
    assert "PDF" in str(e.value)


def test_trailer_certificate_goes_to_trailer():
    """СТС полуприцепа не должен перетереть номер тягача."""
    found = from_entities({"stsfront_car_number": "АВ1234 99",
                           "stsfront_car_brand": "ТОНАР",
                           "stsfront_sts_number": "9900112233"}, "sts.pdf", "vehicle_front")
    assert found["trailer_plate"][0] == "АВ123499"
    assert "vehicle_plate" not in found
    assert "vehicle_brand" not in found


def test_body_number_is_not_trailer_plate():
    """Номер кузова из бланка СТС — не госномер прицепа."""
    found = from_entities({"stsfront_car_trailer_number": "XL962000010002647"},
                          "sts.pdf", "vehicle_front")
    assert "trailer_plate" not in found


def test_dates_without_time_are_confident():
    found = from_entities(PASSPORT_ENTITIES, "passport.pdf", "passport")
    assert found["driver_passport_date"][1] >= 0.75
    assert str(found["driver_passport_date"][0]) == "2015-06-05"



# ── подготовка страницы ─────────────────────────────────────────────────────

def _sheet_with_card():
    """Лист A4 с маленькой «карточкой» в углу — как был отсканирован ВУ."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (2480, 3508), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((150, 200, 1150, 900), fill=(230, 200, 220))
    for y in range(260, 880, 40):
        d.rectangle((200, y, 1100, y + 18), fill="black")
    return img


def test_crop_finds_the_card_on_a4():
    pytest.importorskip("PIL")
    img = _sheet_with_card()
    cut = yv.crop_to_document(img)
    assert cut.width < img.width * 0.6 and cut.height < img.height * 0.4


def test_crop_leaves_blank_sheet_alone():
    Image = pytest.importorskip("PIL.Image")
    blank = Image.new("RGB", (1000, 1400), "white")
    assert yv.crop_to_document(blank).size == blank.size


def test_rotation_tried_only_when_template_read_poorly():
    """Каждый поворот — платный запрос: пробуем только для бланков и только при малом числе полей."""
    Image = pytest.importorskip("PIL.Image")
    import io
    buf = io.BytesIO(); Image.new("RGB", (100, 80), "white").save(buf, format="JPEG")
    page = buf.getvalue()

    class Rotating(FakeClient):
        def recognize(self, data, filename, model):
            self.asked.append(filename)
            full = "90°" in filename
            ents = VU_ENTITIES if full else {"expiration_date": "01.02.2031"}
            return {"result": {"textAnnotation": {"fullText": "",
                    "entities": [{"name": k, "text": v} for k, v in ents.items()]}}}

    c = Rotating({})
    fields, _ = c._read_page(page, "vu.jpg", "driver_license_front")
    assert len(fields) == len(VU_ENTITIES)
    assert c.asked == ["vu.jpg", "vu.jpg (90°)"]

    c2 = Rotating({})
    c2._read_page(page, "zayavka.jpg", "page")
    assert c2.asked == ["zayavka.jpg"], "обычный текст не вертим"
