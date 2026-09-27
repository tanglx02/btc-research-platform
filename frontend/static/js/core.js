/* core.js —— 基础设施：API 客户端、格式化、组件渲染原语、模式切换 */
(function (global) {
  'use strict';

  const API = '/api/v1';

  // ------------------------------------------------------------ 模式
  const State = {
    mode: localStorage.getItem('btc_mode') || 'simple',
    setMode(m) {
      this.mode = m;
      localStorage.setItem('btc_mode', m);
      document.body.classList.toggle('mode-pro', m === 'pro');
      document.querySelectorAll('.mode-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.mode === m));
    },
    isPro() { return this.mode === 'pro'; }
  };

  // ------------------------------------------------------------ HTTP
  // ------------------------------------------------------------ 写操作鉴权
  // 后端所有写接口（POST/PATCH/DELETE 的运维类路由）都要求 X-Admin-Token。
  // 令牌只存在 sessionStorage：关掉标签页即失效，不随浏览器长期留存。
  const AdminAuth = {
    key: 'btc_admin_token',
    get() { try { return sessionStorage.getItem(this.key) || ''; } catch (_) { return ''; } },
    set(t) { try { sessionStorage.setItem(this.key, String(t || '')); } catch (_) { /* 隐私模式 */ } },
    clear() { try { sessionStorage.removeItem(this.key); } catch (_) { /* noop */ } },
    has() { return !!this.get(); }
  };

  function reqId() {
    return 'xxxxxxxx'.replace(/x/g, () => (Math.random() * 16 | 0).toString(16));
  }

  function headers(extra) {
    const h = { 'Accept': 'application/json', 'X-Request-ID': reqId() };
    const t = AdminAuth.get();
    if (t) h['X-Admin-Token'] = t;
    return extra ? Object.assign(h, extra) : h;
  }

  async function get(path, params) {
    const qs = params ? '?' + new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString() : '';
    const res = await fetch(API + path + qs, { headers: headers() });
    return unwrap(res);
  }

  async function post(path, body, params) {
    // 第二个参数才是请求体。曾经有地方写成 post(path, null, {...payload})，把请求体塞进了
    // 第三个参数（那是拼到 URL 上的 query），真正发出去的 body 是 JSON.stringify(null)
    // 也就是字符串 "null"，后端解析模型失败直接 422，界面只剩一句干巴巴的
    // 「请求失败 HTTP 422」，很难看出写错的是参数位置。这里直接把它挡成抛错 ——
    // 宁可当场炸出来，也不要悄悄退化成一次空请求。
    if ((body === null || body === undefined) && params && Object.keys(params).length) {
      throw new Error(
        'post() 调用写错了：请求体必须放在第二个参数，签名是 post(path, body, params)'
      );
    }
    const qs = params ? '?' + new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
    ).toString() : '';
    const res = await fetch(API + path + qs, {
      method: 'POST',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    return unwrap(res);
  }

  async function patch(path, body) {
    const res = await fetch(API + path, {
      method: 'PATCH',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body)
    });
    return unwrap(res);
  }

  async function put(path, body) {
    const res = await fetch(API + path, {
      method: 'PUT',
      headers: headers({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body === undefined ? {} : body)
    });
    return unwrap(res);
  }

  async function del(path) {
    const res = await fetch(API + path, { method: 'DELETE', headers: headers() });
    return unwrap(res);
  }

  async function unwrap(res) {
    const text = await res.text();
    let data;
    try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { raw: text }; }
    if (!res.ok) {
      // 后端业务异常统一是 {error:{message}}；但框架层的校验错误（典型就是 HTTP 422
      // 参数不合法）走的是 FastAPI 标准的 {detail: [...]}。不读 detail 的话，
      // 用户只能看到「请求失败 HTTP 422」，完全不知道是哪个字段不对。
      const detail = data && data.detail;
      const detailText = typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map(d => {
            const loc = (d.loc || []).slice(1).join('.');
            return (loc ? loc + '：' : '') + (d.msg || d.type || '');
          }).join('；')
          : (detail ? JSON.stringify(detail) : '');
      const msg = (data && data.error && data.error.message)
        || detailText
        || ('请求失败 HTTP ' + res.status);
      throw new Error(msg);
    }
    return data;
  }

  // ------------------------------------------------------------ 格式化
  const nf = (n, d = 2) => (n === null || n === undefined || Number.isNaN(+n))
    ? '--' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });
  const nf0 = n => nf(n, 0);
  const pct = (n, d = 2) => (n === null || n === undefined) ? '--' : (Number(n) >= 0 ? '+' : '') + Number(n).toFixed(d) + '%';
  const btc = n => nf(n, 8);

  function cls(n) { return n > 0 ? 'up' : (n < 0 ? 'down' : ''); }
  function sign(n, d = 2) { return (Number(n) >= 0 ? '+' : '') + nf(n, d); }

  function tsLocal(iso) {
    if (!iso) return '--';
    const d = typeof iso === 'number' ? new Date(iso * 1000) : new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    const p = x => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  function dateOf(ts) {
    const d = new Date(ts * 1000);
    const p = x => String(x).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function agoText(iso) {
    if (!iso) return '';
    const t = typeof iso === 'number' ? iso * 1000 : Date.parse(iso);
    const s = (Date.now() - t) / 1000;
    if (s < 60) return Math.floor(s) + ' 秒前';
    if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
    if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    return Math.floor(s / 86400) + ' 天前';
  }

  // ------------------------------------------------------------ 质量/状态徽章
  const QUALITY_CN = {
    VERIFIED: '已交叉验证', CROSS_VERIFIED: '多源一致', SINGLE_SOURCE: '单一数据源',
    ESTIMATED: '估算值', STALE: '过期数据', CONFLICT: '数据源冲突',
    MISSING: '缺失', NOT_CONFIGURED: '数据源未配置'
  };
  const QUALITY_BADGE = {
    VERIFIED: 'b-ok', CROSS_VERIFIED: 'b-ok', SINGLE_SOURCE: 'b-info',
    ESTIMATED: 'b-warn', STALE: 'b-warn', CONFLICT: 'b-bad',
    MISSING: 'b-mute', NOT_CONFIGURED: 'b-mute'
  };
  const STATUS_CN = {
    ONLINE: '在线', DEGRADED: '降级', SLOW: '缓慢', RATE_LIMITED: '限流',
    AUTH_ERROR: '鉴权失败', NETWORK_ERROR: '网络异常', DATA_ERROR: '数据异常',
    OFFLINE: '离线', DISABLED: '已停用', NOT_CONFIGURED: '未配置', UNKNOWN: '未知', ERROR: '错误'
  };
  function qualityBadge(q) {
    if (!q) return '';
    return `<span class="badge ${QUALITY_BADGE[q] || 'b-mute'}">${QUALITY_CN[q] || q}</span>`;
  }
  function statusBadge(s) {
    if (!s) return '';
    const map = { ONLINE: 'b-ok', DEGRADED: 'b-warn', SLOW: 'b-warn', RATE_LIMITED: 'b-warn',
      AUTH_ERROR: 'b-bad', NETWORK_ERROR: 'b-bad', DATA_ERROR: 'b-bad', OFFLINE: 'b-bad',
      DISABLED: 'b-mute', NOT_CONFIGURED: 'b-mute', UNKNOWN: 'b-mute' };
    return `<span class="badge ${map[s] || 'b-mute'}">${STATUS_CN[s] || s}</span>`;
  }

  // ------------------------------------------------------------ 组件
  function el(html) { const d = document.createElement('div'); d.innerHTML = html.trim(); return d.firstElementChild; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function card(title, bodyHtml, hint) {
    return `<section class="card"><h3 class="card-title">${title}` +
      (hint ? `<span class="hint">${hint}</span>` : '') + `</h3>${bodyHtml}</section>`;
  }

  function stat(label, value, extra, clsName) {
    return `<div class="stat"><div class="stat-label">${label}</div>` +
      `<div class="stat-value ${clsName || ''}">${value}</div>` +
      (extra ? `<div class="stat-extra">${extra}</div>` : '') + `</div>`;
  }

  function note(type, text) {
    return `<div class="note note-${type}">${text}</div>`;
  }

  /** 原始数据折叠块：任何结论都必须能一路展开到原始返回值 */
  function rawBlock(title, obj) {
    let text;
    try { text = JSON.stringify(obj, null, 2); } catch (_) { text = String(obj); }
    return `<details class="raw"><summary>${title}（原始数据）</summary><pre>${esc(text)}</pre></details>`;
  }

  /** 数据来源声明块：provider / 质量 / 延迟 / 尝试链路 */
  function sourceBlock(src) {
    if (!src) return '';
    const rows = [];
    rows.push(`<b>来源</b>：${esc(src.display_name || src.provider || '未知')}` +
      (src.used_fallback ? ' <span class="badge b-warn">已切换备用源</span>' : ''));
    if (src.quality) rows.push(qualityBadge(src.quality));
    if (src.src && src.confidence !== undefined) {
      rows.push(`<b>置信度</b> ${(src.confidence * 100).toFixed(0)}%`);
    } else if (src.confidence !== undefined) {
      rows.push(`<b>置信度</b> ${(src.confidence * 100).toFixed(0)}%`);
    }
    if (src.latency_ms) rows.push(`<b>响应</b> ${nf0(src.latency_ms)} ms`);
    const obs = src.observation_time || src.observed_at;
    if (obs) rows.push(`<b>数据时间</b> ${tsLocal(obs)}${agoText(obs) ? '（' + agoText(obs) + '）' : ''}`);
    if (src.failover_reason) rows.push(`<span class="badge b-warn">切换原因：${esc(src.failover_reason)}</span>`);

    let html = `<div class="source-block"><div class="source-line">${rows.join(' ')}</div>`;
    const atts = src.attempts || [];
    if (atts.length) {
      html += `<table class="tbl" style="margin-top:8px"><thead><tr><th>尝试的数据源</th><th>结果</th><th>延迟</th><th>原因</th></tr></thead><tbody>`;
      atts.forEach(a => {
        html += `<tr><td>${esc(a.provider)}</td>` +
          `<td>${a.ok ? '<span class="badge b-ok">成功</span>' : '<span class="badge b-bad">失败</span>'}</td>` +
          `<td class="mono">${nf0(a.latency_ms)} ms</td>` +
          `<td>${esc(a.message || a.failure_type || '')}</td></tr>`;
      });
      html += `</tbody></table>`;
    }
    html += `</div>`;
    return html;
  }

  function crossValidationBlock(cv) {
    if (!cv) return '';
    const verdictCn = {
      VERIFIED: '多源交叉验证一致', CONFLICT: '数据源之间存在冲突',
      SINGLE_SOURCE: '单一数据源，未经交叉验证', NO_DATA: '无数据'
    };
    const cls = cv.verdict === 'CONFLICT' ? 'note-bad' : (cv.verdict === 'VERIFIED' ? 'note-ok' : 'note-info');
    let html = `<div class="note ${cls}"><b>交叉验证：${verdictCn[cv.verdict] || cv.verdict}</b><br>` +
      `参与源 ${cv.source_count || 0} 个 · 中位值 ${nf(cv.median)} · 最大偏差 ${nf(cv.max_deviation_pct, 4)}% · 容差 ${cv.tolerance_pct}%`;
    if (cv.verdict === 'CONFLICT') {
      html += `<br><b>系统不会偷偷选一个源：</b>差异已如实记录，请以下方各源明细为准。`;
    }
    html += `</div>`;
    if (cv.sources && cv.sources.length > 1) {
      html += `<table class="tbl"><thead><tr><th>数据源</th><th>数值</th><th>偏差</th><th>延迟</th></tr></thead><tbody>`;
      cv.sources.forEach(s => {
        html += `<tr><td>${esc(s.provider)}</td><td class="mono">${nf(s.value)}</td>` +
          `<td class="mono ${Math.abs(s.deviation_pct) > (cv.tolerance_pct || 0) ? 'down' : ''}">${nf(s.deviation_pct, 4)}%</td>` +
          `<td class="mono">${nf0(s.latency_ms)} ms</td></tr>`;
      });
      html += `</tbody></table>`;
    }
    return html;
  }

  function table(headers, rows, rowCls) {
    if (!rows || !rows.length) return `<div class="empty">暂无数据</div>`;
    // rowCls 既支持函数（按行返回类名），也支持字符串（统一类名）——避免调用方传字符串时崩溃
    const cls = typeof rowCls === 'string' ? (() => rowCls) : rowCls;
    let html = `<table class="tbl${typeof rowCls === 'string' ? ' ' + rowCls : ''}"><thead><tr>`;
    headers.forEach(h => html += `<th>${h}</th>`);
    html += `</tr></thead><tbody>`;
    rows.forEach(r => {
      html += `<tr${cls && cls(r) ? ' class="' + cls(r) + '"' : ''}>`;
      r.forEach(c => html += `<td>${c}</td>`);
      html += `</tr>`;
    });
    return html + `</tbody></table>`;
  }

  function toast(msg, bad) {
    let wrap = document.querySelector('.toast-wrap');
    if (!wrap) { wrap = el('<div class="toast-wrap"></div>'); document.body.appendChild(wrap); }
    const t = el(`<div class="toast${bad ? ' bad' : ''}">${esc(msg)}</div>`);
    wrap.appendChild(t);
    setTimeout(() => t.remove(), 4200);
  }

  /** 模块不可用时的统一占位：明确告知原因，不用假数据填充 */
  function unavailable(reason, extra) {
    return note('warn', `<b>该模块当前不可用</b><br>${esc(reason || '暂无可用数据源')}`) +
      (extra ? extra : '') +
      `<div class="muted" style="font-size:12px;margin-top:6px">按设计约定：数据源失效时只关闭该模块，其余功能照常运行，且绝不使用模拟数据代替。</div>`;
  }

  global.C = {
    API, State, AdminAuth, get, post, put, patch, del,
    nf, nf0, pct, btc, cls, sign, tsLocal, dateOf, agoText,
    esc, el, card, stat, note, rawBlock, sourceBlock, crossValidationBlock, table, toast,
    unavailable, qualityBadge, statusBadge, QUALITY_CN, STATUS_CN
  };
})(window);
