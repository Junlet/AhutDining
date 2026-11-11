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
    data = call("PUT", "/api/profile", {
        "height_cm": 175, "weight_kg": 68, "gender": "male",
        "activity_level": "moderate", "meals_per_day": 4, "goal": "balanced",
    })
    assert data["metrics"]["meals_per_day"] == 4
    assert data["metrics"]["activity_factor"] == 1.55
    assert len(call("GET", f"/api/records?date={STATE['date']}")["records"]) == 4
    # 还原
    call("PUT", "/api/profile", {
        "height_cm": before["profile"]["height_cm"],
        "weight_kg": before["profile"]["weight_kg"],
        "gender": before["profile"]["gender"],
        "activity_level": before["profile"]["activity_level"],
        "meals_per_day": before["profile"]["meals_per_day"],
        "goal": before["profile"]["goal"],
    })
    assert len(call("GET", f"/api/records?date={STATE['date']}")["records"]) == 3


def test_settings_and_stats() -> None:
    data = call("PUT", "/api/settings", {"slot.lunch.end": "13:45"})
    assert data["settings"]["slot.lunch.end"] == "13:45"
    call("PUT", "/api/settings", {"slot.lunch.end": "13:30"})
    stats = call("GET", "/api/stats")
    assert "remaining" in stats and stats["daily"]["kcal"] > 0
    slot = call("GET", "/api/slot")
    assert slot["slot"] in ("breakfast", "lunch", "dinner")


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
]:
    check(name, fn)

print("-" * 46)
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
