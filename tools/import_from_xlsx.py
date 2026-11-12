"""把「PocketAHUT 食堂菜品价格」Excel 导入为本系统的菜品库。

Excel 里只有：校区 / 食堂楼层 / 窗口 / 菜品 / 售价 / 分类 / 备注，**没有营养成分**。
本脚本按「分类基准 + 菜名关键词」估算每份的蛋白质/脂肪/碳水（kcal 由 4/9/4 换算），
估算口径见 ESTIMATE_DOC，导入后每道菜的备注里会标注「营养为估算」。

用法：
    python tools/import_from_xlsx.py "data/source/食堂菜品价格.xlsx"          # 预览
    python tools/import_from_xlsx.py "data/source/食堂菜品价格.xlsx" --apply  # 写入数据库（整体替换）
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "app"))
sys.path.insert(0, os.path.join(BASE_DIR, "tools"))

import db  # noqa: E402
from import_dishes import COLUMNS, import_csv  # noqa: E402
from xlsx_to_csv import read_workbook  # noqa: E402

ESTIMATE_DOC = "营养值按分类与菜名估算，仅供参考"

# 分类 → (蛋白 g, 脂肪 g, 碳水 g)，按"一份"的常见分量
CATEGORY_BASE = {
    "套餐饭": (18, 16, 80),      # 一份带菜的饭
    "屌丝套餐": (15, 14, 78),
    "配菜": (6, 5, 9),           # 小份配菜（¥1–3 居多）
    "小吃": (4, 6, 18),          # 麻团/糍粑/薯条这类
    "拌面": (12, 10, 72),
    "汤面": (11, 7, 68),
    "汤/拌面": (11, 8, 70),
    "面/粉类": (11, 9, 70),
    "粉类": (10, 7, 62),
    "饺子/馄饨": (13, 9, 50),
    "面点": (6, 3, 42),
    "饼类": (5, 10, 38),
    "粥类": (2, 0.5, 25),
    "包子": (7, 7, 30),
    "汤类": (3, 2, 6),
    "麻辣烫": (16, 18, 30),
    "加面/饭/粉": (4, 2, 50),
}
DEFAULT_BASE = (10, 10, 50)

MEAT_RULES = [
    (("牛肉", "牛腩", "牛排"), 6, 3),
    (("鸡", "鸭"), 5, 4),
    (("鱼", "虾", "鱿鱼", "带鱼"), 5, 2),
    (("排骨", "大排", "小排", "骨头", "骨"), 4, 6),
    (("肉", "猪", "里脊", "五花", "扣肉", "火腿", "培根", "香肠", "肠"), 5, 6),
    (("蛋", "鸡蛋"), 3, 2),
    (("豆腐", "豆干", "腐竹", "豆皮", "素鸡"), 4, 2),
]

VEG_WORDS = ("青菜", "白菜", "生菜", "菠菜", "油菜", "豆角", "土豆", "茄子",
             "番茄", "西红柿", "黄瓜", "萝卜", "冬瓜", "南瓜", "蘑菇", "香菇",
             "木耳", "海带", "豆芽", "西兰花", "花菜", "藕", "豆腐", "素")

CARB_WORDS = (("米饭", 12), ("饭", 10), ("面", 15), ("粉", 15), ("馒头", 12),
              ("饼", 12), ("粥", 8), ("包", 10), ("饺", 10), ("馄饨", 8),
              ("馍", 12), ("烧麦", 8))

FAT_WORDS = (("炸", 7), ("煎", 6), ("酥", 6), ("脆", 5), ("红烧", 5),
             ("回锅", 6), ("干锅", 6), ("干煸", 5), ("烤", 4), ("芝士", 4),
             ("奶油", 5), ("黄油", 5))

LIGHT_WORDS = ("清蒸", "蒸", "凉拌", "拌", "白灼", "水煮", "炖")

SNACK_WORDS = ("糍粑", "麻团", "卷", "蛋糕", "面包", "糕", "酥饼", "薯条", "薯",
               "鸡米花", "汉堡", "热狗", "冰淇淋", "奶茶", "甜", "糖", "饼")
SNACK_CATEGORIES = {"小吃", "饼类", "水果", "饮品", "零食"}
DRINK_WORDS = ("豆浆", "牛奶", "酸奶", "奶茶", "可乐", "雪碧", "果汁", "咖啡",
               "柠檬水", "饮料", "矿泉水", "茶饮", "奶昔", "冰淇淋")

STAPLE_CATEGORIES = {"套餐饭", "屌丝套餐", "面点", "包子", "饼类", "粥类", "拌面",
                     "汤面", "汤/拌面", "面/粉类", "粉类", "饺子/馄饨", "加面/饭/粉"}
STAPLE_NAME_WORDS = ("饭", "面", "粉", "馒头", "包", "粥", "饼", "饺", "馄饨", "馍")


def estimate(name: str, category: str, note: str = "") -> dict:
    protein, fat, carbs = CATEGORY_BASE.get(category, DEFAULT_BASE)

    for words, dp, df in MEAT_RULES:
        if any(w in name for w in words):
            protein += dp
            fat += df

    for word, dc in CARB_WORDS:
        if word in name:
            carbs += dc
            break

    for word, df in FAT_WORDS:
        if word in name:
            fat += df
            break

    if any(w in name for w in LIGHT_WORDS):
        fat = max(2.0, fat - 5)
    if any(w in name for w in VEG_WORDS):
        carbs += 3
        protein += 1

    scale = 1.0
    if "大份" in note or "加量" in note:
        scale = 1.25
    elif "小份" in note or "半份" in note:
        scale = 0.8
    protein = max(1.0, protein * scale)
    fat = max(0.5, fat * scale)
    carbs = max(1.0, carbs * scale)

    kcal = round(protein * 4 + fat * 9 + carbs * 4, 1)
    kcal = min(max(kcal, 60.0), 950.0)

    has_meat = any(w in name for w in ("肉", "鸡", "鸭", "鱼", "虾", "牛", "猪", "排", "骨", "蛋", "肠", "培根"))
    is_drink = any(w in name for w in DRINK_WORDS)
    is_snack = ((category in SNACK_CATEGORIES and category not in ("饼类", "包子"))
                or is_drink)
    is_staple = 0 if is_drink else (
        1 if (category in STAPLE_CATEGORIES
              or any(w in name for w in STAPLE_NAME_WORDS)) else 0)
    is_vegetable = 1 if (not has_meat and not is_snack
                         and (any(w in name for w in VEG_WORDS) or category == "配菜")) else 0
    is_self_service = 1 if ("麻辣烫" in name or "麻辣烫" in category
                            or "自助" in note or "称重" in note) else 0

    tags = [category] if category else []
    if any(w in name for w in ("辣", "麻")):
        tags.append("辣")
    if any(w in name for w in ("炸", "煎", "酥", "脆")):
        tags.append("油炸")
    if any(w in name for w in LIGHT_WORDS):
        tags.append("清淡")
    if has_meat:
        tags.append("含肉")
    if is_vegetable:
        tags.append("素")

    return {
        "protein": round(protein, 1),
        "fat": round(fat, 1),
        "carbs": round(carbs, 1),
        "kcal": kcal,
        "is_staple": is_staple,
        "is_vegetable": is_vegetable,
        "is_self_service": is_self_service,
        "tags": ",".join(dict.fromkeys(tags)),
    }


def build_rows(xlsx_path: str) -> list[dict]:
    book = read_workbook(xlsx_path)
    sheet = "菜品价格" if "菜品价格" in book else next(iter(book))
    rows = book[sheet]
    header_index = next(i for i, r in enumerate(rows) if r and r[0].strip() == "校区")
    header = rows[header_index]
    index = {name: i for i, name in enumerate(header)}

    out: list[dict] = []
    for raw in rows[header_index + 1:]:
        def cell(key: str) -> str:
            i = index.get(key)
            return raw[i].strip() if i is not None and i < len(raw) else ""

        name = cell("菜品")
        campus = cell("校区")
        canteen = cell("食堂／楼层")
        window = cell("窗口")
        if not (name and campus and canteen):
            continue
        price_text = cell("售价（元）")
        try:
            price = float(price_text)
        except ValueError:
            price = 0.0
        category = cell("分类") or "其他"
        note = cell("备注／规格")
        est = estimate(name, category, note)
        merged_note = "；".join([t for t in (note, ESTIMATE_DOC) if t])
        out.append({
            "campus": campus,
            "canteen": canteen,
            "window": window or "默认窗口",
            "floor": "",
            "open_hours": "",
            "name": name,
            "category": category,
            "price": price,
            "unit": "份",
            "kcal": est["kcal"],
            "protein": est["protein"],
            "fat": est["fat"],
            "carbs": est["carbs"],
            "is_staple": est["is_staple"],
            "is_vegetable": est["is_vegetable"],
            "is_self_service": est["is_self_service"],
            "tags": est["tags"],
            "note": merged_note,
        })
    return out


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="导入真实食堂菜品数据")
    parser.add_argument("xlsx")
    parser.add_argument("--apply", action="store_true", help="写入数据库（会清空现有菜品）")
    parser.add_argument("--csv", default=os.path.join(BASE_DIR, "data", "dishes_real.csv"))
    parser.add_argument("--sample", type=int, default=8, help="预览条数")
    args = parser.parse_args()

    rows = build_rows(args.xlsx)
    print(f"解析到 {len(rows)} 道菜")
    print(f"已写出 CSV -> {args.csv}")
    write_csv(rows, args.csv)

    print(f"\n前 {args.sample} 条预览（菜名 | 分类 | 价格 | 蛋白/脂肪/碳水 | kcal | 主食/素/自助）:")
    for row in rows[: args.sample]:
        print("  {name} | {category} | ¥{price} | {protein}/{fat}/{carbs} | {kcal} | {is_staple}{is_vegetable}{is_self_service}"
              .format(**row))

    if args.apply:
        db.init_db()
        stats = import_csv(args.csv, replace=True)
        print(f"\n导入完成：新增 {stats['inserted']}，更新 {stats['updated']}，跳过 {stats['skipped']}")


if __name__ == "__main__":
    main()
