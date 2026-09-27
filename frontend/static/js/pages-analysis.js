/* pages-analysis.js —— 周期 / 估值 / 链上 / 资金流 / ETF / 衍生品 / Options / 宏观 / 情绪 / 风险 / 综合状态 / 预测
   字段名严格对齐后端各引擎的真实输出契约。 */
(function (global) {
  'use strict';
  const P = {};
  const { get, card, stat, note, rawBlock, sourceBlock, table, nf, nf0, pct, cls,
    tsLocal, dateOf, esc, unavailable, qualityBadge } = C;

  const LEVEL_CN = {
    low: '低', moderate: '中等', elevated: '偏高', high: '高', extreme: '极高',
    deep_undervalue: '严重低估', undervalue: '偏低估', fair: '合理', overvalue: '偏高估', extreme_overvalue: '严重高估'
  };
  const LEVEL_BADGE = {
    low: 'b-ok', deep_undervalue: 'b-ok', undervalue: 'b-ok', fair: 'b-info',
    moderate: 'b-info', elevated: 'b-warn', overvalue: 'b-warn',
    high: 'b-bad', extreme: 'b-bad', extreme_overvalue: 'b-bad'
  };
  const levelBadge = l => `<span class="badge ${LEVEL_BADGE[l] || 'b-mute'}">${LEVEL_CN[l] || l}</span>`;
  const scoreColor = s => s > 70 ? '#d93025' : (s > 45 ? '#b06000' : '#188038');

  let _analysis = null;
  async function analysis(force) {
    if (!_analysis || force) _analysis = await get('/market/analysis').catch(() => null);
    return _analysis;
  }

  // ============================================================ 市场周期
  P.cycle = async function () {
    const a = await analysis();
    let html = `<div class="page-head"><h2 class="page-title">市场周期</h2>
      <span class="page-sub">不是硬编码的「四年周期」，而是趋势 / 动能 / 估值 / 热度四维度实时判定</span></div>`;
    const cy = (a && a.cycle) || null;
    if (!cy) { html += unavailable('周期引擎未返回结果'); return html; }

    html += `<div class="card"><div class="grid g4">
      ${stat('当前阶段', esc(cy.phase_cn || cy.phase), '')}
      ${stat('判断置信度', nf((cy.confidence || 0) * 100, 0) + '%', '证据越充分越高')}
      ${stat('综合评分', nf(cy.composite_score), '', cls(cy.composite_score))}
      ${stat('模型版本', esc(cy.model_version || ''), '结论永久对应版本')}
      </div>
      <div class="note note-info"><b>这一步该注意什么</b><br>${esc(cy.advice || '')}</div>
      ${cy.note ? `<div class="muted" style="font-size:12px">${esc(cy.note)}</div>` : ''}
      </div>`;

    if (cy.dimensions) {
      html += card('四个维度的原始评分',
        table(['维度', '评分', '含义'], Object.entries(cy.dimensions).map(([k, v]) => {
          const cn = { trend: '趋势', momentum: '动能', valuation: '估值', heat: '热度' }[k] || k;
          return [esc(cn), `<span class="mono ${cls(v)}">${nf(v)}</span>`,
          `<span class="muted">${cn}维度的标准化得分，正值偏乐观、负值偏谨慎</span>`];
        })));
    }
    const ev = cy.evidence || [];
    if (ev.length) {
      html += card('支持当前判断的证据',
        `<ul style="margin:0;padding-left:18px">` +
        ev.map(e => `<li><b>${esc(e.dim || '')}</b>：${esc(e.point || '')}</li>`).join('') + `</ul>`);
    }
    const ce = cy.counter_evidence || [];
    html += card('反向证据（同样要看）',
      ce.length
        ? note('warn', '以下信号<b>不支持</b>当前判断：') +
        `<ul style="margin:0;padding-left:18px">` +
        ce.map(e => `<li><b>${esc(e.dim || '')}</b>：${esc(e.point || '')}</li>`).join('') + `</ul>`
        : `<div class="muted" style="font-size:12.5px">当前没有检索到明显的反向证据。这不代表没有风险，只说明四维度读数方向一致。</div>`);

    const sim = cy.similar_periods || [];
    html += card('历史上相似的窗口 <span class="hint">仅用该时点之前的数据匹配，不含未来信息</span>',
      sim.length
        ? table(['起止', '相似度', '此后涨跌'], sim.map(s => [
          `${esc(s.start)} ~ ${esc(s.end)}`, nf((s.similarity || 0) * 100, 1) + '%',
          `<span class="${cls(s.forward_return_pct)}">${pct(s.forward_return_pct)}</span>`]))
        : `<div class="muted" style="font-size:12.5px">未找到符合相似度门槛的历史窗口。样本不足时不强行类比。</div>`);
    html += rawBlock('周期引擎原始输出', cy);
    return html;
  };

  // ============================================================ 估值
  P.valuation = async function () {
    const a = await analysis();
    let html = `<div class="page-head"><h2 class="page-title">估值分析</h2>
      <span class="page-sub">估值高不代表马上跌，估值低也不代表马上涨</span></div>`;
    const v = (a && a.valuation) || null;
    if (!v) { html += unavailable('估值引擎未返回结果'); return html; }

    html += `<div class="card"><div class="grid g4">
      ${stat('估值状态', levelBadge(v.state) + ' ' + esc(v.state_cn || v.state), esc((v.description || '').split('。')[0] || ''))}
      ${stat('综合估值分', nf(v.score), '', v.score > 70 ? 'down' : (v.score < 30 ? 'up' : ''))}
      ${stat('缺失因子', (v.missing_factors || []).length + ' 个', '缺失因子不计入加权')}
      ${stat('模型版本', esc(v.model_version || ''), '')}
      </div>
      ${v.disclaimer ? note('warn', esc(v.disclaimer)) : ''}
      </div>`;

    const contrib = v.contributions || {};
    if (Object.keys(contrib).length) {
      html += `<div class="card"><h3 class="card-title">估值计算过程</h3>` +
        table(['因子', '贡献分', '说明'], Object.entries(contrib).map(([k, c]) => [
          esc(typeof c === 'object' ? (c.name_cn || c.name || k) : k),
          `<span class="mono">${typeof c === 'object' ? nf(c.score ?? c.value ?? c.contribution) : nf(c)}</span>`,
          `<span class="muted">${esc(typeof c === 'object' ? (c.note || c.detail || '') : '')}</span>`])) + `</div>`;
    }
    if ((v.missing_factors || []).length) {
      html += card('未参与计算的因子',
        note('warn', '以下因子当前缺失，已从加权中<b>剔除</b>，而不是用默认值冒充：') +
        `<ul style="margin:0;padding-left:18px">${v.missing_factors.map(f => `<li>${esc(f)}</li>`).join('')}</ul>`);
    }
    html += rawBlock('估值引擎原始输出', v);
    return html;
  };

  // ============================================================ 风险
  P.risk = async function () {
    const a = await analysis();
    let html = `<div class="page-head"><h2 class="page-title">风险监测</h2>
      <span class="page-sub">每个维度都能展开到它依赖的原始数据；缺失维度不计入加权</span></div>`;
    const r = (a && a.risk) || null;
    if (!r) { html += unavailable('风险引擎未返回结果'); return html; }

    html += `<div class="card"><div class="grid g4">
      ${stat('综合风险', levelBadge(r.level) + ' ' + esc(r.level_cn || ''), '')}
      ${stat('风险总分', nf(r.overall_risk) + ' / 100', '', r.overall_risk > 70 ? 'down' : (r.overall_risk < 30 ? 'up' : ''))}
      ${stat('可用维度', `${Object.values(r.data_availability || {}).filter(Boolean).length} / 7`, '')}
      ${stat('模型版本', esc(r.model_version || ''), '')}
      </div>
      <div class="bar-wrap"><div class="bar" style="width:${Math.min(100, r.overall_risk || 0)}%;background:${scoreColor(r.overall_risk || 0)}"></div></div>
      ${r.note ? `<div class="muted" style="font-size:12px;margin-top:6px">${esc(r.note)}</div>` : ''}
      </div>`;

    const dims = r.dimensions || {};
    const cn = { trend: '趋势风险', valuation: '估值风险', volatility: '波动风险', leverage: '杠杆风险', liquidity: '流动性风险', macro: '宏观风险', onchain: '链上风险' };
    Object.entries(dims).forEach(([key, d]) => {
      html += card(`<span style="font-weight:600">${esc(cn[key] || key)}</span>
        ${d.available ? levelBadge(d.level) + ' ' + esc(d.level_cn || '') : '<span class="badge b-mute">数据缺失</span>'}
        <span class="muted" style="font-weight:400;font-size:12px">得分 ${nf(d.score)} · 权重 ${nf((r.weights || {})[key] * 100 || 0, 0)}%</span>
        <div class="bar-wrap"><div class="bar" style="width:${Math.min(100, d.score || 0)}%;background:${d.available ? scoreColor(d.score) : '#cfd4da'}"></div></div>`,
        (d.reasons && d.reasons.length)
          ? `<ul style="margin:0;padding-left:18px;font-size:12.5px">${d.reasons.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`
          : `<div class="muted" style="font-size:12.5px">该维度所需数据当前不可用。</div>`);
    });
    html += rawBlock('风险引擎原始输出', r);
    return html;
  };

  // ============================================================ 综合市场状态
  P.regime = async function () {
    const a = await analysis();
    let html = `<div class="page-head"><h2 class="page-title">综合市场状态</h2>
      <span class="page-sub">9 个子模块状态合成，缺席的模块会明确列出</span></div>`;
    const g = (a && a.regime) || null;
    if (!g) { html += unavailable('综合状态引擎未返回结果'); return html; }
    html += `<div class="card"><div class="grid g4">
      ${stat('市场状态', esc(g.regime), '')}
      ${stat('置信度', nf((g.confidence || 0) * 100, 0) + '%', '缺失模块越多越低')}
      ${stat('缺失模块', (g.unavailable_modules || []).length + ' 个', '')}
      ${stat('模型版本', esc(g.model_version || ''), '')}
      </div><div class="note note-info">${esc(g.summary_cn || '')}</div>
      ${(g.unavailable_modules || []).length
        ? note('warn', '以下模块当前不可用，已从合成中剔除（不是按 0 分参与）：<br>' +
          g.unavailable_modules.map(m => `· ${esc(m)}`).join('<br>'))
        : ''}
      </div>`;
    const map = [['trend_state', '趋势'], ['valuation_state', '估值'], ['capital_flow_state', '资金流'],
    ['onchain_state', '链上'], ['derivative_state', '衍生品'], ['macro_state', '宏观'],
    ['sentiment_state', '情绪'], ['risk_state', '风险'], ['cycle_state', '周期']];
    html += card('9 个子模块读数', table(['模块', '状态读数'],
      map.map(([k, cn2]) => [esc(cn2), `<span class="mono">${esc(g[k] || '')}</span>` +
        ((g[k] === 'unavailable' || !g[k]) ? ' <span class="badge b-mute">不可用</span>' : '')])));
    html += rawBlock('综合状态原始输出', g);
    return html;
  };

  // ============================================================ 链上
  P.onchain = async function () {
    const d = await get('/market/onchain').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">链上数据</h2>
      <span class="page-sub">免费可得的链上指标；需付费 Key 的指标会明确标注「未配置」</span></div>`;
    if (!d || !d.available) {
      html += unavailable(d && d.message || '链上数据源不可用', rawBlock('接口原始返回', d));
      html += note('info', 'MVRV / SOPR / NUPL / Puell 等高级链上指标需要 Glassnode 或 CryptoQuant 的 API Key，' +
        '在「后台管理 → 数据源」配置后自动启用。未配置时这里不会显示任何数字。');
      return html;
    }
    const m = d.metrics || {};
    const rows = Object.entries(m);
    html += card('链上指标', rows.length
      ? table(['指标', '数值', '来源'], rows.map(([k, v]) =>
        [esc(k), `<span class="mono">${nf(v)}</span>`, esc((d.source && d.source.provider) || '')]))
      : `<div class="empty">暂无链上数据</div>`);
    html += sourceBlock(d.source) + rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 资金流
  P.flows = async function () {
    let html = `<div class="page-head"><h2 class="page-title">资金流</h2>
      <span class="page-sub">交易所净流入 / 净流出：筹码移动方向</span></div>`;
    html += unavailable('交易所资金流需要付费数据源（Glassnode / CryptoQuant），当前未配置 Key。');
    html += note('info', '本页<b>刻意留空</b>。系统不会用模拟数据填充；配置 Key 后（数据源中心 → glassnode）' +
      '此处会自动出现真实资金流，无需改动任何代码。');
    return html;
  };

  // ============================================================ ETF
  P.etf = async function () {
    const d = await get('/market/etf').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">ETF 资金流</h2>
      <span class="page-sub">现货 ETF 逐日净申购 / 赎回</span></div>`;
    if (!d || !d.available) {
      html += unavailable((d && d.message) || 'ETF 数据源未配置', rawBlock('接口原始返回', d));
      if (d && d.hint) html += note('info', esc(d.hint));
      return html;
    }
    html += card('ETF 资金流', table(['项目', '内容'], Object.entries(d).map(([k, v]) =>
      [esc(k), `<span class="mono">${esc(typeof v === 'object' ? JSON.stringify(v) : String(v))}</span>`])));
    html += sourceBlock(d.source) + rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 衍生品
  P.derivatives = async function () {
    const d = await get('/market/derivatives').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">衍生品市场</h2>
      <span class="page-sub">资金费率与未平仓量：杠杆情绪的温度计</span></div>`;
    if (!d || !d.available) {
      html += unavailable((d && d.message) || '衍生品数据源不可用', rawBlock('接口原始返回', d));
      return html;
    }
    const f = d.funding || {};
    const oi = d.open_interest || {};
    html += `<div class="card"><div class="grid g4">
      ${stat('当前资金费率', nf(f.funding_rate_pct, 4) + '%',
        f.funding_rate > 0 ? '多头付费给空头（偏热）' : (f.funding_rate < 0 ? '空头付费给多头（偏冷）' : '接近中性'),
        f.funding_rate > 0 ? 'up' : (f.funding_rate < 0 ? 'down' : ''))}
      ${stat('年化资金费率', nf(f.annualized_pct, 2) + '%', '')}
      ${stat('未平仓量', nf0(oi.open_interest_usd / 1e9) + ' 十亿 USD', esc(oi.unit || ''))}
      ${stat('标记价格', nf(oi.mark_price), esc(oi.exchange || ''))}
      </div>
      ${note('info', '资金费率为正说明永续合约多头拥挤；极端正值在历史上常出现在阶段高点附近。' +
        '但单一指标不构成买卖信号，需与价格、成交、情绪一起看。')}
      ${sourceBlock((d.sources && d.sources[0]) || d.source)}
      ${rawBlock('接口原始返回', d)}
      </div>`;
    return html;
  };

  // ============================================================ Options
  P.options = async function () {
    let html = `<div class="page-head"><h2 class="page-title">期权市场</h2>
      <span class="page-sub">隐含波动率、Put/Call 比、最大痛点</span></div>`;
    html += unavailable('期权数据需要专业数据源（Deribit / Coinglass 期权模块），当前未接入。');
    html += note('info', '期权模块的数据契约已经定义好（DataCategory.OPTION_METRIC / options 表）。' +
      '后续新增一个 <code>providers/impl/options_*.py</code> 即可接入，业务层与页面均无需改动。在此之前本页保持留空。');
    return html;
  };

  // ============================================================ 宏观
  P.macro = async function () {
    const d = await get('/market/macro').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">宏观环境</h2>
      <span class="page-sub">严格区分「数据所属时间」与「发布时间」，防止未来信息泄漏</span></div>`;
    if (!d || !d.available) {
      html += unavailable((d && d.message) || '宏观数据源不可用', rawBlock('接口原始返回', d));
      html += note('info', '本平台不会为了填满页面而给出可能过期或有误的宏观数字。' +
        '若某宏观接口被临时变更（例如新增浏览器校验），本模块会自动标记为不可用，而其他模块照常运行。');
      if (d && d.errors && d.errors.length) {
        html += card('各宏观序列的失败原因',
          `<ul style="margin:0;padding-left:18px">${d.errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul>`);
      }
      return html;
    }
    const rows = Object.entries(d.series || {});
    html += card('宏观指标', table(['序列', '数据所属时间', '发布时间（保守可见）'],
      rows.map(([k, v]) => [esc(k),
      `<span class="mono">${esc(typeof v === 'object' ? (v.observation_ts || '') : String(v))}</span>`,
      `<span class="mono">${esc(typeof v === 'object' ? (v.release_ts || '未区分') : '')}</span>`])));
    html += note('warn', '宏观数据存在「事后修订」。回测中一律采用 <b>发布时间 + 1 天</b> 的保守可见假设，' +
      '确保不会用到当时尚未公开的数据。');
    html += sourceBlock(d.source) + rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 情绪
  P.sentiment = async function () {
    const d = await get('/market/sentiment').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">市场情绪</h2>
      <span class="page-sub">恐慌贪婪指数等情绪读数</span></div>`;
    if (!d || !d.available) {
      html += unavailable((d && d.message) || '情绪数据源不可用', rawBlock('接口原始返回', d));
      return html;
    }
    html += `<div class="card"><div class="grid g4">
      ${stat('恐慌贪婪指数', nf(d.index), esc(d.classification_cn || d.classification || ''),
        d.index >= 75 ? 'down' : (d.index <= 25 ? 'up' : ''))}
      ${stat('昨日读数', nf(d.value_yesterday), '')}
      ${stat('日变化', nf(d.change_1d), '', cls(d.change_1d))}
      ${stat('历史序列长度', nf0((d.series || []).length), '')}
      </div>
      ${note('info', '情绪是<b>同向指标</b>而非预测指标：极度恐慌常出现在下跌末段，极度贪婪常出现在上涨末段，' +
        '但都无法单独作为择时依据。')}
      </div>`;
    const s = d.series || [];
    if (s.length) {
      html += `<div class="card"><h3 class="card-title">情绪走势</h3>${CH.h('c-sent', 'chart', 280)}</div>`;
      requestAnimationFrame(() => {
        const pts = s.slice(0, 180).reverse();
        CH.lineChart('c-sent', pts.map(p => C.dateOf(p.ts)),
          [{ name: '恐慌贪婪指数', data: pts.map(p => p.value), color: '#f9ab00', area: true,
            markLine: { silent: true, symbol: 'none', data: [{ yAxis: 25 }, { yAxis: 75 }], lineStyle: { color: '#cfd4da', type: 'dashed' } } }],
          { scale: false });
      });
    }
    html += card('原始序列（最近 30 条）', table(['日期', '读数', '分类'],
      (d.series || []).slice(0, 30).map(p => [C.dateOf(p.ts), `<span class="mono">${nf(p.value)}</span>`, esc(p.classification || '')])));
    html += sourceBlock(d.source) + rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 概率预测
  P.forecast = async function (params) {
    const horizon = parseInt(params.get('horizon') || '30', 10);
    const d = await get('/market/forecast', { horizon }).catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">概率预测</h2>
      <span class="page-sub">只输出概率区间与不确定性；样本不足时明确拒绝输出</span>
      <div class="right"><select class="inp" id="sel-h">${[7, 14, 30, 60, 90, 180].map(h =>
        `<option value="${h}"${h === horizon ? ' selected' : ''}>未来 ${h} 天</option>`).join('')}</select></div></div>`;
    requestAnimationFrame(() => {
      const s = document.getElementById('sel-h');
      if (s) s.onchange = () => location.hash = `#/forecast?horizon=${s.value}`;
    });
    if (!d) { html += unavailable('预测服务不可用'); return html; }
    if (!d.available) {
      html += unavailable(d.message || '当前无法可靠预测', rawBlock('接口原始返回', d));
      html += note('info', '这正是预期行为：当历史样本不足、或相似样本的一致性太低时，' +
        '系统会明确告诉你「现在预测不可靠」，而不是编一个看起来专业的数字。');
      return html;
    }
    const pr = d.probabilities || {};
    const rg = d.range || {};
    html += `<div class="card"><div class="grid g4">
      ${stat(horizon + ' 天后上涨概率', nf(pr.up, 1) + '%', '', 'up')}
      ${stat('震荡概率', nf(pr.flat, 1) + '%', '', '')}
      ${stat('下跌概率', nf(pr.down, 1) + '%', '', 'down')}
      ${stat('样本数 / 一致性', `${nf0(d.sample_count)} / ${nf((d.consistency || 0) * 100, 0)}%`, '低于门槛时拒绝输出')}
      </div>
      <div class="note note-info"><b>价格区间（${horizon} 天后）</b><br>
        悲观 P10 ${nf(rg.p10)} · P25 ${nf(rg.p25)} · 中位 ${nf(rg.median)} · P75 ${nf(rg.p75)} · 乐观 P90 ${nf(rg.p90)}
        <br>当前价格 ${nf(d.current_price)}
      </div>
      ${CH.h('c-fc', 'chart-sm', 250)}
      ${(d.uncertainty || []).length ? card('不确定性来源',
        `<ul style="margin:0;padding-left:18px">${d.uncertainty.map(u => `<li>${esc(u)}</li>`).join('')}</ul>`) : ''}
      ${note('warn', '<b>' + esc(d.disclaimer || '预测只描述概率分布，不构成投资建议。') + '</b>')}
      ${rawBlock('预测接口原始输出', d)}
      </div>`;
    requestAnimationFrame(() => {
      const p0 = d.current_price;
      CH.rangeChart('c-fc', ['当前', `${horizon} 天后`],
        [p0, rg.p10], [p0, rg.p90], [p0, rg.median]);
    });
    return html;
  };

  global.PA = P;
})(window);
