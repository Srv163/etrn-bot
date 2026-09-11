"""
Профили организаций-грузоотправителей.

Организаций несколько: бухгалтер выписывает накладные от разных юрлиц, поэтому
реквизиты — не константа в коде и не одна переменная окружения, а список
профилей. Заполняет их человек при первом запуске бота (диалог `/orgs`),
дальше при создании накладной выбирает, от кого она.

Хранение: JSON-файл (путь в `ETRN_ORGS_FILE`, по умолчанию `var/orgs.json`).
Файл лежит вне git — там реквизиты организации, а `.gitignore` закрывает `var/`.

Счётчик номеров накладных здесь НЕ хранится: он в общем хранилище
(`store.next_number`), потому что должен быть атомарным между сессиями.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("ETRN_ORGS_FILE", "var/orgs.json"))

# Поля профиля, которые человек заполняет при инициализации, в порядке вопросов.
SETUP_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("inn", "ИНН организации", "10 цифр для ООО, 12 для ИП"),
    ("name", "Наименование", "как в ЕГРЮЛ, с кавычками"),
    ("kpp", "КПП", "можно пропустить, если нет"),
    ("address", "Юридический адрес", "одной строкой"),
    ("phone", "Телефон", "его увидит перевозчик"),
    ("edo_id", "Идентификатор участника ЭДО",
     "из карточки организации в Saby; нужен для имени файла"),
    ("signer_fio", "ФИО подписанта", "кто подписывает накладные, полностью"),
    ("signer_position", "Должность подписанта", "например: главный бухгалтер"),
)


@dataclass
class Profile:
    inn: str
    name: str = ""
    kpp: str = ""
    address: str = ""
    phone: str = ""
    edo_id: str = ""
    signer_fio: str = ""
    signer_position: str = ""
    # Реквизиты-политики: меняются редко, но у разных организаций по-разному.
    signer_status: str = "1"          # 1 — подписывает без доверенности
    is_expeditor: str = "0"           # 0 — не экспедитор
    loader_is_shipper: str = "1"      # грузим сами
    infra_owner_is_shipper: str = "1"  # площадка наша
    mass_method: str = "01"           # взвешивание по общей массе
    transit_days: int = 2
    contact: str = ""
    extra_defaults: dict = field(default_factory=dict)
    # Условия заявки перевозчику — юридический текст организации. Пусто —
    # берутся стандартные из zayavka.DEFAULT_TERMS. Правятся без разработчика.
    zayavka_terms: list = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """Хватает ли профиля, чтобы собрать накладную (без имени файла)."""
        return all([self.inn, self.name, self.address, self.phone, self.signer_fio])

    def missing(self) -> list[str]:
        need = {"inn": "ИНН", "name": "наименование", "address": "адрес",
                "phone": "телефон", "signer_fio": "ФИО подписанта",
                "edo_id": "идентификатор ЭДО"}
        return [title for key, title in need.items() if not getattr(self, key)]

    def terms(self) -> list[str]:
        from .zayavka import DEFAULT_TERMS
        return list(self.zayavka_terms) or list(DEFAULT_TERMS)

    def title(self) -> str:
        return f"{self.name or 'без названия'} ({self.inn})"

    def org_settings(self, next_number=None) -> dict:
        """
        Настройки для Resolver: политика организации + источник номера.

        Всё, что здесь попадает в `defaults`, бот подставляет без вопросов —
        это осознанные решения организации, а не его догадки.
        """
        defaults = {
            "shipper_phone": self.phone,
            "signer_fio": self.signer_fio,
            "signer_position": self.signer_position,
            "signer_status": self.signer_status,
            "shipper_is_expeditor": self.is_expeditor,
            "loader_is_shipper": self.loader_is_shipper,
            "infra_owner_is_shipper": self.infra_owner_is_shipper,
            "mass_method": self.mass_method,
            "redirect_person": "Грузоотправитель",
        }
        if self.contact:
            defaults["shipper_contact"] = self.contact
        defaults.update(self.extra_defaults)
        return {
            "inn": self.inn,
            "counter": 0,
            "next_number": next_number,
            "default_transit_days": self.transit_days,
            "defaults": defaults,
        }

    def directory_card(self) -> dict:
        """Карточка для справочника организаций — по ней резолвер тянет реквизиты."""
        card = {
            "shipper_name": self.name,
            "shipper_address": self.address,
            "shipper_edo_id": self.edo_id,
        }
        if self.kpp:
            card["shipper_kpp"] = self.kpp
        return card


class Registry:
    """Список профилей на диске."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or DEFAULT_PATH)

    def load(self) -> dict[str, Profile]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {inn: Profile(**data) for inn, data in raw.items()}

    def save(self, profiles: dict[str, Profile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {inn: asdict(p) for inn, p in profiles.items()}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        # Реквизиты организации — не секрет, но и не для всех: файл читает
        # только владелец процесса.
        self.path.chmod(0o600)

    def add(self, profile: Profile) -> None:
        profiles = self.load()
        profiles[profile.inn] = profile
        self.save(profiles)

    def get(self, inn: str) -> Profile | None:
        return self.load().get(inn)

    def list(self) -> list[Profile]:
        return sorted(self.load().values(), key=lambda p: p.name or p.inn)
