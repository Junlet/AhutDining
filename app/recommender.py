"""推荐引擎：套餐组合、打分、批次刷新、自助选菜评级、剩余配额调整。

一条推荐链路的顺序是：
    用户资料 → BMR/TDEE → 每日目标 → 扣除今天已吃 → 本餐目标
    → 按食堂枚举「主食 + 荤菜 + 素菜 (+ 汤)」组合
    → 逐项打分（热量/蛋白/脂肪/碳水/多样性/口味）
    → 取分数最高的若干套，跨食堂去重后返回
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Iterable, Optional

import db
import nutrition

# 每个角色最多参与枚举的候选菜数（控制组合爆炸）
ROLE_TOP_K = 5

ROLE_RULES = {
    "breakfast": [
        ("staple", ("早餐", "主食", "套餐"), 1),
        ("drink", ("饮品", "水果", "汤"), 0),
    ],
    "lunch": [
        ("staple", ("主食", "套餐"), 1),
        ("meat", ("荤菜", "小吃"), 1),
        ("veg", ("素菜", "汤"), 1),
    ],
    "dinner": [
        ("staple", ("主食", "套餐"), 1),
        ("meat", ("荤菜", "小吃"), 1),
        ("veg", ("素菜", "汤"), 1),
    ],
    "snack": [
        ("snack", ("水果", "饮品", "小吃", "汤"), 1),
    ],
}


# ── 取数 ────────────────────────────────────────────────────────────────
def load_dishes() -> list[dict]:
    """所有在售菜品 + 窗口/食堂信息 + 单项营养分数 + 口味分。"""
    rows = db.query(
        """
        SELECT d.*, w.name AS window_name, w.floor, w.open_hours,
               c.id AS canteen_id, c.name AS canteen_name, c.campus
        FROM dishes d
        JOIN windows w ON w.id = d.window_id
        JOIN canteens c ON c.id = w.canteen_id
        WHERE d.active = 1
        ORDER BY c.id, w.id, d.id
        """
    )
    fav = {r["dish_id"] for r in db.query("SELECT dish_id FROM favorites")}
    ratings = {
        r["dish_id"]: r for r in db.query(
            "SELECT dish_id, COUNT(*) AS cnt, AVG(rating) AS avg FROM comments GROUP BY dish_id"
        )
    }
    images = {}
    for r in db.query("SELECT owner_id, path FROM images WHERE owner_type = 'dish' ORDER BY id DESC"):
        images.setdefault(r["owner_id"], r["path"])

    for row in rows:
        scores = nutrition.dish_scores(row)
        row["scores"] = scores
        row["favorite"] = 1 if row["id"] in fav else 0
        stat = ratings.get(row["id"])
        row["rating_count"] = stat["cnt"] if stat else 0
        row["rating_avg"] = round(stat["avg"], 2) if stat else None
        row["image"] = images.get(row["id"])
        # 口味分：收藏 0.6 + 历史评分（归一化到 0.4）
        taste = 0.6 * row["favorite"]
        if row["rating_avg"]:
            taste += 0.4 * (row["rating_avg"] / 5.0)
        row["taste_score"] = round(min(1.0, taste), 3)
        row["is_self_service"] = int(row["is_self_service"])
        row["tags_list"] = [t for t in (row.get("tags") or "").split(",") if t]
    return rows


def window_ratings() -> dict[int, dict]:
    """窗口评分：把该窗口下所有菜品评论汇总（用于「窗口评分排行榜」）。"""
    rows = db.query(
        """
        SELECT w.id AS window_id, w.name AS window_name, c.name AS canteen_name,
               c.campus, COUNT(cm.id) AS cnt, AVG(cm.rating) AS avg
        FROM windows w
        JOIN canteens c ON c.id = w.canteen_id
        LEFT JOIN dishes d ON d.window_id = w.id
        LEFT JOIN comments cm ON cm.dish_id = d.id
        GROUP BY w.id ORDER BY avg DESC NULLS LAST
        """
    )
    return {r["window_id"]: r for r in rows}


# ── 目标 ────────────────────────────────────────────────────────────────
def meal_of(profile: dict, slot: str) -> dict:
    metrics = nutrition.profile_metrics(profile)
    for meal in metrics["meals"]:
        if meal["slot"] == slot:
            return meal
    # 当天没有该时段（例如只吃两餐时没有早餐），退化为最接近的一餐
    return metrics["meals"][0]


def consumed_today(date: str) -> dict:
    rows = db.query(
        "SELECT kcal, protein, fat, carbs FROM meal_records "
        "WHERE date = ? AND status IN ('confirmed','manual')",
        (date,),
    )
    return {
        "kcal": round(sum(r["kcal"] for r in rows), 1),
        "protein": round(sum(r["protein"] for r in rows), 1),
        "fat": round(sum(r["fat"] for r in rows), 1),
        "carbs": round(sum(r["carbs"] for r in rows), 1),
    }


def effective_target(profile: dict, date: str, slot: str) -> tuple[dict, dict]:
    """当前这一餐的实际目标 = 今日剩余额度 × 当前餐在剩余餐次中的占比。

    这样"早上吃多了，中午自动吃得轻一些"，并把已吃掉的量体现在推荐结果里。
    注意：不会因为"上一餐没吃"就把目标放大成整天的量——缩放系数被夹在
    [0.5, 1.35] 之间，既体现偏差又保持每餐目标合理。
    """
    metrics = nutrition.profile_metrics(profile)
    daily = metrics["daily"]
    consumed = consumed_today(date)
    meals = metrics["meals"]

    matched = next((m for m in meals if m["slot"] == slot), None)
    if matched:
        cur = matched
        idx = meals.index(matched)
        share = cur["share"]
        meal_name = cur["name"]
        meal_index = cur["index"]
    else:
        # 例如只吃三餐时用户点开"加餐"：给一个独立的轻量目标
        share = 0.12
        meal_name = nutrition.SLOT_LABELS.get(slot, "加餐")
        meal_index = len(meals) + 1
        idx = len(meals)

    remaining_share = sum(m["share"] for m in meals[idx:]) or share

    def scale(total: float, eaten: float) -> float:
        nominal = total * share
        if eaten <= 0:
            return nominal
        available = max(total * 0.2, total - eaten)
        value = available * share / remaining_share
        return max(nominal * 0.5, min(value, nominal * 1.35))

    kcal = scale(daily["kcal"], consumed["kcal"])
    protein = scale(daily["protein"], consumed["protein"])
    fat = scale(daily["fat"], consumed["fat"])
    carbs = scale(daily["carbs"], consumed["carbs"])

    target = {
        "kcal": round(kcal, 1),
        "protein": round(protein, 1),
        "fat": round(fat, 1),
        "carbs": round(carbs, 1),
        "slot": slot,
        "meal_name": meal_name,
        "meal_index": meal_index,
        "share": share,
    }
    context = {
        "daily": daily,
        "consumed": consumed,
        "remaining": {
            "kcal": round(max(0.0, daily["kcal"] - consumed["kcal"]), 1),
            "protein": round(max(0.0, daily["protein"] - consumed["protein"]), 1),
            "fat": round(max(0.0, daily["fat"] - consumed["fat"]), 1),
            "carbs": round(max(0.0, daily["carbs"] - consumed["carbs"]), 1),
        },
        "adjusted": consumed["kcal"] > 0,
    }
    return target, context


# ── 组合与打分 ──────────────────────────────────────────────────────────
def _role_pool(dishes: list[dict], categories: Iterable[str]) -> list[dict]:
    cats = set(categories)
    pool = [d for d in dishes if d["category"] in cats]
    pool.sort(key=lambda d: d["scores"]["total"], reverse=True)
    return pool[:ROLE_TOP_K]


def _bundle(items: list[dict]) -> dict:
    totals = {
        "kcal": round(sum(i["kcal"] for i in items), 1),
        "protein": round(sum(i["protein"] for i in items), 1),
        "fat": round(sum(i["fat"] for i in items), 1),
        "carbs": round(sum(i["carbs"] for i in items), 1),
        "price": round(sum(i["price"] for i in items), 1),
    }
    return totals


def _variety_score(items: list[dict]) -> float:
    """一份套餐里有主食、有荤、有素、再加汤，得分更高。"""
    cats = [i["category"] for i in items]
    has_staple = any(c in ("主食", "套餐", "早餐", "自助") for c in cats)
    has_meat = any(c in ("荤菜", "小吃") for c in cats)
    has_veg = any(c in ("素菜", "汤") for c in cats) or any(i["is_vegetable"] for i in items)
    score = 0.35 * has_staple + 0.35 * has_meat + 0.30 * has_veg
    # 全是主食（比如两份米饭）扣分
    if cats.count("主食") > 1:
        score -= 0.2
    return max(0.0, min(1.0, score))


def score_bundle(items: list[dict], target: dict) -> dict:
    totals = _bundle(items)
    evaluation = nutrition.evaluate_totals(totals, target)
    variety = _variety_score(items)
    taste = sum(i["taste_score"] for i in items) / max(1, len(items))
    # 价格分：10 元以内最舒服，超过 25 元逐步扣分
    price_score = max(0.0, 1.0 - max(0.0, totals["price"] - 10.0) / 20.0)
    total = (
        0.45 * evaluation["total"]
        + 0.18 * variety
        + 0.12 * taste
        + 0.10 * price_score
        + 0.15 * (sum(i["scores"]["total"] for i in items) / max(1, len(items)))
    )
    return {
        "scores": {
            "total": round(total, 3),
            "nutrition": evaluation["total"],
            "energy": evaluation["energy"],
            "protein": evaluation["protein"],
            "fat": evaluation["fat"],
            "carbs": evaluation["carbs"],
            "variety": round(variety, 3),
            "taste": round(taste, 3),
            "price": round(price_score, 3),
        },
        "evaluation": evaluation,
        "advice": nutrition.advice(evaluation, target),
        "totals": totals,
        "macro_pct": evaluation["macro_pct"],
    }


def _item_view(dish: dict) -> dict:
    return {
        "dish_id": dish["id"],
        "name": dish["name"],
        "category": dish["category"],
        "price": dish["price"],
        "unit": dish["unit"],
        "kcal": dish["kcal"],
        "protein": dish["protein"],
        "fat": dish["fat"],
        "carbs": dish["carbs"],
        "is_self_service": dish["is_self_service"],
        "is_staple": int(dish.get("is_staple") or 0),
        "is_vegetable": int(dish.get("is_vegetable") or 0),
        "tags": dish["tags_list"],
        "image": dish["image"],
        "favorite": dish["favorite"],
        "scores": dish["scores"],
        "window": dish["window_name"],
        "floor": dish["floor"],
        "canteen": dish["canteen_name"],
        "campus": dish["campus"],
        "taste_score": dish["taste_score"],
        "rating_avg": dish["rating_avg"],
        "rating_count": dish["rating_count"],
    }


def _top(items: list[dict], categories: Iterable[str], k: int = ROLE_TOP_K) -> list[dict]:
    cats = set(categories)
    pool = [d for d in items if d["category"] in cats]
    pool.sort(key=lambda d: d["scores"]["total"], reverse=True)
    return pool[:k]


def _extend(items: list[dict], pool: list[dict], target: dict,
            limit: int = 8, max_items: int = 6) -> list[dict]:
    """热量不够时反复贪心补菜，直到接近目标或再加也没有提升。

    单餐目标很大时（例如一天只吃两餐，每餐要扛半天）允许补到 6 样，
    否则一份"主食+荤+素"最多 900 kcal 左右，永远够不着目标。
    """
    target_kcal = float(target["kcal"])
    if _bundle(items)["kcal"] >= target_kcal * 0.88:
        return items

    chosen = {i["id"] for i in items}
    best_score = score_bundle(items, target)["scores"]["total"]
    guard = 0
    while len(items) < max_items and _bundle(items)["kcal"] < target_kcal * 0.92 and guard < 10:
        guard += 1
        best, best_score_new = None, best_score
        for cand in pool[:limit]:
            if cand["id"] in chosen:
                continue
            trial = items + [cand]
            score = score_bundle(trial, target)["scores"]["total"]
            if score > best_score_new + 1e-6:
                best, best_score_new = trial, score
        if best is None:
            break
        items = best
        best_score = best_score_new
        chosen.add(best[-1]["id"])
    return items


def _dedupe_key(combo: list[dict]) -> tuple:
    return tuple(sorted(i["id"] for i in combo))


def _dedupe_key_plan(plan: dict) -> tuple:
    return tuple(sorted(i["dish_id"] for i in plan["items"]))


def candidate_plans(slot: str, target: dict, dishes: list[dict]) -> list[dict]:
    """枚举某餐次的所有可行组合并打分，按分数降序返回（不含 id/batch 等元数据）。"""
    by_canteen: dict[int, list[dict]] = {}
    for dish in dishes:
        by_canteen.setdefault(dish["canteen_id"], []).append(dish)

    candidates: list[dict] = []
    seen: set[tuple] = set()

    for canteen_id, canteen_dishes in by_canteen.items():
        if slot == "breakfast":
            staples = _top(canteen_dishes, ("早餐", "主食"), k=5)
            drinks = _top(canteen_dishes, ("饮品", "水果", "汤"), k=3)
            if not staples:
                continue
            raw_combos = [[s] for s in staples]
            raw_combos += [[s, d] for s in staples for d in drinks]
        elif slot == "snack":
            pool = _top(canteen_dishes, ("水果", "饮品", "小吃"), k=4)
            if not pool:
                continue
            raw_combos = [[p] for p in pool] + [[a, b] for a in pool[:2] for b in pool[1:3]
                                                if a["id"] != b["id"]]
        else:
            staples = _top(canteen_dishes, ("主食", "套餐"), k=4)
            meats = _top(canteen_dishes, ("荤菜", "小吃"), k=4)
            greens = _top(canteen_dishes, ("素菜", "汤"), k=4)
            soups = _top(canteen_dishes, ("汤",), k=3)
            if not (staples and meats and greens):
                continue
            raw_combos = []
            for s in staples:
                for m in meats:
                    for v in greens:
                        base = [s, m, v]
                        raw_combos.append(base)
                        for soup in soups[:1]:
                            if soup["id"] not in {s["id"], m["id"], v["id"]}:
                                raw_combos.append(base + [soup])
                        for extra in meats:
                            if extra["id"] != m["id"]:
                                raw_combos.append(base + [extra])

        pool_all = sorted(canteen_dishes, key=lambda d: d["scores"]["total"], reverse=True)
        for combo in raw_combos:
            key = _dedupe_key(combo)
            if key in seen:
                continue
            seen.add(key)
            used = {i["id"] for i in combo}
            extras = [d for d in pool_all if d["id"] not in used]
            big_meal = float(target.get("kcal") or 0) >= 1000
            if slot in ("lunch", "dinner"):
                # 补菜只补素菜或汤；只有"一餐扛半天"的大目标才允许再补一份主食
                allowed = ("素菜", "汤") + (("主食",) if big_meal else ())
                extras = [d for d in extras if d["category"] in allowed]
                if big_meal:
                    # 最多再补一份主食，避免"三碗米饭"这种不合理的组合
                    staples_extra = [d for d in extras if d["category"] == "主食"][:1]
                    extras = staples_extra + [d for d in extras if d["category"] != "主食"]
            elif slot == "snack":
                extras = [d for d in extras if d["category"] in ("水果", "饮品", "小吃")]
            else:  # 早餐：可以配饮品/水果/小吃/小份主食，但不再叠一份大主食
                extras = [d for d in extras
                          if d["category"] in ("饮品", "水果", "小吃", "早餐", "汤")]
            combo = _extend(combo, extras, target)
            scored = score_bundle(combo, target)
            first = combo[0]
            candidates.append({
                "slot": slot,
                "canteen": {
                    "id": canteen_id,
                    "name": first["canteen_name"],
                    "campus": first["campus"],
                },
                "items": [_item_view(i) for i in combo],
                **scored,
            })

    candidates.sort(key=lambda c: c["scores"]["total"], reverse=True)
    return candidates


def build_plans(slot: str, date: str, batch_no: int, count: int = 6) -> list[dict]:
    """枚举并打分，返回若干套推荐套餐（已按分数排序、跨食堂去重）。"""
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = 1") or {}
    target, context = effective_target(profile, date, slot)
    dishes = load_dishes()
    candidates = candidate_plans(slot, target, dishes)
    if not candidates:
        return []

    rng = random.Random(f"{date}-{slot}-{batch_no}")

    # 刷新要能真的换内容：在"分数最高的若干套"里按批次做加权随机抽样，
    # 同时优先覆盖不同食堂，避免每次刷新都是同一批。
    # 候选池：全局前列 + 每个食堂自己的前列，保证既有最优也有跨食堂的选择
    per_canteen: dict[int, list[dict]] = {}
    for cand in candidates:
        per_canteen.setdefault(cand["canteen"]["id"], []).append(cand)
    pool: list[dict] = list(candidates[: max(8, count)])
    for lst in per_canteen.values():
        pool.extend(lst[: max(3, count // 2)])
    seen_pool: set[tuple] = set()
    uniq: list[dict] = []
    for cand in pool:
        key = _dedupe_key_plan(cand)
        if key in seen_pool:
            continue
        seen_pool.add(key)
        uniq.append(cand)
    pool = uniq

    picked: list[dict] = []
    used_keys: set[tuple] = set()
    used_canteens: set[int] = set()

    def _pick(candidates_pool: list[dict], ignore_canteen: bool) -> None:
        items = [c for c in candidates_pool
                 if _dedupe_key_plan(c) not in used_keys
                 and (ignore_canteen or c["canteen"]["id"] not in used_canteens)]
        while items and len(picked) < count:
            weights = [max(0.02, c["scores"]["total"]) ** 4 for c in items]
            total = sum(weights)
            roll = rng.random() * total
            acc = 0.0
            chosen = items[-1]
            for idx, weight in enumerate(weights):
                acc += weight
                if acc >= roll:
                    chosen = items[idx]
                    break
            picked.append(chosen)
            used_keys.add(_dedupe_key_plan(chosen))
            used_canteens.add(chosen["canteen"]["id"])
            items = [c for c in items
                     if c is not chosen
                     and (ignore_canteen or c["canteen"]["id"] not in used_canteens)]

    # 第一名固定给全局最优，其余按"先跨食堂、再允许重复"的顺序抽取
    if candidates:
        picked.append(candidates[0])
        used_keys.add(_dedupe_key_plan(candidates[0]))
        used_canteens.add(candidates[0]["canteen"]["id"])
    _pick(pool, ignore_canteen=False)
    _pick(pool, ignore_canteen=True)
    if len(picked) < count:                    # 池子太小则放宽到全部候选
        _pick(candidates, ignore_canteen=True)

    picked.sort(key=lambda c: c["scores"]["total"], reverse=True)

    for idx, plan in enumerate(picked, start=1):
        plan["id"] = f"{date}-{slot}-{batch_no}-{idx}"
        plan["batch"] = batch_no
        plan["date"] = date
        plan["target"] = target
        plan["context"] = context
        plan["meal_name"] = target["meal_name"]
    return picked


def get_or_create_batch(slot: str, date: str, batch_no: Optional[int] = None) -> dict:
    """取某一时段的推荐批次；不存在就按 batch_no（默认 1）生成并落库。"""
    batch_no = batch_no or 1
    row = db.query_one(
        "SELECT * FROM plan_batches WHERE user_id = 1 AND date = ? AND slot = ? AND batch_no = ?",
        (date, slot, batch_no),
    )
    if row:
        plans = json.loads(row["plans_json"])
        if plans:
            return {"date": date, "slot": slot, "batch": batch_no, "plans": plans}

    count = int(db.setting("plan.count", "6") or 6)
    plans = build_plans(slot, date, batch_no, count=count)
    db.execute(
        "INSERT INTO plan_batches (user_id, date, slot, batch_no, plans_json, created_at) "
        "VALUES (1, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, date, slot, batch_no) "
        "DO UPDATE SET plans_json = excluded.plans_json, created_at = excluded.created_at",
        (date, slot, batch_no, json.dumps(plans, ensure_ascii=False), db.now()),
    )
    return {"date": date, "slot": slot, "batch": batch_no, "plans": plans}


def next_batch_no(slot: str, date: str) -> int:
    row = db.query_one(
        "SELECT MAX(batch_no) AS m FROM plan_batches WHERE user_id = 1 AND date = ? AND slot = ?",
        (date, slot),
    )
    return int((row["m"] or 0) + 1)


def evaluate_custom(dish_ids: list[int], slot: str, date: Optional[str] = None) -> dict:
    """需求 8：用户自行选菜（如麻辣烫），给出推荐度与改进建议。"""
    date = date or db.today()
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = 1") or {}
    target, context = effective_target(profile, date, slot)
    dishes = {d["id"]: d for d in load_dishes()}
    items = [dishes[i] for i in dish_ids if i in dishes]
    if not items:
        raise ValueError("没有选中任何有效菜品")

    scored = score_bundle(items, target)
    # 自助类菜品提示按实际取餐量修正
    tips = list(scored["advice"])
    if any(i["is_self_service"] for i in items):
        tips.append("自选/自助类按「一份」估算，实际取餐重量不同请自行折算")
    return {
        "items": [_item_view(i) for i in items],
        "target": target,
        "context": context,
        "recommend_level": _level(scored["scores"]["total"]),
        **scored,
        "advice": tips,
    }


def _level(score: float) -> str:
    if score >= 0.85:
        return "强烈推荐"
    if score >= 0.70:
        return "推荐"
    if score >= 0.55:
        return "一般"
    return "不推荐"


def current_slot(now: Optional[datetime] = None) -> dict:
    """按设置里的时间点判断当前处于哪一餐时段（需求 11 的"固定时间点"）。"""
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    order = ["breakfast", "lunch", "dinner"]
    windows = []
    for slot in order:
        start = db.setting(f"slot.{slot}.start", "06:30")
        end = db.setting(f"slot.{slot}.end", "20:00")
        windows.append((slot, start, end))

    for slot, start, end in windows:
        if start <= hhmm <= end:
            return {"slot": slot, "label": nutrition.SLOT_LABELS[slot],
                    "in_window": True, "now": hhmm, "window": f"{start}-{end}"}

    # 不在任何时段：返回下一餐（跨天则回到早餐）
    for slot, start, end in windows:
        if hhmm < start:
            return {"slot": slot, "label": nutrition.SLOT_LABELS[slot],
                    "in_window": False, "now": hhmm, "window": f"{start}-{end}",
                    "hint": f"当前不在用餐时段，展示{nutrition.SLOT_LABELS[slot]}推荐"}
    slot, start, end = windows[0]
    return {"slot": slot, "label": nutrition.SLOT_LABELS[slot], "in_window": False,
            "now": hhmm, "window": f"{start}-{end}", "hint": "今日用餐时段已结束，展示明日早餐"}


def search_dishes(q: str = "", canteen: str = "", category: str = "",
                  window: str = "", sort: str = "score") -> list[dict]:
    dishes = load_dishes()
    q = (q or "").strip().lower()
    out = []
    for d in dishes:
        if q and q not in d["name"].lower() and q not in (d.get("tags") or "").lower():
            continue
        if canteen and d["canteen_name"] != canteen:
            continue
        if category and d["category"] != category:
            continue
        if window and d["window_name"] != window:
            continue
        out.append(d)
    if sort == "price":
        out.sort(key=lambda d: d["price"])
    elif sort == "protein":
        out.sort(key=lambda d: d["protein"], reverse=True)
    elif sort == "kcal":
        out.sort(key=lambda d: d["kcal"])
    else:
        out.sort(key=lambda d: d["scores"]["total"], reverse=True)
    return out

# ── 今日用餐计划（方案 A/B/C → 选定 → 可单餐替换） ──────────────────────
DAY_OPTION_LABELS = ["A", "B", "C", "D", "E"]


def meal_windows() -> dict[str, tuple[str, str]]:
    """从设置里取每个餐次的时间窗。"""
    out = {}
    for slot in ("breakfast", "lunch", "dinner"):
        start = db.setting(f"slot.{slot}.start", "06:30")
        end = db.setting(f"slot.{slot}.end", "20:00")
        out[slot] = (start, end)
    out["snack"] = ("00:00", "23:59")
    return out


def _now_hhmm() -> str:
    return datetime.now().strftime("%H:%M")


def meal_timeline(date: str) -> list[dict]:
    """今天每一餐的状态：已记录 / 已错过 / 待安排。"""
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = 1") or {}
    metrics = nutrition.profile_metrics(profile)
    windows = meal_windows()
    now = _now_hhmm()
    is_today = date == db.today()
    records = {
        r["meal_index"]: r for r in db.query(
            "SELECT * FROM meal_records WHERE user_id = 1 AND date = ?", (date,))
    }

    timeline = []
    for meal in metrics["meals"]:
        idx = meal["index"]
        slot = meal["slot"]
        start, end = windows.get(slot, ("00:00", "23:59"))
        record = records.get(idx)
        recorded = bool(record and record["status"] in ("confirmed", "manual"))
        passed = is_today and now > end
        is_now = start <= now <= end

        if recorded:
            status = "recorded"
        elif passed:
            status = "missed"
        else:
            status = "planned"

        timeline.append({
            "meal_index": idx,
            "meal_name": meal["name"],
            "slot": slot,
            "slot_label": nutrition.SLOT_LABELS.get(slot, meal["name"]),
            "share": meal["share"],
            "window": f"{start}-{end}",
            "status": status,
            "is_now": is_now,
            "recorded_items": json.loads(record["items_json"]) if record else [],
            "recorded_kcal": record["kcal"] if record else 0,
        })
    return timeline


def _pick_weighted(candidates: list[dict], rng: random.Random, k: int = 5) -> Optional[dict]:
    """在分数最高的 k 个候选里按 score^4 加权抽一个（保证刷新能换内容）。"""
    top = candidates[:k]
    if not top:
        return None
    weights = [max(0.02, c["scores"]["total"]) ** 4 for c in top]
    roll = rng.random() * sum(weights)
    acc = 0.0
    for cand, weight in zip(top, weights):
        acc += weight
        if acc >= roll:
            return cand
    return top[-1]


def _plan_meal_block(meal: dict, target: dict, items: list[dict], pinned: bool,
                     status: str, source: str = "auto") -> dict:
    if items:
        scored = score_bundle(items, target)
        return {
            **meal,
            "status": status,
            "pinned": pinned,
            "source": source,
            "items": items,
            "totals": scored["totals"],
            "scores": scored["scores"],
            "evaluation": scored["evaluation"],
            "macro_pct": scored["macro_pct"],
            "advice": scored["advice"],
            "level": _level(scored["scores"]["total"]),
            "target": target,
        }
    return {
        **meal,
        "status": status,
        "pinned": pinned,
        "source": source,
        "items": [],
        "totals": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0, "price": 0},
        "scores": {"total": 0},
        "evaluation": {},
        "macro_pct": {"protein": 0, "fat": 0, "carbs": 0},
        "advice": ["这个时段暂时凑不出合适的组合，可到「搜索」页挑一道菜，再用「选为这一餐」替换"],
        "level": "不推荐",
        "target": target,
    }


def summarize_plan(plan: dict) -> dict:
    """重算整日汇总（供方案生成、单餐替换、确认记录后复用）。"""
    blocks = plan.get("meals", [])
    counted = [b for b in blocks if b["status"] in ("planned", "recorded")]
    daily = plan["daily"]
    planned_kcal = sum(float(b["totals"]["kcal"]) for b in counted)
    scores = [float(b["scores"].get("total") or 0) for b in counted if b.get("items")]
    plan["planned_kcal"] = round(planned_kcal, 1)
    plan["remaining_indices"] = [b["meal_index"] for b in blocks if b["status"] == "planned"]
    plan["recorded_indices"] = [b["meal_index"] for b in blocks if b["status"] == "recorded"]
    plan["missed_indices"] = [b["meal_index"] for b in blocks if b["status"] == "missed"]
    plan["avg_score"] = round(sum(scores) / len(scores), 3) if scores else 0.0
    plan["level"] = _level(plan["avg_score"]) if scores else "不推荐"
    plan["summary"] = {
        "meals_total": len(blocks),
        "meals_remaining": len([b for b in blocks if b["status"] == "planned"]),
        "meals_recorded": len([b for b in blocks if b["status"] == "recorded"]),
        "meals_missed": len([b for b in blocks if b["status"] == "missed"]),
        "plan_kcal": round(planned_kcal, 1),
        "plan_protein": round(sum(float(b["totals"]["protein"]) for b in counted), 1),
        "plan_fat": round(sum(float(b["totals"]["fat"]) for b in counted), 1),
        "plan_carbs": round(sum(float(b["totals"]["carbs"]) for b in counted), 1),
        "plan_price": round(sum(float(b["totals"]["price"]) for b in counted), 1),
        "gap_kcal": round(float(daily["kcal"]) - planned_kcal, 1),
        "coverage": round(planned_kcal / float(daily["kcal"]), 3) if daily.get("kcal") else 0,
    }
    return plan


def build_day_plan(date: Optional[str] = None, pins: Optional[dict[int, list[dict]]] = None,
                   seed: int = 0, include_passed: bool = False,
                   force_include: Optional[set[int]] = None) -> dict:
    """生成一套整日方案。

    * 只安排「还没记录、且时间还没过」的餐次；已过时段且没记录的算"错过"（除非 include_passed）
    * 已被用户固定的餐次原样保留，其余餐次围绕它重新分配剩余额度
    """
    date = date or db.today()
    profile = db.query_one("SELECT * FROM profiles WHERE user_id = 1") or {}
    metrics = nutrition.profile_metrics(profile)
    daily = metrics["daily"]
    consumed = consumed_today(date)
    dishes = load_dishes()
    timeline = meal_timeline(date)

    pins = pins or {}
    force = set(force_include or ())
    remaining = [
        meal for meal in timeline
        if meal["status"] == "planned"
        or (include_passed and meal["status"] == "missed")
        or meal["meal_index"] in force
    ]
    total_share = sum(meal["share"] for meal in remaining) or 1.0

    avail = {
        key: max(0.0, float(daily[key]) - float(consumed[key]))
        for key in ("kcal", "protein", "fat", "carbs")
    }
    planned_totals = {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0, "price": 0.0}

    rng = random.Random(f"{date}-day-{seed}")
    blocks: list[dict] = []
    for meal in timeline:
        idx = meal["meal_index"]
        if meal["status"] == "recorded":
            blocks.append(_plan_meal_block(
                meal, {"kcal": meal["recorded_kcal"]}, meal["recorded_items"], False,
                "recorded", "recorded"))
            continue
        if meal["status"] == "missed" and not include_passed and idx not in force:
            blocks.append(_plan_meal_block(meal, {"kcal": 0}, [], False, "missed"))
            continue

        left_shares = sum(m["share"] for m in remaining
                          if m["meal_index"] >= idx) or total_share
        target = {}
        for key in ("kcal", "protein", "fat", "carbs"):
            left = max(0.0, avail[key] - planned_totals[key])
            target[key] = round(left * meal["share"] / left_shares, 1)
        target["kcal"] = max(200.0, target["kcal"])
        target.update({
            "slot": meal["slot"],
            "meal_name": meal["meal_name"],
            "meal_index": idx,
            "share": meal["share"],
        })

        if idx in pins and pins[idx]:
            block = _plan_meal_block(meal, target, pins[idx], True, "planned", "pinned")
        else:
            candidates = candidate_plans(meal["slot"], target, dishes)
            chosen = _pick_weighted(candidates, rng)
            block = _plan_meal_block(meal, target, chosen["items"] if chosen else [],
                                     False, "planned", "auto")

        for key in ("kcal", "protein", "fat", "carbs", "price"):
            planned_totals[key] += float(block["totals"].get(key) or 0)
        blocks.append(block)

    plan = {
        "date": date,
        "now": _now_hhmm(),
        "seed": seed,
        "include_passed": bool(include_passed),
        "daily": daily,
        "consumed": consumed,
        "meals": blocks,
        "suggest_include_passed": bool(
            not remaining and any(m["status"] == "missed" for m in timeline)),
    }
    return summarize_plan(plan)


# ── 状态存取 ────────────────────────────────────────────────────────────
def _load_state_row(date: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT data_json FROM day_plans WHERE user_id = 1 AND date = ?", (date,))
    if not row:
        return None
    try:
        data = json.loads(row["data_json"])
    except json.JSONDecodeError:
        return None
    if data.get("stage"):
        return data
    # 兼容旧格式（直接把 plan 存了进去）
    return {
        "stage": "chosen",
        "date": date,
        "seed": data.get("seed", 0),
        "include_passed": bool(data.get("include_passed")),
        "signature": data.get("signature"),
        "options": [],
        "plan": data,
    }


def _save_state_row(date: str, state: dict) -> None:
    db.execute(
        "INSERT INTO day_plans (user_id, date, data_json, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET data_json = excluded.data_json, "
        "updated_at = excluded.updated_at",
        (date, json.dumps(state, ensure_ascii=False), db.now()),
    )


def _signature(date: str, include_passed: bool) -> list:
    tl = meal_timeline(date)
    return [
        [m["meal_index"] for m in tl if m["status"] == "recorded"],
        [m["meal_index"] for m in tl if m["status"] == "missed"],
        bool(include_passed),
    ]


def _pins_from(plan: dict) -> dict[int, list[dict]]:
    return {b["meal_index"]: b["items"] for b in plan.get("meals", []) if b.get("pinned")}


def build_day_options(date: Optional[str] = None, seed: int = 0,
                      include_passed: bool = False, count: int = 3) -> list[dict]:
    """生成 A/B/C 三套整日方案（尽量互不相同，每套都自带汇总与推荐度）。"""
    date = date or db.today()
    options: list[dict] = []
    used: set[tuple] = set()
    attempt = 0
    while len(options) < count and attempt < count * 8:
        plan = build_day_plan(date, seed=seed + attempt * 37, include_passed=include_passed)
        attempt += 1
        key = tuple(sorted(
            tuple(sorted(i["dish_id"] for i in b["items"]))
            for b in plan["meals"] if b["status"] == "planned"
        ))
        if not key or key in used:
            continue
        used.add(key)
        plan["label"] = DAY_OPTION_LABELS[len(options)]
        options.append(plan)
    return options


def _state_include_passed(state: Optional[dict]) -> bool:
    return bool(state.get("include_passed")) if state else False


def _remaining_meal_count(date: str) -> int:
    return len([m for m in meal_timeline(date) if m["status"] == "planned"])


def _auto_include_passed(date: str, include_passed: bool) -> tuple[bool, bool]:
    """所有时段都过了但没任何记录时（深夜），自动把已过时段也算进方案。

    返回 (最终 include_passed, 是否自动开启)。
    """
    if include_passed:
        return True, False
    if _remaining_meal_count(date) == 0:
        return True, True
    return False, False


def get_day_state(date: Optional[str] = None) -> dict:
    """读取今天的状态：还没选方案时给 A/B/C；已选则回放选中的计划。

    如果"已记录 / 已错过"的餐次变了（比如刚确认了一餐、或时段过去了），
    自动重新生成：未选方案时重抽 A/B/C，已选方案时保留固定餐、重排其余。
    """
    date = date or db.today()
    state = _load_state_row(date)
    need_init = state is None

    include_passed, auto_passed = _auto_include_passed(date, _state_include_passed(state))
    sig = _signature(date, include_passed)

    if not need_init and state.get("signature") != sig:
        if state["stage"] == "options":
            state["options"] = build_day_options(date, state.get("seed", 0) + 1, include_passed)
            state["seed"] = state.get("seed", 0) + 1
        else:
            old_plan = state.get("plan") or {}
            pins = _pins_from(old_plan)
            label = old_plan.get("label")
            rebuilt = build_day_plan(date, pins=pins,
                                     seed=state.get("seed", 0) + 1,
                                     include_passed=include_passed)
            if label:
                rebuilt["label"] = label
            state["plan"] = rebuilt
            state["seed"] = state.get("seed", 0) + 1
        state["include_passed"] = include_passed
        state["auto_include_passed"] = auto_passed
        state["signature"] = sig
        state["date"] = date
        state["now"] = _now_hhmm()
        _save_state_row(date, state)
        return state

    if need_init:
        seed = 0
        state = {
            "stage": "options",
            "date": date,
            "now": _now_hhmm(),
            "seed": seed,
            "include_passed": include_passed,
            "auto_include_passed": auto_passed,
            "signature": sig,
            "options": build_day_options(date, seed, include_passed),
            "plan": None,
        }
        _save_state_row(date, state)
    return state


def refresh_day_options(date: Optional[str] = None,
                        include_passed: Optional[bool] = None) -> dict:
    """换一批 A/B/C 方案（也可以用来切换"是否包含已过时段"）。"""
    date = date or db.today()
    state = _load_state_row(date) or {}
    if include_passed is None:
        include_passed = _state_include_passed(state)
    include_passed, auto_passed = _auto_include_passed(date, include_passed)
    if state.get("stage") == "chosen":
        raise ValueError("已经选好方案了，如需重选请先点「重新选择方案」")
    seed = int(state.get("seed") or 0) + 1
    state = {
        "stage": "options",
        "date": date,
        "now": _now_hhmm(),
        "seed": seed,
        "include_passed": bool(include_passed),
        "auto_include_passed": auto_passed,
        "signature": _signature(date, bool(include_passed)),
        "options": build_day_options(date, seed, bool(include_passed)),
        "plan": None,
    }
    _save_state_row(date, state)
    return state


def choose_day_option(date: Optional[str] = None, index: int = 0) -> dict:
    """把 A/B/C 里的某一套变成"今日用餐计划"。"""
    date = date or db.today()
    state = _load_state_row(date)
    if not state or state.get("stage") != "options":
        raise ValueError("当前没有待选的方案（可能已经选好了）")
    options = state.get("options") or []
    if not options:
        raise ValueError("方案列表为空，请先刷新方案")
    if index < 0 or index >= len(options):
        raise ValueError("方案序号超出范围")
    plan = options[index]
    plan["label"] = plan.get("label") or DAY_OPTION_LABELS[index]
    state["stage"] = "chosen"
    state["plan"] = plan
    state["options"] = []
    state["signature"] = _signature(date, bool(state.get("include_passed")))
    state["now"] = _now_hhmm()
    _save_state_row(date, state)
    return state


def _require_chosen(date: str) -> tuple[dict, dict]:
    """已选方案（chosen）与手动挑选（manual）都算"有计划"。"""
    state = _load_state_row(date)
    if not state or state.get("stage") not in ("chosen", "manual") or not state.get("plan"):
        raise ValueError("还没有今日计划：可以在「推荐」页选一套方案，或用「手动选择」自己挑")
    return state, state["plan"]


def refresh_day_plan(date: Optional[str] = None, only_meal: Optional[int] = None,
                     include_passed: Optional[bool] = None) -> dict:
    """刷新已选方案：默认只换未固定的餐；only_meal 时只换指定的那一餐。"""
    date = date or db.today()
    state, plan = _require_chosen(date)
    include_passed = state.get("include_passed") if include_passed is None else bool(include_passed)

    if only_meal is not None:
        blocks = {b["meal_index"]: b for b in plan.get("meals", [])}
        if only_meal not in blocks:
            raise ValueError("这一餐不在今日计划里")
        pins = {b["meal_index"]: b["items"] for b in plan.get("meals", [])
                if b.get("items") and b["meal_index"] != only_meal
                and b["status"] in ("planned", "missed")}
    else:
        pins = _pins_from(plan)

    seed = int(state.get("seed") or 0) + 1
    state["plan"] = build_day_plan(date, pins=pins, seed=seed, include_passed=include_passed)
    state["plan"]["label"] = plan.get("label")
    state["seed"] = seed
    state["include_passed"] = include_passed
    state["signature"] = _signature(date, include_passed)
    state["now"] = _now_hhmm()
    _save_state_row(date, state)
    return state


def pin_day_meal(date: Optional[str], meal_index: int, pinned: bool) -> dict:
    """固定 / 取消固定某一餐（只改标记，不改内容）。"""
    state, plan = _require_chosen(date)
    block = next((b for b in plan.get("meals", []) if b["meal_index"] == meal_index), None)
    if block is None:
        raise ValueError("这一餐不在今日计划里")
    if pinned and not block.get("items"):
        raise ValueError("这一餐还没有内容，无法固定")
    block["pinned"] = bool(pinned)
    _save_state_row(date, state)
    return state


def _dish_index() -> dict[int, dict]:
    return {d["id"]: d for d in load_dishes()}


def _items_for(dish_ids: list[int]) -> list[dict]:
    dishes = _dish_index()
    return [_item_view(dishes[i])
            for i in dict.fromkeys(int(x) for x in dish_ids) if i in dishes]


def _meal_view(date: str, meal_index: int) -> Optional[dict]:
    return next((m for m in meal_timeline(date) if m["meal_index"] == meal_index), None)


def save_manual_record(date: str, meal_index: int, dish_ids: list[int]) -> dict:
    """把某餐直接写成"手动补记"的用餐记录（用于已经过了时段的餐）。"""
    items = _items_for(dish_ids)
    if not items:
        raise ValueError("没有找到要记录的菜品")
    totals = _bundle(items)
    meal = _meal_view(date, meal_index)
    name = meal["meal_name"] if meal else f"第{meal_index}餐"
    row = db.query_one(
        "SELECT id FROM meal_records WHERE user_id = 1 AND date = ? AND meal_index = ?",
        (date, meal_index))
    if row:
        db.execute(
            """UPDATE meal_records SET status = 'manual', meal_name = ?, items_json = ?,
               kcal = ?, protein = ?, fat = ?, carbs = ?, updated_at = ? WHERE id = ?""",
            (name, json.dumps(items, ensure_ascii=False), totals["kcal"], totals["protein"],
             totals["fat"], totals["carbs"], db.now(), row["id"]))
    else:
        db.execute(
            """INSERT INTO meal_records (user_id, date, meal_index, meal_name, status,
               items_json, kcal, protein, fat, carbs, created_at, updated_at)
               VALUES (1, ?, ?, ?, 'manual', ?, ?, ?, ?, ?, ?, ?)""",
            (date, meal_index, name, json.dumps(items, ensure_ascii=False),
             totals["kcal"], totals["protein"], totals["fat"], totals["carbs"],
             db.now(), db.now()))
    return {"meal_index": meal_index, "meal_name": name, "items": items, "totals": totals}


def manual_meal(date: Optional[str], meal_index: int, dish_ids: list[int]) -> dict:
    """「手动选择」一餐：直接把这一餐替换成你挑的菜。

    * 未到时段的餐：替换计划里的这一餐（stage 变 manual，等用户「设为今日计划」）
    * 已到时段的餐（已错过 / 已记录）：同样替换计划内容，并同步更新/新建用餐记录，
      保证「记录合计」和「计划」一致
    返回 {"state", "saved_as": "plan", "record_synced": bool}
    """
    date = date or db.today()
    meal = _meal_view(date, meal_index)
    if meal is None:
        raise ValueError("这一餐不在今天的餐次里")
    items = _items_for(dish_ids)
    if not items:
        raise ValueError("没有找到要用的菜品")

    state = _load_state_row(date)
    passed = meal["status"] in ("recorded", "missed")

    # 时段已过：先同步记录（这样重建计划时该餐会被算作"已记录"并带上新内容）
    if passed:
        save_manual_record(date, meal_index, dish_ids)

    if state and state.get("stage") in ("chosen", "manual"):
        stage = state["stage"]
        pins = dict(_pins_from(state.get("plan") or {}))
        label = (state.get("plan") or {}).get("label")
        include_passed = bool(state.get("include_passed"))
    else:
        stage = "manual"
        pins = {}
        label = "手动"
        include_passed = bool((state or {}).get("include_passed"))

    pins[meal_index] = items
    seed = int((state or {}).get("seed") or 0) + 1
    plan = build_day_plan(date, pins=pins, seed=seed, include_passed=include_passed,
                          force_include={meal_index})
    if label:
        plan["label"] = label
    new_state = {
        "stage": stage,
        "date": date,
        "now": _now_hhmm(),
        "seed": seed,
        "include_passed": include_passed,
        "signature": _signature(date, include_passed),
        "options": [],
        "plan": plan,
    }
    _save_state_row(date, new_state)
    return {"state": new_state, "saved_as": "plan", "record_synced": passed, "meal": None}


def commit_manual_plan(date: Optional[str] = None) -> dict:
    """把手动挑选的计划正式设为「今日计划」。"""
    date = date or db.today()
    state = _load_state_row(date)
    if not state or state.get("stage") != "manual" or not state.get("plan"):
        raise ValueError("当前没有待确认的手动计划")
    state["stage"] = "chosen"
    state["plan"]["label"] = state["plan"].get("label") or "手动"
    state["signature"] = _signature(date, bool(state.get("include_passed")))
    state["now"] = _now_hhmm()
    _save_state_row(date, state)
    return state


def set_day_meal(date: Optional[str], meal_index: int, dish_ids: list[int]) -> dict:
    """用用户自己选的菜（1 道或多道）替换某一餐，并重算这一餐与全天的推荐度。

    被替换的这一餐会自动「固定」，之后刷新整日时不会被换掉。
    """
    state, plan = _require_chosen(date)
    block = next((b for b in plan.get("meals", []) if b["meal_index"] == meal_index), None)
    if block is None:
        raise ValueError("这一餐不在今日计划里")
    if block["status"] == "recorded":
        raise ValueError("这一餐已经记录过了，请到「用餐记录」里修改")

    dishes = _dish_index()
    items = [_item_view(dishes[i]) for i in dict.fromkeys(int(x) for x in dish_ids) if i in dishes]
    if not items:
        raise ValueError("没有找到要替换的菜品")

    scored = score_bundle(items, block["target"])
    block.update({
        "items": items,
        "totals": scored["totals"],
        "scores": scored["scores"],
        "evaluation": scored["evaluation"],
        "macro_pct": scored["macro_pct"],
        "advice": scored["advice"],
        "level": _level(scored["scores"]["total"]),
        "source": "manual",
        "pinned": True,
        "status": "planned",
    })
    plan = summarize_plan(plan)
    state["plan"] = plan
    state["now"] = _now_hhmm()
    _save_state_row(date, state)
    return state


def complete_day_meal(date: Optional[str], meal_index: int) -> dict:
    """给"自己选的这一餐"自动补齐：在推荐度能提高的前提下补 1–3 道菜。"""
    state, plan = _require_chosen(date)
    block = next((b for b in plan.get("meals", []) if b["meal_index"] == meal_index), None)
    if block is None or not block.get("items"):
        raise ValueError("这一餐还没有内容，先选一道菜吧")

    dishes = load_dishes()
    base_ids = {i["dish_id"] for i in block["items"]}
    canteen = block["items"][0].get("canteen")
    pool = [d for d in dishes if d["id"] not in base_ids
            and d["category"] in ("荤菜", "素菜", "汤", "主食", "小吃")]
    pool.sort(key=lambda d: (d.get("canteen_name") != canteen, -d["scores"]["total"]))

    target = block["target"]
    items = [_dish_index_view(i) for i in block["items"]]
    best = score_bundle(items, target)
    added = 0
    while added < 3 and best["totals"]["kcal"] < float(target["kcal"]) * 0.95:
        improved = False
        for cand in pool[:12]:
            trial = items + [_item_view(cand)]
            scored = score_bundle(trial, target)
            if scored["scores"]["total"] > best["scores"]["total"] + 1e-6:
                items, best = trial, scored
                pool = [d for d in pool if d["id"] != cand["id"]]
                added += 1
                improved = True
                break
        if not improved:
            break

    block.update({
        "items": items,
        "totals": best["totals"],
        "scores": best["scores"],
        "evaluation": best["evaluation"],
        "macro_pct": best["macro_pct"],
        "advice": best["advice"] + [f"已自动补齐 {added} 道，可继续按口味调整"],
        "level": _level(best["scores"]["total"]),
        "source": "manual",
        "pinned": True,
    })
    plan = summarize_plan(plan)
    state["plan"] = plan
    _save_state_row(date, state)
    return state


def reset_day_plan(date: Optional[str] = None) -> dict:
    """清空今天的方案与固定项，回到 A/B/C 选择状态。"""
    date = date or db.today()
    db.execute("DELETE FROM day_plans WHERE user_id = 1 AND date = ?", (date,))
    return get_day_state(date)


def _dish_index_view(item: dict) -> dict:
    """把已存进计划的菜品视图补齐成 _item_view 需要的字段（用于再次打分）。"""
    return dict(item)


# 兼容旧调用（服务端只读接口）
def get_day_plan(date: Optional[str] = None, refresh: bool = False,
                 include_passed: Optional[bool] = None,
                 pin: Optional[tuple[int, bool]] = None,
                 only_meal: Optional[int] = None) -> dict:
    date = date or db.today()
    if include_passed is not None:
        state = _load_state_row(date)
        if state and bool(state.get("include_passed")) != bool(include_passed):
            if state.get("stage") == "chosen":
                return refresh_day_plan(date, only_meal=only_meal, include_passed=include_passed)
            return refresh_day_options(date, include_passed=include_passed)
    if pin is not None:
        return pin_day_meal(date, pin[0], pin[1])
    if refresh:
        state = _load_state_row(date)
        if state and state.get("stage") == "chosen":
            return refresh_day_plan(date, only_meal=only_meal)
        return refresh_day_options(date)
    return get_day_state(date)
