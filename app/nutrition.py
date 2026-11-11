"""营养计算：BMR / TDEE / 活动系数 / 三大营养素目标 / 菜品单项分数。

依据
----
《中国居民膳食指南（2022）》能量供给比（AMDR）：
* 碳水化合物 50%–65% 能量
* 脂肪       20%–30% 能量
* 蛋白质     10%–20% 能量（并参考成人蛋白质推荐摄入量 男 65 g/d、女 55 g/d）

能量换算：碳水 4 kcal/g、蛋白质 4 kcal/g、脂肪 9 kcal/g。
基础代谢用 Mifflin-St Jeor 公式；总消耗 = BMR × 活动系数。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

# ── 活动系数（用户可选） ────────────────────────────────────────────────
ACTIVITY_LEVELS = {
    "sedentary": {"factor": 1.2, "label": "久坐不动", "desc": "几乎不运动，长时间坐着学习"},
    "light": {"factor": 1.375, "label": "轻度活动", "desc": "每周 1–3 天轻度运动"},
    "moderate": {"factor": 1.55, "label": "中度活动", "desc": "每周 3–5 天中等强度运动"},
    "heavy": {"factor": 1.725, "label": "高度活动", "desc": "每周 6–7 天高强度运动"},
    "extreme": {"factor": 1.9, "label": "极高强度", "desc": "体力劳动或每天两次训练"},
}

GOALS = {
    "balanced": {"label": "保持体重", "kcal_ratio": 1.0},
    "lose": {"label": "减脂", "kcal_ratio": 0.85},
    "gain": {"label": "增肌", "kcal_ratio": 1.10},
}

GENDERS = {"male": "男", "female": "女"}

# 《中国居民膳食指南(2022)》AMDR：能量占比区间
AMDR = {
    "carbs": (0.50, 0.65),
    "fat": (0.20, 0.30),
    "protein": (0.10, 0.20),
}

KCAL_PER_GRAM = {"carbs": 4.0, "protein": 4.0, "fat": 9.0}

# 成人蛋白质推荐摄入量（g/d），膳食指南 2022 表
PROTEIN_RNI = {"male": 65.0, "female": 55.0}

# 一天按餐数切分能量（早/午/晚 + 加餐）
MEAL_SPLITS = {
    1: [("正餐", 1.00)],
    2: [("午餐", 0.50), ("晚餐", 0.50)],
    3: [("早餐", 0.30), ("午餐", 0.40), ("晚餐", 0.30)],
    4: [("早餐", 0.25), ("午餐", 0.30), ("加餐", 0.15), ("晚餐", 0.30)],
    5: [("早餐", 0.20), ("上午加餐", 0.10), ("午餐", 0.30), ("下午加餐", 0.10), ("晚餐", 0.30)],
    6: [("早餐", 0.18), ("上午加餐", 0.09), ("午餐", 0.26), ("下午加餐", 0.09), ("晚餐", 0.26), ("夜宵", 0.12)],
}

SLOT_OF_MEAL = {
    "早餐": "breakfast",
    "午餐": "lunch",
    "晚餐": "dinner",
    "加餐": "snack",
    "正餐": "lunch",
    "上午加餐": "snack",
    "下午加餐": "snack",
    "夜宵": "snack",
}

SLOT_LABELS = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "snack": "加餐",
}


def age_from_birth(birth_date: str, on: Optional[date] = None) -> int:
    """根据出生日期算周岁，解析失败时按 20 岁兜底。"""
    on = on or date.today()
    try:
        born = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return 20
    years = on.year - born.year - ((on.month, on.day) < (born.month, born.day))
    return max(10, min(years, 100))


def bmr(gender: str, weight_kg: float, height_cm: float, age: int) -> float:
    """Mifflin-St Jeor 基础代谢率（kcal/d）。"""
    base = 10.0 * weight_kg + 6.25 * height_cm - 5.0 * age
    return base + (5.0 if gender == "male" else -161.0)


def tdee(bmr_value: float, activity_level: str) -> float:
    factor = ACTIVITY_LEVELS.get(activity_level, ACTIVITY_LEVELS["light"])["factor"]
    return bmr_value * factor


def macro_targets(energy_kcal: float, weight_kg: float) -> dict:
    """把每日目标能量拆成三大营养素克数，尽量同时满足 AMDR 与蛋白质 RNI。

    步骤：
    1. 蛋白质先按 1.2 g/kg 体重估（学习场景下的温和高蛋白），再夹到 AMDR 的 10%–20%E
       与 RNI（男 65g / 女 55g）之间；
    2. 脂肪先按 25%E 取中值；
    3. 碳水吃掉剩余能量；若碳水超出 50%–65%E，就在脂肪 20%–30%E 区间内挪动来修正。
    """
    weight_kg = max(30.0, min(weight_kg, 200.0))
    energy_kcal = max(1000.0, energy_kcal)

    lo_p, hi_p = AMDR["protein"][0] * energy_kcal / KCAL_PER_GRAM["protein"], \
        AMDR["protein"][1] * energy_kcal / KCAL_PER_GRAM["protein"]
    protein_g = _clamp(1.2 * weight_kg, lo_p, hi_p)

    fat_share = 0.25
    carb_share = 1.0 - protein_g * KCAL_PER_GRAM["protein"] / energy_kcal - fat_share

    lo_c, hi_c = AMDR["carbs"]
    lo_f, hi_f = AMDR["fat"]
    # 碳水偏高 → 用脂肪上限吸收
    while carb_share > hi_c and fat_share < hi_f:
        fat_share = min(hi_f, fat_share + 0.01)
        carb_share = 1.0 - protein_g * KCAL_PER_GRAM["protein"] / energy_kcal - fat_share
    # 碳水偏低 → 先降脂肪，再降蛋白（不低于 10%E）
    while carb_share < lo_c and fat_share > lo_f:
        fat_share = max(lo_f, fat_share - 0.01)
        carb_share = 1.0 - protein_g * KCAL_PER_GRAM["protein"] / energy_kcal - fat_share
    while carb_share < lo_c and protein_g > lo_p:
        protein_g = max(lo_p, protein_g - 1.0)
        carb_share = 1.0 - protein_g * KCAL_PER_GRAM["protein"] / energy_kcal - fat_share

    fat_g = fat_share * energy_kcal / KCAL_PER_GRAM["fat"]
    carbs_g = max(0.0, carb_share) * energy_kcal / KCAL_PER_GRAM["carbs"]

    return {
        "kcal": round(energy_kcal, 1),
        "protein": round(protein_g, 1),
        "fat": round(fat_g, 1),
        "carbs": round(carbs_g, 1),
        "shares": {
            "protein": round(protein_g * KCAL_PER_GRAM["protein"] / energy_kcal, 4),
            "fat": round(fat_share, 4),
            "carbs": round(max(0.0, carb_share), 4),
        },
        "protein_per_kg": round(protein_g / weight_kg, 2),
    }


def profile_metrics(profile: dict) -> dict:
    """把用户资料换算成每日与单餐目标。"""
    gender = profile.get("gender", "male")
    weight = float(profile.get("weight_kg") or 65)
    height = float(profile.get("height_cm") or 172)
    age = age_from_birth(profile.get("birth_date") or "2004-01-01")
    activity = profile.get("activity_level") or "light"
    goal = profile.get("goal") or "balanced"

    bmr_value = bmr(gender, weight, height, age)
    total = tdee(bmr_value, activity)
    ratio = GOALS.get(goal, GOALS["balanced"])["kcal_ratio"]
    energy = total * ratio
    target = macro_targets(energy, weight)

    meals_per_day = int(profile.get("meals_per_day") or 3)
    meals_per_day = max(1, min(meals_per_day, 6))
    splits = MEAL_SPLITS[meals_per_day]

    meals = []
    for idx, (name, share) in enumerate(splits, start=1):
        meals.append({
            "index": idx,
            "name": name,
            "slot": SLOT_OF_MEAL.get(name, "lunch"),
            "share": share,
            "kcal": round(target["kcal"] * share, 1),
            "protein": round(target["protein"] * share, 1),
            "fat": round(target["fat"] * share, 1),
            "carbs": round(target["carbs"] * share, 1),
        })

    return {
        "age": age,
        "gender": gender,
        "gender_label": GENDERS.get(gender, "男"),
        "activity_level": activity,
        "activity_label": ACTIVITY_LEVELS.get(activity, ACTIVITY_LEVELS["light"])["label"],
        "activity_factor": ACTIVITY_LEVELS.get(activity, ACTIVITY_LEVELS["light"])["factor"],
        "goal": goal,
        "goal_label": GOALS.get(goal, GOALS["balanced"])["label"],
        "bmr": round(bmr_value, 1),
        "tdee": round(total, 1),
        "daily": target,
        "meals_per_day": meals_per_day,
        "meals": meals,
        "amdr": {k: (lo * 100, hi * 100) for k, (lo, hi) in AMDR.items()},
        "reference": "《中国居民膳食指南（2022）》能量供给比（AMDR）",
    }


# ── 评分 ────────────────────────────────────────────────────────────────
def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def band_score(value: float, lo: float, hi: float, softness: float = 0.06) -> float:
    """落在区间内得满分；越出区间后按 softness 线性衰减到 0。

    这样"刚好在指南区间内"是 1.0，"略微超一点"仍能拿到 0.7+，
    避免分数呈现非黑即白。
    """
    if lo <= value <= hi:
        return 1.0
    distance = (lo - value) if value < lo else (value - hi)
    return max(0.0, 1.0 - distance / softness)


def dish_macro_energy(dish: dict) -> dict:
    """一道菜三大营养素各自的能量占比（0–1）与绝对克数。"""
    protein = float(dish.get("protein") or 0)
    fat = float(dish.get("fat") or 0)
    carbs = float(dish.get("carbs") or 0)
    kcal = float(dish.get("kcal") or 0)
    computed = protein * 4 + fat * 9 + carbs * 4
    energy = kcal if kcal > 0 else computed
    if energy <= 0:
        return {"kcal": 0.0, "protein": 0.0, "fat": 0.0, "carbs": 0.0,
                "protein_share": 0.0, "fat_share": 0.0, "carbs_share": 0.0}
    return {
        "kcal": round(energy, 1),
        "protein": protein,
        "fat": fat,
        "carbs": carbs,
        "protein_share": round(protein * 4 / energy, 4),
        "fat_share": round(fat * 9 / energy, 4),
        "carbs_share": round(carbs * 4 / energy, 4),
    }


def dish_scores(dish: dict) -> dict:
    """单道菜三大营养素分数（0–1），以及综合营养分。

    说明：单道菜很难同时满足全天比例，所以这里是"这道菜自身结构是否均衡"的
    指标——主食类碳水占比高、清炒素菜蛋白占比低都会被如实扣分，
    推荐套餐时会靠组合来补齐（见 recommender.py）。
    """
    shares = dish_macro_energy(dish)
    protein_score = band_score(shares["protein_share"], *AMDR["protein"], softness=0.10)
    fat_score = band_score(shares["fat_share"], *AMDR["fat"], softness=0.12)
    carbs_score = band_score(shares["carbs_share"], *AMDR["carbs"], softness=0.18)

    # 蛋白质密度加分：每 100 kcal 提供的蛋白质量（15 g/100kcal 视为满分）
    density = (float(dish.get("protein") or 0) / shares["kcal"] * 100.0) if shares["kcal"] else 0.0
    density_bonus = _clamp(density / 15.0, 0.0, 1.0)

    total = (
        0.40 * protein_score
        + 0.25 * fat_score
        + 0.20 * carbs_score
        + 0.15 * density_bonus
    )
    return {
        "protein_score": round(protein_score, 3),
        "fat_score": round(fat_score, 3),
        "carbs_score": round(carbs_score, 3),
        "protein_density": round(density, 2),
        "total": round(total, 3),
        "shares": {
            "protein": round(shares["protein_share"] * 100, 1),
            "fat": round(shares["fat_share"] * 100, 1),
            "carbs": round(shares["carbs_share"] * 100, 1),
        },
    }


def evaluate_totals(totals: dict, target: dict) -> dict:
    """评估一份套餐（或一餐）合计值相对单餐目标的表现。"""
    kcal = float(totals.get("kcal") or 0)
    protein = float(totals.get("protein") or 0)
    fat = float(totals.get("fat") or 0)
    carbs = float(totals.get("carbs") or 0)

    target_kcal = max(1.0, float(target.get("kcal") or 1))
    energy_score = band_score(kcal / target_kcal, 0.90, 1.10, softness=0.25)

    energy = protein * 4 + fat * 9 + carbs * 4
    if energy > 0:
        p_share, f_share, c_share = protein * 4 / energy, fat * 9 / energy, carbs * 4 / energy
    else:
        p_share = f_share = c_share = 0.0

    protein_score = band_score(p_share, *AMDR["protein"], softness=0.08)
    fat_score = band_score(f_share, *AMDR["fat"], softness=0.08)
    carbs_score = band_score(c_share, *AMDR["carbs"], softness=0.12)

    total = (
        0.30 * energy_score
        + 0.30 * protein_score
        + 0.20 * fat_score
        + 0.20 * carbs_score
    )
    return {
        "energy": round(energy_score, 3),
        "protein": round(protein_score, 3),
        "fat": round(fat_score, 3),
        "carbs": round(carbs_score, 3),
        "total": round(total, 3),
        "macro_pct": {
            "protein": round(p_share * 100, 1),
            "fat": round(f_share * 100, 1),
            "carbs": round(c_share * 100, 1),
        },
        "kcal": round(kcal, 1),
        "target_kcal": round(target_kcal, 1),
    }


def advice(evaluation: dict, target: dict) -> list[str]:
    """根据评估结果给出中文提示，用于推荐卡片与自助选菜的反馈。"""
    tips: list[str] = []
    pct = evaluation["macro_pct"]
    target_kcal = max(1.0, float(target.get("kcal") or 1))
    ratio = evaluation["kcal"] / target_kcal
    if ratio < 0.85:
        tips.append(f"总热量偏低（{evaluation['kcal']:.0f} / 目标 {target_kcal:.0f} kcal，仅 {ratio:.0%}）")
    elif ratio < 0.92:
        tips.append(f"总热量略低于目标（{evaluation['kcal']:.0f} / {target_kcal:.0f} kcal）")
    elif ratio > 1.15:
        tips.append(f"总热量偏高（{evaluation['kcal']:.0f} / 目标 {target_kcal:.0f} kcal，达 {ratio:.0%}）")
    elif ratio > 1.08:
        tips.append(f"总热量略高于目标（{evaluation['kcal']:.0f} / {target_kcal:.0f} kcal）")

    if pct["protein"] < AMDR["protein"][0] * 100:
        tips.append(f"蛋白质占比 {pct['protein']:.0f}% 低于指南下限 10%，建议加一份肉类/蛋/豆制品")
    elif pct["protein"] > AMDR["protein"][1] * 100:
        tips.append(f"蛋白质占比 {pct['protein']:.0f}% 超出指南上限 20%")

    if pct["fat"] > AMDR["fat"][1] * 100:
        tips.append(f"脂肪占比 {pct['fat']:.0f}% 偏高，可换清淡窗口或减少油炸菜")
    elif pct["fat"] < AMDR["fat"][0] * 100:
        tips.append(f"脂肪占比 {pct['fat']:.0f}% 偏低，可加少量坚果/豆制品")

    if pct["carbs"] > AMDR["carbs"][1] * 100:
        tips.append(f"碳水占比 {pct['carbs']:.0f}% 偏高，主食可减半")
    elif pct["carbs"] < AMDR["carbs"][0] * 100:
        tips.append(f"碳水占比 {pct['carbs']:.0f}% 偏低，建议补一份主食")

    if not tips:
        tips.append("三大营养素与热量都落在指南推荐区间内，可以放心吃")
    return tips
