"""
Распознавание печатных документов.

Тесты идут на настоящем файле (`tests/fixtures/zayavka_418.png`) и настоящем
tesseract: подставной OCR проверял бы только наш разбор, а тут важно, что весь
путь «картинка → текст → реквизиты» действительно работает.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from etrn_bot import ocr
from etrn_bot.extract import Document, Extractor
from etrn_bot.fields import BY_KEY

FIXTURE = Path(__file__).parent / "fixtures" / "zayavka_418.png"

pytestmark = pytest.mark.skipif(
    not ocr.available() or "rus" not in ocr.languages(),
    reason="нужен tesseract с русским языком",
)


@pytest.fixture(scope="module")
def recognised():
    docs = [Document(FIXTURE.name, "image/png", FIXTURE.read_bytes())]
    return Extractor(ocr.TesseractClient(), "tesseract", mode="ocr").extract(docs)


def test_scan_finds_main_requisites(recognised):
    """На печатной заявке должны находиться реквизиты, из-за которых всё это и делается."""
    for key in ("consignee_inn", "carrier_inn", "cargo_name", "cargo_mass_gross_kg",
                "cargo_places", "vehicle_plate", "driver_fio", "driver_license",
                "pickup_address", "delivery_address", "pickup_datetime"):
        assert key in recognised, f"не распознали {BY_KEY[key].title}"


def test_values_are_correct(recognised):
    assert recognised["cargo_mass_gross_kg"][0] == 20000
    assert recognised["cargo_places"][0] == 40
    assert recognised["consignee_inn"][0] == "7707083893"
    # латиница в номере машины приводится к кириллице нашим парсером
    assert recognised["vehicle_plate"][0] == "А123ВС777"
    assert recognised["driver_fio"][0] == ("Петров", "Иван", "Сергеевич")


def test_nothing_accepted_silently(recognised):
    """Всё распознанное идёт человеку на подтверждение."""
    assert all(conf <= 0.85 for _v, conf, _s in recognised.values())


def test_source_points_at_file_and_line(recognised):
    _v, _c, source = recognised["cargo_name"]
    assert FIXTURE.name in source and "Наименование груза" in source


def test_blank_page_gives_nothing():
    """Приёмка задачи 4: на пустом листе — пусто, а не выдумка."""
    from io import BytesIO
    pil = pytest.importorskip("PIL.Image")
    buf = BytesIO()
    pil.new("RGB", (900, 1200), "white").save(buf, format="PNG")
    docs = [Document("blank.png", "image/png", buf.getvalue())]
    assert Extractor(ocr.TesseractClient(), "tesseract", mode="ocr").extract(docs) == {}


def test_unreadable_file_fails_loudly():
    """Битый файл — внятная фраза, а не трейсбек PIL в чате у бухгалтера."""
    client = ocr.TesseractClient()
    with pytest.raises(ocr.OCRError) as e:
        client.complete(documents=[Document("bad.png", "image/png", b"not an image")])
    assert "не картинка" in str(e.value) or "не открывается" in str(e.value)


def test_pdf_without_poppler_says_what_to_install(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr.shutil, "which", lambda name: None if name == "pdftoppm" else "/x")
    with pytest.raises(ocr.OCRError) as e:
        ocr.read_pdf(tmp_path / "scan.pdf")
    assert "poppler" in str(e.value)


def test_docx_glues_broken_runs(tmp_path):
    """
    Word рвёт строку на куски по каждому изменению шрифта. Если их не склеить,
    дата рождения приезжает пятью строками и ни один парсер её не узнает.
    """
    import zipfile
    doc = tmp_path / "card.docx"
    xml = ('<?xml version="1.0"?><w:document xmlns:w="x"><w:body>'
           '<w:p><w:r><w:t>Водитель: </w:t></w:r><w:r><w:t>Иванов Иван</w:t></w:r>'
           '<w:r><w:t> Иванович</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Телефон водителя: +7 916 000-11-22</w:t></w:r></w:p>'
           '</w:body></w:document>')
    with zipfile.ZipFile(doc, "w") as z:
        z.writestr("word/document.xml", xml)
    text = ocr.read_docx(doc)
    assert "Водитель: Иванов Иван Иванович" in text
    from etrn_bot.textscan import scan
    found = scan(text, "card.docx")
    assert found["driver_fio"][0] == ("Иванов", "Иван", "Иванович")


def test_missing_language_is_named():
    with pytest.raises(ocr.OCRError) as e:
        ocr.TesseractClient(langs="rus+klingon")
    assert "klingon" in str(e.value)


def test_default_client_is_ocr_when_no_model(monkeypatch):
    from etrn_bot import vlm
    monkeypatch.delenv("ETRN_VLM_BASE_URL", raising=False)
    assert isinstance(vlm.from_env(), ocr.TesseractClient)
    assert vlm.mode_from_env() == "ocr"
