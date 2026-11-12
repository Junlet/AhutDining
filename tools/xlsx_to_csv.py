"""把上传的 Excel 读成 CSV（零依赖：xlsx = zip + XML，用标准库解析）。

用法：
    python tools/xlsx_to_csv.py <xlsx> [--sheet 名称] [--out 输出.csv]
    python tools/xlsx_to_csv.py <xlsx> --inspect     # 只看结构，不写文件
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _text_of(node) -> str:
    """取 <si>/<is> 里所有 <t> 文本（合并富文本 run）。"""
    parts = [t.text or "" for t in node.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")]
    return "".join(parts)


def read_workbook(path: str) -> dict:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            shared = [_text_of(si) for si in root.findall("main:si", NS)]

        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.get("Id"): r.get("Target") for r in rels}
        sheets = []
        for sheet in workbook.findall("main:sheets/main:sheet", NS):
            rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_map.get(rid, "")
            if target.startswith("/"):
                target = target.lstrip("/")
            else:
                target = "xl/" + target
            sheets.append((sheet.get("name"), target))

        result = {}
        for name, target in sheets:
            if target not in names:
                continue
            root = ET.fromstring(zf.read(target))
            rows: list[list[str]] = []
            for row in root.findall("main:sheetData/main:row", NS):
                values: dict[int, str] = {}
                for cell in row.findall("main:c", NS):
                    ref = cell.get("r") or ""
                    col_letters = re.match(r"([A-Z]+)", ref)
                    if not col_letters:
                        continue
                    col = 0
                    for ch in col_letters.group(1):
                        col = col * 26 + (ord(ch) - 64)
                    col -= 1
                    cell_type = cell.get("t")
                    if cell_type == "inlineStr":
                        value = _text_of(cell)
                    else:
                        v = cell.find("main:v", NS)
                        value = v.text if v is not None and v.text is not None else ""
                        if cell_type == "s" and value.isdigit():
                            idx = int(value)
                            value = shared[idx] if idx < len(shared) else ""
                    values[col] = value
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
            result[name] = rows
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx")
    parser.add_argument("--sheet", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args()

    if not os.path.isfile(args.xlsx):
        raise SystemExit(f"找不到文件：{args.xlsx}")

    book = read_workbook(args.xlsx)
    print(f"文件: {args.xlsx}")
    print(f"工作表（{len(book)} 个）:")
    for name, rows in book.items():
        print(f"  - {name}: {len(rows)} 行, 最宽 {max((len(r) for r in rows), default=0)} 列")

    target = args.sheet or next(iter(book))
    rows = book.get(target, [])
    print(f"\n== {target} 前 {args.limit} 行 ==")
    for i, row in enumerate(rows[: args.limit]):
        print(f"[{i}] " + " | ".join(str(c) for c in row))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerows(rows)
        print(f"\n已写出 {len(rows)} 行 -> {args.out}")


if __name__ == "__main__":
    main()
