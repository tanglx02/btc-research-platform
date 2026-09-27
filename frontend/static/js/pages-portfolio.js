/* pages-portfolio.js —— 我的计划 / 我的资产 / 定投模拟 */
(function (global) {
  'use strict';
  const P = {};
  const { get, post, del, patch, card, stat, note, rawBlock, table, nf, nf0, pct, cls, btc,
    tsLocal, dateOf, esc, el, unavailable, toast } = C;

  const FREQ_CN = { monthly: '每月', weekly: '每周', none: '不定期' };

  // ============================================================ 我的计划
  P.plans = async function () {
    const d = await get('/portfolio/plans').catch(() => null);
    const plans = (d && d.plans) || [];
    let html = `<div class="page-head"><h2 class="page-title">我的资金计划</h2>
      <span class="page-sub">规则完全由你自己定义；系统只做计算与模拟，<b>不会自动下单</b></span></div>`;

    html += `<div class="card"><h3 class="card-title">新建计划</h3>
      <div class="form-row">
        <label>计划名称</label><input class="inp" id="pl-name" value="我的 BTC 定投" style="min-width:180px">
        <label>频率</label>
        <select class="inp" id="pl-freq"><option value="monthly">每月</option><option value="weekly">每周</option></select>
        <label>每期金额</label><input class="inp" type="number" id="pl-amount" value="2000" min="1" step="100">
        <label>初始本金</label><input class="inp" type="number" id="pl-capital" value="0" min="0" step="100">
      </div>
      <div class="form-row">
        <label>开始日期</label><input class="inp" type="date" id="pl-start" value="2018-01-01">
        <label>单期上限</label><input class="inp" type="number" id="pl-max" value="0" min="0" step="100" title="0 表示不限制">
        <label>现金留存</label><input class="inp" type="number" id="pl-reserve" value="0" min="0" step="100">
        <label>策略</label><select class="inp" id="pl-strategy">
          <option value="dca_fixed">固定金额定投</option>
          <option value="dca_drawdown">回撤加仓</option>
          <option value="dca_valuation">估值加仓</option>
          <option value="dca_risk">风险调整</option>
          <option value="dca_cycle">周期调整</option>
        </select>
        <button class="btn btn-primary" id="pl-create">创建</button>
      </div>
      ${note('info', '「单期上限」是给你的计划加一道保险：无论策略建议加多少倍，都不会超过这个金额。')}
      </div>`;

    if (!plans.length) {
      html += `<div class="card"><div class="empty">还没有计划。创建后可以在这里查看下次投入建议、并对计划做历史回测。</div></div>`;
    } else {
      html += card('已有计划', table(['名称', '频率', '每期金额', '初始本金', '策略', '创建时间', '操作'],
        plans.map(p => [
          `<a href="#/holdings?plan_id=${p.id}">${esc(p.name)}</a>`,
          esc(FREQ_CN[p.contribution_frequency] || p.contribution_frequency),
          `<span class="mono">${nf0(p.weekly_contribution || p.monthly_contribution)}</span>`,
          `<span class="mono">${nf0(p.initial_capital)}</span>`,
          esc(p.strategy_code), tsLocal(p.created_at),
          `<button class="btn btn-del" data-id="${p.id}">删除</button>`])));
    }

    requestAnimationFrame(() => {
      const btn = document.getElementById('pl-create');
      if (btn) btn.onclick = async () => {
        const freq = document.getElementById('pl-freq').value;
        const body = {
          name: document.getElementById('pl-name').value,
          contribution_frequency: freq,
          monthly_contribution: freq === 'monthly' ? +document.getElementById('pl-amount').value : 0,
          weekly_contribution: freq === 'weekly' ? +document.getElementById('pl-amount').value : 0,
          initial_capital: +document.getElementById('pl-capital').value,
          start_date: document.getElementById('pl-start').value,
          max_single_contribution: +document.getElementById('pl-max').value,
          cash_reserve: +document.getElementById('pl-reserve').value,
          strategy_code: document.getElementById('pl-strategy').value,
        };
        try { await post('/portfolio/plans', body); toast('计划已创建'); App.reload(); }
        catch (e) { toast(String(e.message || e), true); }
      };
      document.querySelectorAll('.btn-del').forEach(b => {
        b.onclick = async () => {
          if (!confirm('确认删除该计划？此操作不可撤销。')) return;
          try { await del('/portfolio/plans/' + b.dataset.id); toast('已删除'); App.reload(); }
          catch (e) { toast(String(e.message || e), true); }
        };
      });
    });
    return html;
  };

  // ============================================================ 我的资产（含下次投入建议）
  P.holdings = async function (params) {
    const planId = params.get('plan_id');
    const [plansRes, hd] = await Promise.all([
      get('/portfolio/plans').catch(() => null),
      get('/portfolio/holdings', { plan_id: planId }).catch(() => null)
    ]);
    const plans = (plansRes && plansRes.plans) || [];
    const pid = planId || (plans[0] && plans[0].id);

    let html = `<div class="page-head"><h2 class="page-title">我的资产</h2>
      <span class="page-sub">持仓、平均成本与浮盈亏都来自你自己录入的真实交易</span>
      ${plans.length ? `<div class="right"><select class="inp" id="sel-plan">${plans.map(p =>
        `<option value="${p.id}"${String(p.id) === String(pid) ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>` : ''}
      </div>`;

    requestAnimationFrame(() => {
      const s = document.getElementById('sel-plan');
      if (s) s.onchange = () => location.hash = `#/holdings?plan_id=${s.value}`;
    });

    const h = (hd && (hd.holdings || hd)) || null;
    const hasHoldings = h && (h.total_btc !== undefined);

    if (hasHoldings) {
      html += `<div class="card"><h3 class="card-title">持仓概览</h3><div class="grid g4">
        ${stat('持有 BTC', nf(h.total_btc, 8), '')}
        ${stat('平均成本', nf0(h.avg_cost) + ' 元', '')}
        ${stat('累计投入', nf0(h.total_invested) + ' 元', '')}
        ${stat('当前市值', nf0(h.current_value) + ' 元', '', cls(h.unrealized_pnl))}
        ${stat('浮动盈亏', nf0(h.unrealized_pnl) + ' 元', pct(h.unrealized_pnl_pct), cls(h.unrealized_pnl))}
        ${stat('已实现盈亏', nf0(h.realized_pnl || 0) + ' 元', '', cls(h.realized_pnl))}
        ${stat('手续费合计', nf0(h.total_fees || 0) + ' 元', '')}
        ${stat('参考价格来源', esc(h.price_provider || ''), h.price_stale ? '降级数据' : '实时')}
        </div>
        ${h.price_stale ? note('warn', '当前价格来自最后一次成功获取的数据（数据源暂不可用），没有用估算值代替。') : ''}
        </div>`;
    } else {
      html += `<div class="card"><div class="empty">还没有持仓记录。在下方录入你的真实交易后，这里会自动计算成本与盈亏。</div></div>`;
    }

    // 交易录入
    html += `<div class="card"><h3 class="card-title">录入一笔交易</h3>
      <div class="form-row">
        <label>日期</label><input class="inp" type="date" id="tx-date">
        <label>方向</label><select class="inp" id="tx-side"><option value="buy">买入</option><option value="sell">卖出</option></select>
        <label>单价</label><input class="inp" type="number" id="tx-price" step="0.01" placeholder="例如 84000">
        <label>数量(BTC)</label><input class="inp" type="number" id="tx-btc" step="0.00000001" placeholder="例如 0.01">
        <label>金额(法币)</label><input class="inp" type="number" id="tx-fiat" step="0.01" placeholder="留空则按单价×数量自动算">
        <label>备注</label><input class="inp" id="tx-note">
        <button class="btn btn-primary" id="tx-add">保存</button>
      </div>
      ${note('info', '只记录你已经真实发生的交易。系统会用它计算平均成本，但不会替你下单，也不会给出「该买了」的结论。')}
      </div>`;

    const txs = await get('/portfolio/transactions', { plan_id: pid, limit: 200 }).catch(() => null);
    const rows = (txs && txs.transactions) || [];
    if (rows.length) {
      html += card('交易明细', table(['日期', '方向', '单价', '数量(BTC)', '金额', '手续费', '备注', '操作'],
        rows.map(t => [
          esc(t.date),
          t.side === 'buy' ? '<span class="badge b-ok">买入</span>' : '<span class="badge b-bad">卖出</span>',
          `<span class="mono">${nf(t.price)}</span>`,
          `<span class="mono">${nf(t.amount_btc, 8)}</span>`,
          `<span class="mono">${nf0(t.amount_fiat)}</span>`,
          `<span class="mono">${nf(t.fee || 0)}</span>`,
          `<span class="muted">${esc(t.note || '')}</span>`,
          `<button class="btn btn-txdel" data-id="${t.id}">删除</button>`])));
    }

    // 下次投入建议
    if (pid) {
      const nx = await get(`/portfolio/plans/${pid}/next`).catch(() => null);
      if (nx && nx.available) {
        html += `<div class="card"><h3 class="card-title">下次投入建议 <span class="hint">按你自己设定的规则计算</span></h3>
          <div class="grid g4">
          ${stat('建议日期', esc(nx.next_date || ''), '')}
          ${stat('基准金额', nf0(nx.base_amount) + ' 元', '')}
          ${stat('规则倍率', nf(nx.multiplier, 2) + ' ×', '')}
          ${stat('建议金额', nf0(nx.suggested_amount) + ' 元', '', cls(nx.multiplier - 1))}
          </div>
          ${(nx.rule_explanation || nx.reason) ? `<div class="explain">${esc(nx.rule_explanation || nx.reason)}</div>` : ''}
          ${nx.disclaimer ? note('warn', esc(nx.disclaimer)) : ''}
          ${rawBlock('原始返回', nx)}
          </div>`;
      } else if (nx) {
        html += card('下次投入建议', unavailable(nx.message || '暂无法计算'));
      }
    }

    requestAnimationFrame(() => {
      const add = document.getElementById('tx-add');
      if (add) add.onclick = async () => {
        const body = {
          date: document.getElementById('tx-date').value || undefined,
          side: document.getElementById('tx-side').value,
          price: +document.getElementById('tx-price').value,
          amount_btc: +document.getElementById('tx-btc').value || 0,
          amount_fiat: +document.getElementById('tx-fiat').value || 0,
          note: document.getElementById('tx-note').value || undefined,
        };
        if (!body.price) { toast('请填写单价', true); return; }
        try {
          await post(`/portfolio/transactions/${pid}`, body);
          toast('已保存'); App.reload();
        } catch (e) { toast(String(e.message || e), true); }
      };
      document.querySelectorAll('.btn-txdel').forEach(b => {
        b.onclick = async () => {
          if (!confirm('确认删除这笔交易？')) return;
          try { await del('/portfolio/transactions/' + b.dataset.id); toast('已删除'); App.reload(); }
          catch (e) { toast(String(e.message || e), true); }
        };
      });
    });
    return html;
  };

  // ============================================================ 定投模拟
  P.dca = async function () {
    const st = await get('/portfolio/strategies').catch(() => null);
    const list = (st && st.strategies) || [];
    let html = `<div class="page-head"><h2 class="page-title">定投模拟</h2>
      <span class="page-sub">用真实历史数据模拟「如果按这个规则投，结果会怎样」</span></div>`;

    html += `<div class="card"><h3 class="card-title">模拟参数</h3>
      <div class="form-row">
        <label>策略</label><select class="inp" id="dc-strategy">${list.map(s =>
        `<option value="${esc(s.code)}">${esc(s.name_cn || s.code)}</option>`).join('')}</select>
        <label>开始</label><input class="inp" type="date" id="dc-start" value="2018-01-01">
        <label>结束</label><input class="inp" type="date" id="dc-end">
        <label>频率</label><select class="inp" id="dc-freq"><option value="monthly">每月</option><option value="weekly">每周</option></select>
        <label>每期金额</label><input class="inp" type="number" id="dc-amount" value="2000" min="1" step="100">
        <label>单期上限</label><input class="inp" type="number" id="dc-max" value="0" min="0" step="100">
        <button class="btn btn-primary" id="dc-run">开始模拟</button>
      </div>
      <div id="dc-result"></div></div>`;

    html += `<div class="card"><h3 class="card-title">可选规则</h3>` +
      table(['规则', '通俗说明', '它试图解决什么'], list.map(s => [
        esc(s.name_cn || s.code),
        `<span class="muted">${esc(s.description || '')}</span>`,
        `<span class="muted">${esc(s.solves || '')}</span>`])) + `</div>`;

    html += note('warn', '模拟结果<b>不代表未来</b>。不同规则在不同历史区间表现差异巨大，' +
      '请务必结合「策略实验室」里写明的适用条件与失效条件一起看。');

    requestAnimationFrame(() => {
      const btn = document.getElementById('dc-run');
      if (!btn) return;
      btn.onclick = async () => {
        const freq = document.getElementById('dc-freq').value;
        const body = {
          strategy_code: document.getElementById('dc-strategy').value,
          start_date: document.getElementById('dc-start').value,
          end_date: document.getElementById('dc-end').value || null,
          contribution_frequency: freq,
          monthly_contribution: freq === 'monthly' ? +document.getElementById('dc-amount').value : 0,
          weekly_contribution: freq === 'weekly' ? +document.getElementById('dc-amount').value : 0,
          max_single_contribution: +document.getElementById('dc-max').value,
          validation_mode: 'oos',
          name: '定投模拟',
        };
        btn.disabled = true; btn.textContent = '模拟中…';
        const box = document.getElementById('dc-result');
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在模拟…</div>`;
        try {
          const r = await post('/research/backtest', body);
          box.innerHTML = r.ok ? DCA.renderSim(r) : unavailable(r.message || '模拟失败');
        } catch (e) { box.innerHTML = unavailable(String(e.message || e)); }
        btn.disabled = false; btn.textContent = '开始模拟';
      };
    });
    return html;
  };

  P.renderSim = function (r) {
    const bh = r.buy_and_hold || {};
    return `<div class="grid g4" style="margin-top:12px">
      ${stat('投入本金', nf0(r.total_invested) + ' 元', '')}
      ${stat('最终资产', nf0(r.final_value) + ' 元', '', cls(r.net_profit))}
      ${stat('收益率', pct(r.roi), `年化 ${pct(r.cagr)}`, cls(r.roi))}
      ${stat('最大回撤', pct(r.max_drawdown), '', 'down')}
      ${stat('平均成本', nf0(r.avg_cost) + ' 元', `持有 ${nf(r.btc_amount, 6)} BTC`)}
      ${stat('一次性买入对照', nf0(bh.final_value) + ' 元', '同本金首日买入')}
      ${stat('交易次数', nf0(r.trades), '')}
      ${stat('手续费', nf0(r.total_fees) + ' 元', '')}
      </div>
      ${(r.warnings || []).length ? card('局限', `<ul style="margin:0;padding-left:18px">${
        r.warnings.map(w => `<li>${esc(w)}</li>`).join('')}</ul>`) : ''}
      ${rawBlock('模拟原始返回', {
        final_value: r.final_value, roi: r.roi, cagr: r.cagr,
        max_drawdown: r.max_drawdown, run_id: r.run_id
      })}`;
  };

  global.PP = P;
  global.DCA = P;
})(window);
