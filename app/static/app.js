/* 安工大校园用餐推荐 —— 前端逻辑（原生 JS，无构建步骤） */
const API = '';
const state = {
  boot: null,
  slot: 'lunch',
  plans: [],
  batch: 1,
  date: '',
  recordDate: '',
  customSelected: new Map(), // dish_id -> dish
  customSlot: 'lunch',
  lastSlotKey: '',
};

const ACTIVITY = [
  ['sedentary', '久坐不动', '几乎不运动'],
  ['light', '轻度活动', '每周 1–3 次'],
  ['moderate', '中度活动', '每周 3–5 次'],
  ['heavy', '高度活动', '每周 6–7 次'],
  ['extreme', '极高强度', '体力劳动/每天两练'],
];

/* ── 基础工具 ───────────────────────────────────────────────── */
async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const json = await res.json();
  if (!json.ok) throw new Error(json.error || '请求失败');
  return json.data !== undefined ? json.data : json;
}

function el(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html.trim();
  return tpl.content.firstElementChild;
}

function toast(text) {
  const node = document.getElementById('toast');
  node.textContent = text;
  node.classList.add('show');
  clearTimeout(node._timer);
  node._timer = setTimeout(() => node.classList.remove('show'), 2200);
}

function fmt(n, digits = 0) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return '—';
  return Number(n).toFixed(digits).replace(/\.0+$/, '');
}

function pct(n) { return `${fmt(n, 0)}%`; }

function stars(n) {
  const full = Math.round(n || 0);
  return '★'.repeat(Math.max(0, Math.min(5, full))) + '☆'.repeat(Math.max(0, 5 - full));
}

function scoreClass(score) {
  if (score >= 0.75) return 'badge';
  if (score >= 0.55) return 'badge amber';
  return 'badge grey';
}

function adviceClass(plan) {
  const s = plan.scores.total;
  if (s >= 0.72) return 'advice';
  if (s >= 0.5) return 'advice warn';
  return 'advice bad';
}

function thumb(item) {
  if (item.image) return `<img class="dish-thumb" src="/media/${item.image}" alt="">`;
  return `<div class="dish-thumb placeholder">图</div>`;
}

function macroBars(pctObj) {
  return `
    <div class="bar protein"><i style="width:${Math.min(100, pctObj.protein || 0)}%"></i></div>
    <div class="bar fat"><i style="width:${Math.min(100, pctObj.fat || 0)}%"></i></div>
    <div class="bar carbs"><i style="width:${Math.min(100, pctObj.carbs || 0)}%"></i></div>`;
}

/* ── 初始化 ─────────────────────────────────────────────────── */
async function init() {
  bindTabs();
  document.getElementById('btn-refresh').addEventListener('click', refreshPlans);
  document.getElementById('btn-day-refresh').addEventListener('click', refreshDayPlan);
  document.getElementById('btn-day-reselect').addEventListener('click', reselectDayPlan);
  document.getElementById('day-include-passed').addEventListener('change', loadDayPlan);
  document.getElementById('btn-search').addEventListener('click', runSearch);
  document.getElementById('search-q').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') runSearch();
  });
  document.getElementById('btn-evaluate').addEventListener('click', evaluateCustom);
  document.getElementById('custom-q').addEventListener('input', renderPickList);
  document.getElementById('btn-record-today').addEventListener('click', () => {
    state.recordDate = state.date;
    document.getElementById('record-date').value = state.date;
    loadRecords();
  });
  document.getElementById('record-date').addEventListener('change', (e) => {
    state.recordDate = e.target.value;
    loadRecords();
  });
  document.getElementById('profile-form').addEventListener('submit', saveProfile);
  document.getElementById('btn-save-slots').addEventListener('click', saveSlots);
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal').addEventListener('click', (e) => {
    if (e.target.id === 'modal') closeModal();
  });

  await loadBootstrap();
  await loadPlans();
  setInterval(checkSlot, 30000);
}

async function loadBootstrap() {
  const data = await api('/api/bootstrap');
  state.boot = data;
  state.date = data.date;
  state.slot = data.slot.slot;
  state.recordDate = data.date;
  renderTodaybar();
  renderSlotSelect();
  renderTargets();
  renderProfileForm();
  renderMetrics();
  renderSlotSettings();
  renderFilters();
  document.getElementById('record-date').value = data.date;
  await loadDayPlan();
  await loadRecords();
  await loadFavorites();
  await renderPickList();
}

function renderTodaybar() {
  const { consumed, metrics } = state.boot;
  const daily = metrics.daily;
  const ratio = daily.kcal ? consumed.kcal / daily.kcal : 0;
  const cls = ratio > 1.05 ? 'progress over' : ratio > 0.9 ? 'progress warn' : 'progress';
  document.getElementById('todaybar').innerHTML = `
    <span>今日目标 <strong>${fmt(daily.kcal)} kcal</strong></span>
    <span>已记录 <strong>${fmt(consumed.kcal)} kcal</strong></span>
    <div class="${cls}" style="width:180px"><i style="width:${Math.min(100, ratio * 100)}%"></i></div>
    <span>蛋白质 <strong>${fmt(consumed.protein, 1)}</strong> / ${fmt(daily.protein, 1)} g</span>
    <span>脂肪 <strong>${fmt(consumed.fat, 1)}</strong> / ${fmt(daily.fat, 1)} g</span>
    <span>碳水 <strong>${fmt(consumed.carbs, 1)}</strong> / ${fmt(daily.carbs, 1)} g</span>
    <span class="muted">数据为示例菜品库（${state.boot.datasets.dishes} 道），可用 tools/import_dishes.py 导入真实数据</span>`;
}

function renderSlotSelect() {
  const slots = [
    ['breakfast', '早餐'], ['lunch', '午餐'], ['dinner', '晚餐'], ['snack', '加餐'],
  ];
  const sel = document.getElementById('home-slot-select');
  sel.innerHTML = slots.map(([v, l]) => `<option value="${v}">${l}</option>`).join('');
  sel.value = state.slot;
  sel.onchange = async () => {
    state.slot = sel.value;
    state.batch = 1;
    await loadPlans();
  };
  const info = state.boot.slot;
  document.getElementById('home-slot').textContent =
    `当前时段：${info.label}（${info.window}）${info.hint ? ' · ' + info.hint : ''}`;
}

function renderTargets() {
  const metrics = state.boot.metrics;
  const daily = metrics.daily;
  document.getElementById('home-targets').innerHTML = `
    <div class="target"><div class="label">每日目标热量</div>
      <div class="value">${fmt(daily.kcal)} <small>kcal</small></div>
      <div class="muted">BMR ${fmt(metrics.bmr)} × ${metrics.activity_factor} = ${fmt(metrics.tdee)} kcal（${metrics.goal_label}）</div></div>
    <div class="target"><div class="label">蛋白质</div>
      <div class="value">${fmt(daily.protein, 1)} <small>g</small></div>
      <div class="muted">占能量 ${pct(daily.shares.protein * 100)}（指南 10–20%）· ${daily.protein_per_kg} g/kg</div>
      ${macroBars({ protein: daily.shares.protein * 100 })}</div>
    <div class="target"><div class="label">脂肪</div>
      <div class="value">${fmt(daily.fat, 1)} <small>g</small></div>
      <div class="muted">占能量 ${pct(daily.shares.fat * 100)}（指南 20–30%）</div></div>
    <div class="target"><div class="label">碳水化合物</div>
      <div class="value">${fmt(daily.carbs, 1)} <small>g</small></div>
      <div class="muted">占能量 ${pct(daily.shares.carbs * 100)}（指南 50–65%）</div></div>`;
}

/* ── 推荐 ───────────────────────────────────────────────────── */
async function loadPlans() {
  const data = await api(`/api/plans?slot=${state.slot}`);
  state.plans = data.plans;
  state.batch = data.batch;
  renderPlans();
}

async function refreshPlans() {
  const btn = document.getElementById('btn-refresh');
  btn.disabled = true;
  btn.textContent = '刷新中…';
  try {
    const data = await api('/api/plans/refresh', {
      method: 'POST',
      body: JSON.stringify({ slot: state.slot }),
    });
    state.plans = data.plans;
    state.batch = data.batch;
    renderPlans();
    toast(`已换一批推荐（第 ${data.batch} 批）`);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '换一批推荐';
  }
}

function renderPlans() {
  const list = document.getElementById('plan-list');
  if (!state.plans.length) {
    list.innerHTML = `<div class="placeholder-text">这个时段暂时凑不出符合条件的套餐，试试切换餐次或补充菜品数据。</div>`;
    return;
  }
  list.innerHTML = '';
  state.plans.forEach((plan) => list.appendChild(planCard(plan)));
}

function planCard(plan) {
  const target = plan.target;
  const card = el(`
    <div class="card">
      <div class="card-head">
        <div>
          <div class="name">${plan.canteen.name}</div>
          <div class="dish-meta">${plan.canteen.campus} · ${plan.meal_name}目标 ${fmt(target.kcal)} kcal</div>
        </div>
        <div style="text-align:right">
          <div class="score">${(plan.scores.total * 100).toFixed(0)}<small> 分</small></div>
          <span class="${scoreClass(plan.scores.total)}">${plan.scores.total >= 0.75 ? '很合适' : plan.scores.total >= 0.55 ? '还不错' : '勉强'}</span>
        </div>
      </div>
      <div class="dish-list"></div>
      <div class="macro-line">
        <span><b>${fmt(plan.totals.kcal)}</b> kcal</span>
        <span>蛋白 <b>${fmt(plan.totals.protein, 1)}</b>g</span>
        <span>脂肪 <b>${fmt(plan.totals.fat, 1)}</b>g</span>
        <span>碳水 <b>${fmt(plan.totals.carbs, 1)}</b>g</span>
        <span><b>¥${fmt(plan.totals.price, 1)}</b></span>
      </div>
      <div class="macro-line" style="gap:14px">
        <span class="badge">蛋白 ${pct(plan.macro_pct.protein)}</span>
        <span class="badge amber">脂肪 ${pct(plan.macro_pct.fat)}</span>
        <span class="badge grey">碳水 ${pct(plan.macro_pct.carbs)}</span>
      </div>
      <div class="${adviceClass(plan)}">${plan.advice.slice(0, 2).join('；')}</div>
      <div class="card-actions">
        <button class="btn primary grow" data-act="confirm">确认这餐</button>
        <button class="btn" data-act="evaluate">单独评估</button>
      </div>
    </div>`);

  const dishList = card.querySelector('.dish-list');
  plan.items.forEach((item) => {
    const row = el(`
      <div class="dish-row">
        ${thumb(item)}
        <div class="grow">
          <div class="dish-name">${item.name}</div>
          <div class="dish-meta">${item.window} · ¥${fmt(item.price, 1)} · ${fmt(item.kcal)} kcal
            · 蛋白${fmt(item.protein, 1)} 脂肪${fmt(item.fat, 1)} 碳水${fmt(item.carbs, 1)}</div>
        </div>
        <div class="dish-meta" style="text-align:right">
          营养分<br><b>${(item.scores.total * 100).toFixed(0)}</b>
        </div>
        <button class="btn small" data-pick="1" title="用这道菜替换今日计划里的某一餐">换入</button>
      </div>`);
    row.addEventListener('click', () => openDish(item.dish_id));
    row.querySelector('[data-pick]').addEventListener('click', (e) => {
      e.stopPropagation();
      pickMealForDish(item.dish_id);
    });
    dishList.appendChild(row);
  });

  card.querySelector('[data-act="confirm"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    await confirmPlan(plan);
  });
  card.querySelector('[data-act="evaluate"]').addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const data = await api('/api/plans/evaluate', {
        method: 'POST',
        body: JSON.stringify({
          dish_ids: plan.items.map((i) => i.dish_id),
          slot: plan.slot,
        }),
      });
      showEvaluation(data, '#modal-body');
      document.getElementById('modal').classList.add('open');
    } catch (err) { toast(err.message); }
  });
  return card;
}

async function confirmPlan(plan) {
  const mealIndex = plan.target.meal_index;
  try {
    const data = await api('/api/records/confirm', {
      method: 'POST',
      body: JSON.stringify({
        date: state.date,
        meal_index: mealIndex,
        plan_id: plan.id,
        items: plan.items.map((i) => ({ dish_id: i.dish_id })),
        status: 'confirmed',
      }),
    });
    toast(`已记录${plan.meal_name}：${plan.items.map((i) => i.name).join(' + ')}`);
    await refreshAfterConfirm(data);
  } catch (err) {
    toast(err.message);
  }
}

async function refreshAfterConfirm(data) {
  const boot = await api('/api/bootstrap');
  state.boot = boot;
  renderTodaybar();
  renderMetrics();
  await loadDayPlan();
  await loadRecords();
  // 已确认这一餐后，自动把首页切到下一餐的推荐
  if (data && data.next_meal) {
    const nextSlot = data.next_meal.slot || state.slot;
    if (nextSlot !== state.slot) {
      state.slot = nextSlot;
      document.getElementById('home-slot-select').value = nextSlot;
    }
    await loadPlans();
    toast(`下一餐已按剩余额度重新计算（${data.next_meal.meal_name}）`);
  }
}

/* ── 今日用餐计划（方案 A/B/C → 选定 → 单餐替换） ─────────────── */
const DAY_STATUS_TEXT = { planned: '待安排', recorded: '已记录', missed: '已错过' };

function levelClass(level) {
  if (level === '强烈推荐' || level === '推荐') return 'level-badge level-good';
  if (level === '一般') return 'level-badge level-mid';
  return 'level-badge level-bad';
}

async function loadDayPlan() {
  const includePassed = document.getElementById('day-include-passed').checked;
  const data = await api(`/api/day-plan?include_passed=${includePassed ? 1 : 0}`);
  state.day = data;
  renderDayState(data);
}

function renderDayState(data) {
  state.day = data;
  const chosen = data.stage === 'chosen' && data.plan;
  const plan = chosen ? data.plan : null;
  const opts = data.options || [];
  const includePassed = !!data.include_passed;

  document.getElementById('day-include-passed').checked = includePassed;
  document.getElementById('btn-day-reselect').classList.toggle('hidden', !chosen);
  document.getElementById('day-options-title').classList.toggle('hidden', chosen);
  document.getElementById('btn-day-refresh').textContent = chosen
    ? '刷新未固定的餐' : '换一批方案';

  const hint = document.getElementById('day-hint');
  const nowMeals = chosen ? plan.summary : (opts[0] ? opts[0].summary : null);
  let text = `现在是 ${data.now}。`;
  if (chosen) {
    text += `已选定方案 ${plan.label || ''}：共 ${plan.summary.meals_total} 餐，`
      + `已记录 ${plan.summary.meals_recorded} 餐、待安排 ${plan.summary.meals_remaining} 餐`
      + (plan.summary.meals_missed ? `、已错过 ${plan.summary.meals_missed} 餐` : '')
      + '。可单独刷新某一餐，或用「选为这一餐」把自选菜换进计划。';
  } else if (opts.length) {
    const s = opts[0].summary;
    text += `今天还剩 ${s.meals_remaining} 餐待安排（共 ${s.meals_total} 餐`
      + (s.meals_missed ? `，${s.meals_missed} 餐已错过` : '') + '），'
      + '下面是 A / B / C 三套整日方案，选一套后即为今日计划。';
  } else {
    text += '这个时间点没有可安排的餐次了。';
  }
  if (data.auto_include_passed) text += '（所有时段都已过，已自动把全天重新排上）';
  if (data.suggest_include_passed) text += '';
  hint.textContent = text;
  if (data.suggest_include_passed) {
    const cta = el('<button class="btn small primary" style="margin-left:8px">把已过时段也安排上</button>');
    cta.addEventListener('click', () => {
      document.getElementById('day-include-passed').checked = true;
      loadDayPlan();
    });
    hint.appendChild(cta);
  }

  renderDaySummary(data, plan, opts);
  renderDayOptions(opts, chosen);

  const mealsBox = document.getElementById('day-meals');
  mealsBox.innerHTML = '';
  if (chosen) plan.meals.forEach((meal) => mealsBox.appendChild(dayMealCard(meal, plan)));
}

function renderDaySummary(data, plan, opts) {
  const box = document.getElementById('day-summary');
  if (data.stage !== 'chosen' || !plan) {
    box.innerHTML = opts.length ? `
      <div class="target"><div class="label">待选方案</div>
        <div class="value">${opts.length} <small>套</small></div>
        <div class="muted">每套都按你的每日目标配平</div></div>
      <div class="target"><div class="label">每日目标热量</div>
        <div class="value">${fmt(opts[0].daily.kcal)} <small>kcal</small></div></div>
      <div class="target"><div class="label">每日蛋白质</div>
        <div class="value">${fmt(opts[0].daily.protein, 1)} <small>g</small></div></div>
      <div class="target"><div class="label">今日已记录</div>
        <div class="value">${fmt(opts[0].consumed.kcal)} <small>kcal</small></div></div>`
      : `<div class="target"><div class="label">没有可安排的餐次</div>
         <div class="value">—</div>
         <div class="muted">可以到「用餐记录」补记今天吃过的</div></div>`;
    return;
  }
  const s = plan.summary;
  const daily = plan.daily;
  box.innerHTML = `
    <div class="target"><div class="label">整日计划覆盖</div>
      <div class="value">${fmt(s.plan_kcal)} <small>/ ${fmt(daily.kcal)} kcal</small></div>
      <div class="bar"><i style="width:${Math.min(100, (s.coverage || 0) * 100)}%"></i></div>
      <div class="muted">还差 ${fmt(s.gap_kcal)} kcal（${pct((s.coverage || 0) * 100)}）</div></div>
    <div class="target"><div class="label">整日推荐度</div>
      <div class="value">${(plan.avg_score * 100).toFixed(0)} <small>分</small></div>
      <div class="muted"><span class="${levelClass(plan.level)}">${plan.level}</span></div></div>
    <div class="target"><div class="label">计划蛋白质 / 脂肪</div>
      <div class="value">${fmt(s.plan_protein, 1)} <small>g</small> · ${fmt(s.plan_fat, 1)} <small>g</small></div>
      <div class="muted">目标 ${fmt(daily.protein, 1)}g / ${fmt(daily.fat, 1)}g</div></div>
    <div class="target"><div class="label">预计花费</div>
      <div class="value">¥${fmt(s.plan_price, 1)}</div>
      <div class="muted">已固定 ${plan.meals.filter((m) => m.pinned).length} 餐 · 方案 ${plan.label || ''}</div></div>`;
}

function renderDayOptions(options, chosen) {
  const box = document.getElementById('day-options');
  box.innerHTML = '';
  if (chosen || !options.length) return;
  options.forEach((opt, index) => {
    const card = el(`
      <div class="card option-card">
        <div class="card-head">
          <div>
            <div class="name">方案 ${opt.label}</div>
            <div class="dish-meta">${fmt(opt.summary.plan_kcal)} / ${fmt(opt.daily.kcal)} kcal · ¥${fmt(opt.summary.plan_price, 1)}</div>
          </div>
          <div style="text-align:right">
            <div class="score">${(opt.avg_score * 100).toFixed(0)}<small> 分</small></div>
            <span class="${levelClass(opt.level)}">${opt.level}</span>
          </div>
        </div>
        <div class="option-meals"></div>
        <div class="card-actions">
          <button class="btn primary grow">选择这个方案</button>
        </div>
      </div>`);
    const mealsBox = card.querySelector('.option-meals');
    opt.meals.forEach((meal) => {
      const items = meal.items.length ? meal.items.map((i) => i.name).join(' + ') : '—';
      const status = meal.status === 'missed' ? '（已错过）'
        : meal.status === 'recorded' ? '（已记录）' : '';
      mealsBox.appendChild(el(`
        <div class="option-meal">
          <div class="option-meal-name">${meal.meal_name} ${status}
            <span class="dish-meta">· ${fmt(meal.totals.kcal)} kcal · ${fmt(meal.scores.total * 100).toFixed(0)} 分</span>
          </div>
          <div class="option-meal-items">${items}</div>
        </div>`));
    });
    const btn = card.querySelector('button');
    btn.addEventListener('click', () => chooseDayOption(index));
    box.appendChild(card);
  });
}

async function chooseDayOption(index) {
  try {
    const data = await api('/api/day-plan/choose', {
      method: 'POST',
      body: JSON.stringify({ index }),
    });
    renderDayState(data);
    toast(`已选定方案 ${data.plan.label}，其它方案已收起`);
  } catch (err) { toast(err.message); }
}

function dayMealCard(meal, plan) {
  let cls = 'record';
  if (meal.status === 'missed') cls += ' empty missed';
  if (meal.status === 'planned' && !meal.items.length) cls += ' empty';
  if (meal.pinned) cls += ' pinned';

  const sourceBadge = meal.source === 'manual'
    ? '<span class="badge manual">我的选择</span>'
    : meal.source === 'pinned' ? '<span class="badge">固定</span>' : '';

  const card = el(`
    <div class="${cls}">
      <h3>
        <span>${meal.meal_name}
          <span class="meal-window">${meal.window}</span></span>
        <span class="row gap" style="gap:6px">
          ${sourceBadge}
          <span class="${meal.status === 'planned' ? 'badge amber' : meal.status === 'recorded' ? 'badge' : 'badge grey'}">
            ${DAY_STATUS_TEXT[meal.status] || meal.status}</span>
        </span>
      </h3>
      <div class="dish-meta">
        ${meal.status === 'planned'
          ? `本餐目标 ${fmt(meal.target.kcal)} kcal · 蛋白 ${fmt(meal.target.protein, 1)}g`
          : meal.status === 'recorded'
            ? `实际摄入 ${fmt(meal.totals.kcal)} kcal`
            : '已过时段且没有记录'}
      </div>
      <div class="dish-list"></div>
      ${meal.status === 'planned' && meal.items.length ? `
        <div class="macro-line">
          <span><b>${fmt(meal.totals.kcal)}</b> kcal</span>
          <span>蛋白 <b>${fmt(meal.totals.protein, 1)}</b>g</span>
          <span>脂肪 <b>${fmt(meal.totals.fat, 1)}</b>g</span>
          <span>碳水 <b>${fmt(meal.totals.carbs, 1)}</b>g</span>
          <span><b>¥${fmt(meal.totals.price, 1)}</b></span>
        </div>
        <div class="macro-line" style="gap:12px;align-items:center">
          <span class="${levelClass(meal.level)}">推荐度 ${(meal.scores.total * 100).toFixed(0)} · ${meal.level}</span>
          <span class="badge">蛋白 ${pct(meal.macro_pct.protein)}</span>
          <span class="badge amber">脂肪 ${pct(meal.macro_pct.fat)}</span>
          <span class="badge grey">碳水 ${pct(meal.macro_pct.carbs)}</span>
        </div>
        <div class="${meal.scores.total >= 0.72 ? 'advice' : 'advice warn'}">
          ${(meal.advice || []).slice(0, 2).join('；')}
        </div>` : ''}
      <div class="card-actions"></div>
    </div>`);

  const list = card.querySelector('.dish-list');
  if (meal.items && meal.items.length) {
    meal.items.forEach((item) => {
      const row = el(`<div class="dish-row">
        <div class="grow">
          <div class="dish-name">${item.name}</div>
          <div class="dish-meta">${item.canteen || ''} · ${item.window || ''} · ¥${fmt(item.price, 1)} · ${fmt(item.kcal)} kcal</div>
        </div>
        <div class="dish-meta">蛋白 ${fmt(item.protein, 1)}g</div>
      </div>`);
      row.addEventListener('click', () => openDish(item.dish_id));
      list.appendChild(row);
    });
  } else if (meal.status === 'missed') {
    list.innerHTML = `<div class="dish-meta" style="padding:6px 0">这一餐已经过了时段，没有记录。可到「用餐记录」补记，或勾选上方「包含已过时段」重新安排。</div>`;
  } else if (meal.status === 'planned') {
    list.innerHTML = `<div class="dish-meta" style="padding:6px 0">这个时段暂时没有凑出合适组合。</div>`;
  }

  const actions = card.querySelector('.card-actions');
  if (meal.status === 'planned' && meal.items.length) {
    const pinBtn = el(`<button class="btn ${meal.pinned ? '' : 'ghost'}">${meal.pinned ? '取消固定' : '固定这餐'}</button>`);
    pinBtn.addEventListener('click', () => toggleDayPin(meal.meal_index, !meal.pinned));
    actions.appendChild(pinBtn);

    const swapBtn = el('<button class="btn">刷新这一餐</button>');
    swapBtn.addEventListener('click', () => refreshOnlyMeal(meal.meal_index));
    actions.appendChild(swapBtn);

    if (meal.source === 'manual') {
      const fillBtn = el('<button class="btn">自动补齐</button>');
      fillBtn.addEventListener('click', () => completeDayMeal(meal.meal_index));
      actions.appendChild(fillBtn);
    }

    const okBtn = el('<button class="btn primary grow">确认这餐</button>');
    okBtn.addEventListener('click', () => confirmDayMeal(meal, plan));
    actions.appendChild(okBtn);
  } else if (meal.status === 'recorded') {
    actions.appendChild(el('<span class="muted">这一餐已计入今日摄入，可在「用餐记录」里修改</span>'));
    const goBtn = el('<button class="btn">去记录页</button>');
    goBtn.addEventListener('click', () => switchTab('records'));
    actions.appendChild(goBtn);
  }
  return card;
}

async function toggleDayPin(mealIndex, pinned) {
  try {
    const data = await api('/api/day-plan/pin', {
      method: 'POST',
      body: JSON.stringify({ meal_index: mealIndex, pinned }),
    });
    renderDayState(data);
    toast(pinned ? '已固定这一餐，刷新不会改动它' : '已取消固定');
  } catch (err) { toast(err.message); }
}

async function refreshOnlyMeal(mealIndex) {
  try {
    const data = await api('/api/day-plan/refresh', {
      method: 'POST',
      body: JSON.stringify({ only_meal: mealIndex }),
    });
    renderDayState(data);
    toast('已刷新这一餐，其余保持不变');
  } catch (err) { toast(err.message); }
}

async function completeDayMeal(mealIndex) {
  try {
    const data = await api('/api/day-plan/complete-meal', {
      method: 'POST',
      body: JSON.stringify({ meal_index: mealIndex }),
    });
    renderDayState(data);
    const meal = data.updated_meal;
    toast(meal ? `已补齐：推荐度 ${(meal.scores.total * 100).toFixed(0)} · ${meal.level}` : '已自动补齐');
  } catch (err) { toast(err.message); }
}

async function confirmDayMeal(meal, plan) {
  try {
    await api('/api/records/confirm', {
      method: 'POST',
      body: JSON.stringify({
        date: plan.date,
        meal_index: meal.meal_index,
        items: meal.items.map((i) => ({ dish_id: i.dish_id })),
        status: 'confirmed',
      }),
    });
    toast(`已记录${meal.meal_name}`);
    const boot = await api('/api/bootstrap');
    state.boot = boot;
    renderTodaybar();
    renderMetrics();
    await loadDayPlan();
    await loadRecords();
  } catch (err) { toast(err.message); }
}

async function refreshDayPlan() {
  const includePassed = document.getElementById('day-include-passed').checked;
  try {
    const data = await api('/api/day-plan/refresh', {
      method: 'POST',
      body: JSON.stringify({ include_passed: includePassed }),
    });
    renderDayState(data);
    if (data.stage === 'options') toast('已换一批方案');
    else {
      const pinned = data.plan.meals.filter((m) => m.pinned).length;
      toast(pinned ? `已刷新，${pinned} 餐固定不变` : '已刷新整日计划');
    }
  } catch (err) { toast(err.message); }
}

async function reselectDayPlan() {
  try {
    const data = await api('/api/day-plan/reset', { method: 'POST', body: '{}' });
    renderDayState(data);
    toast('已回到方案选择（固定项已清空）');
  } catch (err) { toast(err.message); }
}

/* 用一道菜替换某一餐（推荐页 / 搜索结果 / 菜品详情里调用） */
async function pickMealForDish(dishId) {
  let data;
  try {
    data = await api('/api/day-plan');
  } catch (err) { toast(err.message); return; }
  if (data.stage !== 'chosen' || !data.plan) {
    toast('请先在「今日计划」里选择一个方案');
    switchTab('day');
    return;
  }
  const meals = data.plan.meals.filter((m) => m.status === 'planned');
  if (!meals.length) {
    toast('今天没有可替换的餐次了（都已记录或已过时段）');
    return;
  }
  if (meals.length === 1) {
    await applySetMeal(meals[0].meal_index, dishId);
    return;
  }
  const body = document.getElementById('modal-body');
  body.innerHTML = `
    <h2>替换到哪一餐？</h2>
    <p class="muted">选中的菜会替换该餐的全部内容，并自动固定；之后可以点「自动补齐」把这一餐补完整。</p>
    <div class="pick-list" id="meal-pick"></div>`;
  const pick = body.querySelector('#meal-pick');
  meals.forEach((meal) => {
    const row = el(`<label class="pick-item">
      <input type="radio" name="mealpick" value="${meal.meal_index}">
      <span class="grow">${meal.meal_name}
        <span class="dish-meta"> · ${meal.window} · 当前：${meal.items.map((i) => i.name).join(' + ') || '空'}</span></span>
    </label>`);
    pick.appendChild(row);
  });
  const confirm = el('<button class="btn primary" style="margin-top:12px">替换这一餐</button>');
  confirm.addEventListener('click', async () => {
    const checked = pick.querySelector('input[name="mealpick"]:checked')
      || pick.querySelector('input[name="mealpick"]');
    if (!checked) return;
    closeModal();
    await applySetMeal(Number(checked.value), dishId);
  });
  body.appendChild(confirm);
  document.getElementById('modal').classList.add('open');
}

async function applySetMeal(mealIndex, dishId) {
  try {
    const data = await api('/api/day-plan/set-meal', {
      method: 'POST',
      body: JSON.stringify({ meal_index: mealIndex, dish_ids: [dishId] }),
    });
    renderDayState(data);
    const meal = data.updated_meal;
    toast(meal
      ? `已替换${meal.meal_name}：推荐度 ${(meal.scores.total * 100).toFixed(0)} · ${meal.level}`
      : '已替换这一餐');
  } catch (err) { toast(err.message); }
}

/* ── 时段自动刷新（需求 11） ─────────────────────────────────── */
async function checkSlot() {
  try {
    const info = await api('/api/slot');
    const key = `${state.date}-${info.slot}`;
    if (state.lastSlotKey && state.lastSlotKey !== key) {
      state.slot = info.slot;
      document.getElementById('home-slot-select').value = info.slot;
      state.boot.slot = info;
      renderSlotSelect();
      await loadPlans();
      await loadRecords();
      await loadDayPlan();
      toast(`进入${info.label}时段，推荐已自动刷新`);
    }
    state.lastSlotKey = key;
  } catch (err) { /* 静默失败，下个周期再试 */ }
}

/* ── 搜索 ───────────────────────────────────────────────────── */
function renderFilters() {
  const { canteens, categories } = state.boot;
  const canteenSel = document.getElementById('search-canteen');
  canteenSel.innerHTML = `<option value="">全部食堂</option>` +
    canteens.map((c) => `<option value="${c.name}">${c.campus} · ${c.name}</option>`).join('');
  const catSel = document.getElementById('search-category');
  catSel.innerHTML = `<option value="">全部分类</option>` +
    categories.map((c) => `<option value="${c}">${c}</option>`).join('');
}

async function runSearch() {
  const q = document.getElementById('search-q').value.trim();
  const canteen = document.getElementById('search-canteen').value;
  const category = document.getElementById('search-category').value;
  const sort = document.getElementById('search-sort').value;
  const params = new URLSearchParams({ q, canteen, category, sort });
  const data = await api(`/api/dishes?${params}`);
  document.getElementById('search-hint').textContent = `找到 ${data.total} 道菜`;
  const list = document.getElementById('search-list');
  list.innerHTML = '';
  data.items.forEach((item) => list.appendChild(dishCard(item)));
}

function dishCard(item) {
  const card = el(`
    <div class="card">
      <div class="card-head">
        ${thumb(item)}
        <div class="grow">
          <div class="name">${item.name} ${item.favorite ? '<span class="badge">已收藏</span>' : ''}</div>
          <div class="dish-meta">${item.campus} · ${item.canteen} · ${item.window} · ¥${fmt(item.price, 1)}</div>
        </div>
        <div style="text-align:right">
          <div class="score">${(item.scores.total * 100).toFixed(0)}<small> 分</small></div>
          <span class="${scoreClass(item.scores.total)}">营养分</span>
        </div>
      </div>
      <div class="macro-line">
        <span><b>${fmt(item.kcal)}</b> kcal</span>
        <span>蛋白 <b>${fmt(item.protein, 1)}</b>g</span>
        <span>脂肪 <b>${fmt(item.fat, 1)}</b>g</span>
        <span>碳水 <b>${fmt(item.carbs, 1)}</b>g</span>
      </div>
      <div class="macro-line" style="gap:14px">
        <span class="badge">蛋白分 ${(item.scores.protein_score * 100).toFixed(0)}</span>
        <span class="badge amber">脂肪分 ${(item.scores.fat_score * 100).toFixed(0)}</span>
        <span class="badge grey">碳水 ${pct(item.scores.shares.carbs)}</span>
      </div>
      <div class="card-actions">
        <button class="btn grow" data-act="detail">查看详情 / 评论</button>
        <button class="btn" data-act="pick">选为这一餐</button>
        <button class="btn" data-act="fav">${item.favorite ? '取消收藏' : '收藏'}</button>
      </div>
    </div>`);
  card.querySelector('[data-act="detail"]').addEventListener('click', () => openDish(item.dish_id));
  card.querySelector('[data-act="pick"]').addEventListener('click', () => pickMealForDish(item.dish_id));
  card.querySelector('[data-act="fav"]').addEventListener('click', async (e) => {
    const res = await api(`/api/favorites/${item.dish_id}`, { method: 'POST' });
    item.favorite = res.favorite;
    e.target.textContent = res.favorite ? '取消收藏' : '收藏';
    toast(res.favorite ? '已收藏' : '已取消收藏');
    await loadFavorites();
  });
  return card;
}

/* ── 自选评估（需求 8） ─────────────────────────────────────── */
async function renderPickList() {
  const q = (document.getElementById('custom-q').value || '').trim();
  const data = await api(`/api/dishes?q=${encodeURIComponent(q)}&sort=score`);
  const box = document.getElementById('custom-pick');
  box.innerHTML = '';
  data.items.slice(0, 80).forEach((item) => {
    const row = el(`
      <label class="pick-item">
        <input type="checkbox" ${state.customSelected.has(item.dish_id) ? 'checked' : ''}>
        <span class="grow">${item.name}
          <span class="dish-meta"> · ${item.canteen} · ¥${fmt(item.price, 1)} · ${fmt(item.kcal)} kcal</span></span>
        <span class="dish-meta">蛋白 ${fmt(item.protein, 1)}g</span>
      </label>`);
    const cb = row.querySelector('input');
    cb.addEventListener('change', () => {
      if (cb.checked) state.customSelected.set(item.dish_id, item);
      else state.customSelected.delete(item.dish_id);
      renderChosen();
    });
    box.appendChild(row);
  });
}

function renderChosen() {
  const box = document.getElementById('custom-chosen');
  document.getElementById('custom-count').textContent = state.customSelected.size;
  box.innerHTML = '';
  state.customSelected.forEach((item) => {
    const chip = el(`<span class="chip">${item.name}<button title="移除">×</button></span>`);
    chip.querySelector('button').addEventListener('click', () => {
      state.customSelected.delete(item.dish_id);
      renderChosen();
      renderPickList();
    });
    box.appendChild(chip);
  });
}

async function evaluateCustom() {
  if (!state.customSelected.size) {
    toast('先勾选几道菜');
    return;
  }
  const slot = document.getElementById('custom-slot').value;
  try {
    const data = await api('/api/plans/evaluate', {
      method: 'POST',
      body: JSON.stringify({ dish_ids: [...state.customSelected.keys()], slot }),
    });
    showEvaluation(data);
  } catch (err) { toast(err.message); }
}

function showEvaluation(data, container = '#custom-result') {
  const levelColor = data.recommend_level === '强烈推荐' ? 'var(--green)'
    : data.recommend_level === '推荐' ? 'var(--green)'
      : data.recommend_level === '一般' ? 'var(--amber)' : 'var(--red)';
  document.querySelector(container).innerHTML = `
    <div class="form-card" style="margin-top:10px">
      <h2>这套搭配怎么样</h2>      <div class="row between center">
        <div>
          <div class="muted">推荐度</div>
          <div class="level" style="color:${levelColor}">${data.recommend_level}</div>
        </div>
        <div class="score">${(data.scores.total * 100).toFixed(0)}<small> 分</small></div>
      </div>
      <div class="metric-row"><span>热量</span><span>${fmt(data.totals.kcal)} / ${fmt(data.target.kcal)} kcal</span></div>
      <div class="metric-row"><span>蛋白质</span><span>${fmt(data.totals.protein, 1)} / ${fmt(data.target.protein, 1)} g（${pct(data.macro_pct.protein)}）</span></div>
      <div class="metric-row"><span>脂肪</span><span>${fmt(data.totals.fat, 1)} / ${fmt(data.target.fat, 1)} g（${pct(data.macro_pct.fat)}）</span></div>
      <div class="metric-row"><span>碳水</span><span>${fmt(data.totals.carbs, 1)} / ${fmt(data.target.carbs, 1)} g（${pct(data.macro_pct.carbs)}）</span></div>
      <div class="${data.scores.total >= 0.7 ? 'advice' : 'advice warn'}" style="margin:12px 0 0">
        ${data.advice.map((t) => `· ${t}`).join('<br>')}
      </div>
    </div>`;
}

/* ── 用餐记录（需求 7） ─────────────────────────────────────── */
async function loadRecords() {
  const date = state.recordDate || state.date;
  const data = await api(`/api/records?date=${date}`);
  const box = document.getElementById('record-list');
  box.innerHTML = '';
  data.records.forEach((record) => box.appendChild(recordCard(data.date, record)));
  const daily = data.daily;
  const used = data.consumed;
  document.getElementById('record-summary').innerHTML = `
    <h3>${date} 合计</h3>
    <div class="metric-row"><span>热量</span><span>${fmt(used.kcal)} / ${fmt(daily.kcal)} kcal</span></div>
    <div class="metric-row"><span>蛋白质</span><span>${fmt(used.protein, 1)} / ${fmt(daily.protein, 1)} g</span></div>
    <div class="metric-row"><span>脂肪</span><span>${fmt(used.fat, 1)} / ${fmt(daily.fat, 1)} g</span></div>
    <div class="metric-row"><span>碳水</span><span>${fmt(used.carbs, 1)} / ${fmt(daily.carbs, 1)} g</span></div>
    <p class="muted" style="margin-top:10px">历史上记录过的日期：${data.recent_dates.length ? data.recent_dates.join('、') : '暂无'}</p>`;
}

function recordCard(date, record) {
  const statusText = record.status === 'confirmed' ? '已确认'
    : record.status === 'manual' ? '手动补记' : '未记录';
  const badgeClass = record.status === 'empty' ? 'badge grey' : 'badge';
  const card = el(`
    <div class="record ${record.status === 'empty' ? 'empty' : ''}">
      <h3>${record.meal_name}
        <span class="${badgeClass}">${statusText}</span>
      </h3>
      <div class="dish-meta">目标 ${fmt(record.target.kcal)} kcal</div>
      <div class="dish-list"></div>
      <div class="macro-line">
        <span><b>${fmt(record.kcal)}</b> kcal</span>
        <span>蛋白 ${fmt(record.protein, 1)}g</span>
        <span>脂肪 ${fmt(record.fat, 1)}g</span>
        <span>碳水 ${fmt(record.carbs, 1)}g</span>
      </div>
      <div class="card-actions">
        <button class="btn primary grow" data-act="edit">${record.status === 'empty' ? '手动补记' : '修改'}</button>
        ${record.status === 'empty' ? '' : '<button class="btn" data-act="clear">清空</button>'}
      </div>
    </div>`);
  const list = card.querySelector('.dish-list');
  if (record.items && record.items.length) {
    record.items.forEach((item) => {
      const row = el(`<div class="dish-row">
        <div class="grow"><div class="dish-name">${item.name}</div>
        <div class="dish-meta">${item.canteen || ''} ${item.window || ''} · ${fmt(item.kcal)} kcal</div></div>
        <div class="dish-meta">¥${fmt(item.price, 1)}</div></div>`);
      row.addEventListener('click', () => openDish(item.dish_id));
      list.appendChild(row);
    });
  } else {
    list.innerHTML = `<div class="dish-meta" style="padding:6px 0">还没有记录这一餐</div>`;
  }
  card.querySelector('[data-act="edit"]').addEventListener('click', () => openRecordEditor(date, record));
  const clearBtn = card.querySelector('[data-act="clear"]');
  if (clearBtn) {
    clearBtn.addEventListener('click', async () => {
      await api(`/api/records/${date}/${record.meal_index}`, { method: 'DELETE' });
      toast('已清空该餐记录');
      await loadRecords();
      state.boot = await api('/api/bootstrap');
      renderTodaybar();
    });
  }
  return card;
}

async function openRecordEditor(date, record) {
  const data = await api('/api/dishes?sort=score');
  const selected = new Set((record.items || []).map((i) => i.dish_id));
  const body = document.getElementById('modal-body');
  body.innerHTML = `
    <h2>${date} · ${record.meal_name} 补记</h2>
    <p class="muted">勾选这一餐实际吃的菜，保存后会重新计算当天的营养素汇总。</p>
    <input id="rec-q" class="input" style="width:100%;margin-bottom:10px" placeholder="搜索菜品">
    <div class="pick-list" id="rec-pick"></div>
    <div class="row gap" style="margin-top:14px">
      <button class="btn primary" id="rec-save">保存记录</button>
      <span class="muted" id="rec-msg"></span>
    </div>`;
  const pick = body.querySelector('#rec-pick');
  const draw = (q = '') => {
    pick.innerHTML = '';
    data.items
      .filter((i) => !q || i.name.includes(q) || (i.tags || []).some((t) => t.includes(q)))
      .slice(0, 120)
      .forEach((item) => {
        const row = el(`<label class="pick-item">
          <input type="checkbox" ${selected.has(item.dish_id) ? 'checked' : ''}>
          <span class="grow">${item.name}
            <span class="dish-meta"> · ${item.canteen} · ¥${fmt(item.price, 1)} · ${fmt(item.kcal)} kcal</span></span>
        </label>`);
        const cb = row.querySelector('input');
        cb.addEventListener('change', () => {
          if (cb.checked) selected.add(item.dish_id); else selected.delete(item.dish_id);
        });
        pick.appendChild(row);
      });
  };
  draw();
  body.querySelector('#rec-q').addEventListener('input', (e) => draw(e.target.value.trim()));
  body.querySelector('#rec-save').addEventListener('click', async () => {
    if (!selected.size) { toast('至少选一道菜'); return; }
    await api('/api/records/confirm', {
      method: 'POST',
      body: JSON.stringify({
        date,
        meal_index: record.meal_index,
        items: [...selected].map((id) => ({ dish_id: id })),
        status: 'manual',
      }),
    });
    closeModal();
    toast('记录已保存');
    await loadRecords();
    state.boot = await api('/api/bootstrap');
    renderTodaybar();
  });
  document.getElementById('modal').classList.add('open');
}

/* ── 收藏 ───────────────────────────────────────────────────── */
async function loadFavorites() {
  const data = await api('/api/favorites');
  const box = document.getElementById('favorite-list');
  box.innerHTML = '';
  if (!data.items.length) {
    box.innerHTML = `<div class="placeholder-text">还没有收藏。在搜索页或菜品详情里点「收藏」即可。</div>`;
    return;
  }
  data.items.forEach((item) => box.appendChild(dishCard(item)));
}

/* ── 设置 ───────────────────────────────────────────────────── */
function renderProfileForm() {
  const profile = state.boot.profile;
  const form = document.getElementById('profile-form');
  ['nickname', 'birth_date', 'height_cm', 'weight_kg', 'allergies'].forEach((key) => {
    if (form.elements[key]) form.elements[key].value = profile[key] ?? '';
  });
  form.elements.gender.value = profile.gender;
  form.elements.goal.value = profile.goal;

  const actBox = document.getElementById('activity-group');
  actBox.innerHTML = '';
  ACTIVITY.forEach(([value, label, desc]) => {
    const opt = el(`<div class="radio-opt ${profile.activity_level === value ? 'active' : ''}" data-value="${value}">
      <span>${label}</span><small>${desc}</small></div>`);
    opt.addEventListener('click', () => {
      actBox.querySelectorAll('.radio-opt').forEach((n) => n.classList.remove('active'));
      opt.classList.add('active');
    });
    actBox.appendChild(opt);
  });

  const mealsBox = document.getElementById('meals-group');
  mealsBox.innerHTML = '';
  [1, 2, 3, 4, 5, 6].forEach((n) => {
    const opt = el(`<div class="radio-opt ${Number(profile.meals_per_day) === n ? 'active' : ''}" data-value="${n}">
      <span>${n} 餐</span></div>`);
    opt.addEventListener('click', () => {
      mealsBox.querySelectorAll('.radio-opt').forEach((x) => x.classList.remove('active'));
      opt.classList.add('active');
    });
    mealsBox.appendChild(opt);
  });
}

function renderMetrics() {
  const m = state.boot.metrics;
  document.getElementById('metrics-box').innerHTML = `
    <div class="metric-row"><span>年龄 / 性别</span><span>${m.age} 岁 · ${m.gender_label}</span></div>
    <div class="metric-row"><span>活动系数</span><span>${m.activity_label} ×${m.activity_factor}</span></div>
    <div class="metric-row"><span>基础代谢 BMR</span><span>${fmt(m.bmr)} kcal</span></div>
    <div class="metric-row"><span>每日消耗 TDEE</span><span>${fmt(m.tdee)} kcal</span></div>
    <div class="metric-row"><span>目标热量（${m.goal_label}）</span><span><b>${fmt(m.daily.kcal)}</b> kcal</span></div>
    <div class="metric-row"><span>蛋白质 / 脂肪 / 碳水</span>
      <span>${fmt(m.daily.protein, 1)}g / ${fmt(m.daily.fat, 1)}g / ${fmt(m.daily.carbs, 1)}g</span></div>
    <div class="metric-row"><span>餐次拆分</span><span>${m.meals.map((x) => `${x.name} ${fmt(x.kcal)}`).join(' · ')}</span></div>
    <p class="muted" style="margin-top:10px">依据：${m.reference}。碳水 ${m.amdr.carbs[0]}–${m.amdr.carbs[1]}%、
      脂肪 ${m.amdr.fat[0]}–${m.amdr.fat[1]}%、蛋白质 ${m.amdr.protein[0]}–${m.amdr.protein[1]}% 供能比。</p>`;
}

function renderSlotSettings() {
  const s = state.boot.settings || {};
  const box = document.getElementById('slot-settings');
  const fields = [
    ['slot.breakfast.start', '早餐开始'],
    ['slot.breakfast.end', '早餐结束'],
    ['slot.lunch.start', '午餐开始'],
    ['slot.lunch.end', '午餐结束'],
    ['slot.dinner.start', '晚餐开始'],
    ['slot.dinner.end', '晚餐结束'],
  ];
  box.innerHTML = fields.map(([key, label]) =>
    `<label>${label}<input class="input" data-key="${key}" type="time" value="${s[key] || '08:00'}"></label>`).join('');
}

async function saveProfile(e) {
  e.preventDefault();
  const form = e.target;
  const activeAct = document.querySelector('#activity-group .radio-opt.active');
  const activeMeals = document.querySelector('#meals-group .radio-opt.active');
  const payload = {
    nickname: form.elements.nickname.value,
    gender: form.elements.gender.value,
    birth_date: form.elements.birth_date.value,
    height_cm: Number(form.elements.height_cm.value),
    weight_kg: Number(form.elements.weight_kg.value),
    goal: form.elements.goal.value,
    allergies: form.elements.allergies.value,
    activity_level: activeAct ? activeAct.dataset.value : 'light',
    meals_per_day: activeMeals ? Number(activeMeals.dataset.value) : 3,
  };
  try {
    const data = await api('/api/profile', { method: 'PUT', body: JSON.stringify(payload) });
    state.boot.profile = data.profile;
    state.boot.metrics = data.metrics;
    renderMetrics();
    renderTargets();
    document.getElementById('profile-msg').textContent = '已保存，推荐目标已更新';
    await loadPlans();
    await loadRecords();
    toast('资料已保存');
  } catch (err) {
    document.getElementById('profile-msg').textContent = err.message;
  }
}

async function saveSlots() {
  const inputs = document.querySelectorAll('#slot-settings input[data-key]');
  const payload = {};
  inputs.forEach((i) => { payload[i.dataset.key] = i.value; });
  const data = await api('/api/settings', { method: 'PUT', body: JSON.stringify(payload) });
  state.boot.settings = data.settings;
  document.getElementById('slot-msg').textContent = '时间点已保存';
  toast('时间点已保存');
}

/* ── 菜品详情 ───────────────────────────────────────────────── */
async function openDish(dishId) {
  const dish = await api(`/api/dishes/${dishId}`);
  const body = document.getElementById('modal-body');
  body.innerHTML = `
    <h2>${dish.name} ${dish.favorite ? '<span class="badge">已收藏</span>' : ''}</h2>
    <p class="muted">${dish.campus} · ${dish.canteen} · ${dish.window}${dish.floor ? '（' + dish.floor + '）' : ''} · ¥${fmt(dish.price, 1)}/${dish.unit}</p>
    <div class="gallery" id="dish-gallery">
      ${dish.images.map((img) => `<img src="/media/${img.path}" alt="">`).join('')}
    </div>
    <div class="row gap" style="align-items:center;margin:6px 0 10px">
      <input type="file" id="img-file" accept="image/*" style="display:none">
      <button class="btn small" id="btn-upload">＋ 添加图片</button>
      <button class="btn small" id="btn-fav">${dish.favorite ? '取消收藏' : '收藏'}</button>
      <button class="btn small primary" id="btn-pick-meal">选为这一餐</button>
      <span class="muted">热量 ${fmt(dish.kcal)} kcal · 蛋白 ${fmt(dish.protein, 1)}g · 脂肪 ${fmt(dish.fat, 1)}g · 碳水 ${fmt(dish.carbs, 1)}g</span>
    </div>
    <h3>三大营养素评分（0–100）</h3>
    <div class="score-bars">
      <div class="score-bar"><div class="label">蛋白质分</div>
        <div class="val">${(dish.scores.protein_score * 100).toFixed(0)}</div>
        <div class="muted">占能量 ${pct(dish.scores.shares.protein)}（指南 10–20%）· 密度 ${dish.scores.protein_density} g/100kcal</div></div>
      <div class="score-bar"><div class="label">脂肪分</div>
        <div class="val">${(dish.scores.fat_score * 100).toFixed(0)}</div>
        <div class="muted">占能量 ${pct(dish.scores.shares.fat)}（指南 20–30%）</div></div>
      <div class="score-bar"><div class="label">碳水</div>
        <div class="val">${(dish.scores.carbs_score * 100).toFixed(0)}</div>
        <div class="muted">占能量 ${pct(dish.scores.shares.carbs)}（指南 50–65%）</div></div>
    </div>
    <div class="${dish.scores.total >= 0.7 ? 'advice' : 'advice warn'}">
      综合营养分 ${(dish.scores.total * 100).toFixed(0)} / 100${dish.note ? ' · ' + dish.note : ''}
      ${dish.tags.length ? '<br>标签：' + dish.tags.join('、') : ''}
    </div>
    ${dish.price_compare.length > 1 ? `
      <h3 style="margin-top:16px">全校比价</h3>
      <table class="cmp">
        <tr><th>食堂</th><th>窗口</th><th>价格</th></tr>
        ${dish.price_compare.map((p) => `<tr><td>${p.campus} · ${p.canteen}</td><td>${p.window}</td><td>¥${fmt(p.price, 1)}</td></tr>`).join('')}
      </table>` : ''}
    ${dish.window_rating && dish.window_rating.avg ? `
      <p class="muted" style="margin-top:12px">窗口「${dish.window}」平均评分
        <span class="stars">${stars(dish.window_rating.avg)}</span> ${fmt(dish.window_rating.avg, 1)}
        （${dish.window_rating.cnt} 条评价）</p>` : ''}
    <h3 style="margin-top:16px">我的评价</h3>
    <div id="comment-list"></div>
    <div class="row gap" style="margin-top:10px;flex-wrap:wrap">
      <select id="comment-rating" class="select">
        <option value="5">★★★★★ 5</option><option value="4">★★★★ 4</option>
        <option value="3">★★★ 3</option><option value="2">★★ 2</option><option value="1">★ 1</option>
      </select>
      <input id="comment-text" class="input grow" placeholder="说说这道菜怎么样（口味、分量、性价比）">
      <button class="btn primary" id="btn-comment">发表</button>
    </div>`;

  const commentBox = body.querySelector('#comment-list');
  const drawComments = (comments) => {
    commentBox.innerHTML = comments.length ? comments.map((c) => `
      <div class="comment">
        <div class="row between center">
          <span class="stars">${stars(c.rating)}</span>
          <span class="muted">${c.created_at}</span>
        </div>
        <div>${escapeHtml(c.content)}</div>
        <button class="btn small" data-del="${c.id}" style="margin-top:6px">删除</button>
      </div>`).join('') : '<p class="muted">还没有评价。</p>';
    commentBox.querySelectorAll('[data-del]').forEach((btn) => {
      btn.addEventListener('click', async () => {
        await api(`/api/comments/${btn.dataset.del}`, { method: 'DELETE' });
        toast('已删除评价');
        openDish(dishId);
      });
    });
  };
  drawComments(dish.comments);

  body.querySelector('#btn-comment').addEventListener('click', async () => {
    const content = body.querySelector('#comment-text').value.trim();
    if (!content) { toast('先写点内容'); return; }
    await api('/api/comments', {
      method: 'POST',
      body: JSON.stringify({
        dish_id: dishId,
        rating: Number(body.querySelector('#comment-rating').value),
        content,
      }),
    });
    toast('已发表评价');
    openDish(dishId);
  });

  body.querySelector('#btn-pick-meal').addEventListener('click', () => {
    closeModal();
    pickMealForDish(dishId);
  });

  body.querySelector('#btn-fav').addEventListener('click', async () => {
    const res = await api(`/api/favorites/${dishId}`, { method: 'POST' });
    toast(res.favorite ? '已收藏' : '已取消收藏');
    await loadFavorites();
    openDish(dishId);
  });

  const fileInput = body.querySelector('#img-file');
  body.querySelector('#btn-upload').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const dataUrl = await readFileAsDataURL(file);
    await api('/api/images', {
      method: 'POST',
      body: JSON.stringify({
        owner_type: 'dish',
        owner_id: dishId,
        filename: file.name,
        data: dataUrl,
      }),
    });
    toast('图片已添加');
    openDish(dishId);
    await loadPlans();
  });

  document.getElementById('modal').classList.add('open');
}

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text ?? '';
  return div.innerHTML;
}

function closeModal() {
  document.getElementById('modal').classList.remove('open');
}

/* ── 标签页 ─────────────────────────────────────────────────── */
function bindTabs() {
  document.querySelectorAll('.tab').forEach((tab) => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });
}

init().catch((err) => {
  document.body.innerHTML = `<div style="padding:40px;font-family:sans-serif">
    <h2>前端启动失败</h2><p>${err.message}</p>
    <p>请确认服务已启动：双击 start.bat，或运行 python run.py</p></div>`;
});
