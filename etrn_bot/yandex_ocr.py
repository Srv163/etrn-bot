"""
Распознавание документов через Yandex Vision OCR.

Почему именно он. У сервиса есть готовые модели под ровно те документы, что
приходят по рейсу, и они возвращают не «текст со страницы», а именованные поля
(массив `entities`): паспорт, права (лицевая и оборот), СТС (лицевая и оборот),
автомобильные номера. Это снимает и слабое место нашего локального OCR (бланки
с защитной сеткой он не читает), и необходимость угадывать поля регулярками.

Данные остаются в российском облаке — для фото прав и паспортов это важнее
качества распознавания.

Настройка (ключи только в env, не в коде):

    ETRN_YANDEX_API_KEY   API-ключ из AI Studio (роль ai.vision.user)
    ETRN_YANDEX_FOLDER_ID каталог — необязателен: ключ из AI Studio уже привязан
                          к каталогу, запрос без x-folder-id проходит (проверено
                          10.09.2026). Нужен только для ключей без привязки.

Документ: POST https://ai.api.cloud.yandex.net/ocr/v1/recognizeText,
тело {mimeType, languageCodes, model, content(base64)}, заголовки
Authorization: Api-Key …, x-folder-id: …

Ограничения сервиса: JPEG, PNG, PDF, не более 10 МБ и 20 мегапикселей.
Только стандартная библиотека: `requests` в ядро не тащим.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ENDPOINT = os.environ.get("ETRN_YANDEX_OCR_URL",
                          "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText")
MAX_BYTES = 10 * 1024 * 1024
# Квота по умолчанию — 1 запрос в секунду на облако (проверено 10.09.2026:
# второй запрос подряд получил 429). Держим интервал с запасом.
MIN_INTERVAL = float(os.environ.get("ETRN_YANDEX_MIN_INTERVAL", 1.2))
RETRIES_429 = 3
# PDF сервис в синхронном режиме не декодирует («Can't decode image» на всех
# пяти сканах), поэтому разворачиваем страницы у себя. 200 dpi — A4 выходит
# около 4 мегапикселей, в пределах лимита 20.
PDF_DPI = int(os.environ.get("ETRN_YANDEX_PDF_DPI", 200))

# Модели под конкретные документы. Ключ — как мы называем тип документа внутри.
MODELS = {
    "page": "page",                                   # произвольный текст (заявки)
    "passport": "passport",
    "driver_license_front": "driver-license-front",
    "driver_license_back": "driver-license-back",
    "vehicle_front": "vehicle-registration-front",
    "vehicle_back": "vehicle-registration-back",
    "plates": "license-plates",
}

# Как называется документ в имени файла — бухгалтерия называет файлы осмысленно,
# и это самая дешёвая подсказка о том, какую модель просить.
FILENAME_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("passport", ("паспорт", "passport")),
    ("driver_license_front", ("вод", "ву", "удост", "права", "license")),
    ("vehicle_front", ("стс", "свидетельств", "sts", "registration")),
)


class YandexOCRError(RuntimeError):
    """Сервис не ответил или отказал. Наружу — человеческой фразой."""


# Двухсторонние документы часто сканируют одним PDF: лицевая, затем оборот.
BACK_SIDE = {"driver_license_front": "driver_license_back",
             "vehicle_front": "vehicle_back"}


def pdf_pages(data: bytes, dpi: int = PDF_DPI) -> list[bytes]:
    """PDF → JPEG-страницы. Без pdftoppm честно отказываемся."""
    if not shutil.which("pdftoppm"):
        raise YandexOCRError("PDF не развернуть — нет pdftoppm "
                             "(brew install poppler / apt install poppler-utils)")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.pdf"
        src.write_bytes(data)
        try:
            subprocess.run(["pdftoppm", "-r", str(dpi), "-jpeg", str(src), str(Path(tmp) / "p")],
                           capture_output=True, check=True, timeout=120)
        except subprocess.CalledProcessError as e:
            raise YandexOCRError("PDF не открывается — возможно, файл повреждён. "
                                 "Пришлите страницы фотографиями") from e
        except subprocess.TimeoutExpired as e:
            raise YandexOCRError("PDF разворачивается слишком долго") from e
        pages = [p.read_bytes() for p in sorted(Path(tmp).glob("p*.jpg"))]
        if not pages:
            raise YandexOCRError("в PDF нет страниц")
        return pages


# ── подготовка страницы ─────────────────────────────────────────────────────
#
# Проверено 10.09.2026 на реальных сканах: права лежали в углу листа A4, и без
# обрезки сервис находил на лицевой стороне 1–2 поля, после обрезки до карточки —
# 7. В СТС обрезка исправила цифры госномера («к556мн99» → «к555мн199»).
# Поворот тоже важен: права были отсканированы боком.

def crop_to_document(img, threshold: int = 200, density: float = 0.02, pad: int = 30):
    """
    Обрезать лист до самого документа.

    Считаем долю тёмных пикселей в каждой строке и столбце: сначала бинаризуем,
    потом сжимаем в полоску шириной в пиксель — среднее бинарной картинки и есть
    эта доля. Сглаживаем окном, чтобы точки пыли на пустом листе не считались
    документом. Без numpy: на сервере его нет.
    """
    from PIL import Image

    gray = img.convert("L")
    binary = gray.point(lambda p: 255 if p < threshold else 0)
    w, h = gray.size

    def band(profile: list[float], size: int) -> tuple[int, int]:
        win = max(5, size // 200)
        half = win // 2
        smooth = [sum(profile[max(0, i - half):i + half + 1]) / win for i in range(size)]
        marks = [i for i, v in enumerate(smooth) if v > density]
        return (marks[0], marks[-1]) if marks else (0, size - 1)

    # tobytes(), а не getdata(): последний удаляют в Pillow 14 (2027-10).
    rows = [v / 255 for v in binary.resize((1, h), Image.BOX).tobytes()]
    cols = [v / 255 for v in binary.resize((w, 1), Image.BOX).tobytes()]
    top, bottom = band(rows, h)
    left, right = band(cols, w)
    if (right - left) < w * 0.1 or (bottom - top) < h * 0.1:
        return img                      # ничего внятного не нашли — не режем
    return img.crop((max(0, left - pad), max(0, top - pad),
                     min(w, right + pad), min(h, bottom + pad)))


def _jpeg(img) -> bytes:
    import io
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def prepare_page(data: bytes) -> bytes:
    """Страница → JPEG, обрезанный до документа. Без PIL — как есть."""
    try:
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(data))
    except Exception:                  # noqa: BLE001 — не картинка: пусть решает сервис
        return data
    return _jpeg(crop_to_document(img))


# Если лицевая сторона дала меньше полей, пробуем повороты. Каждый поворот —
# платный запрос, поэтому только для бланков и только когда полей мало.
ROTATE_IF_FEWER = 4
TEMPLATE_KINDS = {"passport", "driver_license_front", "driver_license_back",
                  "vehicle_front", "vehicle_back"}


def guess_kind(filename: str) -> str:
    """Тип документа по имени файла; не угадали — читаем как обычный текст."""
    low = filename.lower()
    for kind, words in FILENAME_HINTS:
        if any(w in low for w in words):
            return kind
    return "page"


def mime_of(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "pdf": "PDF"}.get(ext, "PNG")


@dataclass
class YandexVisionClient:
    api_key: str
    folder_id: str = ""
    timeout: int = 120
    _last_call: float = field(default=0.0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _wait_turn(self) -> None:
        """Не чаще MIN_INTERVAL: иначе квота отвечает 429 и документ теряется."""
        with self._lock:
            gap = time.monotonic() - self._last_call
            if gap < MIN_INTERVAL:
                time.sleep(MIN_INTERVAL - gap)
            self._last_call = time.monotonic()

    @classmethod
    def from_env(cls) -> "YandexVisionClient":
        key = os.environ.get("ETRN_YANDEX_API_KEY")
        folder = os.environ.get("ETRN_YANDEX_FOLDER_ID", "")
        if not key:
            raise YandexOCRError(
                "нет ETRN_YANDEX_API_KEY — распознавание через Yandex Vision не настроено"
            )
        return cls(api_key=key, folder_id=folder,
                   timeout=int(os.environ.get("ETRN_OCR_TIMEOUT", 120)))

    # ── низкий уровень ──────────────────────────────────────────────────────

    def recognize(self, data: bytes, filename: str, model: str) -> dict:
        """Один запрос к сервису. Возвращает разобранный JSON ответа."""
        if len(data) > MAX_BYTES:
            raise YandexOCRError(
                f"{filename}: файл больше 10 МБ, сервис такой не примет — "
                "пересохраните страницы отдельными снимками"
            )
        payload = json.dumps({
            "mimeType": mime_of(filename),
            "languageCodes": ["*"],
            "model": model,
            "content": base64.b64encode(data).decode("ascii"),
        }).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Api-Key {self.api_key}"}
        if self.folder_id:
            headers["x-folder-id"] = self.folder_id
        req = urllib.request.Request(ENDPOINT, data=payload, method="POST", headers=headers)
        for attempt in range(RETRIES_429 + 1):
            self._wait_turn()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < RETRIES_429:
                    time.sleep(MIN_INTERVAL * (attempt + 1))
                    continue
                self._raise_http(e, filename)
            except urllib.error.URLError as e:
                raise YandexOCRError(f"сервис недоступен: {e.reason}") from e
            except TimeoutError as e:
                raise YandexOCRError(f"{filename}: сервис не ответил за {self.timeout} с") from e
        raise YandexOCRError(f"{filename}: квота запросов исчерпана, повторите позже")

    @staticmethod
    def _raise_http(e: urllib.error.HTTPError, filename: str) -> None:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        if e.code in (401, 403):
            # Причину сервис пишет в теле ответа: отозванный ключ, нет роли,
            # закончился грант — без неё гадать приходится вслепую.
            raise YandexOCRError(
                f"сервис не принял ключ ({e.code}): {detail[:200]}"
            ) from e
        if e.code == 400 and "decode" in detail.lower():
            raise YandexOCRError(
                f"{filename}: сервис не смог открыть файл. Пришлите фото или "
                "скан картинкой (JPEG, PNG)"
            ) from e
        raise YandexOCRError(f"{filename}: сервис ответил {e.code}: {detail}") from e

    @staticmethod
    def _result(body: dict) -> dict:
        # Синхронный ответ приходит либо как {result: …}, либо плоским.
        return body.get("result", body) or {}

    def full_text(self, body: dict) -> str:
        res = self._result(body)
        text = res.get("textAnnotation", {}).get("fullText") or res.get("fullText") or ""
        return text

    def entities(self, body: dict) -> dict[str, str]:
        """Именованные поля шаблонного документа: {имя поля: значение}."""
        res = self._result(body)
        raw = res.get("textAnnotation", {}).get("entities") or res.get("entities") or []
        out: dict[str, str] = {}
        for item in raw:
            name, text = item.get("name"), item.get("text")
            if name and text:
                out[str(name)] = str(text).strip()
        return out

    # ── интерфейс клиента распознавания ─────────────────────────────────────

    def complete(self, *, documents: list, model: str = "", system: str = "",
                 response_format: str = "text") -> str:
        """
        Текст документов — для `Extractor(mode="ocr")`.

        Для шаблонных документов дополнительно подставляем распознанные поля
        строками «имя: значение»: наш разбор по подписям читает их так же, как
        печатный текст, и ему не нужно знать про Yandex Vision.
        """
        parts: list[str] = []
        for doc in documents:
            _kind, fields, text = self.read(doc)
            if fields:
                text = "\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n" + text
            parts.append(text)
        return "\n".join(p for p in parts if p.strip())

    def read(self, doc) -> tuple[str, dict[str, str], str]:
        """
        Один запрос — всё, что сервис знает о документе:
        (тип, именованные поля, полный текст).

        Именно один: запросы платные постранично, и разделять «сначала поля,
        потом текст» значит платить дважды за одну страницу.
        """
        kind = guess_kind(doc.filename)
        if doc.filename.lower().endswith(".pdf"):
            pages = pdf_pages(doc.data, dpi=300)
            names = [f"{doc.filename} стр.{i + 1}.jpg" for i in range(len(pages))]
        else:
            pages, names = [doc.data], [doc.filename]

        fields: dict[str, str] = {}
        texts: list[str] = []
        for i, (data, name) in enumerate(zip(pages, names)):
            # Вторая страница прав или СТС — почти всегда оборот со своей моделью.
            page_kind = BACK_SIDE.get(kind, kind) if i == 1 else kind
            page_fields, text = self._read_page(prepare_page(data), name, page_kind)
            for key, value in page_fields.items():
                fields.setdefault(key, value)       # первое найденное значение побеждает
            texts.append(text)
        return kind, fields, "\n".join(t for t in texts if t)

    def _read_page(self, data: bytes, name: str, kind: str) -> tuple[dict[str, str], str]:
        """Страница: если бланк прочитан плохо — пробуем повернуть, берём лучший."""
        body = self.recognize(data, name, MODELS[kind])
        best, text = self.entities(body), self.full_text(body)
        if kind not in TEMPLATE_KINDS or len(best) >= ROTATE_IF_FEWER:
            return best, text
        try:
            import io
            from PIL import Image
            img = Image.open(io.BytesIO(data))
        except Exception:              # noqa: BLE001
            return best, text
        for angle in (90, 270):
            rotated = _jpeg(img.rotate(angle, expand=True))
            rbody = self.recognize(rotated, f"{name} ({angle}°)", MODELS[kind])
            got = self.entities(rbody)
            if len(got) > len(best):
                best, text = got, self.full_text(rbody)
            if len(best) >= ROTATE_IF_FEWER:
                break
        return best, text

    def document_fields(self, doc) -> tuple[str, dict[str, str]]:
        """Совместимость: только именованные поля."""
        kind, fields, _text = self.read(doc)
        return kind, fields


def available() -> bool:
    return bool(os.environ.get("ETRN_YANDEX_API_KEY"))
