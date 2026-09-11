"""
Распознавание пачки документов.

Ключевое решение: промпт СТРОИТСЯ ИЗ РЕЕСТРА ПОЛЕЙ, а не пишется руками.
Добавили поле в fields.py — модель начинает его искать. Промпт и схема
не разъезжаются никогда.

Модель обязана возвращать по каждому полю:
  value      — значение как в документе, без нормализации (нормализуем парсерами);
  confidence — честная уверенность 0..1;
  source     — какой документ и где именно.
Не найдено — поле просто не включается в ответ. Придумывать запрещено.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .fields import REGISTRY, Strategy

OCR_SYSTEM = """Ты переписываешь текст с изображения документа.

Правила:
1. Перепиши ВЕСЬ видимый текст построчно, сохраняя порядок строк.
2. Ничего не переводи, не сокращай и не исправляй — как в документе.
3. Не добавляй ничего от себя: ни заголовков, ни пояснений, ни markdown.
4. Если на изображении нет текста — верни пустой ответ.
"""

SYSTEM = """Ты извлекаешь реквизиты перевозки из документов российской компании.

Правила:
1. Возвращай ТОЛЬКО JSON, без пояснений и без markdown-обёртки.
2. Если значения нет в документах — не включай поле. Никогда не выдумывай.
3. confidence — честная. Плохой скан, обрезанный край, рукописная правка → снижай.
4. value — как написано в документе. Не нормализуй, не переводи единицы.
5. source — конкретно: «заявка №418, строка "Груз"», а не «из документов».

Формат ответа:
{"поле": {"value": "...", "confidence": 0.0-1.0, "source": "..."}, ...}
"""


def build_schema_prompt() -> str:
    """Список полей для модели — генерируется из реестра."""
    lines = ["Поля, которые нужно найти:"]
    for f in REGISTRY:
        if Strategy.EXTRACT not in f.strategies:
            continue
        desc = f"- {f.key} — {f.title}"
        if f.hint:
            desc += f" ({f.hint})"
        lines.append(desc)
    return "\n".join(lines)


@dataclass
class Document:
    filename: str
    mime: str
    data: bytes


class Extractor:
    """
    Обёртка над VLM. Провайдер выбирается в конфиге.
    Персональные данные (ВУ, паспорт) уходят наружу — провайдер должен быть
    российским либо локальным. См. раздел про ПД в техплане.
    """

    # Сколько раз переспросить модель, если она вернула не JSON. Дальше —
    # честный отказ: лучше пустой результат и вопросы человеку, чем догадки.
    RETRIES = 2

    def __init__(self, client, model: str, mode: str = "json") -> None:
        """
        mode="json" — модель сама возвращает реквизиты по схеме из реестра.
            Годится для сильной модели.
        mode="ocr"  — модель только переписывает текст, а реквизиты из него
            достаёт наш разбор по подписям (`textscan`). Так работает маленькая
            модель на своём сервере: промпт на 78 полей она не держит (проверено
            на qwen2.5vl:3b — 11 минут и ноль полей), а текст переписывает.
        """
        self.client = client
        self.model = model
        self.mode = mode

    def _ask(self, docs: list[Document]) -> dict | None:
        """Запрос к модели с ретраями на невалидный JSON. None — не получилось."""
        system = SYSTEM + "\n\n" + build_schema_prompt()
        last = ""
        for attempt in range(self.RETRIES + 1):
            if attempt:
                system += ("\n\nПРЕДЫДУЩИЙ ОТВЕТ НЕ БЫЛ JSON. Верни только JSON-объект, "
                           "без пояснений и без markdown-обёртки.")
            payload = self.client.complete(
                model=self.model,
                system=system,
                documents=docs,
                response_format="json",
            )
            last = payload or ""
            text = last.strip()
            # Модели любят обёртывать ответ в ```json — снимаем, это не ошибка формата.
            if text.startswith("```"):
                text = text.strip("`")
                text = text.split("\n", 1)[-1] if "\n" in text else text
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
        return None

    def extract(self, docs: list[Document]) -> dict[str, tuple[object, float, str]]:
        if self.mode == "ocr":
            return self._extract_via_ocr(docs)
        raw = self._ask(docs)
        if raw is None:
            return {}

        out: dict[str, tuple[object, float, str]] = {}
        for key, item in raw.items():
            if not isinstance(item, dict) or item.get("value") is None:
                continue
            out[key] = (
                item.get("value"),
                float(item.get("confidence", 0.5)),
                str(item.get("source", "документ")),
            )
        return out

    def _extract_via_ocr(self, docs: list[Document]) -> dict[str, tuple[object, float, str]]:
        """
        Распознаём документы и достаём реквизиты.

        Если клиент умеет именованные поля (`document_fields` — так делает
        Yandex Vision для паспортов, прав и СТС), берём их: сервис уже разобрал
        документ, и гадать по подписям незачем. Иначе работаем по тексту.
        """
        from .textscan import from_entities, scan

        if hasattr(self.client, "read"):
            # Один запрос на документ: сервис отдаёт и именованные поля, и текст.
            # Разделять их на два вызова значит платить дважды за одну страницу.
            found: dict[str, tuple[object, float, str]] = {}
            for doc in docs:
                kind, fields, text = self.client.read(doc)
                for key, value in from_entities(fields, doc.filename, kind).items():
                    found.setdefault(key, value)
                for key, value in scan(text, doc.filename).items():
                    found.setdefault(key, value)
            return found

        text = self.client.complete(
            model=self.model,
            system=OCR_SYSTEM,
            documents=docs,
            response_format="text",
        )
        if not text or not text.strip():
            return {}
        return scan(text, source=_ocr_source(docs))


def _ocr_source(docs: list[Document]) -> str:
    """Как назвать источник в карточке: имя файла человеку понятнее, чем «документ»."""
    return docs[0].filename if len(docs) == 1 else f"{len(docs)} документ(ов)"


def merge(*results: dict[str, tuple[object, float, str]]) -> dict[str, tuple[object, float, str]]:
    """
    Несколько документов дали разные значения одного поля — берём то,
    у которого выше уверенность. Расхождение при высокой уверенности с обеих
    сторон — повод спросить человека, а не выбирать молча.
    """
    merged: dict[str, tuple[object, float, str]] = {}
    for res in results:
        for key, (val, conf, src) in res.items():
            prev = merged.get(key)
            if prev is None:
                merged[key] = (val, conf, src)
                continue
            pval, pconf, psrc = prev
            if pval != val and min(pconf, conf) > 0.8:
                merged[key] = (val if conf > pconf else pval, 0.5,
                               f"расхождение: {psrc} / {src}")
            elif conf > pconf:
                merged[key] = (val, conf, src)
    return merged
