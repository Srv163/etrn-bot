"""
Карта XSD: печатает дерево элементов и атрибутов схемы с обязательностью.

Зачем отдельным инструментом: имена элементов ЭТрН придумывать по смыслу нельзя
(антипаттерн в CLAUDE.md), а .docx с описанием формата читать глазами каждый раз
дорого. Инструмент даёт машинный срез схемы — из него берутся точные пути для
`xsd=` в реестре и для шаблона XML.

Схемы ФНС — windows-1251; XML-парсер разбирает кодировку по декларации сам.

    python3 -m etrn_bot.tools.xsd_map schema/ON_TRNACLGROT_1_973_01_05_01_02.xsd
    python3 -m etrn_bot.tools.xsd_map <файл> --required   # только обязательные
    python3 -m etrn_bot.tools.xsd_map <файл> --depth 4
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET

XS = "{http://www.w3.org/2001/XMLSchema}"


class Schema:
    def __init__(self, path: str) -> None:
        self.root = ET.parse(path).getroot()
        self.types: dict[str, ET.Element] = {}
        for kind in ("complexType", "simpleType"):
            for t in self.root.findall(XS + kind):
                name = t.get("name")
                if name:
                    self.types[name] = t

    @staticmethod
    def doc(node: ET.Element) -> str:
        ann = node.find(XS + "annotation")
        if ann is None:
            return ""
        d = ann.find(XS + "documentation")
        return " ".join((d.text or "").split()) if d is not None else ""

    def top(self) -> ET.Element:
        el = self.root.find(XS + "element")
        if el is None:
            raise SystemExit("в схеме нет корневого элемента")
        return el


class Walker:
    def __init__(self, schema: Schema, max_depth: int, only_required: bool) -> None:
        self.s = schema
        self.max_depth = max_depth
        self.only_required = only_required
        self.rows: list[tuple[str, bool, str, str]] = []

    def run(self) -> list[tuple[str, bool, str, str]]:
        top = self.s.top()
        self._element(top, "", 0, True, set())
        return self.rows

    def _element(self, el: ET.Element, parent: str, depth: int,
                 required: bool, seen: frozenset | set) -> None:
        name = el.get("name") or el.get("ref") or "?"
        path = f"{parent}/{name}"
        mn = el.get("minOccurs", "1")
        mx = el.get("maxOccurs", "1")
        req = required and mn != "0"
        card = "" if (mn, mx) == ("1", "1") else f"[{mn}..{mx}]"
        if not self.only_required or req:
            self.rows.append((path + (" " + card if card else ""), req,
                              el.get("type") or "", self.s.doc(el)))
        if depth >= self.max_depth:
            return
        self._body(el, path, depth, req, seen)

    def _body(self, node: ET.Element, path: str, depth: int,
              required: bool, seen: frozenset | set) -> None:
        """Разворачивает и inline-тип, и ссылку type="ИмяТипа"."""
        tname = node.get("type")
        bodies: list[ET.Element] = [c for c in node if c.tag == XS + "complexType"]
        if tname and tname in self.s.types:
            # защита от циклов: один и тот же тип на одной ветке не разворачиваем дважды
            if tname in seen:
                return
            seen = set(seen) | {tname}
            bodies.append(self.s.types[tname])
        for body in bodies:
            self._children(body, path, depth, required, seen)

    def _children(self, node: ET.Element, path: str, depth: int,
                  required: bool, seen: frozenset | set) -> None:
        for child in node:
            tag = child.tag.replace(XS, "")
            if tag == "element":
                self._element(child, path, depth + 1, required, seen)
            elif tag == "attribute":
                req = required and child.get("use") == "required"
                if not self.only_required or req:
                    self.rows.append((f"{path}/@{child.get('name')}", req,
                                      child.get("type") or "", self.s.doc(child)))
            elif tag == "choice":
                # внутри choice ни одна ветка не обязательна сама по себе
                self._children(child, path, depth, False, seen)
            elif tag in ("sequence", "all", "complexContent", "simpleContent",
                         "extension", "restriction", "group"):
                self._children(child, path, depth, required, seen)


def main() -> None:
    ap = argparse.ArgumentParser(description="карта XSD с обязательностью полей")
    ap.add_argument("xsd")
    ap.add_argument("--depth", type=int, default=12)
    ap.add_argument("--required", action="store_true",
                    help="только безусловно обязательные узлы")
    args = ap.parse_args()

    rows = Walker(Schema(args.xsd), args.depth, args.required).run()
    for path, req, typ, doc in rows:
        mark = "ОБЯЗ" if req else "    "
        tail = f"  :{typ}" if typ else ""
        print(f"{mark} {path}{tail}")
        if doc:
            print(f"       — {doc}")
    total = len(rows)
    hard = sum(1 for _, r, _, _ in rows if r)
    print(f"\nвсего узлов: {total}, из них безусловно обязательных: {hard}")


if __name__ == "__main__":
    main()
