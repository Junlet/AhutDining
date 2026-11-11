"""项目快照备份：把整个工程打成一个 zip 放到 backups/ 下。

用途：改代码 / 改数据前先跑一次，出问题可以整目录回滚。
用法：
    python tools/backup.py                # 备份
    python tools/backup.py --list         # 列出已有备份
    python tools/backup.py --restore 20261111-230000   # 解压某个备份到 restored/<时间戳>/
    python tools/backup.py --tag before-dayplan-fix    # 带备注标签

默认排除：.git、backups、__pycache__、虚拟环境、运行期数据库、用户上传的图片。
（data/dishes_sample.csv、media/.gitkeep 会被保留，因为它们属于工程内容）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

EXCLUDE_DIRS = {".git", "backups", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".db-journal"}
EXCLUDE_REL = {
    os.path.join("media", ".gitkeep"),  # 保留占位，但下面的规则会跳过 media 内容
}
SAVE_EMPTY_DIRS = {"media"}


def _iter_files(base: str):
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel_root = os.path.relpath(root, base)
        for name in files:
            if name.endswith(tuple(EXCLUDE_SUFFIX)):
                continue
            rel = os.path.normpath(os.path.join(rel_root, name)) if rel_root != "." else name
            if rel_root.split(os.sep)[0] == "media" and name != ".gitkeep":
                continue  # 用户上传的图片不进备份
            yield rel, os.path.join(root, name)


def make_backup(tag: str = "") -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"AhutDining-{stamp}{'-' + tag if tag else ''}.zip"
    path = os.path.join(BACKUP_DIR, name)
    files = list(_iter_files(BASE_DIR))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, full in files:
            zf.write(full, os.path.join("AhutDining", rel))
        for empty in SAVE_EMPTY_DIRS:
            target = os.path.join(BASE_DIR, empty)
            if os.path.isdir(target) and not any(
                    f.startswith(empty + os.sep) for f, _ in files):
                zf.writestr(os.path.join("AhutDining", empty) + "/", "")
    size = os.path.getsize(path)
    print(f"[备份] {name}  ({len(files)} 个文件, {size / 1024:.0f} KB) -> {path}")
    return path


def list_backups() -> None:
    if not os.path.isdir(BACKUP_DIR):
        print("还没有任何备份")
        return
    items = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".zip"))
    if not items:
        print("还没有任何备份")
        return
    for name in items:
        full = os.path.join(BACKUP_DIR, name)
        kb = os.path.getsize(full) / 1024
        print(f"{name}  {kb:8.0f} KB")


def restore(key: str) -> str:
    if not os.path.isdir(BACKUP_DIR):
        raise SystemExit("还没有任何备份")
    matches = [f for f in sorted(os.listdir(BACKUP_DIR))
               if f.endswith(".zip") and key in f]
    if not matches:
        raise SystemExit(f"没有匹配 '{key}' 的备份")
    src = os.path.join(BACKUP_DIR, matches[-1])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(BASE_DIR, "restored", stamp)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(src) as zf:
        zf.extractall(out)
    print(f"[还原] {matches[-1]} -> {out}")
    print("       对照后把需要的文件拷回工程目录即可（不会自动覆盖当前代码）")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="AhutDining 项目快照备份")
    parser.add_argument("--tag", default="", help="备份标签，例如 before-dayplan-fix")
    parser.add_argument("--list", action="store_true", help="列出已有备份")
    parser.add_argument("--restore", metavar="KEY", help="解压匹配 KEY 的备份到 restored/")
    args = parser.parse_args()

    if args.list:
        list_backups()
        return
    if args.restore:
        restore(args.restore)
        return
    make_backup(args.tag)


if __name__ == "__main__":
    main()
