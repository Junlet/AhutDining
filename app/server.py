"""零依赖 HTTP 服务：标准库 http.server + sqlite3 实现全部 REST 接口。

启动：python run.py（或双击 start.bat）
默认地址：http://127.0.0.1:8848
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import traceback
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import db  # noqa: E402
import nutrition  # noqa: E402
import recommender  # noqa: E402
import seed_data  # noqa: E402

STATIC_DIR = os.path.join(APP_DIR, "static")
MAX_BODY = 8 * 1024 * 1024  # 8MB，够放一张手机照片
ALLOWED_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# ── 小工具 ──────────────────────────────────────────────────────────────
def ok(data: Any = None, **extra) -> tuple[int, dict]:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return 200, payload


def fail(message: str, status: int = 400) -> tuple[int, dict]:
    return status, {"ok": False, "error": message}


def profile_dict() -> dict:
    row = db.query_one("SELECT * FROM profiles WHERE user_id = 1")
    if not row:
        db.init_db()
        row = db.query_one("SELECT * FROM profiles WHERE user_id = 1")
    return row or {}


def meal_slots(date: str) -> list[dict]:
    """按 meals_per_day 生成当天的餐次栏位，缺行的自动补空记录。"""
    profile = profile_dict()
    metrics = nutrition.profile_metrics(profile)
    existing = {r["meal_index"]: r for r in db.query(
        "SELECT * FROM meal_records WHERE user_id = 1 AND date = ? ORDER BY meal_index", (date,)
    )}
    rows = []
    for meal in metrics["meals"]:
        idx = meal["index"]
        record = existing.get(idx)
        if record is None:
            db.execute(
                """INSERT INTO meal_records
                   (user_id, date, meal_index, meal_name, status, items_json,
                    kcal, protein, fat, carbs, created_at, updated_at)
                   VALUES (1, ?, ?, ?, 'empty', '[]', 0, 0, 0, 0, ?, ?)""",
                (date, idx, meal["name"], db.now(), db.now()),
            )
            record = db.query_one(
                "SELECT * FROM meal_records WHERE user_id = 1 AND date = ? AND meal_index = ?",
                (date, idx),
            )
        record = dict(record)
        # 餐数/拆分改过之后，旧行的 meal_name 可能过期（例如 3 餐的「晚餐」在 4 餐里应是「加餐」）
        if record.get("meal_name") != meal["name"]:
            db.execute(
                "UPDATE meal_records SET meal_name = ? WHERE user_id = 1 AND date = ? AND meal_index = ?",
                (meal["name"], date, idx),
            )
            record["meal_name"] = meal["name"]
        record["items"] = json.loads(record.pop("items_json") or "[]")
        record["target"] = {
            "kcal": meal["kcal"], "protein": meal["protein"],
            "fat": meal["fat"], "carbs": meal["carbs"],
        }
        record["slot"] = meal["slot"]
        rows.append(record)
    # 饭量设置调小后，多出来的旧记录不显示但保留（避免丢数据）
    return rows


def recalc_record(date: str, meal_index: int, dish_ids: list[int], status: str,
                  plan_id: str = "", note: str = "") -> dict:
    dishes = {d["id"]: d for d in recommender.load_dishes()}
    items = []
    for dish_id in dish_ids:
        dish = dishes.get(int(dish_id))
        if not dish:
            continue
        items.append(recommender._item_view(dish))
    totals = recommender._bundle(items) if items else {
        "kcal": 0, "protein": 0, "fat": 0, "carbs": 0, "price": 0}
    db.execute(
        """UPDATE meal_records
           SET status = ?, plan_id = ?, items_json = ?, kcal = ?, protein = ?, fat = ?,
               carbs = ?, note = ?, updated_at = ?
           WHERE user_id = 1 AND date = ? AND meal_index = ?""",
        (status if items else "empty", plan_id,
         json.dumps(items, ensure_ascii=False),
         totals["kcal"], totals["protein"], totals["fat"], totals["carbs"],
         note, db.now(), date, meal_index),
    )
    return {"items": items, "totals": totals}


# ── 业务处理 ────────────────────────────────────────────────────────────
def handle_bootstrap():
    profile = profile_dict()
    metrics = nutrition.profile_metrics(profile)
    slot = recommender.current_slot()
    date = db.today()
    records = meal_slots(date)
    consumed = recommender.consumed_today(date)
    canteens = db.query(
        "SELECT DISTINCT c.name, c.campus FROM canteens c ORDER BY c.campus, c.name"
    )
    categories = db.query(
        "SELECT DISTINCT category FROM dishes WHERE active = 1 ORDER BY category"
    )
    now_meal = next((r for r in records if r["status"] == "empty"), records[0] if records else None)
    return ok({
        "profile": profile,
        "metrics": metrics,
        "slot": slot,
        "date": date,
        "records": records,
        "consumed": consumed,
        "canteens": canteens,
        "categories": [c["category"] for c in categories],
        "current_meal": now_meal,
        "datasets": db.query_one("SELECT COUNT(*) AS dishes FROM dishes"),
        "settings": {k["key"]: k["value"] for k in db.query("SELECT key, value FROM settings")},
    })


def handle_profile_update(body: dict):
    profile = profile_dict()
    editable = {
        "nickname": str,
        "gender": str,
        "birth_date": str,
        "height_cm": float,
        "weight_kg": float,
        "activity_level": str,
        "goal": str,
        "meals_per_day": int,
        "allergies": str,
        "dislikes": str,
    }
    updates, params = [], []
    for key, caster in editable.items():
        if key not in body:
            continue
        try:
            value = caster(body[key])
        except (TypeError, ValueError):
            return fail(f"字段 {key} 的值不合法")
        if key == "gender" and value not in ("male", "female"):
            return fail("性别只能是 male 或 female")
        if key == "activity_level" and value not in nutrition.ACTIVITY_LEVELS:
            return fail("未知的活动系数")
        if key == "goal" and value not in nutrition.GOALS:
            return fail("未知的目标类型")
        if key == "meals_per_day":
            value = max(1, min(int(value), 6))
        if key == "height_cm":
            value = max(120.0, min(float(value), 230.0))
        if key == "weight_kg":
            value = max(30.0, min(float(value), 200.0))
        updates.append(f"{key} = ?")
        params.append(value)
    if not updates:
        return fail("没有可更新的字段")
    updates.append("updated_at = ?")
    params.append(db.now())
    db.execute(
        f"UPDATE profiles SET {', '.join(updates)} WHERE user_id = 1", params
    )
    profile = profile_dict()
    return ok({"profile": profile, "metrics": nutrition.profile_metrics(profile)})


def handle_plans_current(query: dict):
    slot = (query.get("slot") or [""])[0] or recommender.current_slot()["slot"]
    batch_no = int((query.get("batch") or ["0"])[0] or 0) or None
    date = (query.get("date") or [db.today()])[0]
    if batch_no is None:
        latest = db.query_one(
            "SELECT MAX(batch_no) AS m FROM plan_batches WHERE user_id = 1 AND date = ? AND slot = ?",
            (date, slot),
        )
        batch_no = int((latest["m"] or 1)) if latest and latest["m"] else 1
    data = recommender.get_or_create_batch(slot, date, batch_no)
    data["slot_info"] = recommender.current_slot()
    data["metrics"] = nutrition.profile_metrics(profile_dict())
    return ok(data)


def handle_plans_refresh(body: dict):
    slot = body.get("slot") or recommender.current_slot()["slot"]
    date = body.get("date") or db.today()
    batch_no = recommender.next_batch_no(slot, date)
    data = recommender.get_or_create_batch(slot, date, batch_no)
    data["slot_info"] = recommender.current_slot()
    return ok(data)


def handle_plans_evaluate(body: dict):
    dish_ids = [int(i) for i in (body.get("dish_ids") or [])]
    if not dish_ids:
        return fail("请至少选择一道菜")
    slot = body.get("slot") or recommender.current_slot()["slot"]
    try:
        result = recommender.evaluate_custom(dish_ids, slot, body.get("date"))
    except ValueError as exc:
        return fail(str(exc))
    return ok(result)


def handle_dishes(query: dict):
    dishes = recommender.search_dishes(
        q=(query.get("q") or [""])[0],
        canteen=(query.get("canteen") or [""])[0],
        category=(query.get("category") or [""])[0],
        window=(query.get("window") or [""])[0],
        sort=(query.get("sort") or ["score"])[0],
    )
    total = len(dishes)
    try:
        offset = max(0, int((query.get("offset") or ["0"])[0] or 0))
    except ValueError:
        offset = 0
    try:
        limit = int((query.get("limit") or ["120"])[0] or 120)
    except ValueError:
        limit = 120
    limit = max(1, min(limit, 600))
    page = dishes[offset:offset + limit]
    return ok({
        "items": [recommender._item_view(d) for d in page],
        "total": total,
        "offset": offset,
        "limit": limit,
        "truncated": offset + len(page) < total,
    })


def handle_dish_detail(dish_id: int):
    dishes = {d["id"]: d for d in recommender.load_dishes()}
    dish = dishes.get(dish_id)
    if not dish:
        return fail("菜品不存在", 404)
    comments = db.query(
        "SELECT * FROM comments WHERE dish_id = ? ORDER BY created_at DESC", (dish_id,)
    )
    images = db.query(
        "SELECT id, path, created_at FROM images WHERE owner_type = 'dish' AND owner_id = ? "
        "ORDER BY id DESC", (dish_id,)
    )
    # 全校比价：同名菜在不同窗口的价格（对应 PocketAHUT 食堂模块的「比价」思路）
    same_name = db.query(
        """SELECT d.name, d.price, w.name AS window, c.name AS canteen, c.campus
           FROM dishes d JOIN windows w ON w.id = d.window_id
           JOIN canteens c ON c.id = w.canteen_id
           WHERE d.active = 1 AND d.name = ? ORDER BY d.price""",
        (dish["name"],),
    )
    window_rating = recommender.window_ratings().get(dish["window_id"])
    detail = recommender._item_view(dish)
    detail.update({
        "note": dish.get("note") or "",
        "unit": dish.get("unit") or "份",
        "window_id": dish["window_id"],
        "window_rating": window_rating,
        "comments": comments,
        "images": images,
        "price_compare": same_name,
    })
    return ok(detail)


def handle_add_comment(body: dict):
    dish_id = int(body.get("dish_id") or 0)
    content = (body.get("content") or "").strip()
    rating = max(1, min(int(body.get("rating") or 5), 5))
    if not dish_id:
        return fail("缺少 dish_id")
    if not content:
        return fail("评论内容不能为空")
    recommender.invalidate_cache()
    comment_id = db.execute(
        "INSERT INTO comments (user_id, dish_id, rating, content, created_at, updated_at) "
        "VALUES (1, ?, ?, ?, ?, ?)",
        (dish_id, rating, content, db.now(), db.now()),
    )
    return ok(db.query_one("SELECT * FROM comments WHERE id = ?", (comment_id,)))


def handle_delete_comment(comment_id: int):
    db.execute("DELETE FROM comments WHERE id = ? AND user_id = 1", (comment_id,))
    recommender.invalidate_cache()
    return ok({"deleted": comment_id})


def handle_toggle_favorite(dish_id: int):
    recommender.invalidate_cache()
    row = db.query_one("SELECT 1 AS x FROM favorites WHERE user_id = 1 AND dish_id = ?", (dish_id,))
    if row:
        db.execute("DELETE FROM favorites WHERE user_id = 1 AND dish_id = ?", (dish_id,))
        return ok({"dish_id": dish_id, "favorite": 0})
    db.execute(
        "INSERT INTO favorites (user_id, dish_id, created_at) VALUES (1, ?, ?)",
        (dish_id, db.now()),
    )
    return ok({"dish_id": dish_id, "favorite": 1})


def handle_favorites():
    dishes = {d["id"]: d for d in recommender.load_dishes()}
    ids = [r["dish_id"] for r in db.query(
        "SELECT dish_id FROM favorites WHERE user_id = 1 ORDER BY created_at DESC")]
    items = [recommender._item_view(dishes[i]) for i in ids if i in dishes]
    return ok({"items": items, "total": len(items)})


def handle_upload_image(body: dict):
    owner_type = body.get("owner_type") or "dish"
    if owner_type not in ("dish", "window", "canteen"):
        return fail("owner_type 只能是 dish / window / canteen")
    owner_id = int(body.get("owner_id") or 0)
    if not owner_id:
        return fail("缺少 owner_id")
    filename = (body.get("filename") or "upload.png").strip()
    ext = os.path.splitext(filename)[1].lower()
    data_url = body.get("data") or ""
    if "," in data_url and data_url.strip().startswith("data:"):
        header, data_url = data_url.split(",", 1)
        mime = header[5:].split(";")[0]
        ext = mimetypes.guess_extension(mime) or ext
    if ext not in ALLOWED_IMAGE_EXT:
        ext = ".png"
    try:
        raw = base64.b64decode(data_url, validate=False)
    except Exception:
        return fail("图片数据不是合法的 base64")
    if not raw:
        return fail("图片内容为空")
    if len(raw) > MAX_BODY:
        return fail("图片太大（上限 8MB）")

    os.makedirs(db.MEDIA_DIR, exist_ok=True)
    stored = f"{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:10]}{ext}"
    with open(os.path.join(db.MEDIA_DIR, stored), "wb") as fh:
        fh.write(raw)
    recommender.invalidate_cache()
    image_id = db.execute(
        "INSERT INTO images (owner_type, owner_id, path, created_at) VALUES (?, ?, ?, ?)",
        (owner_type, owner_id, stored, db.now()),
    )
    return ok({"id": image_id, "path": stored, "url": f"/media/{stored}"})


def handle_records(query: dict):
    date = (query.get("date") or [db.today()])[0]
    records = meal_slots(date)
    consumed = {"kcal": round(sum(r["kcal"] for r in records), 1),
                "protein": round(sum(r["protein"] for r in records), 1),
                "fat": round(sum(r["fat"] for r in records), 1),
                "carbs": round(sum(r["carbs"] for r in records), 1)}
    metrics = nutrition.profile_metrics(profile_dict())
    dates = [r["date"] for r in db.query(
        "SELECT DISTINCT date FROM meal_records WHERE status IN ('confirmed','manual') "
        "ORDER BY date DESC LIMIT 14")]
    return ok({
        "date": date,
        "records": records,
        "consumed": consumed,
        "daily": metrics["daily"],
        "recent_dates": dates,
    })


def handle_confirm(body: dict):
    """确认一餐：可以直接确认推荐套餐（传 plan_id + items），也可以只传菜品 id。"""
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    if not meal_index:
        return fail("缺少 meal_index")
    items = body.get("items") or []
    dish_ids = [int(i.get("dish_id") if isinstance(i, dict) else i) for i in items]
    if not dish_ids and body.get("plan_items"):
        dish_ids = [int(i["dish_id"]) for i in body["plan_items"]]
    if not dish_ids:
        return fail("没有要确认的菜品")
    result = recalc_record(date, meal_index, dish_ids,
                           status=body.get("status") or "confirmed",
                           plan_id=body.get("plan_id") or "",
                           note=body.get("note") or "")
    records = meal_slots(date)
    record = next((r for r in records if r["meal_index"] == meal_index), None)
    consumed = recommender.consumed_today(date)
    # 下一餐的推荐要点：告诉前端"因为这一餐，下一餐目标怎么调"
    next_meal = next((r for r in records if r["status"] == "empty"), None)
    return ok({"record": record, "consumed": consumed, "next_meal": next_meal, "recalc": result})


def handle_clear_record(date: str, meal_index: int):
    recalc_record(date, meal_index, [], status="empty")
    records = meal_slots(date)
    return ok({"records": records})


def handle_settings(body: dict):
    allowed = [k for k in db.DEFAULT_SETTINGS]
    changed = {}
    for key, value in body.items():
        if key in allowed:
            db.set_setting(key, str(value))
            changed[key] = str(value)
    settings = {k["key"]: k["value"] for k in db.query("SELECT key, value FROM settings")}
    return ok({"changed": changed, "settings": settings})


def _day_payload(state: dict) -> dict:
    """统一两种阶段（未选方案 / 已选方案）的顶层字段，方便前端直接读。"""
    plan = state.get("plan")
    options = state.get("options") or []
    ref = plan or (options[0] if options else None)
    if ref:
        state.setdefault("daily", ref.get("daily"))
        state.setdefault("consumed", ref.get("consumed"))
        state.setdefault("suggest_include_passed", ref.get("suggest_include_passed"))
    state.setdefault("now", recommender._now_hhmm())
    if plan:
        state.setdefault("summary", plan.get("summary"))
        state.setdefault("meals", plan.get("meals"))
    state["slot_info"] = recommender.current_slot()
    return state


def handle_day_plan(query: dict):
    date = (query.get("date") or [db.today()])[0]
    if "include_passed" in query:
        include_passed = (query.get("include_passed") or ["0"])[0] in ("1", "true", "yes")
        try:
            state = recommender.get_day_plan(date, include_passed=include_passed)
        except ValueError as exc:
            return fail(str(exc))
        return ok(_day_payload(state))
    return ok(_day_payload(recommender.get_day_state(date)))


def handle_day_plan_refresh(body: dict):
    date = body.get("date") or db.today()
    include_passed = body.get("include_passed")
    only_meal = body.get("only_meal")
    try:
        state = recommender.get_day_plan(
            date, refresh=True,
            include_passed=None if include_passed is None else bool(include_passed),
            only_meal=int(only_meal) if only_meal else None)
    except ValueError as exc:
        return fail(str(exc))
    return ok(_day_payload(state))


def handle_day_plan_choose(body: dict):
    date = body.get("date") or db.today()
    index = int(body.get("index") or 0)
    try:
        state = recommender.choose_day_option(date, index)
    except ValueError as exc:
        return fail(str(exc))
    return ok(_day_payload(state))


def handle_day_plan_fill_meal(body: dict):
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    if not meal_index:
        return fail("缺少 meal_index")
    try:
        state = recommender.fill_meal(date, meal_index)
    except ValueError as exc:
        return fail(str(exc))
    payload = _day_payload(state)
    payload["updated_meal"] = next(
        (b for b in (state.get("plan") or {}).get("meals", [])
         if b["meal_index"] == meal_index), None)
    return ok(payload)


def handle_day_plan_fill_remaining(body: dict):
    date = body.get("date") or db.today()
    try:
        state = recommender.fill_remaining(date)
    except ValueError as exc:
        return fail(str(exc))
    return ok(_day_payload(state))

def handle_day_plan_manual_meal(body: dict):
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    dish_ids = [int(x) for x in (body.get("dish_ids") or [])]
    if not meal_index or not dish_ids:
        return fail("需要 meal_index 和 dish_ids")
    try:
        result = recommender.manual_meal(date, meal_index, dish_ids)
    except ValueError as exc:
        return fail(str(exc))
    state = result["state"]
    payload = _day_payload(state)
    payload["saved_as"] = result.get("saved_as", "plan")
    payload["record_synced"] = bool(result.get("record_synced"))
    payload["manual_meal"] = result.get("meal")
    return ok(payload)


def handle_day_plan_commit(body: dict):
    date = body.get("date") or db.today()
    try:
        state = recommender.commit_manual_plan(date)
    except ValueError as exc:
        return fail(str(exc))
    return ok(_day_payload(state))

def handle_day_plan_pin(body: dict):
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    if not meal_index:
        return fail("缺少 meal_index")
    try:
        state = recommender.pin_day_meal(date, meal_index, bool(body.get("pinned")))
    except ValueError as exc:
        return fail(str(exc))
    return ok(_day_payload(state))


def handle_day_plan_set_meal(body: dict):
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    dish_ids = body.get("dish_ids") or []
    if not meal_index or not dish_ids:
        return fail("需要 meal_index 和 dish_ids")
    try:
        state = recommender.set_day_meal(date, meal_index, [int(x) for x in dish_ids])
    except ValueError as exc:
        return fail(str(exc))
    payload = _day_payload(state)
    plan = state.get("plan") or {}
    block = next((b for b in plan.get("meals", []) if b["meal_index"] == meal_index), None)
    payload["updated_meal"] = block
    return ok(payload)


def handle_day_plan_complete_meal(body: dict):
    date = body.get("date") or db.today()
    meal_index = int(body.get("meal_index") or 0)
    try:
        state = recommender.complete_day_meal(date, meal_index)
    except ValueError as exc:
        return fail(str(exc))
    payload = _day_payload(state)
    block = next((b for b in (state.get("plan") or {}).get("meals", [])
                  if b["meal_index"] == meal_index), None)
    payload["updated_meal"] = block
    return ok(payload)


def handle_day_plan_reset(body: dict):
    date = body.get("date") or db.today()
    return ok(_day_payload(recommender.reset_day_plan(date)))


def handle_stats():
    date = db.today()
    records = meal_slots(date)
    metrics = nutrition.profile_metrics(profile_dict())
    consumed = recommender.consumed_today(date)
    confirmed = [r for r in records if r["status"] in ("confirmed", "manual")]
    return ok({
        "date": date,
        "daily": metrics["daily"],
        "consumed": consumed,
        "remaining": {
            "kcal": round(max(0.0, metrics["daily"]["kcal"] - consumed["kcal"]), 1),
            "protein": round(max(0.0, metrics["daily"]["protein"] - consumed["protein"]), 1),
            "fat": round(max(0.0, metrics["daily"]["fat"] - consumed["fat"]), 1),
            "carbs": round(max(0.0, metrics["daily"]["carbs"] - consumed["carbs"]), 1),
        },
        "meals": [{"index": r["meal_index"], "name": r["meal_name"], "status": r["status"],
                   "kcal": r["kcal"]} for r in records],
        "confirmed_count": len(confirmed),
        "windows": list(recommender.window_ratings().values()),
    })


# ── 路由表 ──────────────────────────────────────────────────────────────
Route = tuple[str, str, Callable[..., tuple[int, dict]]]

ROUTES: list[Route] = [
    ("GET", r"^/api/health$", lambda req, m, q, b: ok({"service": "ahut-dining", "time": db.now()})),
    ("GET", r"^/api/bootstrap$", lambda req, m, q, b: handle_bootstrap()),
    ("GET", r"^/api/profile$", lambda req, m, q, b: ok({
        "profile": profile_dict(),
        "metrics": nutrition.profile_metrics(profile_dict()),
    })),
    ("PUT", r"^/api/profile$", lambda req, m, q, b: handle_profile_update(b)),
    ("GET", r"^/api/plans$", lambda req, m, q, b: handle_plans_current(q)),
    ("POST", r"^/api/plans/refresh$", lambda req, m, q, b: handle_plans_refresh(b)),
    ("POST", r"^/api/plans/evaluate$", lambda req, m, q, b: handle_plans_evaluate(b)),
    ("GET", r"^/api/day-plan$", lambda req, m, q, b: handle_day_plan(q)),
    ("POST", r"^/api/day-plan/refresh$", lambda req, m, q, b: handle_day_plan_refresh(b)),
    ("POST", r"^/api/day-plan/choose$", lambda req, m, q, b: handle_day_plan_choose(b)),
    ("POST", r"^/api/day-plan/manual-meal$", lambda req, m, q, b: handle_day_plan_manual_meal(b)),
    ("POST", r"^/api/day-plan/fill-meal$", lambda req, m, q, b: handle_day_plan_fill_meal(b)),
    ("POST", r"^/api/day-plan/fill-remaining$", lambda req, m, q, b: handle_day_plan_fill_remaining(b)),
    ("POST", r"^/api/day-plan/commit$", lambda req, m, q, b: handle_day_plan_commit(b)),
    ("POST", r"^/api/day-plan/pin$", lambda req, m, q, b: handle_day_plan_pin(b)),
    ("POST", r"^/api/day-plan/set-meal$", lambda req, m, q, b: handle_day_plan_set_meal(b)),
    ("POST", r"^/api/day-plan/complete-meal$",
     lambda req, m, q, b: handle_day_plan_complete_meal(b)),
    ("POST", r"^/api/day-plan/reset$", lambda req, m, q, b: handle_day_plan_reset(b)),
    ("GET", r"^/api/dishes$", lambda req, m, q, b: handle_dishes(q)),
    ("GET", r"^/api/dishes/(?P<id>\d+)$", lambda req, m, q, b: handle_dish_detail(int(m["id"]))),
    ("POST", r"^/api/comments$", lambda req, m, q, b: handle_add_comment(b)),
    ("DELETE", r"^/api/comments/(?P<id>\d+)$",
     lambda req, m, q, b: handle_delete_comment(int(m["id"]))),
    ("POST", r"^/api/favorites/(?P<id>\d+)$", lambda req, m, q, b: handle_toggle_favorite(int(m["id"]))),
    ("GET", r"^/api/favorites$", lambda req, m, q, b: handle_favorites()),
    ("POST", r"^/api/images$", lambda req, m, q, b: handle_upload_image(b)),
    ("GET", r"^/api/records$", lambda req, m, q, b: handle_records(q)),
    ("POST", r"^/api/records/confirm$", lambda req, m, q, b: handle_confirm(b)),
    ("DELETE", r"^/api/records/(?P<date>[\d-]+)/(?P<idx>\d+)$",
     lambda req, m, q, b: handle_clear_record(m["date"], int(m["idx"]))),
    ("PUT", r"^/api/settings$", lambda req, m, q, b: handle_settings(b)),
    ("GET", r"^/api/stats$", lambda req, m, q, b: handle_stats()),
    ("GET", r"^/api/slot$", lambda req, m, q, b: ok(recommender.current_slot())),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "AhutDining/1.0"
    protocol_version = "HTTP/1.1"

    # ── 基础 ──────────────────────────────────────────────────────────
    def log_message(self, fmt: str, *args) -> None:  # 安静一点，只记错误
        if args and str(args[1]).startswith(("4", "5")):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: Optional[str] = None) -> None:
        if not os.path.isfile(path):
            self._send_json(404, {"ok": False, "error": "文件不存在"})
            return
        content_type = content_type or mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            data = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("请求体不是合法 JSON")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if method == "GET":
            if path in ("/", "/index.html"):
                self._send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path.startswith("/static/"):
                self._send_file(os.path.join(STATIC_DIR, path[len("/static/"):]))
                return
            if path.startswith("/media/"):
                name = os.path.basename(path[len("/media/"):])
                self._send_file(os.path.join(db.MEDIA_DIR, name))
                return

        path_matched = False
        for route_method, pattern, func in ROUTES:
            match = re.match(pattern, path)
            if not match:
                continue
            path_matched = True
            if route_method != method:
                continue
            try:
                body = self._read_body() if method in ("POST", "PUT") else {}
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            try:
                status, payload = func(self, match.groupdict(), query, body)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._send_json(500, {"ok": False, "error": f"服务器内部错误: {exc}"})
                return
            self._send_json(status, payload)
            return

        if path_matched:
            self._send_json(405, {"ok": False, "error": f"{method} 不允许"})
            return
        self._send_json(404, {"ok": False, "error": f"未找到路径 {path}"})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")


PROJECT_DIR = os.path.dirname(APP_DIR)
REAL_DISHES_CSV = os.path.join(PROJECT_DIR, "data", "dishes_real.csv")


def bootstrap_data() -> dict:
    """启动时准备数据：库里有菜就跳过；空的先导入真实 CSV，再不行才用示例数据。"""
    db.init_db()
    existing = db.query_one("SELECT COUNT(*) AS c FROM dishes")["c"]
    if existing:
        return {"skipped": True, "dishes": existing}

    if os.path.isfile(REAL_DISHES_CSV):
        try:
            sys.path.insert(0, os.path.join(PROJECT_DIR, "tools"))
            from import_dishes import import_csv  # 延迟导入，避免启动依赖 tools
            stats = import_csv(REAL_DISHES_CSV, replace=True)
            return {"source": "dishes_real.csv", **stats}
        except Exception as exc:  # noqa: BLE001
            print(f"[数据] 导入 dishes_real.csv 失败，回退示例数据：{exc}")
    return seed_data.seed(force=False)


def pick_port(preferred: int = 8848) -> int:
    for port in [preferred, 8849, 8850, 8851, 8000]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def serve(port: Optional[int] = None) -> ThreadingHTTPServer:
    bootstrap_data()
    wanted = port or int(os.environ.get("AHUT_PORT") or 0)
    if wanted:
        try:
            return ThreadingHTTPServer(("127.0.0.1", wanted), Handler)
        except OSError:
            print(f"[提示] 端口 {wanted} 已被占用，自动换一个端口")
    return ThreadingHTTPServer(("127.0.0.1", pick_port()), Handler)


if __name__ == "__main__":
    server = serve()
    host, port = server.server_address[:2]
    print(f"安徽工业大学校园用餐推荐 已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
