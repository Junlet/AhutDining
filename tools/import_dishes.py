"""菜品数据导入 / 导出工具（CSV 与数据库互转）。

用法：
    python tools/import_dishes.py --export data/dishes.csv     # 把当前库导出成 CSV
    python tools/import_dishes.py --file data/dishes.csv        # 导入（同名同窗口视为更新）
    python tools/import_dishes.py --file my.csv --replace       # 先清空菜品再导入
    python tools/import_dishes.py --file my.csv --dry-run       # 只校验不写库

CSV 列（表头必须一致，顺序无所谓，允许留空）：
    campus,canteen,window,floor,open_hours,name,category,price,unit,
    kcal,protein,fat,carbs,is_staple,is_vegetable,is_self_service,tags,note

约定：
* kcal 留空时按 4×蛋白 + 9×脂肪 + 4×碳水 自动算；
* 食堂 / 窗口不存在会自动创建；
* 「按安工大真实菜单替换」时，最省事的做法是先 --export 拿到模板，
  在表格软件里改完再 --file 导入。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "app"))

import db  # noqa: E402

COLUMNS = [
    "campus", "canteen", "window", "floor", "open_hours", "name", "category",
    "price", "unit", "kcal", "protein", "fat", "carbs",
    "is_staple", "is_vegetable", "is_self_service", "tags", "note",
]


def to_float(value, default=0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_int(value, default=0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def ensure_canteen(campus: str, name: str) -> int:
    row = db.query_one("SELECT id FROM canteens WHERE campus = ? AND name = ?", (campus, name))
    if row:
        return row["id"]
    return db.execute("INSERT INTO canteens (campus, name, note) VALUES (?, ?, '')",
                      (campus, name))


def ensure_window(canteen_id: int, name: str, floor: str = "", hours: str = "") -> int:
    row = db.query_one("SELECT id FROM windows WHERE canteen_id = ? AND name = ?",
                       (canteen_id, name))
    if row:
        db.execute("UPDATE windows SET floor = COALESCE(NULLIF(?, ''), floor), "
                   "open_hours = COALESCE(NULLIF(?, ''), open_hours) WHERE id = ?",
                   (floor, hours, row["id"]))
        return row["id"]
    return db.execute(
        "INSERT INTO windows (canteen_id, name, floor, open_hours) VALUES (?, ?, ?, ?)",
        (canteen_id, name, floor, hours),
    )


def export_csv(path: str) -> int:
    rows = db.query(
        """
        SELECT c.campus, c.name AS canteen, w.name AS window, w.floor, w.open_hours,
               d.name, d.category, d.price, d.unit, d.kcal, d.protein, d.fat, d.carbs,
               d.is_staple, d.is_vegetable, d.is_self_service, d.tags, d.note
        FROM dishes d JOIN windows w ON w.id = d.window_id
        JOIN canteens c ON c.id = w.canteen_id
        WHERE d.active = 1 ORDER BY c.campus, c.name, w.name, d.name
        """
    )
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in COLUMNS})
    return len(rows)


def import_csv(path: str, replace: bool = False, dry_run: bool = False) -> dict:
    if not os.path.isfile(path):
        raise SystemExit(f"找不到文件：{path}")
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SystemExit(f"CSV 缺少列：{', '.join(missing)}")
        rows = list(reader)

    stats = {"rows": len(rows), "inserted": 0, "updated": 0, "skipped": 0}
    if dry_run:
        for row in rows:
            if not (row.get("name") or "").strip():
                stats["skipped"] += 1
        return stats

    if replace:
        db.execute("DELETE FROM dishes")
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            stats["skipped"] += 1
            continue
        campus = (row.get("campus") or "").strip() or "本部"
        canteen = (row.get("canteen") or "").strip()
        window = (row.get("window") or "").strip() or "默认窗口"
        if not canteen:
            stats["skipped"] += 1
            continue

        protein = to_float(row.get("protein"))
        fat = to_float(row.get("fat"))
        carbs = to_float(row.get("carbs"))
        kcal = to_float(row.get("kcal")) or round(protein * 4 + fat * 9 + carbs * 4, 1)

        canteen_id = ensure_canteen(campus, canteen)
        window_id = ensure_window(canteen_id, window,
                                  (row.get("floor") or "").strip(),
                                  (row.get("open_hours") or "").strip())
        values = (
            (row.get("category") or "其他").strip(), to_float(row.get("price")),
            (row.get("unit") or "份").strip(), kcal, protein, fat, carbs,
            to_int(row.get("is_staple")), to_int(row.get("is_vegetable")),
            to_int(row.get("is_self_service")), (row.get("tags") or "").strip(),
            (row.get("note") or "").strip(),
        )
        existing = db.query_one(
            "SELECT id FROM dishes WHERE name = ? AND window_id = ?", (name, window_id))
        if existing:
            db.execute(
                """UPDATE dishes SET category=?, price=?, unit=?, kcal=?, protein=?, fat=?,
                   carbs=?, is_staple=?, is_vegetable=?, is_self_service=?, tags=?, note=?,
                   active=1 WHERE id=?""",
                values + (existing["id"],),
            )
            stats["updated"] += 1
        else:
            db.execute(
                """INSERT INTO dishes (category, price, unit, kcal, protein, fat, carbs,
                   is_staple, is_vegetable, is_self_service, tags, note, name, window_id, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                values + (name, window_id),
            )
            stats["inserted"] += 1
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="安工大用餐推荐 · 菜品数据导入导出")
    parser.add_argument("--file", help="要导入的 CSV 路径")
    parser.add_argument("--export", help="导出到指定 CSV 路径")
    parser.add_argument("--replace", action="store_true", help="导入前清空现有菜品")
    parser.add_argument("--dry-run", action="store_true", help="只检查不写库")
    args = parser.parse_args()

    db.init_db()
    if args.export:
        count = export_csv(args.export)
        print(f"已导出 {count} 道菜 -> {args.export}")
        return
    if args.file:
        stats = import_csv(args.file, replace=args.replace, dry_run=args.dry_run)
        print(("（dry-run）" if args.dry_run else "") +
              f"共 {stats['rows']} 行：新增 {stats['inserted']}、更新 {stats['updated']}、跳过 {stats['skipped']}")
        return
    parser.print_help()


if __name__ == "__main__":
    main()
