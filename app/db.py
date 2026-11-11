"""数据库层：SQLite 连接、建表、通用查询helper。

设计要点
--------
* 单机单用户，但表里保留 user_id 字段（固定为 1），方便以后升级成多账号。
* 所有"个人数据"（收藏 / 评论 / 用餐记录 / 资料）都落在本地 SQLite，
  不依赖任何外部服务。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
DB_PATH = os.path.join(DATA_DIR, "dining.db")

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;

-- 校区 / 食堂
CREATE TABLE IF NOT EXISTS canteens (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    campus  TEXT NOT NULL,          -- 本部 / 东区
    name    TEXT NOT NULL,
    note    TEXT DEFAULT '',
    UNIQUE (campus, name)
);

-- 窗口（一个食堂内的售卖档口）
CREATE TABLE IF NOT EXISTS windows (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    canteen_id INTEGER NOT NULL REFERENCES canteens(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    floor      TEXT DEFAULT '',
    open_hours TEXT DEFAULT '',     -- 例如 06:30-09:00,10:30-13:30
    note       TEXT DEFAULT '',
    UNIQUE (canteen_id, name)
);

-- 菜品：三大营养素与热量按「一份 / 一两」等服务单位给出
CREATE TABLE IF NOT EXISTS dishes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    window_id     INTEGER NOT NULL REFERENCES windows(id) ON DELETE CASCADE,
    category      TEXT NOT NULL DEFAULT '其他',   -- 主食/荤菜/素菜/汤/饮品/小吃/套餐
    price         REAL NOT NULL DEFAULT 0,
    unit          TEXT NOT NULL DEFAULT '份',
    kcal          REAL NOT NULL DEFAULT 0,        -- 千卡
    protein       REAL NOT NULL DEFAULT 0,        -- 克
    fat           REAL NOT NULL DEFAULT 0,        -- 克
    carbs         REAL NOT NULL DEFAULT 0,        -- 克
    is_staple     INTEGER NOT NULL DEFAULT 0,
    is_vegetable  INTEGER NOT NULL DEFAULT 0,
    is_self_service INTEGER NOT NULL DEFAULT 0,   -- 自助/自选类（麻辣烫等）
    tags          TEXT DEFAULT '',                -- 逗号分隔，如 清淡,辣,清真
    note          TEXT DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dish_window ON dishes(window_id);
CREATE INDEX IF NOT EXISTS idx_dish_category ON dishes(category);

-- 用户资料（单机单用户，固定 id=1）
CREATE TABLE IF NOT EXISTS profiles (
    user_id        INTEGER PRIMARY KEY,
    nickname       TEXT NOT NULL DEFAULT '安工大同学',
    gender         TEXT NOT NULL DEFAULT 'male',   -- male / female
    birth_date     TEXT NOT NULL DEFAULT '2004-01-01',
    height_cm      REAL NOT NULL DEFAULT 172,
    weight_kg      REAL NOT NULL DEFAULT 65,
    activity_level TEXT NOT NULL DEFAULT 'light',  -- sedentary/light/moderate/heavy/extreme
    goal           TEXT NOT NULL DEFAULT 'balanced',-- balanced / lose / gain
    meals_per_day  INTEGER NOT NULL DEFAULT 3,
    allergies      TEXT DEFAULT '',                -- 逗号分隔，如 花生,海鲜
    dislikes       TEXT DEFAULT '',
    updated_at     TEXT NOT NULL
);

-- 收藏
CREATE TABLE IF NOT EXISTS favorites (
    user_id    INTEGER NOT NULL DEFAULT 1,
    dish_id    INTEGER NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, dish_id)
);

-- 评论 / 评分（单用户，记录个人对菜品的评价）
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL DEFAULT 1,
    dish_id    INTEGER NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    rating     INTEGER NOT NULL DEFAULT 5,        -- 1-5 星
    content    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comment_dish ON comments(dish_id);

-- 图片：可以挂在菜品 / 窗口 / 食堂上
CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,      -- dish / window / canteen
    owner_id   INTEGER NOT NULL,
    path       TEXT NOT NULL,      -- 相对 media/ 的文件名
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_owner ON images(owner_type, owner_id);

-- 用餐记录：每天按 meals_per_day 生成空栏位，确认后写入内容
CREATE TABLE IF NOT EXISTS meal_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL DEFAULT 1,
    date       TEXT NOT NULL,          -- YYYY-MM-DD
    meal_index INTEGER NOT NULL,       -- 1..meals_per_day
    meal_name  TEXT NOT NULL,          -- 早餐 / 午餐 / 晚餐 / 加餐
    status     TEXT NOT NULL DEFAULT 'empty',  -- empty / confirmed / manual
    plan_id    TEXT DEFAULT '',
    items_json TEXT NOT NULL DEFAULT '[]',
    kcal       REAL NOT NULL DEFAULT 0,
    protein    REAL NOT NULL DEFAULT 0,
    fat        REAL NOT NULL DEFAULT 0,
    carbs      REAL NOT NULL DEFAULT 0,
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, date, meal_index)
);
CREATE INDEX IF NOT EXISTS idx_record_date ON meal_records(date);

-- 推荐批次：同一时段刷新会新增批次，用于「固定时间点刷新」与历史回看
CREATE TABLE IF NOT EXISTS plan_batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL DEFAULT 1,
    date       TEXT NOT NULL,
    slot       TEXT NOT NULL,          -- breakfast / lunch / dinner / snack
    batch_no   INTEGER NOT NULL DEFAULT 1,
    plans_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, date, slot, batch_no)
);

-- 键值设置
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    # 固定刷新时点（本地时间，24h）
    "slot.breakfast.start": "06:30",
    "slot.breakfast.end": "09:30",
    "slot.lunch.start": "10:30",
    "slot.lunch.end": "13:30",
    "slot.dinner.start": "16:30",
    "slot.dinner.end": "20:00",
    # 每个时点自动刷新的间隔（分钟），0 表示不自动轮换
    "plan.auto_rotate_minutes": "0",
    # 推荐数量
    "plan.count": "6",
    "ui.theme": "light",
}


def connect() -> sqlite3.Connection:
    """每个线程一个连接（http.server 是多线程的）。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(MEDIA_DIR, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    # 默认资料与设置
    cur = conn.execute("SELECT COUNT(*) AS c FROM profiles")
    if cur.fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO profiles (user_id, updated_at) VALUES (1, ?)",
            (now(),),
        )
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    conn.commit()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def query(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    conn = connect()
    cur = conn.execute(sql, tuple(params))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    return rows


def query_one(sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    conn = connect()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    rowid = cur.lastrowid
    cur.close()
    return rowid


def setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
