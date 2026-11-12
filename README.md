# 安工大校园用餐推荐（AhutDining）

面向安徽工业大学的本地校园用餐推荐程序：根据你的身高体重、年龄性别和活动强度算出每日能量与
三大营养素目标，再按《中国居民膳食指南（2022）》的供能比从食堂菜品里组合出可执行的套餐，
并记录你每天实际吃了什么。

**零依赖**：只用 Python 标准库（`http.server` + `sqlite3`），不需要 pip 装任何包，双击 `start.bat` 就能用。

## 快速开始

```bat
:: 方式一：双击
start.bat

:: 方式二：命令行
python run.py
```

浏览器会自动打开 <http://127.0.0.1:8848>（端口被占用会自动往后找 8849/8850/8851/8000）。

要求：Python 3.9+（已在 Python 3.14 上验证）。首次启动会自动建库并写入 61 道示例菜品。

## 功能一览

| 模块 | 做了什么 |
|------|----------|
| 个人资料 | 昵称、性别、出生日期、身高、体重、目标（保持/减脂/增肌）、忌口 |
| 活动系数 | 久坐不动 1.2 / 轻度 1.375 / 中度 1.55 / 高度 1.725 / 极高 1.9 |
| 能量计算 | Mifflin-St Jeor BMR × 活动系数 = TDEE，再乘目标系数 |
| 营养目标 | 按 AMDR 拆成蛋白/脂肪/碳水克数，并给出每餐拆分（默认三餐 30/40/30） |
| 菜品评分 | 每道菜单独算蛋白质分、脂肪分、碳水（碳水占比）分 + 综合营养分 |
| 套餐推荐 | 各食堂枚举「主食+荤+素(+汤)」组合 → 按热量/蛋白/脂肪/碳水/多样性/口味/价格打分 → 一次 6 套，可无限换批 |
| 推荐模式（设置里切换） | **单道菜**：只推荐当前这一餐的套餐；**一天方案**：按当前时间 + 用餐记录算出还剩几餐，给出 A/B/C 三套整日方案，选定后进入「今日」 |
| 今日（计划与记录合并） | 一个页面同时看今日计划与当天用餐记录；每餐都有**手动选择**（多选菜品）：**手选直接替换该餐内容**（时段已过会同步用餐记录），早/午/晚都可手选，全部拼好后可「设为今日计划」；卡片右上角 ↻ 单餐刷新，另有固定/自动补齐/确认，下方是当日合计与历史日期回溯 |
| 用餐记录 | 确认推荐即记录；忘记确认可事后手动补记；栏位数随「每天几餐」变化 |
| 自选评估 | 自助类（麻辣烫、称重快餐）自己勾选，给出推荐度与改进建议 |
| 搜索 / 收藏 / 评论 | 按菜名、标签、食堂、分类搜索；收藏；对菜品打星+写评论（可删） |
| 图片 | 为菜品上传实拍图，搜索与推荐卡片中显示缩略图 |
| 定时刷新 | 设置页可改早/午/晚时段，跨时段时首页推荐自动重新生成 |
| 比价 | 菜品详情显示同名菜在不同食堂窗口的价格（参考 PocketAHUT 的比价思路） |

## 目录结构

```
AhutDining/
├── start.bat                 # 双击启动
├── run.py                    # 启动入口（建库 → 灌示例数据 → 起服务 → 开浏览器）
├── app/
│   ├── db.py                 # SQLite 连接 + 表结构 + 默认设置
│   ├── nutrition.py          # BMR/TDEE/AMDR/菜品营养素分数/建议文案
│   ├── recommender.py        # 套餐枚举打分、批次刷新、自助评估、时段判断
│   ├── seed_data.py          # 示例食堂/窗口/菜品（可被 CSV 覆盖）
│   ├── server.py             # 零依赖 HTTP 服务 + 全部 REST 接口
│   └── static/               # 前端：index.html / app.js / style.css（原生 JS）
├── data/
│   ├── dining.db             # 运行期数据库（gitignore）
│   └── dishes_sample.csv     # 示例菜品 CSV（可当导入模板）
├── media/                    # 用户上传的菜品图片（gitignore）
├── tools/
│   ├── import_dishes.py      # CSV 导入 / 导出
│   └── backup.py             # 项目快照备份 / 回滚
├── tests/
│   ├── smoke_test.py         # 22 项接口冒烟测试
│   ├── contract_test.js      # 前端数据契约校验（Node）
│   └── dom_smoke.js          # 前端渲染冒烟（Node + 极简 DOM 桩，防白屏）
└── docs/
    ├── 需求对照.md           # 13 条需求 → 实现位置 → 验收方式
    └── 数据格式.md           # CSV 字段与替换真实菜单的方法
```

## 营养学口径

* **依据**：《中国居民膳食指南（2022）》能量供给比（AMDR）——碳水 50–65%、脂肪 20–30%、蛋白质 10–20% 供能比；
  蛋白质同时参考成人推荐摄入量（男 65 g/d、女 55 g/d）。
* **能量换算**：碳水 4 kcal/g、蛋白质 4 kcal/g、脂肪 9 kcal/g。
* **BMR**：Mifflin-St Jeor（男 `10w+6.25h-5a+5`，女 `10w+6.25h-5a-161`）。
* **每日目标**：TDEE × 目标系数（保持 1.0、减脂 0.85、增肌 1.10）；
  蛋白质先取 1.2 g/kg 并夹到 AMDR 区间，脂肪取 25%E，碳水吃掉剩余能量，越界时在区间内自动微调。
* **评分**：区间内满分、越界线性衰减（见 `nutrition.band_score`），因此"略超"不会一刀切成 0 分。

> 说明：官方目前最新版即 2022 版。"2025"若指某份具体文件/比例，把数值给我可直接替换 `app/nutrition.py::AMDR`。

## 数据说明（重要）

工作区里的 **PocketAHUT 只有文档、没有菜品数据**（数据在其闭源 App 后端），因此本项目的：
* 食堂/窗口结构参考其 `docs/features/canteen.md`（本部+东区、窗口维度、比价、窗口评分）；
* **菜品数值为示例估算**（61 道常见菜），用于跑通整条链路。

替换成真实菜单只需一条命令：

```bash
python tools/import_dishes.py --export data/dishes.csv      # 导出模板
python tools/import_dishes.py --file 真实菜单.csv --replace   # 导入真实数据
```

详见 `docs/数据格式.md`。

## 接口一览

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/api/bootstrap` | 首屏数据（资料、指标、当前时段、记录、筛选项） |
| GET/PUT | `/api/profile` | 读取/保存个人资料 |
| GET | `/api/plans?slot=lunch` | 取某时段推荐批次 |
| POST | `/api/plans/refresh` | 换一批推荐 |
| POST | `/api/plans/evaluate` | 自选菜品评分（推荐度） |
| GET | `/api/day-plan?include_passed=0/1` | 今日用餐计划（剩余餐次 + 整日方案） |
| POST | `/api/day-plan/refresh` | 换一批 A/B/C（未选时）或刷新未固定的餐（可传 `only_meal`） |
| POST | `/api/day-plan/choose` | 选定 A/B/C 中的一套作为今日计划 |
| POST | `/api/day-plan/set-meal` | 用自选菜替换某一餐并重算推荐度 |
| POST | `/api/day-plan/complete-meal` | 给自选的一餐自动补齐 |
| POST | `/api/day-plan/pin` | 固定 / 取消固定某一餐 |
| POST | `/api/day-plan/reset` | 重置今日计划（清固定项） |
| GET | `/api/dishes?q=&canteen=&category=&sort=` | 搜索/筛选菜品 |
| GET | `/api/dishes/{id}` | 菜品详情（评分、评论、图片、比价） |
| POST | `/api/comments`、DELETE `/api/comments/{id}` | 评论增删 |
| POST | `/api/favorites/{id}`、GET `/api/favorites` | 收藏开关与列表 |
| POST | `/api/images` | 上传图片（base64 JSON） |
| GET | `/api/records?date=` | 某天用餐记录（缺栏位自动补） |
| POST | `/api/records/confirm` | 确认/补记一餐 |
| DELETE | `/api/records/{date}/{idx}` | 清空某餐 |
| GET | `/api/stats`、`/api/slot` | 今日统计、当前时段 |
| PUT | `/api/settings` | 刷新时间点等设置 |

## 测试

```bash
python run.py                  # 先启动服务
python tests/smoke_test.py     # 22 项接口测试（含 9 项今日计划）
node tests/contract_test.js    # 8 项前端字段契约（需 Node）
node tests/dom_smoke.js        # 11 项前端渲染冒烟（需 Node，防白屏/标签失效/函数缺失）
```

## 备份与回滚

每次改代码/改数据前后都可以打个快照（zip 存在 `backups/`，已 gitignore）：

```bash
python tools/backup.py --tag before-xxx     # 改之前
python tools/backup.py --tag after-xxx      # 改完验证通过后
python tools/backup.py --list               # 列出所有备份
python tools/backup.py --restore 20261111-225418   # 解压到 restored/<时间戳>/ 对照
```

备份内容包含代码、文档、示例数据 CSV，**不包含** `.git`、运行期数据库 `data/dining.db`、
`media/` 里用户上传的图片（这些体积大且可重建/应自行保留）。
`--restore` 只解压到 `restored/` 供对照，不会覆盖当前代码，确认后手动拷回即可。

## 上传到 GitHub

本地仓库已经分阶段提交好（每个模块一个 commit），推送前只需：

```bash
# 1) 在 GitHub 上新建空仓库（不要勾选初始化 README）
# 2) 关联并推送
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

> 提示：`data/dining.db` 和 `media/` 里的图片不会进版本库（已 gitignore），换机器时需自行拷贝。
> 如果你的 git 是 Windows Schannel 后端且推送报 TLS 错误，可用
> `git config --http.sslBackend openssl`（仅本仓库生效）。

## 已知限制

1. 菜品数据为示例，需替换成真实菜单（结构已就绪，一条命令即可）。
2. 每份菜的营养素是估算值，"一份"的实际克数会带来误差，误差主要来自主食与油量。
3. 单机单用户免登录（表结构已保留 `user_id`，后续加账号成本很低）。
4. 定时刷新采用"前端轮询 + 跨时段重建"的方式，不是常驻后台任务。
5. 营养评分是相对参考，不构成医疗或营养处方建议。
