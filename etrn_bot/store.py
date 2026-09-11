"""
Хранилище состояния: сессии диалога и счётчики номеров накладных.

Зачем не словарь в памяти процесса: бот перезапускается (деплой, падение), а
бухгалтер в этот момент на середине накладной — заново кидать пять документов
она не должна. И счётчик номеров общий: две параллельные сессии не имеют права
получить один номер.

Две реализации одного интерфейса:
  RedisStore  — рабочая: TTL на сессию, атомарный INCR для счётчика;
  MemoryStore — для тестов и локального прогона без Redis.

ПД: в сессии лежат распознанные значения (ФИО водителя, номер ВУ) — это
неизбежно, иначе не продолжить диалог. Поэтому TTL, и никогда не сами сканы:
исходники удаляются сразу после извлечения (правило 6 в CLAUDE.md).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Protocol

from .resolve import Carriage, Value

SESSION_TTL = int(os.environ.get("ETRN_SESSION_TTL", 12 * 3600))
# Сколько хранить выписанные заявки и поручения. ЭТрН по ним собирают, когда
# машина пришла, — это дни, а не часы. Дольше держать незачем: там реквизиты
# контрагентов, а иногда и данные водителя.
ISSUED_TTL = int(os.environ.get("ETRN_ISSUED_TTL", 60 * 24 * 3600))
ISSUED_LIMIT = 50                       # последних документов на организацию


def issued_record(c: Carriage, org_inn: str, doc_type: str) -> dict:
    """Запись о выписанном документе: по ней потом строится ЭТрН."""
    return {
        "id": f"{doc_type}:{c.get('doc_number')}",
        "org_inn": org_inn,
        "doc_type": doc_type,
        "number": str(c.get("doc_number") or ""),
        "date": str(c.get("doc_date") or ""),
        # Для списка выбора: по чему человек узнаёт свой рейс.
        "counterparty": str(c.get("carrier_name") or c.get("forwarder_name")
                            or c.get("consignee_name") or ""),
        "route": " → ".join(str(x) for x in (c.get("pickup_address"),
                                             c.get("delivery_address")) if x),
        "cargo": str(c.get("cargo_name") or ""),
        "carriage": dumps(c),
    }


# ── сериализация состояния накладной ────────────────────────────────────────

def _encode(value: object) -> object:
    """JSON не знает про datetime и кортежи, а в значениях они есть."""
    if isinstance(value, datetime):
        return {"__dt__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(v) for v in value]}
    if isinstance(value, list):
        return [_encode(v) for v in value]
    return value


def _decode(value: object) -> object:
    if isinstance(value, dict):
        if "__dt__" in value:
            return datetime.fromisoformat(value["__dt__"])
        if "__date__" in value:
            return date.fromisoformat(value["__date__"])
        if "__tuple__" in value:
            return tuple(_decode(v) for v in value["__tuple__"])
        return value
    if isinstance(value, list):
        return [_decode(v) for v in value]
    return value


def dumps(c: Carriage) -> str:
    return json.dumps({
        "values": {k: {"value": _encode(v.value), "source": v.source,
                       "confidence": v.confidence}
                   for k, v in c.values.items()},
        "deferred": sorted(c.deferred),
        "skipped": sorted(c.skipped),
        "corrections": [[k, _encode(a), _encode(b)] for k, a, b in c.corrections],
        # Без этих двух после перезапуска бот забыл бы, какой документ собирает
        # и сколько вопросов уже задал («Вопрос 1 из 5» вместо «4 из 5»).
        "doc_type": c.doc_type,
        "answered_count": c.answered_count,
    }, ensure_ascii=False)


def loads(raw: str) -> Carriage:
    data = json.loads(raw)
    c = Carriage()
    for key, v in data.get("values", {}).items():
        c.values[key] = Value(_decode(v["value"]), v["source"], v["confidence"])
    c.deferred = set(data.get("deferred", []))
    c.skipped = set(data.get("skipped", []))
    c.corrections = [(k, _decode(a), _decode(b))
                     for k, a, b in data.get("corrections", [])]
    c.doc_type = data.get("doc_type", "etrn")
    c.answered_count = int(data.get("answered_count", 0))
    return c


# ── интерфейс ───────────────────────────────────────────────────────────────

class Store(Protocol):
    def load(self, chat_id: int) -> dict | None: ...
    def save(self, chat_id: int, session: dict) -> None: ...
    def drop(self, chat_id: int) -> None: ...
    def next_number(self, org_inn: str) -> int: ...
    def save_issued(self, record: dict) -> None: ...
    def list_issued(self, org_inn: str, limit: int = 10) -> list[dict]: ...
    def get_issued(self, org_inn: str, doc_id: str) -> dict | None: ...


class MemoryStore:
    """Для тестов и одиночного локального прогона."""

    def __init__(self) -> None:
        self._sessions: dict[int, str] = {}
        self._meta: dict[int, dict] = {}
        self._counters: dict[str, int] = {}

    def load(self, chat_id: int) -> dict | None:
        raw = self._sessions.get(chat_id)
        if raw is None:
            return None
        s = dict(self._meta.get(chat_id, {}))
        s["carriage"] = loads(raw)
        return s

    def save(self, chat_id: int, session: dict) -> None:
        c = session["carriage"]
        self._sessions[chat_id] = dumps(c)
        self._meta[chat_id] = {k: v for k, v in session.items() if k != "carriage"}

    def drop(self, chat_id: int) -> None:
        self._sessions.pop(chat_id, None)
        self._meta.pop(chat_id, None)

    def next_number(self, org_inn: str) -> int:
        self._counters[org_inn] = self._counters.get(org_inn, 0) + 1
        return self._counters[org_inn]

    def save_issued(self, record: dict) -> None:
        items = self.__dict__.setdefault("_issued", {}).setdefault(record["org_inn"], [])
        items[:] = [r for r in items if r["id"] != record["id"]]
        items.insert(0, record)
        del items[ISSUED_LIMIT:]

    def list_issued(self, org_inn: str, limit: int = 10) -> list[dict]:
        return list(self.__dict__.get("_issued", {}).get(org_inn, [])[:limit])

    def get_issued(self, org_inn: str, doc_id: str) -> dict | None:
        return next((r for r in self.__dict__.get("_issued", {}).get(org_inn, [])
                     if r["id"] == doc_id), None)


class RedisStore:
    """
    Рабочее хранилище. Счётчик — через INCR: он атомарен, поэтому две сессии
    не получат одинаковый номер накладной даже при одновременном нажатии.
    """

    def __init__(self, url: str | None = None, ttl: int = SESSION_TTL) -> None:
        import redis

        self.r = redis.Redis.from_url(url or os.environ.get("REDIS_URL",
                                                           "redis://localhost:6379/0"))
        self.ttl = ttl

    def _key(self, chat_id: int) -> str:
        return f"etrn:session:{chat_id}"

    def load(self, chat_id: int) -> dict | None:
        raw = self.r.get(self._key(chat_id))
        if raw is None:
            return None
        data = json.loads(raw)
        session = data.get("meta", {})
        session["carriage"] = loads(data["carriage"])
        return session

    def save(self, chat_id: int, session: dict) -> None:
        payload = json.dumps({
            "carriage": dumps(session["carriage"]),
            "meta": {k: v for k, v in session.items() if k != "carriage"},
        }, ensure_ascii=False)
        self.r.set(self._key(chat_id), payload, ex=self.ttl)

    def drop(self, chat_id: int) -> None:
        self.r.delete(self._key(chat_id))

    def next_number(self, org_inn: str) -> int:
        return int(self.r.incr(f"etrn:counter:{org_inn}"))

    # Выписанные документы: список на организацию, свежие сверху. Список, а не
    # по ключу на документ, — чтобы показать «последние десять» одним запросом.
    def _issued_key(self, org_inn: str) -> str:
        return f"etrn:issued:{org_inn}"

    def save_issued(self, record: dict) -> None:
        key = self._issued_key(record["org_inn"])
        for raw in self.r.lrange(key, 0, -1):
            if json.loads(raw)["id"] == record["id"]:
                self.r.lrem(key, 1, raw)
        pipe = self.r.pipeline()
        pipe.lpush(key, json.dumps(record, ensure_ascii=False))
        pipe.ltrim(key, 0, ISSUED_LIMIT - 1)
        pipe.expire(key, ISSUED_TTL)
        pipe.execute()

    def list_issued(self, org_inn: str, limit: int = 10) -> list[dict]:
        return [json.loads(raw) for raw in self.r.lrange(self._issued_key(org_inn), 0, limit - 1)]

    def get_issued(self, org_inn: str, doc_id: str) -> dict | None:
        return next((r for r in self.list_issued(org_inn, ISSUED_LIMIT) if r["id"] == doc_id), None)


def from_env() -> Store:
    """Redis, если он настроен и отвечает; иначе память с честным предупреждением."""
    url = os.environ.get("REDIS_URL")
    if not url:
        return MemoryStore()
    try:
        store = RedisStore(url)
        store.r.ping()
        return store
    except Exception as e:                       # noqa: BLE001 — причина в тексте
        raise RuntimeError(
            f"REDIS_URL задан ({url}), но Redis не отвечает: {e}. "
            "Либо поднимите Redis, либо уберите REDIS_URL — тогда сессии будут "
            "жить только в памяти процесса и потеряются при перезапуске."
        ) from e
