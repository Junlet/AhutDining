"""接口冒烟测试：不依赖第三方库，直接用 urllib 打本机服务。

用法（服务已启动后）：
    python tests/smoke_test.py            # 默认 http://127.0.0.1:8848
    python tests/smoke_test.py 8849
"""

from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = f"http://127.0.0.1:{sys.argv[1] if len(sys.argv) > 1 else '8848'}"

passed, failed = 0, 0


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    safe_path = urllib.parse.quote(path, safe="/?=&:%")
    req = urllib.request.Request(
        BASE + safe_path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
    if not payload.get("ok"):
        raise AssertionError(f"{method} {path} 失败: {payload.get('error')}")
    return payload.get("data", {})


def check(name: str, fn) -> None:
    global passed, failed
    try:
        fn()
        print(f"[PASS] {name}")
        passed += 1
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {name}: {exc}")
        failed += 1


# 1x1 透明 PNG，用于验证图片上传链路
TINY_PNG = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000a49444154789c6360000002000100ffff0300000600"
    "05570d0e890000000049454e44ae426082"
)).decode()


def test_health() -> None:
    data = call("GET", "/api/health")
    assert data["service"] == "ahut-dining"


def test_bootstrap() -> None:
    data = call("GET", "/api/bootstrap")
    assert data["metrics"]["daily"]["kcal"] > 1000
    assert data["datasets"]["dishes"] > 30
    assert data["metrics"]["daily"]["shares"]["protein"] >= 0.10
    assert len(data["records"]) == data["metrics"]["meals_per_day"]
    global STATE
    STATE["date"] = data["date"]
    STATE["meal_index"] = data["records"][0]["meal_index"]


def test_plans() -> None:
    data = call("GET", "/api/plans?slot=lunch")
    assert data["plans"], "没有生成推荐套餐"
    plan = data["plans"][0]
    assert plan["items"] and plan["target"]["kcal"] > 300
    STATE["plan"] = plan
    STATE["batch"] = data["batch"]


def test_refresh() -> None:
    data = call("POST", "/api/plans/refresh", {"slot": "lunch"})
    assert data["batch"] == STATE["batch"] + 1, "刷新没有产生新批次"
    ids = {tuple(sorted(i["dish_id"] for i in p["items"])) for p in data["plans"]}
    assert ids, "刷新后没有套餐"


def test_evaluate_custom() -> None:
    dishes = call("GET", "/api/dishes?q=麻辣烫")["items"]
    assert dishes, "找不到麻辣烫示例数据"
    data = call("POST", "/api/plans/evaluate",
                {"dish_ids": [d["dish_id"] for d in dishes], "slot": "lunch"})
    assert data["recommend_level"] in ("强烈推荐", "推荐", "一般", "不推荐")
    assert 0 <= data["scores"]["total"] <= 1
    assert data["advice"]


def test_search_and_detail() -> None:
    result = call("GET", "/api/dishes?q=牛肉")
    assert result["total"] >= 1
    dish_id = result["items"][0]["dish_id"]
    detail = call("GET", f"/api/dishes/{dish_id}")
    assert detail["scores"]["total"] > 0
    assert isinstance(detail["price_compare"], list)
    STATE["dish_id"] = dish_id


def test_comment() -> None:
    created = call("POST", "/api/comments",
                   {"dish_id": STATE["dish_id"], "rating": 5, "content": "冒烟测试评价"})
    detail = call("GET", f"/api/dishes/{STATE['dish_id']}")
    assert any(c["id"] == created["id"] for c in detail["comments"])
    call("DELETE", f"/api/comments/{created['id']}")
    detail = call("GET", f"/api/dishes/{STATE['dish_id']}")
    assert not any(c["id"] == created["id"] for c in detail["comments"])


def test_favorite() -> None:
    res = call("POST", f"/api/favorites/{STATE['dish_id']}")
    assert res["favorite"] == 1
    assert any(i["dish_id"] == STATE["dish_id"] for i in call("GET", "/api/favorites")["items"])
    res = call("POST", f"/api/favorites/{STATE['dish_id']}")
    assert res["favorite"] == 0


def test_image_upload() -> None:
    res = call("POST", "/api/images", {
        "owner_type": "dish", "owner_id": STATE["dish_id"],
        "filename": "test.png", "data": TINY_PNG,
    })
    assert res["path"].endswith(".png")
    with urllib.request.urlopen(BASE + res["url"], timeout=10) as resp:
        assert resp.status == 200
    detail = call("GET", f"/api/dishes/{STATE['dish_id']}")
    assert any(img["path"] == res["path"] for img in detail["images"]), "详情里没有新图片"


def test_confirm_record() -> None:
    plan = STATE["plan"]
    data = call("POST", "/api/records/confirm", {
        "date": STATE["date"], "meal_index": plan["target"]["meal_index"],
        "plan_id": plan["id"], "items": [{"dish_id": i["dish_id"]} for i in plan["items"]],
        "status": "confirmed",
    })
    record = data["record"]
    assert record["status"] == "confirmed"
    assert abs(record["kcal"] - plan["totals"]["kcal"]) < 0.5
    records = call("GET", f"/api/records?date={STATE['date']}")["records"]
    assert any(r["status"] == "confirmed" for r in records)


def test_manual_record_and_clear() -> None:
    dishes = call("GET", "/api/dishes?q=米饭")["items"][:2]
    call("POST", "/api/records/confirm", {
        "date": STATE["date"], "meal_index": 2,
        "items": [{"dish_id": d["dish_id"]} for d in dishes], "status": "manual",
    })
    records = call("GET", f"/api/records?date={STATE['date']}")["records"]
    assert any(r["status"] == "manual" for r in records)
    # 两个餐次都清空，保持演示数据干净
    call("DELETE", f"/api/records/{STATE['date']}/1")
    call("DELETE", f"/api/records/{STATE['date']}/2")
    records = call("GET", f"/api/records?date={STATE['date']}")["records"]
    assert all(r["status"] == "empty" for r in records)


def test_profile_update() -> None:
    before = call("GET", "/api/profile")
    before_meals = before["profile"]["meals_per_day"]
    data = call("PUT", "/api/profile", {
        "height_cm": 175, "weight_kg": 68, "gender": "male",
        "activity_level": "moderate", "meals_per_day": 4, "goal": "balanced",
    })
    assert data["metrics"]["meals_per_day"] == 4
    assert data["metrics"]["activity_factor"] == 1.55
    records = call("GET", f"/api/records?date={STATE['date']}")["records"]
    assert len(records) == 4, f"改成一餐数 4 后应有 4 个栏位，实际 {len(records)}"
    assert records[2]["meal_name"] == "加餐", f"第 3 餐应叫加餐，实际 {records[2]['meal_name']}"
    # 还原
    call("PUT", "/api/profile", {
        "height_cm": before["profile"]["height_cm"],
        "weight_kg": before["profile"]["weight_kg"],
        "gender": before["profile"]["gender"],
        "activity_level": before["profile"]["activity_level"],
        "meals_per_day": before_meals,
        "goal": before["profile"]["goal"],
    })
    records = call("GET", f"/api/records?date={STATE['date']}")["records"]
    assert len(records) == before_meals, f"还原后应有 {before_meals} 个栏位，实际 {len(records)}"


def test_settings_and_stats() -> None:
    data = call("PUT", "/api/settings", {"slot.lunch.end": "13:45"})
    assert data["settings"]["slot.lunch.end"] == "13:45"
    call("PUT", "/api/settings", {"slot.lunch.end": "13:30"})
    stats = call("GET", "/api/stats")
    assert "remaining" in stats and stats["daily"]["kcal"] > 0
    slot = call("GET", "/api/slot")
    assert slot["slot"] in ("breakfast", "lunch", "dinner")




def _meals_per_day() -> int:
    return call("GET", "/api/profile")["metrics"]["meals_per_day"]


def _day_state(include_passed: int = 1) -> dict:
    return call("GET", f"/api/day-plan?include_passed={include_passed}")


def _ensure_options(include_passed: int = 1) -> dict:
    call("POST", "/api/day-plan/reset", {})
    state = _day_state(include_passed)
    if state["stage"] != "options":
        raise AssertionError(f"重置后应回到方案选择，实际 {state['stage']}")
    return state


def _ensure_chosen(include_passed: int = 1) -> dict:
    state = _day_state(include_passed)
    if state["stage"] == "options":
        state = call("POST", "/api/day-plan/choose", {"index": 0})
    return state


def test_day_plan_options() -> None:
    state = _ensure_options()
    options = state["options"]
    assert len(options) == 3, f"A/B/C 应有 3 套方案，实际 {len(options)}"
    labels = [o["label"] for o in options]
    assert labels == ["A", "B", "C"], labels
    for opt in options:
        assert len(opt["meals"]) == _meals_per_day()
        planned = [m for m in opt["meals"] if m["status"] == "planned"]
        assert planned, "方案里没有可安排餐次"
        for meal in planned:
            assert meal["items"], f"{opt['label']} 的 {meal['meal_name']} 没有菜品"
        assert opt["summary"]["plan_kcal"] > 500
        assert 0 <= opt["avg_score"] <= 1
        assert opt["level"] in ("强烈推荐", "推荐", "一般", "不推荐")
    # 三套方案不能完全相同
    signatures = {tuple(tuple(i["dish_id"] for i in m["items"]) for m in o["meals"]) for o in options}
    assert len(signatures) > 1, "三套方案内容完全一样"


def test_day_plan_reselect() -> None:
    data = call("POST", "/api/day-plan/refresh", {"include_passed": True})
    assert data["stage"] == "options"
    first = [tuple(i["dish_id"] for i in m["items"]) for m in data["options"][0]["meals"]]
    again = call("POST", "/api/day-plan/refresh", {"include_passed": True})
    second = [tuple(i["dish_id"] for i in m["items"]) for m in again["options"][0]["meals"]]
    assert first != second, "换一批方案后内容没有变化"


def test_day_plan_choose() -> None:
    _ensure_options()
    state = call("POST", "/api/day-plan/choose", {"index": 1})
    assert state["stage"] == "chosen"
    assert state["plan"]["label"] == "B"
    assert state["options"] == [], "选定后其它方案应消失"
    assert len(state["plan"]["meals"]) == _meals_per_day()
    STATE["day_plan"] = state["plan"]


def test_day_plan_time_filter() -> None:
    state = _day_state(0)
    plan = state.get("plan") or {}
    meals = plan.get("meals") or []
    for meal in meals:
        if meal["status"] == "missed":
            assert not meal["items"], "已错过的餐次不该有推荐内容"
    if not state.get("include_passed"):
        assert not any(m["status"] == "missed" and m["items"] for m in meals)
    else:
        assert state.get("auto_include_passed") is True, "自动包含已过时段时应给出标记"


def test_day_plan_pin_and_refresh() -> None:
    state = _ensure_chosen()
    planned = [m for m in state["plan"]["meals"] if m["status"] == "planned"]
    assert len(planned) >= 2, "至少两餐才能验证固定后刷新"
    target = planned[0]
    pinned_ids = [i["dish_id"] for i in target["items"]]
    after = call("POST", "/api/day-plan/pin",
                 {"meal_index": target["meal_index"], "pinned": True})
    block = next(m for m in after["plan"]["meals"] if m["meal_index"] == target["meal_index"])
    assert block["pinned"] is True

    refreshed = call("POST", "/api/day-plan/refresh", {"include_passed": True})
    block2 = next(m for m in refreshed["plan"]["meals"] if m["meal_index"] == target["meal_index"])
    assert [i["dish_id"] for i in block2["items"]] == pinned_ids, "固定餐次被改掉了"


def test_day_plan_only_meal() -> None:
    state = _ensure_chosen()
    planned = [m for m in state["plan"]["meals"] if m["status"] == "planned" and m["items"]]
    assert len(planned) >= 2, "至少两餐才能验证单餐刷新"
    keep = {m["meal_index"]: [i["dish_id"] for i in m["items"]] for m in planned}
    change_index = planned[0]["meal_index"]
    swapped = call("POST", "/api/day-plan/refresh",
                   {"only_meal": change_index, "include_passed": True})
    for meal in swapped["plan"]["meals"]:
        idx = meal["meal_index"]
        if idx in keep and idx != change_index and meal["status"] == "planned":
            assert [i["dish_id"] for i in meal["items"]] == keep[idx], "单餐刷新改动了其它餐"


def test_day_plan_set_meal() -> None:
    state = _ensure_chosen()
    planned = [m for m in state["plan"]["meals"] if m["status"] == "planned"]
    assert planned
    target = planned[0]
    dishes = call("GET", "/api/dishes?q=" + "%E7%B1%B3%E9%A5%AD")["items"]
    assert dishes, "找不到米饭示例菜品"
    dish_id = dishes[0]["dish_id"]
    before_score = target["scores"]["total"]

    data = call("POST", "/api/day-plan/set-meal",
                {"meal_index": target["meal_index"], "dish_ids": [dish_id]})
    block = data["updated_meal"]
    assert [i["dish_id"] for i in block["items"]] == [dish_id], "替换后的内容不对"
    assert block["source"] == "manual"
    assert block["pinned"] is True, "用户自选的餐应自动固定"
    assert block["level"] in ("强烈推荐", "推荐", "一般", "不推荐")
    assert block["scores"]["total"] != before_score

    # 单菜通常不均衡，自动补齐后推荐度应不降
    low = block["scores"]["total"]
    filled = call("POST", "/api/day-plan/complete-meal",
                  {"meal_index": target["meal_index"]})
    after = filled["updated_meal"]
    assert len(after["items"]) >= 1
    assert after["scores"]["total"] >= low - 1e-6, "自动补齐后推荐度不应下降"

    # 自选并固定后，刷新整日不应改动这一餐
    kept = [i["dish_id"] for i in after["items"]]
    refreshed = call("POST", "/api/day-plan/refresh", {"include_passed": True})
    block3 = next(m for m in refreshed["plan"]["meals"] if m["meal_index"] == target["meal_index"])
    assert [i["dish_id"] for i in block3["items"]] == kept, "自选的餐在刷新后应保持不变"


def test_day_plan_manual_pick() -> None:
    """手动选择一餐：多选菜品替换该餐，推荐度重算，且刷新后保持不变。"""
    state = _ensure_chosen()
    planned = [m for m in state["plan"]["meals"] if m["status"] == "planned"]
    assert planned, "没有可手动选择的餐次"
    target = planned[0]
    dishes = call("GET", "/api/dishes?sort=score")["items"]
    assert len(dishes) >= 3
    picked = [dishes[0]["dish_id"], dishes[1]["dish_id"], dishes[2]["dish_id"]]

    data = call("POST", "/api/day-plan/set-meal",
                {"meal_index": target["meal_index"], "dish_ids": picked})
    block = data["updated_meal"]
    got = [i["dish_id"] for i in block["items"]]
    assert got == picked, f"多选后的菜品不符：{got} != {picked}"
    assert block["source"] == "manual"
    assert block["pinned"] is True
    assert 0 <= block["scores"]["total"] <= 1
    assert block["level"] in ("强烈推荐", "推荐", "一般", "不推荐")
    assert abs(block["totals"]["kcal"]
               - sum(d["kcal"] for d in dishes[:3])) < 1.0

    refreshed = call("POST", "/api/day-plan/refresh", {"include_passed": True})
    block2 = next(m for m in refreshed["plan"]["meals"]
                  if m["meal_index"] == target["meal_index"])
    assert [i["dish_id"] for i in block2["items"]] == picked, "手动选择后刷新应保持"


def test_day_plan_manual_flow() -> None:
    """手选只替换那一餐：其它餐必须保持"未选择"，不能被系统自动补上。"""
    call("POST", "/api/day-plan/reset", {})
    state = call("GET", "/api/day-plan?include_passed=0")
    options = state.get("options") or []
    assert options, "没有 A/B/C 方案可用"
    meals = options[0]["meals"]
    upcoming = [m for m in meals if m["status"] == "planned"]
    missed = [m for m in meals if m["status"] == "missed"]
    assert upcoming, "当前没有未到时段的餐次"
    dishes = call("GET", "/api/dishes?sort=score")["items"]
    assert len(dishes) >= 3
    first_ids = [dishes[0]["dish_id"], dishes[1]["dish_id"]]
    target_index = upcoming[0]["meal_index"]

    # 1) 手选一餐：只替换这一餐，其它未选餐次应为 empty
    data = call("POST", "/api/day-plan/manual-meal",
                {"meal_index": target_index, "dish_ids": first_ids})
    assert data["saved_as"] == "plan"
    assert data["stage"] == "manual"
    plan = data["plan"]
    block = next(m for m in plan["meals"] if m["meal_index"] == target_index)
    assert [i["dish_id"] for i in block["items"]] == first_ids, "手选应替换该餐内容"
    assert block["pinned"] is True

    others = [m for m in plan["meals"] if m["meal_index"] != target_index]
    filled = [m for m in others if m["status"] == "planned" and m["items"]]
    assert not filled, f"未选餐次不该被自动补上，实际补了 {[m['meal_name'] for m in filled]}"
    empties = [m for m in others if m["status"] == "empty"]
    assert empties, "未选的餐次应为 empty 状态"
    assert plan["summary"]["meals_empty"] == len(empties)

    # 2) 只给某一餐要系统推荐：其它餐仍然保持原样
    if len(empties) >= 1:
        one = empties[0]
        filled_state = call("POST", "/api/day-plan/fill-meal",
                            {"meal_index": one["meal_index"]})
        blk = next(m for m in filled_state["plan"]["meals"]
                   if m["meal_index"] == one["meal_index"])
        assert blk["items"], "系统推荐后这一餐应有内容"
        blk_keep = next(m for m in filled_state["plan"]["meals"]
                        if m["meal_index"] == target_index)
        assert [i["dish_id"] for i in blk_keep["items"]] == first_ids, "手选的那一餐不该被改"

    # 3) 设为今日计划
    committed = call("POST", "/api/day-plan/commit", {})
    assert committed["stage"] == "chosen"

    # 4) 已过时段手选：替换该餐并同步记录
    if missed:
        m = missed[0]
        rec = call("POST", "/api/day-plan/manual-meal",
                   {"meal_index": m["meal_index"], "dish_ids": [dishes[2]["dish_id"]]})
        assert rec["record_synced"] is True
        blk2 = next(b for b in rec["plan"]["meals"] if b["meal_index"] == m["meal_index"])
        assert [i["dish_id"] for i in blk2["items"]] == [dishes[2]["dish_id"]]
        records = call("GET", f"/api/records?date={state['date']}")["records"]
        saved = next(r for r in records if r["meal_index"] == m["meal_index"])
        assert [i["dish_id"] for i in saved["items"]] == [dishes[2]["dish_id"]]
        call("DELETE", f"/api/records/{state['date']}/{m['meal_index']}")

    # 5) 一键补齐剩余餐次
    if empties:
        filled_all = call("POST", "/api/day-plan/fill-remaining", {})
        assert filled_all["plan"]["summary"].get("meals_empty", 0) == 0, "补齐后不应还有空餐次"

    call("POST", "/api/day-plan/reset", {})



def test_cart_day_evaluation() -> None:
    """购物车按「全天合计」评估：target 应是整日额度而不是单餐。"""
    dishes = call("GET", "/api/dishes?sort=score&limit=3")["items"]
    assert len(dishes) == 3
    single = call("POST", "/api/plans/evaluate",
                  {"dish_ids": [dishes[0]["dish_id"]], "slot": "lunch"})
    day = call("POST", "/api/plans/evaluate",
               {"dish_ids": [d["dish_id"] for d in dishes], "slot": "day"})
    assert day["target"]["slot"] == "day"
    assert day["target"]["kcal"] > single["target"]["kcal"], "全天目标应大于单餐目标"
    assert day["recommend_level"] in ("强烈推荐", "推荐", "一般", "不推荐")
    assert day["advice"]
def test_day_plan_confirm_effect() -> None:
    state = _ensure_chosen()
    planned = [m for m in state["plan"]["meals"] if m["status"] == "planned" and m["items"]]
    assert planned
    target = planned[-1]
    call("POST", "/api/records/confirm", {
        "date": state["date"],
        "meal_index": target["meal_index"],
        "items": [{"dish_id": i["dish_id"]} for i in target["items"]],
        "status": "confirmed",
    })
    after = call("GET", "/api/day-plan?include_passed=1")
    block = next(m for m in after["plan"]["meals"] if m["meal_index"] == target["meal_index"])
    assert block["status"] == "recorded", "确认后该餐应变成已记录"
    assert after["plan"]["summary"]["meals_recorded"] >= 1
    call("DELETE", f"/api/records/{state['date']}/{target['meal_index']}")


def test_day_plan_reset() -> None:
    data = call("POST", "/api/day-plan/reset", {})
    assert data["stage"] == "options"
    assert data["plan"] is None
    assert len(data["options"]) == 3


STATE: dict = {}

for name, fn in [
    ("健康检查", test_health),
    ("初始化数据", test_bootstrap),
    ("推荐套餐", test_plans),
    ("刷新推荐", test_refresh),
    ("自选评估", test_evaluate_custom),
    ("搜索与详情", test_search_and_detail),
    ("评论增删", test_comment),
    ("收藏开关", test_favorite),
    ("图片上传", test_image_upload),
    ("确认用餐记录", test_confirm_record),
    ("手动补记与清空", test_manual_record_and_clear),
    ("修改资料联动餐次", test_profile_update),
    ("设置与统计", test_settings_and_stats),
    ("今日计划·A/B/C 方案", test_day_plan_options),
    ("今日计划·换一批方案", test_day_plan_reselect),
    ("今日计划·选择方案", test_day_plan_choose),
    ("今日计划·时间过滤", test_day_plan_time_filter),
    ("今日计划·固定与刷新", test_day_plan_pin_and_refresh),
    ("今日计划·单餐刷新", test_day_plan_only_meal),
    ("今日计划·自选菜替换并补齐", test_day_plan_set_meal),
    ("今日计划·手动多选一餐", test_day_plan_manual_pick),
    ("购物车·全天评估", test_cart_day_evaluation),
    ("今日计划·手动选择补记/计划并确认", test_day_plan_manual_flow),
    ("今日计划·确认联动", test_day_plan_confirm_effect),
    ("今日计划·重置", test_day_plan_reset),
]:
    check(name, fn)

print("-" * 46)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
