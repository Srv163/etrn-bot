"""
Клиент модели, читающей сканы.

Интерфейс зафиксирован в задаче 4: `complete(model, system, documents, response_format)`.
Здесь реализация поверх OpenAI-совместимого HTTP API — так говорят и локальные
серверы (vLLM, Ollama, LM Studio), и большинство облачных провайдеров, поэтому
переезд между ними — смена двух переменных окружения, а не переписывание кода.

Настройка (только через env, ключи в коде не держим):
    ETRN_VLM_BASE_URL   например https://.../v1
    ETRN_VLM_API_KEY
    ETRN_VLM_MODEL
    ETRN_VLM_TIMEOUT    секунды, по умолчанию 120

ПЕРСОНАЛЬНЫЕ ДАННЫЕ. Через этот клиент уходят фото водительских удостоверений,
то есть ПД. Провайдер выбирает владелец бота осознанно: при зарубежном сервисе
это трансграничная передача ПД со своими требованиями 152-ФЗ. Код никому ничего
не запрещает, но и никакого провайдера по умолчанию не подставляет — без
ETRN_VLM_BASE_URL клиент не создаётся.

Только стандартная библиотека: `requests` в ядро не тащим.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass


class VLMError(RuntimeError):
    """Провайдер не ответил или ответил не тем. Наружу — без трейсбека."""


@dataclass
class HttpVLMClient:
    """OpenAI-совместимый /chat/completions с картинками в сообщении."""

    base_url: str
    api_key: str
    timeout: int = 120

    @classmethod
    def from_env(cls) -> "HttpVLMClient":
        base = os.environ.get("ETRN_VLM_BASE_URL")
        if not base:
            raise VLMError(
                "не задан ETRN_VLM_BASE_URL — провайдер распознавания не выбран. "
                "Пока его нет, бот работает без чтения сканов: значения вводятся в диалоге."
            )
        return cls(base_url=base.rstrip("/"),
                   api_key=os.environ.get("ETRN_VLM_API_KEY", ""),
                   timeout=int(os.environ.get("ETRN_VLM_TIMEOUT", 120)))

    def complete(self, *, model: str, system: str, documents: list,
                 response_format: str = "json") -> str:
        content: list[dict] = []
        for doc in documents:
            data = base64.b64encode(doc.data).decode("ascii")
            if doc.mime.startswith("image/"):
                content.append({"type": "image_url",
                                "image_url": {"url": f"data:{doc.mime};base64,{data}"}})
            else:
                # PDF и прочее — не все провайдеры принимают: говорим прямо,
                # а не отправляем в надежде.
                raise VLMError(
                    f"{doc.filename}: тип {doc.mime} этот клиент не отправляет. "
                    "Пришлите страницы фотографиями или картинками."
                )
        content.append({"type": "text",
                        "text": "Извлеки реквизиты из приложенных документов."})

        payload = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "temperature": 0,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise VLMError(f"провайдер ответил {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise VLMError(f"провайдер недоступен: {e.reason}") from e
        except TimeoutError as e:
            raise VLMError(f"провайдер не ответил за {self.timeout} с") from e

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise VLMError(f"неожиданный ответ провайдера: {str(body)[:200]}") from e


class NoVLMClient:
    """
    Заглушка на время, пока провайдер не выбран: честно ничего не распознаёт.
    Бот с ней работает — просто спрашивает всё в диалоге.
    """

    def complete(self, **_kw) -> str:
        return "{}"


def from_env():
    """
    Чем распознавать, по настройкам окружения, в порядке убывания качества:

      ключи Yandex Vision  → сервис с готовыми моделями паспорта, прав и СТС;
      ETRN_VLM_BASE_URL    → языковая модель по этому адресу;
      локальный tesseract  → печатные заявки и вордовские карточки;
      ничего из этого      → заглушка: бот работает и спрашивает всё в диалоге.

    Порядок именно такой: бланки документов читает только специализированный
    сервис, а локальный OCR остаётся запасным вариантом и для того, что не
    требует отправки наружу.
    """
    from . import yandex_ocr

    if yandex_ocr.available():
        try:
            return yandex_ocr.YandexVisionClient.from_env()
        except yandex_ocr.YandexOCRError:
            pass
    if os.environ.get("ETRN_VLM_BASE_URL"):
        return HttpVLMClient.from_env()
    from . import ocr
    if ocr.available():
        try:
            return ocr.TesseractClient()
        except ocr.OCRError:
            return NoVLMClient()
    return NoVLMClient()


def model_from_env() -> str:
    return os.environ.get("ETRN_VLM_MODEL", "stub")


def mode_from_env() -> str:
    """
    json — модель сама возвращает реквизиты; ocr — распознаём текст, а реквизиты
    достаёт наш разбор по подписям (`textscan`).

    По умолчанию ocr: он работает и с tesseract, и со слабой моделью, и не
    требует от модели держать промпт на 78 полей. Режим json включают явно,
    когда за адресом стоит сильная модель.
    """
    mode = os.environ.get("ETRN_VLM_MODE", "ocr").strip().lower()
    return mode if mode in ("json", "ocr") else "ocr"
