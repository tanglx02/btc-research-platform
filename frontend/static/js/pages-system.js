/* pages-system.js —— 数据源中心 / 数据质量 / 系统任务 / 后台管理 */
(function (global) {
  'use strict';
  const P = {};
  const { get, post, put, patch, AdminAuth, card, stat, note, rawBlock, sourceBlock, table, nf, nf0, pct, esc,
    tsLocal, dateOf, unavailable, statusBadge, qualityBadge, toast } = C;

  // ============================================================ 数据源中心
  P.providers = async function () {
    const [dash, cats] = await Promise.all([
      get('/system/providers/dashboard').catch(() => null),
      get('/system/providers/categories').catch(() => null)
    ]);
    let html = `<div class="page-head"><h2 class="page-title">数据源中心</h2>
      <span class="page-sub">每个数据源的实时健康情况、评分与自动切换记录</span>
      <div class="right"><button class="btn" id="btn-reenable">恢复被停用的源</button>
        <button class="btn" id="btn-probe">一键测试全部数据源</button></div></div>`;

    // 未配置 Key 的数据源在这里直接告诉用户去哪配 —— 「未配置」不该是死胡同。
    const needKey = (dash && dash.providers || []).filter(p => p.configured === false);
    if (needKey.length) {
      html += `<div class="note note-warn" style="margin-bottom:14px">
        有 <b>${needKey.length}</b> 个数据源缺少 API 密钥，当前不可用：
        <span class="mono">${esc(needKey.map(p => p.provider).join('、'))}</span>。
        它们如实报「未配置」而<b>不会伪造数据</b>。
        需要用到时，到 <a href="#/settings">系统设置 → 数据源密钥</a> 填写即可，<b>无需编辑 .env、无需重启</b>。
      </div>`;
    }

    const d = (dash && dash.summary) || {};
    html += `<div class="card"><div class="grid g4">
      ${stat('已注册数据源', nf0(d.total_providers), '')}
      ${stat('健康', nf0(d.healthy), `健康率 ${nf((d.health_ratio || 0) * 100, 0)}%`)}
      ${stat('未配置 Key', nf0(d.not_configured), '配置后自动启用')}
      ${stat('今日失败次数', nf0(d.failures_today), `平均延迟 ${nf0(d.avg_latency_ms)} ms`)}
      </div>
      <div id="probe-result"></div>
      </div>`;

    const providers = (dash && dash.providers) || [];
    if (providers.length) {
      html += card('全部数据源', table(
        ['数据源', '类别', '状态', '优先级', '响应', '成功率24h', '健康分', '说明'],
        providers.map(p => [
          `<b>${esc(p.display_name || p.provider)}</b><br><span class="mono muted" style="font-size:11px">${esc(p.provider)}</span>`,
          `<span class="muted" style="font-size:11px">${esc((p.categories || []).join('、'))}</span>`,
          statusBadge(p.status),
          `<span class="mono">${p.priority}</span>`,
          `<span class="mono">${nf0(p.latency_ms)} ms</span>`,
          `<span class="mono">${nf(p.success_rate_24h, 0)}%</span>`,
          `<span class="mono">${nf(p.score, 0)}</span>`,
          `<span class="muted" style="font-size:11px">${esc(p.last_error || p.last_failure_type || '')}</span>`])));
    }

    if (cats) {
      const arr = Array.isArray(cats) ? cats : (cats.categories || []);
      if (arr.length) {
        html += `<div class="card"><h3 class="card-title">数据类别 → 主源与备用链 <span class="hint">自动故障切换的顺序</span></h3>` +
          table(['数据类别', '主源', '备用源', '未配置/已停用'],
            arr.map(c => [
              esc(c.category || c.name),
              `<span class="badge b-info">${esc(c.primary || '—')}</span>`,
              `<span class="muted">${esc((c.backups || []).join(' → ') || '—')}</span>`,
              `<span class="muted" style="font-size:11px">${esc(((c.unconfigured || []).concat(c.disabled || [])).join('、') || '—')}</span>`]))
          + `</div>`;
      }
    }

    const fo = await get('/system/providers/failovers').catch(() => null);
    const events = (fo && (fo.events || fo.failovers)) || [];
    if (events.length) {
      html += card('最近的自动故障切换', table(['时间', '数据类别', '从', '切到', '原因'],
        events.slice(0, 30).map(e => [tsLocal(e.created_at || e.ts), esc(e.category || e.data_category),
        esc(e.from_provider || e.from), esc(e.to_provider || e.to), esc(e.reason || '')])));
    }

    html += rawBlock('数据源面板原始数据', dash);

    requestAnimationFrame(() => {
      const rb = document.getElementById('btn-reenable');
      if (rb) rb.onclick = async () => {
        const box = document.getElementById('probe-result');
        rb.disabled = true;
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在恢复被自动停用的数据源…</div>`;
        try {
          const r = await post('/system/providers/reenable-stale', {});
          box.innerHTML = note(r.restored && r.restored.length ? 'ok' : 'info',
            `${esc(r.note || '')}${r.restored && r.restored.length
              ? ` 已恢复：<span class="mono">${esc(r.restored.join('、'))}</span>`
              : ''}${r.skipped && r.skipped.length
              ? `；手动禁用的 ${r.skipped.length} 个保持不动` : ''}`);
          toast('已恢复 ' + (r.restored || []).length + ' 个数据源');
        } catch (e) { box.innerHTML = unavailable(String(e.message || e)); rb.disabled = false; }
      };

      const b = document.getElementById('btn-probe');
      if (b) b.onclick = async () => {
        const box = document.getElementById('probe-result');
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在逐个测试所有数据源连通性…</div>`;
        try {
          const r = await post('/system/providers/test-all');
          const rows = r.results || r.providers || [];
          box.innerHTML = table(['数据源', '结果', '延迟', '说明'],
            rows.map(x => [esc(x.provider || x.name),
            x.ok ? '<span class="badge b-ok">通</span>' : '<span class="badge b-bad">不通</span>',
            `<span class="mono">${nf0(x.latency_ms || 0)} ms</span>`,
            esc(x.message || x.error || '')]));
        } catch (e) { box.innerHTML = unavailable(String(e.message || e)); }
      };
    });
    return html;
  };

  // ============================================================ 系统设置
  // 设计目标：所有配置都能在页面上改，不必手工编辑 .env。
  // 表单完全由后端 /system/settings 返回的元数据驱动 —— 以后在后端新增配置项，
  // 前端不用改一行代码就会自动出现在这里。
  //
  // 两条容易出错、这里刻意处理过的规则：
  //   1) 敏感项（密钥/密码）后端只回传掩码，前端用 password 框且「留空 = 不修改」，
  //      避免用户打开页面保存一次就把已配好的密钥清成空。
  //   2) 提交时只送改动过的字段（dirty），不做全量回写。

  function _sourceBadge(it) {
    if (it.source === 'database') return '<span class="badge b-ok">后台已配置</span>';
    if (it.source === 'env') return '<span class="badge b-info">来自 .env</span>';
    return '<span class="badge b-mute">默认值</span>';
  }

  function _fieldHtml(it) {
    const id = 'set-' + it.key;
    let input = '';
    if (it.type === 'bool') {
      input = `<input type="checkbox" id="${id}" data-key="${esc(it.key)}" data-type="bool"
                 ${it.value ? 'checked' : ''}>`;
    } else if (it.options && it.options.length) {
      input = `<select id="${id}" data-key="${esc(it.key)}" data-type="str" style="min-width:200px">` +
        it.options.map(o => `<option value="${esc(o.value)}" ${
          String(it.value) === String(o.value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('') +
        `</select>`;
    } else if (it.sensitive) {
      // 留空 = 保持原值。想清空必须显式点「清除」。
      const ph = it.has_value ? `已配置 ${esc(it.masked)}，留空表示不修改` : esc(it.placeholder || '未配置');
      input = `<input type="password" id="${id}" data-key="${esc(it.key)}" data-type="str"
                 data-sensitive="1" placeholder="${ph}" style="min-width:260px">` +
        (it.has_value ? ` <button class="btn" data-clear="${esc(it.key)}">清除</button>` : '');
    } else {
      const t = (it.type === 'int' || it.type === 'float') ? 'number' : 'text';
      const step = it.type === 'float' ? ' step="any"' : '';
      input = `<input type="${t}"${step} id="${id}" data-key="${esc(it.key)}" data-type="${esc(it.type)}"
                 value="${esc(it.value == null ? '' : it.value)}" placeholder="${esc(it.placeholder || '')}"
                 style="min-width:200px">`;
    }

    const restart = it.requires_restart ? ' <span class="badge b-warn">改后需重启</span>' : '';
    const help = it.help ? `<div class="muted" style="font-size:11px;margin-top:3px">${esc(it.help)}</div>` : '';
    return `<div class="form-row" style="align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--border)">
      <label style="min-width:190px;line-height:22px">
        ${esc(it.label)}<br>${_sourceBadge(it)}${restart}
      </label>
      <div style="flex:1;min-width:220px">${input}${help}</div>
    </div>`;
  }

  P.settings = async function () {
    const d = await get('/system/settings');
    const cats = d.categories || [];
    const groups = d.groups || {};

    let html = `<div class="page-head"><h2 class="page-title">系统设置</h2>
      <span class="page-sub">全部 ${d.count} 项配置都可在此修改，保存后立即生效，无需编辑 .env、无需重启</span>
      <div class="right">
        <button class="btn" id="btn-set-reset">放弃修改</button>
        <button class="btn btn-primary" id="btn-set-save">保存修改</button>
      </div></div>`;

    html += `<div class="note note-info" style="margin-bottom:14px">
      当前值优先级：<b>后台配置</b> &gt; .env 环境变量 &gt; 代码默认值。
      标着「后台已配置」的项以这里的取值为准；点「清除」可让它重新跟随 .env 或默认值。
      密钥类配置只显示掩码，填了新的就覆盖旧的，留空则保持原样。
    </div><div id="set-token"></div><div id="set-msg"></div>`;

    let cur = { configured: false };
    try { cur = await get('/system/proxy'); } catch (e) { cur = { configured: false }; }

    html += `<div class="card"><h3 class="card-title">网络代理 <span class="hint">Binance / Coinbase / Kraken / CoinGecko 等在国内直连会失败，配了代理才能取到</span></h3>
      <div class="muted" style="margin-bottom:8px">当前生效：${
        cur.invalid ? `<span class="badge b-bad">地址有误：${esc(cur.error || '')}</span>`
          : cur.configured ? `<code>${esc(cur.masked || '')}</code> <span class="badge b-ok">已生效</span>`
          : '<span class="badge b-mute">未配置（直连）</span>'}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input class="inp" id="px-url" placeholder="socks5://用户名:密码@IP:端口" style="min-width:320px">
        <button class="btn btn-primary" id="px-test">测试代理连通性</button>
        <button class="btn" id="px-direct">直连对照测试</button>
        <button class="btn" id="px-save">保存为全局代理</button>
      </div>
      <div class="muted" style="font-size:11px;margin-top:6px">
        写法很随意都认：<code>socks5://user:pass@1.2.3.4:1080</code>、
        <code>socks5h://...</code>、<code>proxy:user@1.2.3.4:1080</code>、
        <code>user@1.2.3.4:1080</code>。保存后<b>下一次请求即生效，不用重启</b>。
      </div>
      <div id="px-result"></div></div>`;

    html += `<div class="card"><h3 class="card-title">DNS 解析
      <span class="hint">数据源「时通时断」多半不是网络问题，而是本地 DNS 被污染</span></h3>
      <div class="muted" style="margin-bottom:8px">
        污染包会与真实应答抢答，谁先到用谁，于是同一个数据源表现为随机超时。
      </div>
      <div class="muted" style="font-size:12px;margin-bottom:8px">
        典型特征：同一域名反复解析出互不相关的 IP，尤其是 <code>2001::</code>
        开头（Teredo 保留段）的地址。
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input class="inp" id="dns-host" placeholder="api.binance.com" style="min-width:280px">
        <button class="btn btn-primary" id="dns-check">检测是否被污染</button>
      </div>
      <div class="muted" style="font-size:11px;margin-top:6px">
        检测会并列展示「本地 DNS 的答案」与「DoH 的真实答案」做对比。
        判定为污染时，到下方「网络与代理」分类把 <b>DNS 解析方式</b> 改成 <b>DoH</b>，
        保存后下一次请求即生效，不用重启。走代理时由代理方负责解析，此项不生效。
      </div>
      <div id="dns-result"></div></div>`;

    for (const c of cats) {
      const items = groups[c.code] || [];
      if (!items.length) continue;
      html += `<div class="card"><h3 class="card-title">${esc(c.name)}
        <span class="hint">${esc(c.help || '')}</span></h3>`;
      html += items.map(_fieldHtml).join('');
      html += `</div>`;
    }

    html += rawBlock('配置元数据原始返回', d);

    requestAnimationFrame(() => {
      const dirty = Object.create(null);
      const msg = document.getElementById('set-msg');

      // 保存配置是管理接口，需要 ADMIN_TOKEN。没填就没法保存 —— 这里就地给个输入框，
      // 免得用户点了保存却只看到一个「未授权」而不知道该去哪儿填。
      const tokenBox = document.getElementById('set-token');
      const paintToken = () => {
        if (!tokenBox) return;
        if (AdminAuth.has()) {
          tokenBox.innerHTML = note('ok',
            '管理令牌已就绪（仅本次会话有效），配置可以保存。' +
            ' <a href="#/admin">到后台管理更换</a>');
        } else {
          tokenBox.innerHTML = `<div class="note note-info">
            开发环境（<span class="mono">APP_ENV≠production</span>）保存配置免令牌；
            <b>生产环境需要管理令牌</b>（<span class="mono">ADMIN_TOKEN</span>，初始值在 <span class="mono">.env</span> 里，也可在本页「基础」分类修改）。
            <input class="inp" id="set-token-input" type="password" placeholder="在此粘贴 ADMIN_TOKEN" autocomplete="off">
            <button class="btn" id="set-token-save">用这个令牌</button>
            <span class="muted">只存在当前浏览器标签页，刷新标签页后需重新填。</span></div>`;
          const inp = document.getElementById('set-token-input');
          const btn = document.getElementById('set-token-save');
          if (btn) btn.onclick = () => {
            AdminAuth.set(((inp && inp.value) || '').trim());
            paintToken();
            toast('令牌已保存到当前会话');
          };
        }
      };
      paintToken();

      // ---------------- 代理连通性测试 ----------------
      const pxOut = document.getElementById('px-result');
      const pxUrl = document.getElementById('px-url');
      const egressText = (r) => {
        const hit = (r.results || []).find(x => x.key === 'egress_ip' && x.ok && x.preview);
        if (!hit) return '';
        try { return `，当前出口 IP：<b>${esc(String(JSON.parse(hit.preview).ip || '?'))}</b>`; } catch (_) { return ''; }
      };
      const renderProxyResult = (r) => {
        const rows = (r.results || []).map(x => [
          esc(x.label || x.key || ''),
          x.ok ? `<span class="badge b-ok">通过 ${x.status}</span>` : `<span class="badge b-bad">失败</span>`,
          `${x.latency_ms} ms`,
          x.ok ? `<span class="mono">${esc(String(x.preview || '').slice(0, 90))}</span>`
            : `<span class="muted">${esc(x.failure_type || '')} ${esc(String(x.message || '').slice(0, 90))}</span>`
        ]);
        pxOut.innerHTML =
          `<div style="margin-top:10px">${note(r.targets_ok ? 'ok' : 'bad',
            `${esc(r.mode)}：${r.targets_ok}/${r.targets_total} 个目标可达${egressText(r)}`)}</div>` +
          table(['探测目标', '结果', '耗时', '说明 / 错误'], rows) +
          (r.hint ? note('warn', esc(r.hint)) : '');
      };
      const runProxyTest = async (save) => {
        pxOut.innerHTML = `<div class="loading"><span class="spin"></span> 正在逐项探测…</div>`;
        try {
          const r = await post('/system/proxy/test', { proxy_url: pxUrl.value.trim(), save: !!save });
          renderProxyResult(r);
          if (save && r.saved_as) { toast('已保存为全局代理：' + r.saved_as); setTimeout(() => location.reload(), 900); }
        } catch (e) { pxOut.innerHTML = unavailable(String(e.message || e)); }
      };
      const bind = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
      bind('px-test', () => runProxyTest(false));
      bind('px-save', () => runProxyTest(true));
      bind('px-direct', async () => {
        const keep = pxUrl.value; pxUrl.value = '';
        await runProxyTest(false);
        pxUrl.value = keep;
      });

      // ---------------- DNS 污染检测 ----------------
      const dnsOut = document.getElementById('dns-result');
      const dnsHost = document.getElementById('dns-host');
      const VERDICT_TEXT = {
        clean: ['ok', '未被污染：本地 DNS 与 DoH 答案一致，无需改动。'],
        suspicious: ['warn', '高度可疑：本地 DNS 的答案与权威答案完全不一致，可能被劫持。'],
        poisoned: ['bad', '确认被污染：本地 DNS 返回了保留地址段的假 IP。'],
        unknown: ['warn', '无法判定：DoH 服务器连不上，请检查网络后重试。'],
      };
      bind('dns-check', async () => {
        const host = ((dnsHost && dnsHost.value) || '').trim();
        if (!host) { dnsOut.innerHTML = note('warn', '请填一个域名，例如 api.binance.com'); return; }
        dnsOut.innerHTML = `<div class="loading"><span class="spin"></span> 正在对比解析结果…</div>`;
        try {
          const r = await get('/system/dns/diagnose?host=' + encodeURIComponent(host));
          const [tone, headline] = VERDICT_TEXT[r.verdict] || VERDICT_TEXT.unknown;
          const rows = (r.system_ips || []).map(ip => [
            '本地 DNS 答案',
            `<span class="mono">${esc(ip)}</span>`,
            (r.poisoned_ips || []).includes(ip)
              ? '<span class="badge b-bad">假 IP（保留地址段）</span>'
              : ((r.doh_ips || []).includes(ip)
                ? '<span class="badge b-ok">与权威答案一致</span>'
                : '<span class="badge b-warn">与权威答案不一致</span>')
          ]);
          const dohRows = (r.doh_ips || []).length
            ? (r.doh_ips || []).map(ip => [
                'DoH 权威答案', `<span class="mono">${esc(ip)}</span>`,
                '<span class="badge b-ok">可信</span>'])
            : [['DoH 权威答案', '<span class="muted">未取到（DoH 服务器不可达）</span>', '—']];
          dnsOut.innerHTML =
            note(tone, `${esc(headline)} <span class="muted">（判定依据：${esc(r.verdict)}）</span>`) +
            table(['来源', '解析结果', '判定'], rows.concat(dohRows)) +
            (r.action ? note(tone, esc(r.action)) : '') +
            `<div class="muted" style="font-size:11px;margin-top:6px">${
              r.doh_endpoint ? `权威应答来自 <span class="mono">${esc(r.doh_endpoint)}</span>；` : ''
            }当前解析方式：<span class="mono">${esc(r.current_dns_mode || 'system')}</span></div>`;
        } catch (e) { dnsOut.innerHTML = unavailable(String(e.message || e)); }
      });

      const markDirty = (key, changed) => {
        if (changed) dirty[key] = true; else delete dirty[key];
        const n = Object.keys(dirty).length;
        const btn = document.getElementById('btn-set-save');
        if (btn) btn.textContent = n ? `保存修改（${n}）` : '保存修改';
      };

      document.querySelectorAll('[data-key]').forEach(el => {
        const key = el.getAttribute('data-key');
        const type = el.getAttribute('data-type');
        const sensitive = el.getAttribute('data-sensitive') === '1';
        const handler = () => {
          let v;
          if (type === 'bool') v = !!el.checked;
          else if (type === 'int') v = el.value === '' ? '' : parseInt(el.value, 10);
          else if (type === 'float') v = el.value === '' ? '' : parseFloat(el.value);
          else v = el.value;
          if (sensitive && v === '') { delete dirty[key]; }
          else { dirty[key] = v; }
          const n = Object.keys(dirty).length;
          const btn = document.getElementById('btn-set-save');
          if (btn) btn.textContent = n ? `保存修改（${n}）` : '保存修改';
        };
        el.addEventListener(type === 'bool' ? 'change' : 'input', handler);
      });

      // 清除敏感项：显式删除库里的覆盖值
      document.querySelectorAll('[data-clear]').forEach(btn => {
        btn.onclick = async () => {
          const key = btn.getAttribute('data-clear');
          if (!confirm(`确定清除「${key}」的已保存值？清除后它将跟随 .env 或默认值。`)) return;
          try {
            await post('/system/settings/reset', { keys: [key] });
            toast('已清除，正在刷新…');
            setTimeout(() => location.reload(), 600);
          } catch (e) { msg.innerHTML = unavailable(String(e.message || e)); }
        };
      });

      const saveBtn = document.getElementById('btn-set-save');
      if (saveBtn) saveBtn.onclick = async () => {
        const keys = Object.keys(dirty);
        if (!keys.length) { toast('没有需要保存的修改'); return; }
        const payload = {};
        keys.forEach(k => { payload[k] = dirty[k]; });
        saveBtn.disabled = true;
        msg.innerHTML = `<div class="loading"><span class="spin"></span> 正在保存…</div>`;
        try {
          const r = await put('/system/settings', payload);
          msg.innerHTML = `<div class="note note-ok">已保存 ${r.count} 项并立即生效，正在刷新…</div>`;
          toast('配置已保存并生效');
          setTimeout(() => location.reload(), 700);
        } catch (e) {
          saveBtn.disabled = false;
          const hint = AdminAuth.has() ? '' :
            '<br>如果服务运行在<b>生产模式</b>，保存配置需要管理令牌——请在页面顶部的输入框填入 <span class="mono">ADMIN_TOKEN</span> 后重试。';
          msg.innerHTML = `<div class="note note-bad">保存失败：${esc(String(e.message || e))}${hint}</div>`;
        }
      };

      const resetBtn = document.getElementById('btn-set-reset');
      if (resetBtn) resetBtn.onclick = () => location.reload();
    });
    return html;
  };

  // ============================================================ 数据质量
  P.quality = async function () {
    const d = await get('/system/data-quality').catch(() => null);
    let html = `<div class="page-head"><h2 class="page-title">数据质量中心</h2>
      <span class="page-sub">本地历史覆盖率、缺口、以及各数据源之间的冲突</span></div>`;
    if (!d) { html += unavailable('数据质量接口不可用'); return html; }

    const cov = d.coverage || d;
    html += `<div class="card"><h3 class="card-title">本地历史覆盖</h3><div class="grid g4">
      ${stat('日线根数', nf0(cov.count || cov.candles), '')}
      ${stat('起始日期', cov.start_ts ? dateOf(cov.start_ts) : '--', '')}
      ${stat('最新日期', cov.end_ts ? dateOf(cov.end_ts) : '--', '')}
      ${stat('应有天数', nf0(cov.expected_days || 0), '')}
      </div>
      ${cov.missing_days ? note(cov.missing_days > 5 ? 'warn' : 'ok',
        `本地序列缺失 ${cov.missing_days} 天，可在「系统任务」执行补洞任务自动修复。`) : ''}
      </div>`;

    const gaps = d.gaps || [];
    if (gaps.length) {
      html += card('发现的数据缺口', table(['缺口时间戳', '对应日期'],
        gaps.slice(0, 50).map(g => [`<span class="mono">${g}</span>`, dateOf(g)])));
    }
    const conflicts = d.conflicts || [];
    if (conflicts.length) {
      html += card('数据源冲突记录 <span class="hint">系统不会偷偷选一个，而是如实记录</span>',
        table(['时间', '类别', '各源取值', '判定'], conflicts.slice(0, 30).map(c => [
          tsLocal(c.ts || c.created_at), esc(c.category || ''),
          `<span class="mono" style="font-size:11px">${esc(JSON.stringify(c.values || c.sources || {}))}</span>`,
          qualityBadge(c.verdict || c.quality)])));
    }
    const qrows = d.quality_records || d.records || [];
    if (qrows.length) {
      html += card('完整性检查历史', table(['类别', '应有', '实有', '缺失', '完整性', '状态'],
        qrows.slice(0, 30).map(q => [esc(q.category), nf0(q.expected), nf0(q.actual), nf0(q.missing_count),
        `${nf((q.completeness || 0) * 100, 2)}%`,
        q.status === 'ok' ? '<span class="badge b-ok">正常</span>' : (q.status === 'warn' ? '<span class="badge b-warn">警告</span>' : '<span class="badge b-bad">异常</span>')])));
    }
    html += rawBlock('接口原始返回', d);
    return html;
  };

  // ============================================================ 系统任务
  P.jobs = async function () {
    const [j, st] = await Promise.all([
      get('/system/jobs').catch(() => null),
      get('/system/stats').catch(() => null)
    ]);
    const jobs = (j && j.jobs) || [];
    let html = `<div class="page-head"><h2 class="page-title">系统任务</h2>
      <span class="page-sub">每个任务都可暂停/恢复/手动执行；长时间任务支持断点续传</span></div>`;

    html += `<div class="card"><h3 class="card-title">数据表统计</h3><div class="grid g4">
      ${Object.entries(st && st.tables ? st.tables : {}).filter(([, v]) => v > 0).slice(0, 24)
        .map(([k, v]) => stat(esc(k), nf0(v), '')).join('')}
      </div></div>`;

    if (jobs.length) {
      html += card('任务列表', table(['任务', '执行间隔', '上次运行', '状态', '说明', '操作'],
        jobs.map(x => [
          `<b>${esc(x.name || x.job_id)}</b><br><span class="mono muted" style="font-size:11px">${esc(x.job_id || '')}</span>`,
          esc(x.interval || x.trigger || ''),
          tsLocal(x.last_run),
          x.last_status === 'ok' ? '<span class="badge b-ok">正常</span>'
            : (x.last_status ? `<span class="badge b-bad">${esc(x.last_status)}</span>` : '<span class="badge b-mute">未运行</span>'),
          `<span class="muted" style="font-size:11px">${esc(x.description || '')}</span>`,
          `<button class="btn btn-jobr" data-id="${esc(x.job_id)}">立即执行</button>`])));
    } else {
      html += `<div class="card"><div class="empty">当前没有运行中的任务</div></div>`;
    }

    // 历史回填（支持断点续传）
    html += `<div class="card"><h3 class="card-title">历史数据回填 <span class="hint">抓一次，永久保存在本地</span></h3>
      <div class="form-row">
        <label>周期</label><select class="inp" id="bf-interval">
          <option value="1d">日线</option><option value="1h">小时线</option><option value="4h">4 小时线</option></select>
        <label>起始日期</label><input class="inp" type="date" id="bf-start" value="2013-01-01">
        <label>结束日期</label><input class="inp" type="date" id="bf-end">
        <button class="btn btn-primary" id="bf-go">开始回填</button>
      </div>
      <div id="bf-result"></div>
      ${note('info', '回填支持<b>断点续传</b>：如果中途失败或中断，已抓取的数据会保留，下次继续从断点往后抓，不会重复劳动。')}
      </div>`;

    requestAnimationFrame(() => {
      document.querySelectorAll('.btn-jobr').forEach(b => {
        b.onclick = async () => {
          b.disabled = true; const t = b.textContent; b.textContent = '执行中…';
          try { const r = await post(`/system/jobs/${b.dataset.id}/run`); toast('任务已触发'); console.log(r); }
          catch (e) { toast(String(e.message || e), true); }
          b.disabled = false; b.textContent = t;
        };
      });
      const go = document.getElementById('bf-go');
      if (go) go.onclick = async () => {
        const box = document.getElementById('bf-result');
        box.innerHTML = `<div class="loading"><span class="spin"></span> 正在回填历史数据，请稍候…</div>`;
        try {
          const r = await post('/system/backfill', null, {
            interval: document.getElementById('bf-interval').value,
            start_date: document.getElementById('bf-start').value,
            end_date: document.getElementById('bf-end').value,
          });
          box.innerHTML = `<div class="note ${r.ok ? 'note-ok' : 'note-warn'}">
            写入 ${nf0(r.rows || 0)} 根${r.stopped_at ? '，中断于 ' + esc(r.stopped_at) : ''}
            ${(r.uncovered_windows || []).length ? '<br>以下窗口所有数据源均无数据（未用假数据填补）：' + esc(r.uncovered_windows.join('、')) : ''}
            </div>` + rawBlock('回填原始返回', r);
        } catch (e) { box.innerHTML = unavailable(String(e.message || e)); }
      };
    });
    return html;
  };

  // ============================================================ 后台管理
  P.admin = async function () {
    const [info, fo] = await Promise.all([
      get('/system/info').catch(() => null),
      get('/system/failover-events').catch(() => null)
    ]);
    let html = `<div class="page-head"><h2 class="page-title">后台管理</h2>
      <span class="page-sub">系统信息、数据源开关与故障策略</span></div>`;

    if (info) {
      html += `<div class="card"><h3 class="card-title">系统信息 <span class="hint">不含任何密钥</span></h3>
        <dl class="kv">${Object.entries(info).map(([k, v]) =>
        `<dt>${esc(k)}</dt><dd>${esc(typeof v === 'object' ? JSON.stringify(v) : String(v))}</dd>`).join('')}</dl>
        </div>`;
    }

    html += `<div class="card"><h3 class="card-title">写操作鉴权 <span class="hint">令牌只存在当前标签页，关闭即失效</span></h3>
      <div class="form-row">
        <label>管理员令牌</label>
        <input class="inp" type="password" id="adm-token" placeholder="X-Admin-Token，留空表示尚未设置" style="min-width:260px">
        <button class="btn" id="adm-token-save">保存</button>
        <button class="btn" id="adm-token-clear">清除</button>
        <span class="muted" id="adm-token-state"></span>
      </div>
      <div class="muted" style="font-size:12px">
        取值为服务端 <span class="mono">.env</span> 中的 <span class="mono">ADMIN_TOKEN</span>。
        停/启用数据源、回填、测试全源等写操作都要带它；不填时页面会如实报错 401/403，不会假装成功。
      </div>
      </div>`;

    html += `<div class="card"><h3 class="card-title">数据源开关与优先级</h3>
      <div class="form-row">
        <label>数据源</label><input class="inp" id="adm-name" placeholder="例如 binance" style="min-width:180px">
        <select class="inp" id="adm-action">
          <option value="enable">启用</option>
          <option value="disable">停用</option>
          <option value="unlock">解除锁定</option>
        </select>
        <label>优先级</label><input class="inp" type="number" id="adm-priority" placeholder="留空表示不改" min="1">
        <button class="btn btn-primary" id="adm-apply">应用</button>
      </div>
      <div id="adm-result"></div>
      ${note('warn', '调整需要管理员权限。停用某个数据源后，系统会自动切换到下一个可用源，业务层不需要任何改动。')}
      </div>`;

    html += `<div class="card"><h3 class="card-title">故障处置策略 <span class="hint">系统内置的自动行为</span></h3>` +
      table(['故障类型', '系统自动采取的动作'], [
        ['timeout / connect_error / dns_error', '自动切换到下一个数据源'],
        ['rate_limited (429)', '降低请求频率并按退避策略重试'],
        ['server_error (5xx)', '重试后仍失败则切换'],
        ['forbidden (403)', '临时停用该数据源并提示管理员'],
        ['auth_error', '不无限重试，提示管理员检查密钥'],
        ['not_configured', '明确显示「数据源未配置」，相关模块置为不可用'],
        ['data_format_error', '接口疑似变更，临时停用并记录'],
        ['data_quality_error', '拒绝写入脏数据并切换到下一个源'],
        ['unsupported_input', '该源不提供此项，仅切下一个源，不停用'],
      ].map(([a, b]) => [`<span class="mono">${esc(a)}</span>`, `<span class="muted">${esc(b)}</span>`])) + `</div>`;

    requestAnimationFrame(() => {
      const tokenInput = document.getElementById('adm-token');
      const tokenState = document.getElementById('adm-token-state');
      const paint = () => {
        if (tokenState) tokenState.textContent = AdminAuth.has() ? '已保存（本次会话有效）' : '未设置';
      };
      paint();
      const save = document.getElementById('adm-token-save');
      if (save) save.onclick = () => { AdminAuth.set(tokenInput.value.trim()); tokenInput.value = ''; paint(); toast('令牌已保存到当前会话'); };
      const clear = document.getElementById('adm-token-clear');
      if (clear) clear.onclick = () => { AdminAuth.clear(); paint(); toast('已清除令牌'); };

      const btn = document.getElementById('adm-apply');
      if (!btn) return;
      btn.onclick = async () => {
        const box = document.getElementById('adm-result');
        const name = document.getElementById('adm-name').value.trim();
        if (!name) { toast('请填写数据源名称', true); return; }
        const action = document.getElementById('adm-action').value;
        // 同时下发语义化的布尔字段与 action 简写，避免任一侧解析差异造成空操作
        const body = { action };
        if (action === 'enable') body.enabled = true;
        if (action === 'disable') body.enabled = false;
        if (action === 'unlock') body.locked = false;
        const pr = document.getElementById('adm-priority').value;
        if (pr) body.priority = +pr;
        try {
          const r = await patch('/system/providers/' + encodeURIComponent(name), body);
          box.innerHTML = `<div class="note note-ok">已变更：${esc((r.changed || []).join(', '))}</div>` + rawBlock('返回', r);
          toast('已应用');
        } catch (e) {
          const msg = String(e.message || e);
          const auth = /401|403|令牌|权限/.test(msg);
          box.innerHTML = note('warn', `<b>未执行变更</b><br>${esc(msg)}` +
            (auth ? '<br>请先在上方「写操作鉴权」中填入与服务端 ADMIN_TOKEN 一致的令牌。' : ''));
          toast('变更失败', true);
        }
      };
    });
    return html;
  };

  global.PS = P;
})(window);
