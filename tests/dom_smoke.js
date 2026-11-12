// 前端渲染冒烟测试：在 Node 里用极简 DOM 桩执行 app.js。
// 目的不是验证样式，而是把"模板字符串写错、调错方法、函数被删掉导致标签点不动"这类
// 错误拦在提交前（真实事故 1：fmt(...).toFixed 导致整页白屏；事故 2：switchTab 被删，标签全失效）。
//
// 用法：node tests/dom_smoke.js [port]
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const port = process.argv[2] || 8848;
const BASE = `http://127.0.0.1:${port}`;
const appPath = path.join(__dirname, '..', 'app', 'static', 'app.js');
const code = fs.readFileSync(appPath, 'utf8');

/* ── 极简 DOM 桩 ─────────────────────────────────────────── */
const htmlWrites = {};
const bodyPrepend = [];
const listeners = {};
const TAB_NAMES = ['home', 'day', 'search', 'custom', 'favorites', 'settings'];

function makeClassList() {
  const set = new Set();
  return {
    add: (...v) => v.forEach((x) => set.add(x)),
    remove: (...v) => v.forEach((x) => set.delete(x)),
    toggle: (v, force) => { (force === undefined ? !set.has(v) : force) ? set.add(v) : set.delete(v); },
    contains: (v) => set.has(v),
  };
}

function makeEl(tag = 'div') {
  const el = {
    tagName: tag.toUpperCase(),
    _html: '',
    textContent: '',
    value: '',
    dataset: {},
    style: {},
    classList: makeClassList(),
    children: [],
    files: [],
    elements: new Proxy({}, { get: (target, key) => (target[key] ||= makeEl()) }),
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      if (this.id) htmlWrites[this.id] = this._html;
    },
    addEventListener: (type, fn) => { (listeners[`${el.id}:${type}`] ||= []).push(fn); },
    appendChild: (child) => { el.children.push(child); return child; },
    prepend: (child) => { el.children.unshift(child); return child; },
    remove: () => {},
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
    getBoundingClientRect: () => ({ width: 0, height: 0, top: 0, left: 0 }),
    focus: () => {},
    click: () => { (listeners[`${el.id}:click`] || []).forEach((fn) => fn({ target: el })); },
  };
  return el;
}

const byId = {};
function getEl(id) {
  if (!byId[id]) {
    const el = makeEl('div');
    el.id = id;
    htmlWrites[id] = '';
    byId[id] = el;
  }
  return byId[id];
}

const body = makeEl('body');
body.prepend = (child) => { bodyPrepend.push(child); body.children.unshift(child); return child; };

const tabEls = TAB_NAMES.map((name) => {
  const el = makeEl('button');
  el.id = `tab-${name}`;
  el.dataset = { tab: name };
  return el;
});
const panelEls = TAB_NAMES.map((name) => {
  const el = makeEl('section');
  el.id = `panel-${name}`;
  el.classList.add('panel');
  return el;
});

const documentStub = {
  // 面板与标签用同一批对象，switchTab 里的 getElementById 才能命中
  getElementById: (id) => panelEls.find((p) => p.id === id)
    || tabEls.find((t) => t.id === id)
    || getEl(id),
  querySelector: () => makeEl(),
  querySelectorAll: (sel) => {
    if (sel === '.tab') return tabEls;
    if (sel === '.panel') return panelEls;
    return [];
  },
  createElement: (tag) => {
    if (tag === 'template') {
      const tpl = { _html: '', content: { firstElementChild: null } };
      Object.defineProperty(tpl, 'innerHTML', {
        get() { return tpl._html; },
        set(v) { tpl._html = String(v); tpl.content.firstElementChild = makeEl(); },
      });
      return tpl;
    }
    return makeEl(tag);
  },
  body,
  addEventListener: () => {},
  title: 'test',
};

const windowStub = {
  addEventListener: (type, fn) => { (listeners[`window:${type}`] ||= []).push(fn); },
  location: { reload: () => {} },
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: (fn) => { try { fn(); } catch (err) { console.error('timer error', err); } return 0; },
  clearTimeout: () => {},
};

const sandbox = {
  document: documentStub,
  window: windowStub,
  location: { reload: () => {} },
  console,
  fetch: (url, opts) => fetch(url.startsWith('http') ? url : BASE + url, opts),
  URLSearchParams,
  FileReader: class { readAsDataURL() { this.onload && this.onload(); } },
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: (fn) => { try { fn(); } catch (err) { console.error('timer error', err); } return 0; },
  clearTimeout: () => {},
  alert: () => {},
};
windowStub.document = documentStub;
sandbox.globalThis = sandbox;

let fatal = null;
process.on('unhandledRejection', (reason) => {
  fatal = String((reason && reason.message) || reason);
  console.error('[unhandledRejection]', reason);
});

try {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'app.js' });
} catch (err) {
  console.log(`[FAIL] app.js 执行抛错：${err.message}`);
  process.exit(1);
}

let dayStage = null;
fetch(`${BASE}/api/day-plan?include_passed=1`)
  .then((res) => res.json())
  .then((json) => { dayStage = (json.data && json.data.stage) || null; })
  .catch(() => { dayStage = null; });

function check(name, fn) {
  try {
    fn();
    console.log(`[PASS] ${name}`);
    return true;
  } catch (err) {
    console.log(`[FAIL] ${name}: ${err.message}`);
    return false;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
  // 首次生成方案需要 1~3 秒（2600+ 道菜），这里最多等 15 秒
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    const ready = (byId['todaybar'] && String(byId['todaybar']._html || '').includes('今日目标'))
      && (byId['plan-list'] && byId['plan-list'].children.length > 0
        || (byId['home-day-options'] && byId['home-day-options'].children.length > 0)
        || (byId['day-hint'] && String(byId['day-hint'].textContent || byId['day-hint']._html || '').length > 0));
    if (ready && !fatal) break;
    await sleep(250);
  }
  await sleep(400);
  let pass = 0;
  let fail = 0;
  const run = (name, fn) => { if (check(name, fn)) pass++; else fail++; };

  run('未触发初始化失败横幅', () => {
    if (bodyPrepend.length) {
      const banner = bodyPrepend[0];
      const text = (banner && (banner.innerHTML || banner.textContent)) || '';
      throw new Error(`页面被替换为错误提示：${String(text).replace(/<[^>]*>/g, '').slice(0, 140)}`);
    }
  });

  run('顶部今日速览已渲染', () => {
    const html = htmlWrites['todaybar'] || '';
    if (!html.includes('今日目标')) throw new Error(`todaybar 内容为空：${html.slice(0, 60)}`);
  });

  run('标签页都绑定了点击事件', () => {
    const missing = tabEls.filter((t) => !(listeners[`${t.id}:click`] || []).length);
    if (missing.length) throw new Error(`这些标签没绑定点击：${missing.map((t) => t.dataset.tab).join(', ')}`);
  });

  run('点击标签能切换面板', () => {
    const target = tabEls.find((t) => t.dataset.tab === 'search');
    target.click();
    const panel = panelEls.find((p) => p.id === 'panel-search');
    if (!panel.classList.contains('active')) throw new Error('点击「搜索」后面板没有激活');
    const others = panelEls.filter((p) => p.id !== 'panel-search' && p.classList.contains('active'));
    if (others.length) throw new Error(`其它面板没有取消激活：${others.map((p) => p.id).join(', ')}`);
  });

  run('推荐页按模式渲染出内容', () => {
    const isDayMode = byId['home-day'] && !byId['home-day'].classList.contains('hidden');
    const dayOptions = byId['home-day-options'] ? byId['home-day-options'].children.length : 0;
    const singleCards = byId['plan-list'] ? byId['plan-list'].children.length : 0;
    if (isDayMode && dayOptions === 0 && dayStage === 'options') {
      throw new Error('一天方案模式下应渲染 A/B/C 卡片');
    }
    if (!isDayMode && singleCards === 0) {
      throw new Error('单道菜模式下推荐列表为空');
    }
  });

  run('今日页按阶段渲染', () => {
    const hint = String((byId['day-hint'] && byId['day-hint'].textContent) || '')
      || String((byId['day-hint'] && byId['day-hint'].innerHTML) || '');
    if (!hint) throw new Error('今日页没有提示文字');
    if (dayStage === 'options' && !hint.includes('还没有选定')) {
      throw new Error(`未选方案时应提示去推荐页，实际：${hint.slice(0, 60)}`);
    }
    if (dayStage === 'chosen') {
      const cards = byId['day-meals'] ? byId['day-meals'].children.length : 0;
      if (cards < 1) throw new Error('已选方案后今日页应显示餐次卡片');
    }
  });

  run('未出现 NaN / undefined / [object', () => {
    const walk = (node, where) => {
      if (!node) return;
      for (const chunk of [String(node._html || ''), String(node.textContent || '')]) {
        for (const bad of ['NaN', 'undefined', '[object']) {
          if (chunk.includes(bad)) throw new Error(`${where} 里出现了 ${bad}：${chunk.slice(0, 120)}`);
        }
      }
      (node.children || []).forEach((child, i) => walk(child, `${where} > child[${i}]`));
    };
    for (const [id, node] of Object.entries(byId)) walk(node, `#${id}`);
  });

  run('没有出现"加载失败"提示', () => {
    const toastText = String((byId['toast'] && byId['toast'].textContent) || '');
    if (toastText.includes('失败')) throw new Error(`页面提示：${toastText}`);
  });

  run('渲染过程中没有未捕获错误', () => {
    if (fatal) throw new Error(fatal);
  });

  run('关键交互函数都已定义（含手动选择）', () => {
    const required = ['switchTab', 'loadHome', 'loadDayPlan', 'loadRecords',
      'renderHomeDay', 'renderDayTab', 'dayMealCard', 'openMealPicker',
      'openRecordEditor', 'pickMealForDish', 'applySetMeal', 'confirmDayMeal',
      'refreshDayPlan', 'reselectDayPlan', 'chooseDayOption', 'renderModeSettings',
      'commitManualPlan', 'openMealPicker',
      'addToCart', 'renderCart', 'evaluateCart', 'applyCartToPlan', 'clearCart'];
    const missing = required.filter((name) => typeof sandbox[name] !== 'function');
    if (missing.length) throw new Error(`未定义：${missing.join(', ')}`);
  });

  run('搜索筛选改变后自动搜索（无需点按钮）', () => {
    const box = byId['search-list'];
    const before = box ? box.children.length : 0;
    const fns = listeners['search-canteen:change'] || [];
    if (!fns.length) throw new Error('食堂筛选没有绑定 change 事件');
    fns.forEach((fn) => fn({ target: getEl('search-canteen') }));
    if (before === 0 && box && box.children.length === 0 && !htmlWrites['search-hint']) {
      throw new Error('改变筛选后没有触发搜索（列表与提示都没更新）');
    }
  });

  run('购物车能加菜并显示合计', () => {
    if (typeof sandbox.addToCart !== 'function') throw new Error('缺少 addToCart');
    sandbox.addToCart({
      dish_id: 999001, name: '测试菜', canteen: '一食堂', window: '测试窗口',
      price: 8, kcal: 400, protein: 20, fat: 10, carbs: 50,
    });
    const summary = byId['cart-summary'];
    if (!summary || !String(summary._html).includes('已选')) {
      throw new Error('购物车合计没有渲染');
    }
    if (!String(summary._html).includes('400')) throw new Error('购物车合计热量不对');
    sandbox.clearCart();
    if (String(byId['cart-summary']._html).includes('已选 1')) throw new Error('清空没生效');
  });

  run('设置页有推荐模式选项', () => {
    const box = byId['mode-group'];
    if (!box) throw new Error('找不到 #mode-group');
    if (!box.children.length) throw new Error('推荐模式没有渲染出选项');
  });

  console.log('-'.repeat(46));
  console.log(`通过 ${pass} 项，失败 ${fail} 项`);
  process.exit(fail ? 1 : 0);
})();
