"""
Прогон настоящего изображения через распознавание (приёмка задачи 4).

Показывает то, что важно проверить перед доверием модели: какие поля она нашла,
с какой уверенностью, на какой документ ссылается — и сколько это заняло.
Модель не «правильная или нет»: её ответ идёт в диалог как догадка, которую
человек подтверждает, поэтому смотреть надо и на честность confidence.

    ETRN_VLM_BASE_URL=http://127.0.0.1:11434/v1 ETRN_VLM_MODEL=qwen2.5vl:3b \
    PYTHONPATH=. python3 -m etrn_bot.tools.try_vlm tests/fixtures/zayavka_418.png

Пустой лист должен давать пустой результат — это тоже часть приёмки:
    ... python3 -m etrn_bot.tools.try_vlm --blank
"""

from __future__ import annotations

import mimetypes
import sys
import time
from pathlib import Path

from .. import vlm
from ..extract import Document, Extractor
from ..fields import BY_KEY


def _blank_page() -> Document:
    """Чистый лист: проверяем, что модель не выдумывает реквизиты из ничего."""
    from io import BytesIO
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("для --blank нужен pillow: pip install pillow")
    buf = BytesIO()
    Image.new("RGB", (900, 1200), "white").save(buf, format="PNG")
    return Document("blank.png", "image/png", buf.getvalue())


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    blank = "--blank" in sys.argv

    if blank:
        docs = [_blank_page()]
    elif args:
        docs = []
        for path in args:
            p = Path(path)
            if not p.exists():
                print(f"нет файла: {p}")
                return 2
            mime = mimetypes.guess_type(p.name)[0] or "image/jpeg"
            docs.append(Document(p.name, mime, p.read_bytes()))
    else:
        print(__doc__)
        return 2

    client = vlm.from_env()
    if isinstance(client, vlm.NoVLMClient):
        print("ETRN_VLM_BASE_URL не задан — распознавать нечем.")
        return 2

    model = vlm.model_from_env()
    print(f"модель: {model}, режим: {vlm.mode_from_env()}, документов: {len(docs)}")
    started = time.monotonic()
    try:
        found = Extractor(client, model, mode=vlm.mode_from_env()).extract(docs)
    except vlm.VLMError as e:
        print(f"провайдер: {e}")
        return 1
    spent = time.monotonic() - started

    print(f"время: {spent:.1f} с, полей найдено: {len(found)}\n")
    for key, (value, conf, source) in sorted(found.items()):
        title = BY_KEY[key].title if key in BY_KEY else f"{key} (НЕТ В РЕЕСТРЕ)"
        mark = " " if conf >= 0.75 else "?"
        print(f" {mark} {title:42} {str(value)[:40]:42} conf={conf:<5} ← {source[:40]}")

    unknown = [k for k in found if k not in BY_KEY]
    if unknown:
        print(f"\nмодель придумала поля вне реестра: {unknown}")
    if blank and found:
        print("\nПЛОХО: на пустом листе модель что-то «нашла» — это выдумка.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
