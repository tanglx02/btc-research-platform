/* pages-market.js —— 首页 / BTC行情 / 历史 blink 数据 / K线 */
(function (global) {
  'use strict';
  const P = {};
  const { get, post, card, stat, note, rawBlock, sourceBlock, crossValidationBlock,
    table, nf, nf0, pct, btc, cls, sign, tsLocal, dateOf, esc, el, unavailable, qualityBadge } = C;

  // ============================================================ 首页总览
  P.overview = async function () {
    const [ov, pr] = await Promise.all([
      get('/market/overview').catch(() => null),
      get('/market/price').catch(() => null)
    ]);
    let html = `<div class="page-head"><h2 class="page-title">今天的市场，用一句话说清楚</h2>
      <span class="page-sub">每个结论都可以一路点开到原始数据</span></div>`;

    if (!pr || !pr.available) {
      html += unavailable(pr && pr.message || '所有数据源均不可用，且本地无历史数据可降级使用');
      html += rawBlock('价格接口返回', pr);
      return html;
    }

    // 价格头条
    const src = pr.source || {};
    html += `<div class="card"><div class="grid g4">
      ${stat('BTC 当前价格', '$' + nf(pr.price), `${esc(src.display_name || src.provider || '')} · ${qualityBadge(src.quality)}`)}
      ${stat('24 小时变化', pct(pr.change_24h || 0), '', cls(pr.change_24h || 0))}
      ${stat('数据源健康', '详见数据源中心', '<a href="#/providers">查看 25 个数据源实时状态 →</a>', '')}
      ${stat('数据延迟', nf0(src.latency_ms) + ' ms', src.stale ? '降级数据' : '实时', src.stale ? 'down' : '')}
      </div>
      ${pr.stale ? note('warn', '<b>当前处于降级运行</b>：数据源暂时不可用，显示的是最后一次成功获取的价格（' + tsLocal(src.observation_time) + '）。系统不会用估算值冒充实时数据。') : ''}
      ${crossValidationBlock(src.cross_validation)}
      ${sourceBlock(src)}
      ${rawBlock('价格接口原始返回', pr)}
      </div>`;

    // 一句话结论
    if (ov && ov.available) {
      const verdictCls = { low: 'note-ok', high: 'note-bad', neutral: 'note-info' };
      html += `<div class="card"><h3 class="card-title">普通人视角的一句话结论</h3>`;
      (ov.headlines || []).forEach(h => {
        html += `<div class="note ${verdictCls[h.level] || 'note-info'}">
          <b>${esc(h.title)}</b><br>${esc(h.text)}
          ${h.detail ? `<br><span class="muted" style="font-size:12px">${esc(h.detail)}</span>` : ''}
        </div>`;
      });
      html += rawBlock('总览接口原始返回', ov) + `</div>`;
    }

    // 关键模块状态一览
    html += `<div class="card"><h3 class="card-title">各分析模块当前状态 <span class="hint">某个模块失效只关闭它自己</span></h3>`;
    const mods = (ov && ov.modules) || [];
    if (!mods.length) {
      html += `<div class="empty">暂无模块状态</div>`;
    } else {
      html += table(['模块', '状态', '说明', '来源'], mods.map(m => [
        `<a href="${esc(m.href || '#')}">${esc(m.name)}</a>`,
        m.available ? '<span class="badge b-ok">可用</span>' : '<span class="badge b-warn">不可用</span>',
        `<span class="muted">${esc(m.reason || '')}</span>`,
        `<span class="muted">${esc(m.provider || '')}</span>`
      ]));
    }
    html += `</div>`;
    return html;
  };

  // ============================================================ BTC 行情
  P.market = async function (params) {
    const interval = params.get('interval') || '1d';
    const limit = parseInt(params.get('limit') || '365', 10);
    const [pr, cd, ind] = await Promise.all([
      get('/market/price').catch(() => null),
      get('/market/candles', { interval, limit }).catch(() => null),
      get('/market/indicators', { interval }).catch(() => null)
    ]);

    let html = `<div class="page-head"><h2 class="page-title">BTC 行情</h2>
      <span class="page-sub">历史数据来自本地数据库，不依赖第三方实时接口</span>
      <div class="right">
        <select class="inp" id="sel-interval">
          ${['1d', '1h', '4h', '1w'].map(i => `<option value="${i}"${i === interval ? ' selected' : ''}>${i}</option>`).join('')}
        </select>
        <select class="inp" id="sel-limit">
          ${[90, 180, 365, 1000, 3000].map(l => `<option value="${l}"${l === limit ? ' selected' : ''}>最近 ${l} 根</option>`).join('')}
        </select>
      </div></div>`;

    if (pr && pr.available) {
      const s = pr.source || {};
      html += `<div class="card"><div class="grid g4">
        ${stat('最新价', '$' + nf(pr.price), esc(s.display_name || s.provider || ''))}
        ${stat('延迟', nf0(s.latency_ms) + ' ms', s.used_fallback ? '已切换备用源' : '主源')}
        ${stat('数据质量', '', qualityBadge(s.quality))}
        ${stat('置信度', nf((s.confidence || 0) * 100, 0) + '%', '')}
        </div>${sourceBlock(s)}${rawBlock('价格原始返回', pr)}</div>`;
    } else {
      html += unavailable(pr && pr.message || '无法获取当前价格');
    }

    if (!cd || !cd.available) {
      html += unavailable('本地暂无该周期历史数据。请先在「系统任务」执行历史回填，或用命令行：<code>python scripts/btcctl.py backfill --interval ' + interval + '</code>');
      return html;
    }

    html += `<div class="card"><h3 class="card-title">K 线 <span class="hint">${cd.count} 根 · 数据来自本地数据库</span></h3>
      ${CH.h('c-candle', 'chart-lg', 460)}
      ${coverageNote(cd.source)}
      ${rawBlock('K 线原始返回（前 3 条）', { count: cd.count, source: cd.source, sample: (cd.candles || []).slice(0, 3) })}
      </div>`;

    requestAnimationFrame(() => {
      const rows = cd.candles || [];
      const closes = rows.map(r => r.close);
      rows.forEach((r, i) => {
        [20, 50, 200].forEach(m => {
          if (i >= m - 1) {
            let sum = 0; for (let k = i - m + 1; k <= i; k++) sum += closes[k];
            r['ma' + m] = +(sum / m).toFixed(2);
          }
        });
      });
      CH.candleChart('c-candle', rows, [20, 50, 200]);
    });

    if (ind && ind.available) {
      const latest = ind.latest || {};
      html += `<div class="card"><h3 class="card-title">技术指标 <span class="hint">${esc(ind.version || '')}</span></h3>
        <div class="grid g4">
        ${Object.entries(ind.display || latest).slice(0, 16).map(([k, v]) =>
        stat(esc(k), typeof v === 'number' ? nf(v) : esc(v), '', '')).join('')}
        </div>
        <details class="raw"><summary>全部指标值（原始数据）</summary><pre>${esc(JSON.stringify(latest, null, 2))}</pre></details>
        ${rawBlock('指标接口原始返回', { available: ind.available, version: ind.version, source: ind.source })}
        </div>`;
    }
    bindFilters(interval, limit, '#/market');
    return html;
  };

  function coverageNote(source) {
    if (!source) return '';
    const cov = source.local_coverage || {};
    return `<div class="source-block">
      <div class="source-line"><b>读取方式</b>：${esc(source.mode === 'local_database' ? '本地数据库' : source.mode || '')}
      <span>${esc(source.note || '')}</span></div>
      <div class="source-line" style="margin-top:4px">
      <span><b>本地覆盖</b>：${cov.count || 0} 根 ·
      ${cov.start_ts ? dateOf(cov.start_ts) : '--'} ~ ${cov.end_ts ? dateOf(cov.end_ts) : '--'}</span>
      </div></div>`;
  }

  function bindFilters(curI, curL, hash) {
    requestAnimationFrame(() => {
      const si = document.getElementById('sel-interval');
      const sl = document.getElementById('sel-limit');
      if (si) si.onchange = () => location.hash = `${hash}?interval=${si.value}&limit=${sl ? sl.value : 365}`;
      if (sl) sl.onchange = () => location.hash = `${hash}?interval=${si ? si.value : '1d'}&limit=${sl.value}`;
    });
  }

  // ============================================================ 时间线
  P.timeline = async function () {
    const data = await get('/market/timeline', { limit: 200 }).catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">市场事件时间线</h2>
      <span class="page-sub">减半、监管、极端行情等关键历史事件</span></div>`;
    const events = (data && data.events) || [];
    if (!events.length) { html += `<div class="empty">暂无事件数据</div>`; return html; }
    html += `<div class="card"><div class="timeline">`;
    events.slice().reverse().forEach(e => {
      html += `<div class="tl-item impact-${esc(e.impact || '')}">
        <div class="tl-date">${esc(e.date)}</div>
        <div class="tl-title">${esc(e.title)}</div>
        <div class="muted" style="font-size:12px">类型 ${esc(e.type || '')}${e.source ? ' · 来源 ' + esc(e.source) : ''}</div>
        </div>`;
    });
    html += `</div>${rawBlock('时间线原始返回', data)}</div>`;
    return html;
  };

  global.PM = P;
})(window);
