/* pages-research.js —— 历史回放 / 策略回测 / 策略实验室 / 模型实验室 / AI 研究助手 / 指标字典 */
(function (global) {
  'use strict';
  const P = {};
  const { get, post, card, stat, note, rawBlock, table, nf, nf0, pct, btc, cls, sign,
    tsLocal, dateOf, esc, unavailable, toast } = C;

  // ============================================================ 历史回放
  P.replay = async function (params) {
    const date = params.get('date') || '2021-11-10';
    const perspective = params.get('perspective') || 'then';
    let d;
    try { d = await get('/research/replay', { date, perspective }); }
    catch (e) { d = null; }

    let html = `<div class="page-head"><h2 class="page-title">历史回放</h2>
      <span class="page-sub">回到某一天，只看<b>当时能看到的信息</b>做判断</span></div>`;

    html += `<div class="card"><div class="form-row">
      <label>回放日期</label><input class="inp" type="date" id="rp-date" value="${esc(date)}">
      <label>视角</label>
      <select class="inp" id="rp-p">
        <option value="then"${perspective === 'then' ? ' selected' : ''}>当时视角（只用当时已知数据）</option>
        <option value="aftermath"${perspective === 'aftermath' ? ' selected' : ''}>事后视角（额外显示后续走势）</option>
      </select>
      <button class="btn btn-primary" id="rp-go">开始回放</button>
      </div>
      ${note('warn', '<b>严格禁止未来信息：</b>回放会把数据截断到目标日期，ATH、分位数、均线等指标全部按「当时可见窗口」重新计算，' +
        '而不是用全样本的结果倒推。')}
      </div>`;

    requestAnimationFrame(() => {
      const go = document.getElementById('rp-go');
      const dt = document.getElementById('rp-date');
      const pp = document.getElementById('rp-p');
      if (go) go.onclick = () => location.hash = `#/replay?date=${dt.value}&perspective=${pp.value}`;
    });

    if (!d) { html += unavailable('回放服务暂时不可用'); return html; }
    if (!d.available) {
      html += unavailable(d.message || d.reason || '该日期无法回放', rawBlock('接口原始返回', d));
      return html;
    }

    const ctx = d.context || d.snapshot || {};
    const ind = d.indicators || {};
    html += `<div class="card"><h3 class="card-title">${esc(date)} 当天的读数</h3><div class="grid g4">
      ${stat('当天价格', nf(ctx.price || ind.price), '')}
      ${stat('距历史高点', pct(ind.drawdown ?? ctx.drawdown), '按当时可见窗口计算')}
      ${stat('价格分位', nf(ind.price_percentile, 1) + '%', '历史上低于该水平的比例')}
      ${stat('RSI(14)', nf(ind.rsi14, 1), '')}
      </div>
      ${sourceNote(d)}
      </div>`;

    const availability = d.availability || d.data_availability || null;
    if (availability) {
      html += card('当时各模块数据可得性',
        table(['模块', '是否可用'], Object.entries(availability).map(([k, v]) =>
          [esc(k), (v === true || v === 'available') ? '<span class="badge b-ok">可用</span>'
            : `<span class="badge b-mute">${esc(String(v))}</span>`])));
    }

    if (perspective === 'aftermath') {
      const after = d.aftermath || d.future || {};
      html += `<div class="card"><h3 class="card-title">事后才知道的事 <span class="hint">这部分在当时是看不到的</span></h3>`;
      const rows = Object.entries(after).filter(([, v]) => typeof v === 'number');
      html += rows.length ? table(['期限', '此后涨跌'], rows.map(([k, v]) =>
        [esc(k), `<span class="${cls(v)}">${pct(v)}</span>`])) : `<div class="empty">暂无</div>`;
      if (d.warning) html += note('warn', esc(d.warning));
      html += `</div>`;
    }

    if (d.timeline && d.timeline.length) {
      html += card('当时已知的重大事件', `<ul style="margin:0;padding-left:18px">` +
        d.timeline.map(t => `<li>${esc(t.date)}：${esc(t.title)}</li>`).join('') + `</ul>`);
    }
    html += rawBlock('回放接口原始返回', d);
    return html;
  };

  function sourceNote(d) {
    if (!d.data_note && !d.note && !d.basis) return '';
    return note('info', esc(d.data_note || d.note || d.basis));
  }

  // ============================================================ 策略回测
  P.backtest = async function (params) {
    const strategies = await get('/research/strategies').catch(() => null);
    const runs = await get('/research/backtest/runs', { limit: 20 }).catch(() => null);
    const list = (strategies && strategies.strategies) || [];

    let html = `<div class="page-head"><h2 class="page-title">策略回测</h2>
      <span class="page-sub">用真实本地历史数据，逐日推进，杜绝未来数据泄漏</span></div>`;

    html += `<div class="card"><h3 class="card-title">新建回测</h3>
      <div class="form-row">
        <label>策略</label>
        <select class="inp" id="bt-strategy">${list.map(s =>
      `<option value="${esc(s.code || s.strategy)}">${esc(s.name_cn || s.name || s.code)}</option>`).join('')}</select>
        <label>开始</label><input class="inp" type="date" id="bt-start" value="2018-01-01">
        <label>结束</label><input class="inp" type="date" id="bt-end">
      </div>
      <div class="form-row">
        <label>频率</label>
        <select class="inp" id="bt-freq">
          <option value="monthly">每月</option><option value="weekly">每周</option>
        </select>
        <label>每期金额</label><input class="inp" type="number" id="bt-amount" value="2000" min="1" step="100">
        <label>初始本金</label><input class="inp" type="number" id="bt-capital" value="0" min="0" step="100">
        <label>验证模式</label>
        <select class="inp" id="bt-mode">
          <option value="insample">样本内</option>
          <option value="oos">样本外（推荐）</option>
        </select>
        <button class="btn btn-primary" id="bt-run">运行回测</button>
      </div>
      ${note('info', '样本外（OOS）会把区间切成训练段 + 测试段并把两段结果都列出来；' +
        '只看训练段的结果没有参考价值，因为它可能只是拟合了历史。')}
      </div>`;

    html += `<div class="card"><h3 class="card-title">策略说明</h3>` +
      table(['策略', '通俗说明', '关键参数'], list.map(s => [
        esc(s.name_cn || s.name || s.code),
        `<span class="muted">${esc(s.description || '')}</span>`,
        `<span class="mono" style="font-size:11px">${esc(JSON.stringify(s.default_params || {}))}</span>`]))
      + `</div>`;

    if (runs && runs.runs && runs.runs.length) {
      html += card('历史回测记录 <span class="hint">每次结果都绑定模型版本与数据版本</span>',
        table(['名称', '策略', '区间', '验证模式', '模型版本', '运行时间'],
          runs.runs.map(r => [esc(r.name), esc(r.strategy_code),
          `${esc(r.start_date)} ~ ${esc(r.end_date)}`, esc(r.validation_mode),
          esc(r.model_version), tsLocal(r.created_at)])));
    }

    html += `<div id="bt-result"></div>`;

    requestAnimationFrame(() => {
      const btn = document.getElementById('bt-run');
      if (!btn) return;
      btn.onclick = async () => {
        const body = {
          strategy_code: document.getElementById('bt-strategy').value,
          start_date: document.getElementById('bt-start').value,
          end_date: document.getElementById('bt-end').value || null,
          contribution_frequency: document.getElementById('bt-freq').value,
          monthly_contribution: document.getElementById('bt-freq').value === 'monthly' ? +document.getElementById('bt-amount').value : 0,
          weekly_contribution: document.getElementById('bt-freq').value === 'weekly' ? +document.getElementById('bt-amount').value : 0,
          initial_capital: +document.getElementById('bt-capital').value,
          validation_mode: document.getElementById('bt-mode').value,
        };
        btn.disabled = true; btn.textContent = '回测中…';
        const box = document.getElementById('bt-result');
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在用真实历史数据回测…</div>`;
        try {
          const r = await post('/research/backtest', body);
          btn.disabled = false; btn.textContent = '运行回测';
          box.innerHTML = r.ok ? renderBacktest(r) : unavailable(r.message || '回测失败', rawBlock('原始返回', r));
        } catch (e) {
          btn.disabled = false; btn.textContent = '运行回测';
          box.innerHTML = unavailable(String(e.message || e), '');
        }
      };
    });
    return html;
  };

  function renderBacktest(r) {
    const v = r.validation || null;
    let h = `<div class="card"><h3 class="card-title">回测结果 <span class="hint">模型版本 ${esc(r.model_version || '')}</span></h3>
      <div class="grid g4">
      ${stat('投入本金', nf0(r.total_invested) + ' 元', '')}
      ${stat('最终资产', nf0(r.final_value) + ' 元', '', cls(r.net_profit))}
      ${stat('净收益', nf0(r.net_profit) + ' 元', pct(r.roi), cls(r.net_profit))}
      ${stat('年化 CAGR', pct(r.cagr), '', cls(r.cagr))}
      ${stat('最大回撤', pct(r.max_drawdown), '', 'down')}
      ${stat('回撤持续', nf0((r.max_drawdown_window || {}).duration_days) + ' 天', '')}
      ${stat('回撤恢复', nf0(r.recovery_days) + ' 天', r.recovery_days ? '' : '截至区间末仍未恢复')}
      ${stat('Sharpe / Sortino', `${nf(r.sharpe)} / ${nf(r.sortino)}`, '年化')}
      ${stat('波动率', pct(r.volatility), '年化')}
      ${stat('平均成本', nf0(r.avg_cost) + ' 元', `持有 ${nf(r.btc_amount, 6)} BTC`)}
      ${stat('手续费合计', nf0(r.total_fees) + ' 元', '')}
      ${stat('买入持有对照', nf0((r.buy_and_hold || {}).final_value) + ' 元', '同本金一次性买入')}
      </div>
      ${(r.warnings || []).length ? card('必须知道的局限',
        `<ul style="margin:0;padding-left:18px">${r.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>`) : ''}
      ${v ? note('info', `<b>样本外验证</b>：训练 ${esc(v.train_period)}（${v.train_days} 天） / 测试 ${esc(v.test_period)}（${v.test_days} 天）。` +
        `${esc(v.note || '')}`) : ''}
      </div>`;

    if (r.equity_curve && r.equity_curve.length) {
      h += `<div class="card"><h3 class="card-title">资产曲线 vs 累计投入</h3>${CH.h('c-bt-eq', 'chart-lg', 420)}</div>`;
      requestAnimationFrame(() => {
        const pts = r.equity_curve;
        CH.lineChart('c-bt-eq', pts.map(q => C.dateOf(q[0])), [
          { name: '资产总值', data: pts.map(q => Math.round(q[1])), area: true, color: '#1a73e8' }
        ], { scale: true, yName: '元' });
      });
    }
    const yr = (r.best_year && r.best_year.all) ? r.best_year.all : null;
    if (yr) {
      const labels = Object.keys(yr), vals = labels.map(k => yr[k]);
      h += card('分年度收益', CH.h('c-bt-yr', 'chart-sm', 240));
      requestAnimationFrame(() => CH.barChart('c-bt-yr', labels, vals));
    }
    h += rawBlock('回测原始返回（含完整月度收益序列）', {
      metrics: { final_value: r.final_value, roi: r.roi, cagr: r.cagr, max_drawdown: r.max_drawdown },
      run_id: r.run_id, model_version: r.model_version
    });
    return h;
  }

  // ============================================================ 策略实验室
  P.strategies = async function () {
    const [d, pt] = await Promise.all([
      get('/research/strategies').catch(() => null),
      get('/portfolio/strategies').catch(() => null)
    ]);
    const list = (d && d.strategies) || [];
    let html = `<div class="page-head"><h2 class="page-title">策略实验室</h2>
      <span class="page-sub">每种策略都写清楚：它在什么情况下有效、在什么情况下会失效</span></div>`;
    if (!list.length) { html += unavailable('策略清单为空'); return html; }
    list.forEach(s => {
      html += card(`${esc(s.name_cn || s.name || s.code)} <span class="hint">${esc(s.code || '')}</span>`,
        `<div class="explain">${esc(s.description || '')}</div>` +
        (s.default_params ? `<dl class="kv">${Object.entries(s.default_params).map(([k, v]) =>
          `<dt>${esc(k)}</dt><dd>${esc(String(v))}</dd>`).join('')}</dl>` : '') +
        (s.applicable_conditions || s.suitable_for
          ? `<div class="note note-info"><b>在什么情况下有效</b><br>${esc(s.applicable_conditions || s.suitable_for)}</div>` : '') +
        (s.failure_conditions || s.not_suitable_for
          ? `<div class="note note-warn"><b>在什么情况下会失效</b><br>${esc(s.failure_conditions || s.not_suitable_for)}</div>` : '')
        + rawBlock('策略定义原始数据', s));
    });
    html += note('warn', '回测结果只说明<b>在过去这段行情</b>下发生了什么。' +
      '任何参数都可能只是拟合历史；请以样本外表现和你自己的风险承受力为准。');
    return html;
  };

  // ============================================================ 模型实验室
  P.models = async function () {
    const d = await get('/research/models').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">模型实验室</h2>
      <span class="page-sub">每个模型都标注版本、适用条件与已知局限</span></div>`;
    const models = (d && (d.models || d.list)) || [];
    if (!models.length) {
      html += unavailable('模型版本记录为空。请先执行 btcctl.py init-db 初始化数据库（会同时登记全部模型版本档案）');
      html += rawBlock('接口原始返回', d);
      return html;
    }
    html += `<div class="card"><div class="muted" style="font-size:13px">${esc(d.disclaimer || '')}</div></div>`;
    html += card('模型清单', table(
      ['模型', '类型', '开发区间', '样本外检验', '当前启用'],
      models.map(m => [
        `<b>${esc(m.name)}</b><br><span class="mono muted" style="font-size:11px">${esc(m.code)}</span>`,
        esc(m.kind || ''),
        `<span class="muted">${esc(m.dev_period || '未登记')}</span>`,
        (() => {
          const v = m.metrics || {};
          const done = v.oos_validated === true;
          const wf = v.walk_forward === true;
          return `${done ? '<span class="badge b-ok">已完成</span>' : '<span class="badge b-warn">未进行</span>'}` +
            (wf ? ' <span class="badge">支持 Walk-Forward</span>' : '');
        })(),
        m.active ? '<span class="badge b-ok">运行中</span>' : '<span class="badge">停用</span>']),
      'table-sm'));
    // 每个模型完整档案：参数 + 适用条件 + 失效条件
    models.forEach(m => {
      const rows = [];
      rows.push(['参数', `<pre class="mono" style="margin:0;white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(m.params || {}, null, 2))}</pre>`]);
      rows.push(['测试区间', `<span class="muted">${esc(m.test_period || '未进行')}</span>`]);
      if ((m.weights || []).length) {
        rows.push(['因子权重', table(['因子', '权重', '说明'],
          m.weights.map(w => [esc(w.factor), String(w.weight), `<span class="muted">${esc(w.rationale || '')}</span>`]))]);
      }
      const validLines = Object.entries(m.metrics || {}).map(([k, v]) => `${k}: ${v}`).join('\n');
      rows.push(['检验状态', `<pre class="mono" style="margin:0;white-space:pre-wrap;font-size:12px">${esc(validLines)}</pre>`]);
      rows.push(['适用与失效条件', `<pre class="mono" style="margin:0;white-space:pre-wrap;font-size:12px;line-height:1.6">${esc(m.notes || '')}</pre>`]);
      html += `<details class="raw"><summary>${esc(m.name)}（${esc(m.code)}）— 展开完整模型档案</summary>
        ${table(['项目', '内容'], rows, 'table-sm')}</details>`;
    });
    html += rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 指标字典
  P.indicatorsPage = async function () {
    const d = await get('/research/indicators/dictionary').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">指标字典</h2>
      <span class="page-sub">每个指标都说人话：它是什么、怎么用、有什么坑</span></div>`;
    const items = (d && (d.indicators || d.items)) || [];
    if (!items.length) { html += unavailable('指标字典为空'); return html; }
    html += card('全部指标', table(['指标', '通俗解释', '怎么用', '常见误区', '数据来源'],
      items.map(i => [
        `<b>${esc(i.name_cn || i.code)}</b><br><span class="mono muted" style="font-size:11px">${esc(i.code)}</span>`,
        `<span class="muted">${esc(i.meaning_cn || i.description || '')}</span>`,
        `<span class="muted">${esc(i.how_to_use || '')}</span>`,
        `<span class="muted">${esc(i.pitfalls || i.caveat || '')}</span>`,
        i.requires_paid_provider ? '<span class="badge b-warn">需付费数据源</span>' : '<span class="badge b-ok">免费可得</span>'])));
    html += rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ AI 研究助手
  P.assistant = async function () {
    let html = `<div class="page-head"><h2 class="page-title">AI 研究助手</h2>
      <span class="page-sub">只解释数据，不拍脑袋预测；每条结论都标注来源</span></div>`;
    html += `<div class="card">
      <div class="form-row">
        <label>你的问题</label>
        <input class="inp" id="ai-q" style="flex:1;min-width:280px" placeholder="例如：现在估值算高吗？RSI 怎么看？为什么数据源显示不可用？">
        <button class="btn btn-primary" id="ai-ask">提问</button>
      </div>
      <div class="form-row">
        ${['现在估值算高吗', '当前处在什么周期阶段', '现在的风险在哪里', 'MVRV 是什么意思',
        '为什么 ETF 数据没有显示', '回测结果可信吗'].map(q =>
        `<button class="btn ai-preset" data-q="${esc(q)}">${esc(q)}</button>`).join(' ')}
      </div>
      <div id="ai-result"></div>
      </div>`;

    requestAnimationFrame(() => {
      const ask = async (q) => {
        const box = document.getElementById('ai-result');
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在基于真实数据整理答案…</div>`;
        try {
          const r = await post('/assistant/ask', { question: q });
          box.innerHTML = renderAnswer(r);
        } catch (e) {
          box.innerHTML = unavailable(String(e.message || e));
        }
      };
      const btn = document.getElementById('ai-ask');
      const inp = document.getElementById('ai-q');
      if (btn) btn.onclick = () => inp.value && ask(inp.value);
      if (inp) inp.onkeydown = e => { if (e.key === 'Enter' && inp.value) ask(inp.value); };
      document.querySelectorAll('.ai-preset').forEach(b => {
        b.onclick = () => { inp.value = b.dataset.q; ask(b.dataset.q); };
      });
    });
    return html;
  };

  function renderAnswer(r) {
    let h = `<div class="card"><h3 class="card-title">回答 <span class="hint">${esc(r.engine || '')}</span></h3>`;
    h += `<div class="explain">${esc(r.answer || r.summary || '')}</div>`;
    const sec = (title, arr, clsName) => {
      if (!arr || !arr.length) return '';
      return card(title, `<ul style="margin:0;padding-left:18px">` +
        arr.map(x => {
          const text = typeof x === 'string' ? x : (x.text || x.point || JSON.stringify(x));
          const src = typeof x === 'object' && x.source ? `<span class="muted"> — ${esc(x.source)}</span>` : '';
          return `<li>${esc(text)}${src}</li>`;
        }).join('') + `</ul>`, clsName);
    };
    h += sec('事实（来自本地真实数据）', r.facts);
    h += sec('统计结论', r.statistics || r.stats);
    h += sec('主观判断（请谨慎对待）', r.judgments);
    h += sec('反向证据', r.counter_evidence);
    h += sec('不确定性与局限', r.uncertainties);
    if (r.data_sources) h += card('本次回答用到的数据来源',
      `<div class="source-block"><div class="source-line">${esc(JSON.stringify(r.data_sources))}</div></div>`);
    if (r.disclaimer) h += note('warn', esc(r.disclaimer));
    h += rawBlock('助手原始输出', r) + `</div>`;
    return h;
  }

  global.PR = P;
})(window);
