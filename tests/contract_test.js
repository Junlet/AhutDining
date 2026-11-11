// 前端数据契约校验：确认 API 返回的字段与 app.js 读取的字段一致
// 用法：node tests/contract_test.js [port]
const port = process.argv[2] || 8848;
const BASE = `http://127.0.0.1:${port}`;
let pass = 0, fail = 0;

const get = async (p) => {
  const res = await fetch(BASE + p);
  const json = await res.json();
  if (!json.ok) throw new Error(`${p} -> ${json.error}`);
  return json.data;
};

function need(obj, path, label) {
  const parts = path.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur === null || cur === undefined || !(p in cur)) {
      throw new Error(`缺少字段 ${label || path}（在 ${p} 处断了）`);
    }
    cur = cur[p];
  }
  return cur;
}

async function test(name, fn) {
  try {
    await fn();
    console.log(`[PASS] ${name}`);
    pass++;
  } catch (err) {
    console.log(`[FAIL] ${name}: ${err.message}`);
    fail++;
  }
}

(async () => {
  let boot, plans, detail, records;

  await test('bootstrap 字段', async () => {
    boot = await get('/api/bootstrap');
    ['profile.nickname', 'profile.birth_date', 'profile.height_cm', 'profile.activity_level',
      'metrics.bmr', 'metrics.tdee', 'metrics.daily.kcal', 'metrics.daily.protein',
      'metrics.daily.fat', 'metrics.daily.carbs', 'metrics.daily.shares.protein',
      'metrics.meals_per_day', 'metrics.goal_label', 'metrics.activity_factor',
      'datasets.dishes', 'slot.slot', 'slot.label', 'slot.window', 'date', 'canteens',
      'categories', 'records', 'consumed.kcal'].forEach((p) => need(boot, p));
    if (!need(boot, 'metrics.meals').length) throw new Error('meals 为空');
    need(boot, 'metrics.meals.0.kcal');
    if (!boot.settings || !boot.settings['slot.lunch.start']) {
      throw new Error('settings 缺少 slot.lunch.start');
    }
  });

  await test('推荐套餐字段', async () => {
    plans = await get('/api/plans?slot=lunch');
    const p = need(plans, 'plans.0');
    ['canteen.name', 'canteen.campus', 'meal_name', 'target.kcal', 'target.protein',
      'target.meal_index', 'scores.total', 'scores.nutrition', 'macro_pct.protein',
      'macro_pct.fat', 'macro_pct.carbs', 'advice', 'items.0.dish_id', 'items.0.name',
      'items.0.window', 'items.0.price', 'items.0.kcal', 'items.0.protein', 'items.0.fat',
      'items.0.carbs', 'items.0.scores.total', 'items.0.scores.protein_score',
      'items.0.scores.shares.carbs', 'items.0.favorite', 'totals.kcal', 'totals.price',
      'batch', 'id'].forEach((path) => need(p, path));
    if (!Array.isArray(p.advice) || !p.advice.length) throw new Error('advice 不是非空数组');
  });

  await test('搜索字段', async () => {
    const q = encodeURIComponent('\u725b\u8089');
    const data = await get(`/api/dishes?q=${q}`);
    need(data, 'items.0.dish_id');
    need(data, 'items.0.scores.shares.protein');
    need(data, 'items.0.scores.protein_score');
    if (!Array.isArray(need(data, 'items.0.tags'))) throw new Error('tags 不是数组');
  });

  await test('菜品详情字段', async () => {
    const first = need(plans, 'plans.0.items.0');
    detail = await get(`/api/dishes/${first.dish_id}`);
    ['name', 'canteen', 'window', 'price', 'unit', 'kcal', 'protein', 'fat', 'carbs',
      'scores.protein_score', 'scores.fat_score', 'scores.carbs_score',
      'scores.shares.fat'].forEach((p) => need(detail, p));
    if (!Array.isArray(detail.comments)) throw new Error('comments 不是数组');
    if (!Array.isArray(detail.images)) throw new Error('images 不是数组');
    if (!Array.isArray(detail.price_compare)) throw new Error('price_compare 不是数组');
  });

  await test('用餐记录字段', async () => {
    records = await get(`/api/records?date=${boot.date}`);
    need(records, 'records.0.meal_name');
    need(records, 'records.0.status');
    need(records, 'records.0.target.kcal');
    need(records, 'records.0.items');
    need(records, 'records.0.kcal');
    need(records, 'consumed.kcal');
    need(records, 'daily.kcal');
    if (!Array.isArray(records.recent_dates)) throw new Error('recent_dates 不是数组');
    if (records.records.length !== boot.metrics.meals_per_day) {
      throw new Error('记录栏位数量与每日餐数不一致');
    }
  });

  await test('今日计划字段', async () => {
    const post = async (p, body = {}) => {
      const res = await fetch(BASE + p, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const json = await res.json();
      if (!json.ok) throw new Error(`${p} -> ${json.error}`);
      return json.data;
    };

    let st = await get('/api/day-plan?include_passed=1');
    ['stage', 'now', 'daily.kcal', 'include_passed'].forEach((p) => need(st, p));

    st = await post('/api/day-plan/reset');
    if (st.stage !== 'options') throw new Error('reset 后 stage 应为 options');
    need(st, 'options.0.label');
    need(st, 'options.0.avg_score');
    need(st, 'options.0.level');
    need(st, 'options.0.summary.plan_kcal');
    need(st, 'options.0.meals.0.meal_name');
    need(st, 'options.0.meals.0.status');
    if (!Array.isArray(st.options[0].meals[0].items)) throw new Error('items 不是数组');

    st = await post('/api/day-plan/choose', { index: 0 });
    if (st.stage !== 'chosen') throw new Error('choose 后 stage 应为 chosen');
    need(st, 'plan.label');
    need(st, 'plan.summary.meals_remaining');
    need(st, 'plan.summary.coverage');
    const meal = st.plan.meals.find((m) => m.status === 'planned') || st.plan.meals[0];
    ['meal_name', 'window', 'status', 'pinned', 'source', 'level', 'target.kcal',
      'totals.kcal', 'totals.price', 'macro_pct.protein', 'advice', 'scores.total'].forEach((p) => need(meal, p));
    if (!Array.isArray(meal.items)) throw new Error('meal.items 不是数组');
    await post('/api/day-plan/reset');
  });

  await test('统计字段', async () => {
    const stats = await get('/api/stats');
    need(stats, 'remaining.kcal');
    need(stats, 'daily.protein');
    need(stats, 'meals.0.name');
    if (!Array.isArray(stats.windows)) throw new Error('windows 不是数组');
  });

  console.log('-'.repeat(46));
  console.log(`通过 ${pass} 项，失败 ${fail} 项`);
  process.exit(fail ? 1 : 0);
})();
