/* app.js —— 路由、导航、模式切换、顶栏状态 */
(function (global) {
  'use strict';

  const NAV = [
    ['总览', [
      ['/', '首页总览', 'overview'],
      ['/market', 'BTC 行情', 'market'],
      ['/timeline', '市场事件时间线', 'timeline'],
    ]],
    ['市场分析', [
      ['/cycle', '市场周期', 'cycle'],
      ['/valuation', '估值分析', 'valuation'],
      ['/risk', '风险监测', 'risk'],
      ['/regime', '综合市场状态', 'regime'],
      ['/forecast', '概率预测', 'forecast'],
    ]],
    ['链上与资金', [
      ['/onchain', '链上数据', 'onchain'],
      ['/flows', '资金流', 'flows'],
      ['/etf', 'ETF 资金流', 'etf'],
      ['/derivatives', '衍生品', 'derivatives'],
      ['/options', '期权市场', 'options'],
    ]],
    ['宏观与情绪', [
      ['/macro', '宏观环境', 'macro'],
      ['/sentiment', '市场情绪', 'sentiment'],
    ]],
    ['回测与研究', [
      ['/replay', '历史回放', 'replay'],
      ['/backtest', '策略回测', 'backtest'],
      ['/strategies', '策略实验室', 'strategies'],
      ['/models', '模型实验室', 'models'],
      ['/indicators', '指标字典', 'indicatorsPage'],
    ]],
    ['我的资金', [
      ['/plans', '我的资金计划', 'plans'],
      ['/holdings', '我的资产', 'holdings'],
      ['/dca', '定投模拟', 'dca'],
    ]],
    ['智能监测', [
      ['/alerts', '智能预警中心', 'alerts'],
      ['/rules', '监测规则', 'rules'],
      ['/alerts-events', '提醒记录', 'alertsEvents'],
      ['/alerts-backtest', '规则回测', 'alertsBacktest'],
      ['/alerts-settings', '邮件设置', 'alertsSettings'],
      ['/alerts-channels', '通知渠道', 'alertsChannels'],
    ]],
    ['系统', [
      ['/settings', '系统设置', 'settings'],
      ['/providers', '数据源中心', 'providers'],
      ['/quality', '数据质量', 'quality'],
      ['/jobs', '系统任务', 'jobs'],
      ['/assistant', 'AI 研究助手', 'assistant'],
      ['/admin', '后台管理', 'admin'],
    ]],
  ];

  // 扁平路由表：path -> {render, group}
  const ROUTES = {};
  NAV.forEach(([group, items]) => items.forEach(([path, title, fn]) => {
    ROUTES[path] = { title, fn, group };
  }));

  function current() {
    const raw = (location.hash || '#/').replace(/^#/, '');
    const [path, qs] = raw.split('?');
    return { path: path || '/', params: new URLSearchParams(qs || '') };
  }

  function renderNav(activePath) {
    const el = document.getElementById('sidebar');
    el.innerHTML = NAV.map(([group, items]) => `
      <div class="nav-group">
        <div class="nav-group-title">${group}</div>
        ${items.map(([path, title]) =>
      `<a class="nav-item${path === activePath ? ' active' : ''}" href="#${path}">${title}</a>`).join('')}
      </div>`).join('');
  }

  let currentRender = null;

  async function route() {
    const { path, params } = current();
    const r = ROUTES[path];
    const content = document.getElementById('content');
    if (!r) {
      renderNav('');
      content.innerHTML = `<div class="empty">页面不存在：${esc(path)}</div>`;
      return;
    }
    renderNav(path);
    document.title = `${r.title} · BTC 研究平台`;
    content.innerHTML = `<div class="loading"><span class="spin"></span> 正在加载「${r.title}」…</div>`;
    content.scrollTop = 0;

    const modules = [
      window.PM && window.PM[r.fn], window.PA && window.PA[r.fn],
      window.PR && window.PR[r.fn], window.PP && window.PP[r.fn], window.PS && window.PS[r.fn],
      window.PA_ALERTS && window.PA_ALERTS[r.fn]
    ].filter(Boolean);
    const render = modules[0];

    if (!render) {
      content.innerHTML = `<div class="empty">该页面尚未实现</div>`;
      return;
    }
    const token = ++currentRender;
    try {
      const html = await render(params);
      if (token !== currentRender) return;   // 防止慢请求覆盖新页面
      content.innerHTML = html;
    } catch (err) {
      if (token !== currentRender) return;
      content.innerHTML = C.unavailable(String(err && err.message || err)) +
        C.rawBlock('页面渲染异常时的原始上下文', { path, params: Object.fromEntries(params) });
    }
  }

  async function refreshTopbar() {
    try {
      const p = await C.get('/market/price');
      const box = document.getElementById('topbar-status');
      const dot = box.querySelector('.dot');
      const price = document.getElementById('price-mini');
      const src = document.getElementById('src-mini');
      if (!p || !p.available) {
        dot.className = 'dot dot-bad';
        price.textContent = '--';
        src.textContent = '数据源不可用';
        return;
      }
      const s = p.source || {};
      price.textContent = '$' + C.nf(p.price);
      src.textContent = `${s.display_name || s.provider} · ${C.QUALITY_CN[s.quality] || s.quality || ''}`;
      dot.className = 'dot ' + (p.stale ? 'dot-warn' : (s.used_fallback ? 'dot-warn' : 'dot-ok'));
      dot.title = p.stale ? '降级运行：显示最后可信数据' : '实时数据';
    } catch (_) { /* 顶栏状态失败不影响页面 */ }
  }

  function bindMode() {
    document.querySelectorAll('.mode-btn').forEach(b => {
      b.onclick = () => {
        C.State.setMode(b.dataset.mode);
        App.reload();
      };
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshTopbar();
  });

  const App = {
    routes: ROUTES,
    reload() { location.hash = location.hash; route(); },
    route,
    start() {
      C.State.setMode(C.State.mode);
      bindMode();
      window.addEventListener('hashchange', route);
      document.getElementById('btn-refresh').onclick = () => App.reload();
      document.getElementById('footer-info').textContent =
        `共 ${Object.keys(ROUTES).length} 个页面 · 数据源与应用解耦`;
      route();
      refreshTopbar();
      setInterval(refreshTopbar, 60000);
    }
  };

  global.App = App;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', App.start);
  } else {
    App.start();
  }
})(window);
