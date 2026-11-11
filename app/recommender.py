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


def _extend(items: list[dict], pool: list[dict], target: dict, limit: int = 8) -> list[dict]:
    """热量不够时贪心补一道菜（汤/小份荤/素），只在能提高总分时采纳。"""
    if _bundle(items)["kcal"] >= float(target["kcal"]) * 0.88:
        return items
    best, best_score = None, score_bundle(items, target)["scores"]["total"]
    chosen = {i["id"] for i in items}
    for cand in pool[:limit]:
        if cand["id"] in chosen:
            continue
        trial = items + [cand]
        score = score_bundle(trial, target)["scores"]["total"]
        if score > best_score + 1e-6:
            best, best_score = trial, score
    return best or items


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
            if slot in ("lunch", "dinner"):
                # 补菜只补素菜或汤，不再补第二份主食/饮料
                extras = [d for d in extras if d["category"] in ("素菜", "汤")]
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

# ── 今日用餐计划（按当前时间点推断还剩几餐，给出全天安排） ──────────────
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
                     status: str, rng: random.Random) -> dict:
    if items:
        scored = score_bundle(items, target)
        return {
            **meal,
            "status": status,
            "pinned": pinned,
            "items": items,
            "totals": scored["totals"],
            "scores": scored["scores"],
            "evaluation": scored["evaluation"],
            "macro_pct": scored["macro_pct"],
            "advice": scored["advice"],
            "target": target,
        }
    return {
        **meal,
        "status": status,
        "pinned": pinned,
        "items": [],
        "totals": {"kcal": 0, "protein": 0, "fat": 0, "carbs": 0, "price": 0},
        "scores": {"total": 0},
        "evaluation": {},
        "macro_pct": {"protein": 0, "fat": 0, "carbs": 0},
        "advice": ["这个时段暂时凑不出合适的组合，可到「搜索」页自己挑，或用自选评估看看推荐度"],
        "target": target,
    }


def build_day_plan(date: Optional[str] = None, pins: Optional[dict[int, list[dict]]] = None,
                   seed: int = 0, include_passed: bool = False) -> dict:
    """生成今天的整日用餐计划。

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
    remaining = [
        meal for meal in timeline
        if meal["status"] == "planned"
        or (include_passed and meal["status"] == "missed")
    ]
    total_share = sum(meal["share"] for meal in remaining) or 1.0

    # 今日剩余额度（扣掉已经吃掉的）
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
                "recorded", rng))
            continue
        if meal["status"] == "missed" and not include_passed:
            blocks.append(_plan_meal_block(meal, {"kcal": 0}, [], False, "missed", rng))
            continue

        # 本餐目标：剩余额度 × 本餐在"剩余餐次"中的占比
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
            items = pins[idx]
            block = _plan_meal_block(meal, target, items, True, "planned", rng)
        else:
            candidates = candidate_plans(meal["slot"], target, dishes)
            chosen = _pick_weighted(candidates, rng)
            items = chosen["items"] if chosen else []
            block = _plan_meal_block(meal, target, items, False, "planned", rng)

        for key in ("kcal", "protein", "fat", "carbs", "price"):
            planned_totals[key] += float(block["totals"].get(key) or 0)
        blocks.append(block)

    planned_kcal = sum(b["totals"]["kcal"] for b in blocks if b["status"] in ("planned", "recorded"))
    today_planned = [b for b in blocks if b["status"] in ("planned", "recorded")]
    signature = [
        [m["meal_index"] for m in timeline if m["status"] == "recorded"],
        [m["meal_index"] for m in timeline if m["status"] == "missed"],
        bool(include_passed),
    ]

    return {
        "date": date,
        "now": _now_hhmm(),
        "seed": seed,
        "include_passed": bool(include_passed),
        "signature": signature,
        "daily": daily,
        "consumed": consumed,
        "meals": blocks,
        "remaining_indices": [b["meal_index"] for b in blocks if b["status"] == "planned"],
        "recorded_indices": [b["meal_index"] for b in blocks if b["status"] == "recorded"],
        "missed_indices": [b["meal_index"] for b in blocks if b["status"] == "missed"],
        "planned_kcal": round(planned_kcal, 1),
        "summary": {
            "meals_total": len(blocks),
            "meals_remaining": len([b for b in blocks if b["status"] == "planned"]),
            "meals_recorded": len([b for b in blocks if b["status"] == "recorded"]),
            "meals_missed": len([b for b in blocks if b["status"] == "missed"]),
            "plan_kcal": round(planned_kcal, 1),
            "plan_protein": round(sum(b["totals"]["protein"] for b in today_planned), 1),
            "plan_fat": round(sum(b["totals"]["fat"] for b in today_planned), 1),
            "plan_carbs": round(sum(b["totals"]["carbs"] for b in today_planned), 1),
            "plan_price": round(sum(b["totals"]["price"] for b in today_planned), 1),
            "gap_kcal": round(daily["kcal"] - planned_kcal, 1),
            "coverage": round(planned_kcal / daily["kcal"], 3) if daily["kcal"] else 0,
        },
    }


def _load_plan_row(date: str) -> Optional[dict]:
    row = db.query_one(
        "SELECT data_json FROM day_plans WHERE user_id = 1 AND date = ?", (date,))
    if not row:
        return None
    try:
        return json.loads(row["data_json"])
    except json.JSONDecodeError:
        return None


def _save_plan_row(date: str, data: dict) -> None:
    db.execute(
        "INSERT INTO day_plans (user_id, date, data_json, updated_at) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(user_id, date) DO UPDATE SET data_json = excluded.data_json, "
        "updated_at = excluded.updated_at",
        (date, json.dumps(data, ensure_ascii=False), db.now()),
    )


def _pins_from(plan: dict) -> dict[int, list[dict]]:
    return {b["meal_index"]: b["items"] for b in plan.get("meals", []) if b.get("pinned")}


def get_day_plan(date: Optional[str] = None, refresh: bool = False,
                 include_passed: Optional[bool] = None,
                 pin: Optional[tuple[int, bool]] = None,
                 only_meal: Optional[int] = None) -> dict:
    """读取 / 维护今日计划。

    * 普通读取：返回已存的计划；若"已记录/已错过"的餐次变了（例如刚确认了一餐），
      自动按新的剩余餐次重建，同时保留用户固定的餐次
    * refresh=True：保留固定的餐次，其余重新抽一轮
    * pin=(meal_index, True/False)：只切换"固定"标记，不改变当前已选内容
    * only_meal=meal_index：只换这一餐，其余餐次全部临时锁定（"换这道餐"按钮用）
    """
    date = date or db.today()
    stored = _load_plan_row(date)

    if include_passed is None:
        include_passed = bool(stored.get("include_passed")) if stored else False

    pins: dict[int, list[dict]] = dict(_pins_from(stored)) if stored else {}

    if only_meal is not None:
        if stored is None:
            raise ValueError("还没有今日计划，没法单独替换某一餐")
        blocks = {b["meal_index"]: b for b in stored.get("meals", [])}
        if only_meal not in blocks:
            raise ValueError("这一餐不在今日计划里")
        pins = {b["meal_index"]: b["items"] for b in stored.get("meals", [])
                if b.get("items") and b["meal_index"] != only_meal
                and b["status"] in ("planned", "missed")}
        refresh = True

    pin_changed = False
    if stored is not None and pin is not None:
        meal_index, pinned = pin
        blocks = {b["meal_index"]: b for b in stored.get("meals", [])}
        block = blocks.get(meal_index)
        if block is None:
            raise ValueError("这一餐不在今日计划里（可能已经记录或时段已过）")
        if pinned:
            if not block.get("items"):
                raise ValueError("这一餐还没有推荐内容，无法固定")
            pins[meal_index] = block["items"]
        else:
            pins.pop(meal_index, None)
        for b in stored.get("meals", []):
            if b["meal_index"] == meal_index:
                b["pinned"] = bool(pinned)
        pin_changed = True

        if not refresh and include_passed == bool(stored.get("include_passed")):
            _save_plan_row(date, stored)
            return stored

    need_rebuild = stored is None or refresh

    if stored is not None and not need_rebuild and not pin_changed:
        timeline_now = meal_timeline(date)
        current_sig = [
            [m["meal_index"] for m in timeline_now if m["status"] == "recorded"],
            [m["meal_index"] for m in timeline_now if m["status"] == "missed"],
            bool(stored.get("include_passed")),
        ]
        if current_sig != stored.get("signature"):
            need_rebuild = True

    if include_passed is not None and stored is not None \
            and bool(include_passed) != bool(stored.get("include_passed")):
        need_rebuild = True

    if need_rebuild:
        seed = int((stored or {}).get("seed") or 0) + 1 if (stored and (refresh or pin_changed)) else 0
        data = build_day_plan(date, pins=pins, seed=seed,
                             include_passed=bool(include_passed))
        _save_plan_row(date, data)
        return data

    return stored
