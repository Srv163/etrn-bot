"""
Распознавание документов: OCR плюс подготовка изображения.

Почему это основной путь, а не языковая модель — замеры 09.09.2026 на печатной
заявке (Hermes, 6 ядер, без GPU):

    qwen2.5vl:3b, «найди поля и верни JSON» — 653 с, 0 полей;
    qwen2.5vl:3b, «перепиши текст»          — >14 мин, не дождались;
    tesseract -l rus+eng + разбор           — 0.4 с, 23 поля.

Подготовка изображения важнее выбора движка. На настоящем скане прав документ
занимал угол листа A4 и лежал боком: без подготовки tesseract выдавал мусор,
после обрезки полей, поворота и контраста — прочитал фамилию, даты и номер.
Поэтому здесь: обрезка по плотности текста → увеличение → контраст → выбор
поворота по количеству осмысленных слов.

PDF (сканы приходят почти всегда в PDF) разбирается постранично через pdftoppm.
DOCX и TXT читаются как текст — распознавать там нечего.

Интерфейс совпадает с клиентом модели (`complete`), поэтому `Extractor` в режиме
`ocr` работает с любым из них, не зная разницы.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from html import unescape
from pathlib import Path

# rus+eng: в русских документах латиницей написаны марки машин, VIN и дубли
# полей загранобразца; с одним rus tesseract превращает «Volvo FH» в «\/о№о ЕН».
DEFAULT_LANGS = os.environ.get("ETRN_OCR_LANGS", "rus+eng")
TIMEOUT = int(os.environ.get("ETRN_OCR_TIMEOUT", 60))
PDF_DPI = int(os.environ.get("ETRN_OCR_DPI", 300))
# Режим сегментации: 6 — «единый блок текста», лучший для бланков и карточек.
PSM = os.environ.get("ETRN_OCR_PSM", "6")

TEXT_SUFFIXES = {".txt", ".csv", ".md"}


class OCRError(RuntimeError):
    """Не удалось распознать. Наружу — человеческой фразой, без трейсбека."""


def available() -> bool:
    return shutil.which("tesseract") is not None


def languages() -> list[str]:
    if not available():
        return []
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True,
                         text=True, timeout=TIMEOUT)
    return [ln.strip() for ln in out.stdout.splitlines()[1:] if ln.strip()]


# ── подготовка изображения ──────────────────────────────────────────────────

def _content_box(img, threshold: int = 190, density: float = 0.02, pad: int = 20):
    """
    Границы плотного текста. Считаем среднюю яркость каждой строки и каждого
    столбца (PIL умеет это одним resize), отбрасываем края, где темнее порога
    почти ничего нет: на сканах поля усыпаны точками, и обрезка «по любому
    тёмному пикселю» не обрезает ничего.
    """
    from PIL import Image

    gray = img.convert("L")
    w, h = gray.size
    # tobytes(), а не getdata(): последний удаляют в Pillow 14 (2027-10),
    # а для режима L байт на пиксель — ровно то, что нужно.
    rows = gray.resize((1, h), Image.BILINEAR).tobytes()
    cols = gray.resize((w, 1), Image.BILINEAR).tobytes()

    def band(profile, size):
        marks = [i for i, v in enumerate(profile) if v < threshold]
        if not marks:
            return 0, size - 1
        return marks[0], marks[-1]

    top, bottom = band(rows, h)
    left, right = band(cols, w)
    if (right - left) < w * 0.05 or (bottom - top) < h * 0.05:
        return 0, 0, w, h           # ничего осмысленного не нашли — не режем
    return (max(0, left - pad), max(0, top - pad),
            min(w, right + pad), min(h, bottom + pad))


def _enhance(img):
    from PIL import ImageFilter, ImageOps

    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    return gray.filter(ImageFilter.UnsharpMask(radius=2, percent=180, threshold=2))


def _words(text: str) -> int:
    """Сколько в тексте слов, похожих на настоящие русские."""
    return len(re.findall(r"[А-Яа-яЁё]{4,}", text))


def prepare(path: Path):
    """Обрезать поля и увеличить документ. Возвращает подготовленное изображение."""
    from PIL import Image

    img = Image.open(path)
    box = _content_box(img)
    img = img.crop(box)
    # Мелкий документ на большом листе (карточка прав на A4) читается только
    # после увеличения: tesseract хочет высоту строки примерно 30 пикселей.
    if img.width < 1600:
        scale = max(2, int(1600 / max(img.width, 1)))
        img = img.resize((img.width * scale, img.height * scale), Image.LANCZOS)
    return _enhance(img)


# ── запуск tesseract ────────────────────────────────────────────────────────

def _run(path: Path, langs: str, timeout: int, psm: str = PSM) -> str:
    try:
        out = subprocess.run(["tesseract", str(path), "stdout", "-l", langs, "--psm", psm],
                             capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise OCRError(f"{path.name}: tesseract не ответил за {timeout} с") from e
    if out.returncode != 0:
        detail = out.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise OCRError(f"{path.name}: не распознать ({detail})")
    return out.stdout.decode("utf-8", errors="replace")


def read_image(path: Path, langs: str = DEFAULT_LANGS, timeout: int = TIMEOUT) -> str:
    """
    Распознать картинку, подобрав поворот.

    Скан кладут как придётся: права и СТС часто лежат боком. Определитель
    ориентации самого tesseract на мелком документе ошибается, поэтому пробуем
    четыре поворота и берём тот, где получилось больше осмысленных слов. Это
    четыре запуска по секунде — дешевле, чем неверно прочитанный номер прав.
    """
    try:
        from PIL import Image, UnidentifiedImageError  # noqa: F401
    except ImportError:
        return _run(path, langs, timeout)      # без PIL — как есть

    try:
        img = prepare(path)
    except UnidentifiedImageError as e:
        raise OCRError(f"{path.name}: это не картинка, распознать нечего") from e
    except OSError as e:
        raise OCRError(f"{path.name}: файл не открывается ({e})") from e
    best_text, best_score = "", -1
    with tempfile.TemporaryDirectory() as tmp:
        for angle in (0, 270, 90, 180):
            rotated = img if angle == 0 else img.rotate(angle, expand=True)
            page = Path(tmp) / f"page_{angle}.png"
            rotated.save(page)
            text = _run(page, langs, timeout)
            score = _words(text)
            if score > best_score:
                best_text, best_score = text, score
            # Прямой скан читается сразу; если слов уже много, вертеть незачем.
            if angle == 0 and score >= 12:
                break
    return best_text


def read_pdf(path: Path, langs: str = DEFAULT_LANGS, timeout: int = TIMEOUT) -> str:
    if not shutil.which("pdftoppm"):
        raise OCRError(
            f"{path.name}: PDF не развернуть — нет pdftoppm. "
            "macOS: brew install poppler; Debian/Ubuntu: apt install poppler-utils"
        )
    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "page"
        try:
            subprocess.run(["pdftoppm", "-r", str(PDF_DPI), "-png", str(path), str(prefix)],
                           capture_output=True, timeout=timeout * 4, check=True)
        except subprocess.CalledProcessError as e:
            detail = e.stderr.decode("utf-8", errors="replace")[:200]
            raise OCRError(f"{path.name}: не развернуть PDF ({detail})") from e
        except subprocess.TimeoutExpired as e:
            raise OCRError(f"{path.name}: PDF разворачивается слишком долго") from e
        pages = sorted(Path(tmp).glob("page*.png"))
        if not pages:
            raise OCRError(f"{path.name}: в PDF нет страниц-изображений")
        return "\n".join(read_image(p, langs, timeout) for p in pages)


def read_docx(path: Path) -> str:
    """
    Текст из .docx. Word рвёт строку на куски (`<w:t>`) по каждому изменению
    оформления, поэтому склеиваем куски внутри абзаца — иначе «21.05.1985»
    приезжает пятью строками и ни один парсер её не узнает.
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    out: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        chunks = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, re.S)
        line = unescape("".join(chunks)).strip()
        if line:
            out.append(line)
    return "\n".join(out)


class TesseractClient:
    """
    Клиент для `Extractor(mode="ocr")`: возвращает текст документов.

    `model` и `system` игнорируются осознанно — у OCR нет промпта. Интерфейс
    сохранён, чтобы вызывающий код не знал, чем именно распознано.
    """

    def __init__(self, langs: str = DEFAULT_LANGS, timeout: int = TIMEOUT) -> None:
        if not available():
            raise OCRError(
                "tesseract не установлен. macOS: brew install tesseract tesseract-lang; "
                "Debian/Ubuntu: apt install tesseract-ocr tesseract-ocr-rus"
            )
        self.langs = langs
        self.timeout = timeout
        missing = [lang for lang in langs.split("+") if lang not in languages()]
        if missing:
            raise OCRError(
                f"в tesseract нет языков {', '.join(missing)}. "
                "Debian/Ubuntu: apt install tesseract-ocr-rus"
            )

    def complete(self, *, documents: list, model: str = "", system: str = "",
                 response_format: str = "text") -> str:
        texts = [self._one(doc) for doc in documents]
        return "\n".join(t for t in texts if t.strip())

    def _one(self, doc) -> str:
        suffix = Path(doc.filename).suffix.lower() or ".png"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"doc{suffix}"
            path.write_bytes(doc.data)
            if suffix == ".pdf":
                return read_pdf(path, self.langs, self.timeout)
            if suffix == ".docx":
                return read_docx(path)
            if suffix in TEXT_SUFFIXES:
                return doc.data.decode("utf-8", errors="replace")
            return read_image(path, self.langs, self.timeout)
