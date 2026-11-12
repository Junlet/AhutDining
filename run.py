"""启动入口：建库 → 灌示例数据 → 起本地服务 → 自动打开浏览器。

用法：
    python run.py            # 默认端口 8848（被占用会自动往后找）
    set AHUT_PORT=9000 && python run.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Windows 控制台默认 GBK，统一改成 UTF-8 并对个别字符兜底，避免中文报错中断启动
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import server  # noqa: E402


def main() -> None:
    print("=" * 56)
    print(" 安工大校园用餐推荐 · 本地版")
    print(" 依据《中国居民膳食指南（2022）》能量供给比")
    print("=" * 56)

    seeded = server.bootstrap_data()
    if seeded.get("skipped"):
        print(f"[数据] 已有 {seeded['dishes']} 道菜品，直接使用现有数据库")
    elif seeded.get("source"):
        print(f"[数据] 已从 {seeded['source']} 导入：新增 {seeded.get('inserted', 0)} 道菜")
    else:
        print(f"[数据] 已写入示例数据：{seeded.get('canteens', 0)} 个食堂 / "
              f"{seeded.get('windows', 0)} 个窗口 / {seeded.get('dishes', 0)} 道菜")

    httpd = server.serve()
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}"
    print(f"[服务] 已启动：{url}")
    print("[提示] 关闭这个窗口即可停止服务（或按 Ctrl+C）")
    print("=" * 56)

    def open_browser() -> None:
        time.sleep(0.8)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if os.environ.get("AHUT_NO_BROWSER") != "1":
        threading.Thread(target=open_browser, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[服务] 已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
