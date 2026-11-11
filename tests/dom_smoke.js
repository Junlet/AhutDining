// 前端渲染冒烟测试：在 Node 里用极简 DOM 桩执行 app.js，
// 目的不是验证样式，而是把"模板字符串里写错、调错方法"这类错误拦在提交前
// （真实事故：fmt(...).toFixed 让人打开就是白屏）。
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
const htmlWrites = {};      // id -> 最后一次写入的 innerHTML
const bodyPrepend = [];     // body.prepend 调用的参数（用来发现 fatal banner）
const listeners = {};
let errorHandlers = [];

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
    click: () => {},
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

const documentStub = {
  getElementById: (id) => getEl(id),
  querySelector: (sel) => makeEl(),
  querySelectorAll: () => [],
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
  addEventListener: (type, fn) => {
    if (type === 'error') errorHandlers.push(fn);
    else (listeners[`window:${type}`] ||= []).push(fn);
  },
  location: { reload: () => {} },
  setInterval: (fn, ms) => 0,
  clearInterval: () => {},
  setTimeout: (fn, ms) => 0,
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
  setInterval: windowStub.setInterval,
  clearInterval: windowStub.clearInterval,
  setTimeout: (fn) => { try { fn(); } catch (err) { console.error('timer error', err); } return 0; },
  clearTimeout: () => {},
  alert: () => {},
};

windowStub.document = documentStub;
sandbox.globalThis = sandbox;

/* ── 执行 app.js ─────────────────────────────────────────── */
let fatal = null;
process.on('unhandledRejection', (reason) => {
  fatal = String((reason && reason.message) || reason);
  console.error('[unhandledRejection]', reason);
});

const origInit = code.indexOf('init().catch');
if (origInit < 0) {
  console.log('[FAIL] app.js 找不到 init() 启动代码');
  process.exit(1);
}

try {
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox, { filename: 'app.js' });
} catch (err) {
  console.log(`[FAIL] app.js 执行抛错：${err.message}`);
  process.exit(1);
}

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

let dayStage = null;
fetch(BASE + '/api/day-plan?include_passed=1')
  .then((res) => res.json())
  .then((json) => { dayStage = (json.data && json.data.stage) || null; })
  .catch(() => { dayStage = null; });

setTimeout(() => {
  let pass = 0, fail = 0;
  const run = (name, fn) => { if (check(name, fn)) pass++; else fail++; };

  run('未触发初始化失败横幅', () => {
    if (bodyPrepend.length) {
      const banner = bodyPrepend[0];
      const text = (banner && (banner.innerHTML || banner.textContent)) || '';
      throw new Error(`页面被替换为错误提示：${String(text).replace(/<[^>]*>/g, '').slice(0, 120)}`);
    }
  });

  run('顶部今日速览已渲染', () => {
    if (!htmlWrites['todaybar'] || !htmlWrites['todaybar'].includes('今日目标')) {
      throw new Error(`todaybar 内容为空：${(htmlWrites['todaybar'] || '').slice(0, 60)}`);
    }
  });

  run('推荐列表已渲染', () => {
    const list = byId['plan-list'];
    if (!list || list.children.length === 0) {
      throw new Error(`plan-list 没有卡片（children=${list ? list.children.length : '无节点'}）`);
    }
  });

  run('今日计划已渲染', () => {
    const options = byId['day-options'];
    const hint = (byId['day-hint'] && byId['day-hint'].textContent) || '';
    const cards = options ? options.children.length : 0;
    if (cards === 0 && !hint) {
      throw new Error('今日计划既没有方案卡片也没有提示文字');
    }
    if (hint && !/[0-9]/.test(hint)) {
      throw new Error(`今日计划提示异常：${hint.slice(0, 60)}`);
    }
  });

  run('未出现 NaN / undefined / [object', () => {
    const walk = (node, where) => {
      if (!node) return;
      const chunks = [String(node._html || ''), String(node.textContent || '')];
      for (const chunk of chunks) {
        for (const bad of ['NaN', 'undefined', '[object']) {
          if (chunk.includes(bad)) {
            throw new Error(`${where} 里出现了 ${bad}：${chunk.slice(0, 120)}`);
          }
        }
      }
      (node.children || []).forEach((child, i) => walk(child, `${where} > child[${i}]`));
    };
    for (const [id, node] of Object.entries(byId)) walk(node, `#${id}`);
  });

  run('渲染过程中没有未捕获错误', () => {
    if (fatal) throw new Error(fatal);
  });

  run('没有出现"加载失败"提示', () => {
    const toastText = String((byId['toast'] && byId['toast'].textContent) || '');
    if (toastText.includes('失败')) throw new Error(`页面提示：${toastText}`);
  });

  run('今日计划按阶段渲染出内容', () => {
    const hint = String((byId['day-hint'] && byId['day-hint'].textContent) || '');
    if (dayStage === 'options') {
      const cards = byId['day-options'] ? byId['day-options'].children.length : 0;
      if (cards < 1) throw new Error(`未选方案阶段应渲染 A/B/C 卡片，实际 ${cards} 张（提示：${hint.slice(0, 60)}）`);
      const first = byId['day-options'].children[0];
      const text = String((first && first._html) || '');
      if (text && !/方案/.test(text) && !/option-card/.test(text)) {
        throw new Error(`方案卡片内容异常：${text.slice(0, 100)}`);
      }
    } else if (dayStage === 'chosen') {
      const cards = byId['day-meals'] ? byId['day-meals'].children.length : 0;
      if (cards < 1) throw new Error(`已选方案阶段应渲染餐次卡片，实际 ${cards} 张`);
    }
  });

  console.log('-'.repeat(46));
  console.log(`通过 ${pass} 项，失败 ${fail} 项`);
  process.exit(fail ? 1 : 0);
}, 2500);
