/* pages-alerts.js —— 智能预警中心
 *
 * 设计原则（对应需求一/三十一/三十四/三十七/四十）：
 * 1. 通用化：所有条件都从 /alerts/catalog 拉取指标与运算符，页面本身不认识任何具体指标。
 *    后端新增一种可监测数据时，这里不需要改一行代码。
 * 2. 普通模式 / 高级模式分离：
 *    - 普通模式：模板 + 下拉选择 + 填阈值，面向非金融用户，自带通俗解释。
 *    - 高级模式：多条件 AND/OR/NOT、嵌套分组、分位、交叉、持续、连续、Provider 确认。
 * 3. 只做事实与统计：所有文案都避免任何「该买/该卖」的暗示，并明确写出「系统只提醒、不自动交易」。
 */
(function (global) {
  'use strict';

  const C = global.C;
  const {
    get, post, put, del, card, stat, note, rawBlock, table, nf, nf0, esc,
    tsLocal, agoText, unavailable, qualityBadge, toast, AdminAuth
  } = C;

  const P = {};

  const SEV_CN = { INFO: '信息', WARNING: '提醒', HIGH: '重要', CRITICAL: '严重' };
  const SEV_BADGE = { INFO: 'b-info', WARNING: 'b-warn', HIGH: 'b-bad', CRITICAL: 'b-bad' };
  const STATE_CN = {
    NORMAL: '正常监测中', TRIGGERED: '已触发', COOLDOWN: '冷却中', RECOVERED: '已恢复'
  };
  const NOTIFY_CN = {
    PENDING: '待发送', SENDING: '发送中', SENT: '已发送', FAILED: '发送失败', SKIPPED: '已跳过'
  };

  function sevBadge(s) {
    return `<span class="badge ${SEV_BADGE[s] || 'b-mute'}">${SEV_CN[s] || s || '提醒'}</span>`;
  }
  function notifyBadge(s) {
    return `<span class="badge ${s === 'SENT' ? 'b-ok' : (s === 'FAILED' ? 'b-bad' : 'b-info')}">${NOTIFY_CN[s] || s}</span>`;
  }

  // ---------------------------------------------------------------- 状态缓存
  const S = {
    catalog: null,
    rules: [],
    events: [],
    channels: [],
    stats: null,
    eventDays: '30'
  };

  async function needCatalog() {
    if (!S.catalog) S.catalog = await get('/alerts/catalog');
    return S.catalog;
  }

  function metricByCode(code) {
    if (!S.catalog) return null;
    return S.catalog.metrics.find(m => m.code === code) || null;
  }
  function metricName(code) {
    const m = metricByCode(code);
    return m ? m.name_cn : code;
  }
  function metricUnit(code) {
    const m = metricByCode(code);
    return m ? (m.unit || '') : '';
  }
  function operatorsFor(metricCode) {
    const m = metricByCode(metricCode);
    if (!S.catalog) return [];
    return S.catalog.operators.filter(o => {
      if (o.needs_window && m && !m.supports_change) return false;
      return true;
    });
  }

  // ---------------------------------------------------------------- 概览

  P.alerts = async function () {
    await needCatalog();
    let s = null;
    try { s = await get('/alerts/stats', { days: 7 }); } catch (_) { s = null; }
    S.stats = s;

    let rules = [];
    try { rules = (await get('/alerts/rules', { limit: 500 })).rules || []; } catch (_) { rules = []; }
    S.rules = rules;

    const head = `
      <div class="page-head">
        <h2>智能预警中心</h2>
        <p class="muted">
          在这里设置「什么情况下通知我」。系统在后台持续检测，条件成立时发邮件到你的邮箱。
          <b>系统只做提醒，不会自动交易，也不构成任何买卖建议。</b>
        </p>
      </div>`;

    const statsRow = s ? `<div class="stat-row">
      ${stat('监测规则', nf0(s.rules_total), `${nf0(s.rules_active)} 条正在生效`)}
      ${stat('近 7 天提醒', nf0(s.events_in_window), '按触发时间统计')}
      ${stat('发送失败未送达', nf0(s.failed_notifications),
        s.failed_notifications > 0 ? '需要检查邮箱配置' : '全部正常',
        s.failed_notifications > 0 ? 'down' : '')}
      ${stat('邮件通道', s.smtp_ready ? '已配置' : '未配置',
        s.smtp_ready ? esc((s.smtp.recipients || []).join(', ')) : '配好后才能收到提醒',
        s.smtp_ready ? '' : 'down')}
    </div>` : '';

    const smtpWarn = (!s || !s.smtp_ready) ? note('warn',
      '<b>邮件通道还没配置</b><br>' +
      '规则可以正常保存并触发，但触发后<strong>发不出邮件</strong>（事件会保留为「发送失败」，不会丢）。' +
      '请到下方「邮件通知设置」里填写 SMTP 服务器、发件人、收件人，然后用「发送测试邮件」验证。'
    ) : '';

    const sev = (s && s.events_by_severity) || {};
    const sevLine = ['CRITICAL', 'HIGH', 'WARNING', 'INFO']
      .map(k => `${sevBadge(k)} <b>${nf0(sev[k] || 0)}</b>`).join(' &nbsp; ');

    return head + statsRow + smtpWarn +
      card('近 7 天提醒等级分布', `<div class="muted">${sevLine}</div>`, '等级只表示提醒的紧急程度，不代表行情好坏') +
      card('快速开始',
        `<div class="quick-grid">
          ${(_templates().length ? _templates() : []).slice(0, 6).map(t => `
            <button class="quick-tpl" data-tpl="${esc(t.code)}">
              <b>${esc(t.name)}</b>
              <span class="muted">${esc(t.description)}</span>
            </button>`).join('')}
        </div>
        <div class="muted" style="margin-top:10px">
          点一个模板即可跳到「新建规则」并把条件填好，你再按自己的情况改数值就行。
        </div>`,
        '不知道从哪开始？先用一个模板') +
      _rulesTable(rules) +
      _eventsCard() +
      _smtpCard() +
      rawBlock('预警中心原始数据', { stats: s, rules_count: rules.length });
  };

  function _templates() {
    return (S.catalog && S.catalog.templates) || [];
  }

  // ---------------------------------------------------------------- 规则列表

  function _rulesTable(rules) {
    if (!rules.length) {
      return card('我的监测规则',
        note('info', '还没有任何规则。<br>可以先点上面的模板快速创建一条，或者点「新建规则」自己设定条件。'));
    }
    const rows = rules.map(r => {
      const stateBadge = r.state === 'TRIGGERED'
        ? `<span class="badge b-bad">${STATE_CN[r.state]}</span>`
        : (r.paused ? '<span class="badge b-mute">已暂停</span>'
          : `<span class="badge b-ok">${STATE_CN[r.state] || r.state}</span>`);
      return `<tr>
        <td>${esc(r.name)}<div class="muted" style="font-size:12px">${esc(r.expression || '')}</div></td>
        <td>${sevBadge(r.severity)}</td>
        <td>${stateBadge}</td>
        <td class="mono">${nf0(r.trigger_count)}</td>
        <td class="muted">${r.last_trigger_at ? tsLocal(r.last_trigger_at) + '<br><span style="font-size:11px">' + agoText(r.last_trigger_at) + '</span>' : '从未触发'}</td>
        <td class="mono">${nf0(Math.round((r.check_interval_seconds || 60) / 60))} 分钟</td>
        <td class="act">
          <button class="mini" data-act="test" data-id="${r.id}" title="立刻判断当前是否成立，不写状态">测试</button>
          <button class="mini" data-act="run" data-id="${r.id}" title="立即执行一次，会真正触发并发送通知">立即执行</button>
          <button class="mini" data-act="${r.paused ? 'resume' : 'pause'}" data-id="${r.id}">${r.paused ? '恢复' : '暂停'}</button>
          <button class="mini" data-act="dup" data-id="${r.id}">复制</button>
          <button class="mini danger" data-act="del" data-id="${r.id}">删除</button>
        </td>
      </tr>`;
    }).join('');

    return card('我的监测规则',
      `<div class="muted" style="margin-bottom:8px">
         共 ${rules.length} 条。「检测周期」是最短多久检查一次；实际触发还受「持续时间」与「冷却时间」约束。
       </div>
       <table class="tbl"><thead><tr>
         <th>规则 / 条件</th><th>等级</th><th>状态</th><th>触发次数</th><th>最近触发</th><th>检测周期</th><th>操作</th>
       </tr></thead><tbody>${rows}</tbody></table>
       <div id="rule-test-result" style="margin-top:10px"></div>`,
      '所有规则都在后台跑，关掉网页也会继续监测');
  }

  /** 绑定规则表格上的按钮（在 alerts 与 rules 页都要用） */
  function bindRuleActions(root) {
    root.querySelectorAll('[data-act]').forEach(btn => {
      btn.onclick = async () => {
        const id = btn.dataset.id;
        const act = btn.dataset.act;
        const box = root.querySelector('#rule-test-result') || document.getElementById('rule-test-result');
        try {
          if (act === 'test') {
            btn.disabled = true; btn.textContent = '测试中…';
            const r = await post(`/alerts/rules/${id}/test`);
            if (box) box.innerHTML = _testResultHtml(r);
            toast(r.satisfied ? '当前条件已成立' : '当前条件尚未成立');
          } else if (act === 'run') {
            if (!confirm('立即执行会真正触发一次检测并发送邮件（如果条件成立）。继续？')) return;
            btn.disabled = true; btn.textContent = '执行中…';
            const r = await post(`/alerts/rules/${id}/run`);
            if (box) box.innerHTML = note('ok',
              `已执行：本轮检查 ${r.checked} 条规则，触发 ${r.triggered} 次。`) + rawBlock('执行结果', r);
            toast('已执行');
            setTimeout(() => location.reload(), 1200);
          } else if (act === 'pause' || act === 'resume') {
            const r = await post(`/alerts/rules/${id}/${act}`);
            toast(r.message || '已更新');
            setTimeout(() => location.reload(), 600);
          } else if (act === 'dup') {
            const r = await post(`/alerts/rules/${id}/duplicate`, undefined);
            toast('已复制：' + r.name);
            setTimeout(() => location.reload(), 600);
          } else if (act === 'del') {
            if (!confirm('删除这条规则？历史触发记录会保留，便于事后追溯。')) return;
            await del(`/alerts/rules/${id}`);
            toast('已删除');
            setTimeout(() => location.reload(), 600);
          }
        } catch (e) {
          const msg = String(e.message || e);
          const auth = /401|403|令牌|权限/.test(msg);
          if (box) box.innerHTML = note('warn', `<b>操作未完成</b><br>${esc(msg)}` +
            (auth ? '<br>请先设置写操作令牌（见「系统 → 后台管理」或下方鉴权区）。' : ''));
          toast('操作失败', true);
        } finally {
          btn.disabled = false;
        }
      };
    });
  }

  function _testResultHtml(r) {
    if (!r) return '';
    const tone = r.would_notify ? 'ok' : (r.satisfied ? 'warn' : 'info');
    const head = r.would_notify
      ? '<b>现在成立，并且会发送提醒</b>'
      : (r.satisfied ? '<b>条件成立，但被数据质量门槛拦下了，不会发提醒</b>' : '<b>现在还不成立</b>');
    const rows = (r.conditions || []).map(c => `<tr>
        <td>${esc(c.description || c.metric_code)}</td>
        <td class="mono">${c.actual === null || c.actual === undefined ? '--' : esc(String(c.actual))}</td>
        <td class="muted mono">${c.threshold === null || c.threshold === undefined ? '--' : esc(String(c.threshold))}${c.threshold_high ? ' ~ ' + esc(String(c.threshold_high)) : ''}</td>
        <td>${c.satisfied ? '<span class="badge b-ok">已满足</span>' : (c.available ? '<span class="badge b-mute">未满足</span>' : '<span class="badge b-warn">数据不可用</span>')}</td>
        <td class="muted">${esc(c.reason || '')}</td>
      </tr>`).join('');

    const ctx = r.market_context || {};
    const ctxLine = Object.keys(ctx).length
      ? Object.entries(ctx).filter(([, v]) => v !== null && v !== undefined && v !== '')
        .map(([k, v]) => `${esc(k)}=${esc(String(v))}`).join(' · ')
      : '（暂无市场状态快照）';

    return note(tone, head) +
      (r.quality_gate && !r.quality_gate.passed ? note('warn', '数据质量门槛：' + esc(r.quality_gate.reason)) : '') +
      `<table class="tbl" style="margin-top:8px"><thead><tr>
        <th>你的条件</th><th>实际值</th><th>阈值</th><th>结论</th><th>判定依据</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="muted" style="margin-top:8px;font-size:12px">
        当时市场状态：${ctxLine}<br>
        数据来源：${esc((r.providers_used || []).join(' → ') || '未知')}
        ${r.used_fallback ? '<span class="badge b-warn">已切换备用源</span>' : ''}
        质量：${qualityBadge(r.data_quality)}
      </div>`;
  }

  // ---------------------------------------------------------------- 新建 / 编辑规则

  P.rules = async function (params) {
    await needCatalog();
    const editId = params && params.get('id') ? params.get('id') : null;
    let editing = null;
    if (editId) {
      try { editing = await get(`/alerts/rules/${editId}`); } catch (_) { editing = null; }
    }

    const tplCode = params && params.get('tpl') ? params.get('tpl') : null;
    let tpl = null;
    if (!editing && tplCode) {
      tpl = _templates().find(t => t.code === tplCode) || null;
    }

    const metrics = S.catalog.metrics;
    const groups = S.catalog.by_group || {};
    const groupNames = Object.keys(groups);

    const metricOptions = groupNames.map(g =>
      `<optgroup label="${esc(g)}">` +
      groups[g].map(m => `<option value="${esc(m.code)}">${esc(m.name_cn)}${m.unit ? '（' + esc(m.unit) + '）' : ''}</option>`).join('') +
      `</optgroup>`).join('');

    const initName = editing ? editing.name : (tpl ? tpl.name : '');
    const initDesc = editing ? (editing.description || '') : (tpl ? tpl.description : '');
    const initSev = editing ? editing.severity : (tpl ? tpl.severity : 'WARNING');
    const initCd = editing ? editing.cooldown_seconds : (tpl ? tpl.cooldown_seconds : 86400);
    const initInterval = editing ? editing.check_interval_seconds : 60;
    // 默认 SINGLE_SOURCE：单源真实数据已属可信（来源会被如实记录），
    // 若默认 VERIFIED 则网络一般时规则会「成立却永不发信」。
    const initMinQ = editing ? editing.min_quality : 'SINGLE_SOURCE';
    const initFresh = editing ? editing.require_fresh : true;
    const initMulti = editing ? editing.require_multi_source : false;
    const initRecover = editing ? editing.notify_on_recover : false;

    const tree = editing ? (editing.groups || []) : (tpl ? tpl.groups : [{ operator: 'AND', conditions: [_blankCond()] }]);

    const head = `<div class="page-head">
      <h2>${editing ? '编辑规则 · ' + esc(editing.name) : (tpl ? '从模板创建规则 · ' + esc(tpl.name) : '新建监测规则')}</h2>
      <p class="muted">${tpl ? esc(tpl.hint || '') : '设置「什么情况下通知我」。保存后系统会自动在后台持续检测。'}</p>
    </div>`;

    const basic = card('1. 基本信息', `
      <div class="form-grid">
        <label>规则名称 <input id="r-name" value="${esc(initName)}" placeholder="例如：价格跌到我的成本线以下" /></label>
        <label>提醒等级
          <select id="r-sev">
            ${['INFO', 'WARNING', 'HIGH', 'CRITICAL'].map(s =>
      `<option value="${s}"${s === initSev ? ' selected' : ''}>${SEV_CN[s]}（${s}）</option>`).join('')}
          </select>
        </label>
        <label class="wide">这条规则是干什么的（可选）
          <input id="r-desc" value="${esc(initDesc)}" placeholder="写给未来的自己看，例如「回撤到 30% 再看一眼」" />
        </label>
      </div>
      <div class="muted" style="margin-top:8px;font-size:12px">
        提醒等级只代表<b>紧急程度</b>，不表示行情好坏。等级不同的规则可以设置不同的冷却时间。
      </div>`);

    const condCard = card('2. 触发条件', _conditionBuilderHtml(tree),
      '普通模式只需选「监测对象 + 条件 + 数值」；需要复杂组合时切换到高级模式');

    const timing = card('3. 检测与冷却', `
      <div class="form-grid">
        <label>检测周期（秒）
          <input id="r-interval" type="number" min="30" max="604800" value="${initInterval}" />
          <span class="muted">最短 30 秒。价格类建议 60；链上/宏观类建议 3600 以上。</span>
        </label>
        <label>冷却时间（秒）
          <input id="r-cooldown" type="number" min="60" max="31536000" value="${initCd}" />
          <span class="muted">触发后这段时间内不重复通知，避免同一件事反复发邮件。</span>
        </label>
      </div>
      <div class="muted" style="margin-top:6px;font-size:12px">
        常用值：1 小时 = 3600 · 12 小时 = 43200 · 1 天 = 86400 · 7 天 = 604800
      </div>`);

    const qualityCard = card('4. 数据质量要求', `
      <div class="form-grid">
        <label>最低数据质量
          <select id="r-minq">
            ${[
        ['ANY', '不限（只要有数据）'],
        ['SINGLE_SOURCE', '单源可用即可'],
        ['VERIFIED', '需通过交叉验证（推荐）'],
        ['CROSS_VERIFIED', '需多个独立数据源一致（最严格）']
      ].map(([v, t]) => `<option value="${v}"${v === initMinQ ? ' selected' : ''}>${t}</option>`).join('')}
          </select>
        </label>
        <label class="chk"><input type="checkbox" id="r-fresh" ${initFresh ? 'checked' : ''} /> 数据必须是新鲜的（拒绝用过期数据触发）</label>
        <label class="chk"><input type="checkbox" id="r-multi" ${initMulti ? 'checked' : ''} /> 必须至少有 2 个独立数据源确认</label>
        <label class="chk"><input type="checkbox" id="r-recover" ${initRecover ? 'checked' : ''} /> 条件恢复时也通知我一次</label>
      </div>
      <div class="muted" style="margin-top:8px;font-size:12px">
        这几项是<b>防误报</b>用的。数据源故障、数据过期时，系统宁可<strong>不发提醒</strong>，
        也不会拿不可靠的数据去触发一条「重要提醒」。
      </div>`);

    const actions = `<div class="form-actions">
      <button class="btn primary" id="r-save">${editing ? '保存修改' : '保存并开始监测'}</button>
      <button class="btn" id="r-test">先测试一下现在是否成立</button>
      ${editing ? '' : '<button class="btn" id="r-preview">预览邮件样式</button>'}
      <button class="btn" id="r-cancel">取消</button>
    </div>
    <div id="r-result" style="margin-top:12px"></div>`;

    const script = `
      <script>
      (function(){
        var CATALOG = ${JSON.stringify(S.catalog)};
        ${_builderScript()}
      })();
      </script>`;

    return head + basic + condCard + timing + qualityCard + actions + script + rawBlock('指标目录（可作为条件的全部数据）', S.catalog);
  };

  function _blankCond() {
    return { metric_code: 'price', operator: 'gt', threshold: null, duration_seconds: 0, consecutive_count: 1 };
  }

  function _conditionBuilderHtml(tree) {
    return `
      <div class="adv-toggle">
        <button class="mode-btn active" data-adv="simple">普通模式</button>
        <button class="mode-btn" data-adv="pro">高级模式</button>
        <span class="muted" style="margin-left:10px;font-size:12px">
          普通模式适合大多数情况；高级模式支持多条件组合（并且/或者/都不满足）与嵌套分组。
        </span>
      </div>
      <div id="cond-root"></div>
      <div id="cond-init" style="display:none">${esc(JSON.stringify(tree))}</div>`;
  }

  /** 条件编辑器：普通/高级共用同一份数据模型，切换模式不丢已填内容 */
  function _builderScript() {
    return `
      var S = { advanced: false, tree: null };

      function el(id){ return document.getElementById(id); }
      function esc2(s){ return String(s==null?'':s).replace(/[&<>"']/g, function(c){
        return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]; }); }

      function metric(code){
        for (var i=0;i<CATALOG.metrics.length;i++) if (CATALOG.metrics[i].code===code) return CATALOG.metrics[i];
        return null;
      }
      function opsFor(code){
        var m = metric(code); var out=[];
        for (var i=0;i<CATALOG.operators.length;i++){
          var o = CATALOG.operators[i];
          if (o.needs_window && m && !m.supports_change) continue;
          out.push(o);
        }
        return out;
      }
      function opMeta(code){
        for (var i=0;i<CATALOG.operators.length;i++) if (CATALOG.operators[i].code===code) return CATALOG.operators[i];
        return null;
      }

      function init(){
        try { S.tree = JSON.parse(el('cond-init').textContent || '[]'); } catch(e){ S.tree=[]; }
        if (!S.tree || !S.tree.length) S.tree = [{ operator:'AND', conditions:[{metric_code:'price',operator:'gt',threshold:null,duration_seconds:0,consecutive_count:1}] }];
        normalize(S.tree);
        render();
        var btns = document.querySelectorAll('[data-adv]');
        for (var i=0;i<btns.length;i++){
          btns[i].onclick = function(){
            S.advanced = this.getAttribute('data-adv')==='pro';
            for (var j=0;j<btns.length;j++) btns[j].classList.toggle('active', btns[j]===this);
            render();
          };
        }
      }
      function normalize(groups){
        groups.forEach(function(g){
          if (!g.operator) g.operator='AND';
          if (!g.conditions) g.conditions=[];
          if (!g.groups) g.groups=[];
          g.conditions.forEach(function(c){
            if (c.duration_seconds==null) c.duration_seconds=0;
            if (c.consecutive_count==null) c.consecutive_count=1;
          });
          normalize(g.groups);
        });
      }

      function metricSelect(code, path){
        var groups = CATALOG.by_group || {};
        var html = '<select data-f="metric_code" data-path="'+path+'">';
        Object.keys(groups).forEach(function(g){
          html += '<optgroup label="'+esc2(g)+'">';
          groups[g].forEach(function(m){
            html += '<option value="'+esc2(m.code)+'"'+(m.code===code?' selected':'')+'>'+esc2(m.name_cn)+(m.unit?'（'+esc2(m.unit)+'）':'')+'</option>';
          });
          html += '</optgroup>';
        });
        return html+'</select>';
      }

      function condRow(c, path, groupPath){
        var m = metric(c.metric_code);
        var ops = opsFor(c.metric_code);
        var meta = opMeta(c.operator);
        var html = '<div class="cond-row" data-path="'+path+'">';
        html += '<div class="cond-main">';
        html += metricSelect(c.metric_code, path);
        html += '<select data-f="operator" data-path="'+path+'">';
        ops.forEach(function(o){
          html += '<option value="'+esc2(o.code)+'"'+(o.code===c.operator?' selected':'')+'>'+esc2(o.label)+'</option>';
        });
        html += '</select>';

        if (meta && meta.needs_window) {
          html += '<select data-f="change_window" data-path="'+path+'">';
          (CATALOG.change_windows||['1h','4h','12h','24h','7d','30d']).forEach(function(w){
            html += '<option value="'+w+'"'+(c.change_window===w?' selected':'')+'>'+w+'</option>';
          });
          html += '</select>';
        }
        if (meta && meta.needs_compare) {
          html += '<span class="muted">对比</span>';
          html += metricSelect(c.compare_metric || 'ma200', path).replace('data-f="metric_code"','data-f="compare_metric"');
        }
        if (meta && meta.needs_state) {
          html += '<input data-f="expected_state" data-path="'+path+'" value="'+esc2(c.expected_state||'')+'" placeholder="目标状态值" style="width:130px" />';
        }
        var arity = meta ? meta.arity : 1;
        if (arity===1) {
          html += '<input type="number" step="any" data-f="threshold" data-path="'+path+'" value="'+(c.threshold==null?'':c.threshold)+'" placeholder="数值" style="width:110px" />';
          if (m && m.unit) html += '<span class="muted">'+esc2(m.unit)+'</span>';
        } else if (arity===2) {
          html += '<input type="number" step="any" data-f="threshold" data-path="'+path+'" value="'+(c.threshold==null?'':c.threshold)+'" placeholder="下界" style="width:100px" />';
          html += '<span class="muted">~</span>';
          html += '<input type="number" step="any" data-f="threshold_high" data-path="'+path+'" value="'+(c.threshold_high==null?'':c.threshold_high)+'" placeholder="上界" style="width:100px" />';
        }
        html += '<button class="mini danger" data-op="del-cond" data-path="'+path+'">移除</button>';
        html += '</div>';

        if (m && m.description) {
          html += '<div class="cond-help">'+esc2(m.description)+'</div>';
        }
        if (S.advanced) {
          html += '<div class="cond-adv">'
            + '<label>持续 <input type="number" min="0" data-f="duration_seconds" data-path="'+path+'" value="'+(c.duration_seconds||0)+'" style="width:90px" /> 秒后仍成立才提醒</label>'
            + '<label>连续 <input type="number" min="1" data-f="consecutive_count" data-path="'+path+'" value="'+(c.consecutive_count||1)+'" style="width:70px" /> 次检测都成立才提醒</label>'
            + '</div>';
        }
        html += '</div>';
        return html;
      }

      function groupBlock(g, gi, parentPath){
        var path = parentPath ? (parentPath+'.groups.'+gi) : (''+gi);
        var html = '<div class="cond-group">';
        if (S.advanced) {
          html += '<div class="group-head"><span class="muted">这组条件需要</span>';
          html += '<select data-f="operator" data-path="'+path+'">'
            + ['AND','OR','NOT'].map(function(o){
                var label = o==='AND'?'全部满足（并且）':(o==='OR'?'任意一个满足（或者）':'全部都不满足');
                return '<option value="'+o+'"'+(g.operator===o?' selected':'')+'>'+label+'</option>';
              }).join('')
            + '</select>';
          html += '<input data-f="label" data-path="'+path+'" value="'+esc2(g.label||'')+'" placeholder="给这组起个名字（可选）" />';
          html += '<button class="mini" data-op="del-group" data-path="'+path+'">移除该组</button>';
          html += '</div>';
        }
        g.conditions.forEach(function(c, ci){ html += condRow(c, path+'.conditions.'+ci, path); });
        g.groups.forEach(function(sub, si){ html += groupBlock(sub, si, path); });

        html += '<div class="group-actions">'
          + '<button class="mini" data-op="add-cond" data-path="'+path+'">+ 加一个条件</button>'
          + (S.advanced ? '<button class="mini" data-op="add-group" data-path="'+path+'">+ 加一个子分组（嵌套）</button>' : '')
          + '</div>';
        html += '</div>';
        return html;
      }

      function render(){
        var root = el('cond-root');
        var html = '';
        if (S.advanced) {
          html += '<div class="muted" style="margin-bottom:8px">根层组合方式：</div>';
          html += '<select id="root-logic" class="mb8">'
            + '<option value="AND">根层：全部组都要满足（并且）</option>'
            + '<option value="OR">根层：任意一组满足即可（或者）</option>'
            + '</select>';
        }
        S.tree.forEach(function(g, gi){ html += groupBlock(g, gi, ''); });
        if (S.advanced) html += '<button class="mini" data-op="add-root-group">+ 新增一个顶层分组</button>';
        root.innerHTML = html;
        bind();
      }

      function setByPath(path, key, val){
        var parts = path.split('.');
        var node = S.tree;
        for (var i=0;i<parts.length;i++){
          var p = parts[i];
          node = (p==='groups'||p==='conditions') ? node[p] : node[parseInt(p,10)];
          if (node==null) return;
        }
        node[key] = val;
      }

      function bind(){
        var root = el('cond-root');
        var inputs = root.querySelectorAll('[data-f]');
        for (var i=0;i<inputs.length;i++){
          (function(inp){
            var ev = (inp.tagName==='SELECT') ? 'change' : 'input';
            inp.addEventListener(ev, function(){
              var path = inp.getAttribute('data-path');
              var f = inp.getAttribute('data-f');
              var v = inp.value;
              if (f==='threshold' || f==='threshold_high' || f==='duration_seconds' || f==='consecutive_count'){
                v = (v===''? (f==='consecutive_count'?1:0) : Number(v));
              }
              setByPath(path, f, v);
              if (f==='metric_code' || f==='operator') render();
            });
          })(inputs[i]);
        }
        var ops = root.querySelectorAll('[data-op]');
        for (var k=0;k<ops.length;k++){
          (function(b){
            b.onclick = function(){
              var op = b.getAttribute('data-op');
              var path = b.getAttribute('data-path')||'';
              var parts = path? path.split('.') : [];
              var idx = parts.length? parseInt(parts[parts.length-1],10) : -1;
              var parentPath = parts.length>1 ? parts.slice(0,-1).join('.') : '';
              if (op==='add-cond'){
                var arr = getArr(parentPath, 'conditions');
                if (arr) arr.push({metric_code:'price',operator:'gt',threshold:null,duration_seconds:0,consecutive_count:1});
              } else if (op==='add-group'){
                var garr = getArr(parentPath, 'groups');
                if (garr) garr.push({operator:'AND',conditions:[{metric_code:'price',operator:'gt',threshold:null,duration_seconds:0,consecutive_count:1}],groups:[]});
              } else if (op==='add-root-group'){
                S.tree.push({operator:'AND',conditions:[{metric_code:'price',operator:'gt',threshold:null,duration_seconds:0,consecutive_count:1}],groups:[]});
              } else if (op==='del-cond'){
                var ca = getArr(parentPath, 'conditions');
                if (ca && ca.length>1) ca.splice(idx,1); else if (ca) ca.splice(idx,1);
              } else if (op==='del-group'){
                S.tree.splice(idx,1);
              }
              render();
            };
          })(ops[k]);
        }
        var rl = el('root-logic');
        if (rl){
          var init = window.__rootLogic || 'AND';
          rl.value = init;
          rl.onchange = function(){ window.__rootLogic = rl.value; };
        }
      }

      function getArr(parentPath, key){
        if (!parentPath) return S.tree;
        var parts = parentPath.split('.');
        var node = S.tree;
        for (var i=0;i<parts.length;i++){
          var p = parts[i];
          node = (p==='groups'||p==='conditions') ? node[p] : node[parseInt(p,10)];
          if (node==null) return null;
        }
        if (!node[key]) node[key]=[];
        return node[key];
      }

      window.__collectRule = function(){
        function clean(groups){
          return groups.map(function(g){
            return {
              operator: g.operator||'AND',
              label: g.label||null,
              conditions: (g.conditions||[]).map(function(c){
                return {
                  metric_code: c.metric_code,
                  operator: c.operator,
                  threshold: c.threshold,
                  threshold_high: c.threshold_high,
                  compare_metric: c.compare_metric||null,
                  change_window: c.change_window||null,
                  expected_state: c.expected_state||null,
                  duration_seconds: c.duration_seconds||0,
                  consecutive_count: c.consecutive_count||1
                };
              }),
              groups: clean(g.groups||[])
            };
          });
        }
        return { groups: clean(S.tree), logic: (el('root-logic') ? el('root-logic').value : 'AND') };
      };

      init();

      // -------- 页面级按钮（在脚本内绑定，保证能拿到 __collectRule）
      function collect(){
        var c = window.__collectRule();
        return {
          name: el('r-name').value.trim(),
          description: el('r-desc').value.trim() || null,
          severity: el('r-sev').value,
          cooldown_seconds: Number(el('r-cooldown').value||86400),
          check_interval_seconds: Number(el('r-interval').value||60),
          min_quality: el('r-minq').value,
          require_fresh: el('r-fresh').checked,
          require_multi_source: el('r-multi').checked,
          notify_on_recover: el('r-recover').checked,
          logic: c.logic,
          groups: c.groups
        };
      }
      function show(res){
        el('r-result').innerHTML = '<div class="note note-ok"><b>'+(res.okText||'已保存')+'</b></div>';
        return res;
      }
      function fail(e){
        var msg = String(e && e.message || e);
        var auth = /401|403|令牌|权限/.test(msg);
        el('r-result').innerHTML = '<div class="note note-warn"><b>未保存</b><br>'+esc2(msg)
          + (auth ? '<br>请先在「系统 → 后台管理」里设置写操作令牌。' : '')+'</div>';
      }

      el('r-save').onclick = function(){
        var body = collect();
        if (!body.name){ fail('请填写规则名称'); return; }
        var EDIT_ID = ${JSON.stringify(null)};
        var url = '/alerts/rules' + (window.__editId ? ('/'+window.__editId) : '');
        var method = window.__editId ? 'PUT' : 'POST';
        fetch('/api/v1'+url, {
          method: method,
          headers: Object.assign({'Content-Type':'application/json','Accept':'application/json'},
            (window.C.AdminAuth.get() ? {'X-Admin-Token': window.C.AdminAuth.get()} : {})),
          body: JSON.stringify(body)
        }).then(function(r){ return r.text().then(function(t){
            var d; try { d = t?JSON.parse(t):{}; } catch(_){ d={raw:t}; }
            if (!r.ok) throw new Error((d.error&&d.error.message)||('HTTP '+r.status));
            return d;
          }); })
          .then(function(res){
            show({okText: window.__editId ? '已保存修改' : '规则已保存，正在后台持续监测'});
            window.C.toast('已保存');
            setTimeout(function(){ location.hash = '#/alerts'; }, 800);
          })
          .catch(fail);
      };

      el('r-test').onclick = function(){
        var body = collect();
        el('r-result').innerHTML = '<div class="muted">正在用当前数据判断…</div>';
        fetch('/api/v1/alerts/rules/test', {
          method:'POST',
          headers: Object.assign({'Content-Type':'application/json','Accept':'application/json'},
            (window.C.AdminAuth.get() ? {'X-Admin-Token': window.C.AdminAuth.get()} : {})),
          body: JSON.stringify(body)
        }).then(function(r){ return r.text().then(function(t){
            var d; try{ d=t?JSON.parse(t):{}; }catch(_){ d={raw:t}; }
            if (!r.ok) throw new Error((d.error&&d.error.message)||('HTTP '+r.status));
            return d;
          }); })
          .then(function(res){
            var tone = res.would_notify ? 'ok' : (res.satisfied ? 'warn' : 'info');
            var head = res.would_notify ? '现在成立，保存后就会发送提醒'
                     : (res.satisfied ? '条件成立，但被数据质量门槛拦下了' : '现在还不成立');
            var rows = (res.conditions||[]).map(function(c){
              return '<tr><td>'+esc2(c.description||c.metric_code)+'</td>'
                + '<td class="mono">'+(c.actual==null?'--':esc2(c.actual))+'</td>'
                + '<td class="mono muted">'+(c.threshold==null?'--':esc2(c.threshold))+'</td>'
                + '<td>'+(c.satisfied?'<span class="badge b-ok">已满足</span>':(c.available?'<span class="badge b-mute">未满足</span>':'<span class="badge b-warn">数据不可用</span>'))+'</td>'
                + '<td class="muted">'+esc2(c.reason||'')+'</td></tr>';
            }).join('');
            el('r-result').innerHTML = '<div class="note note-'+tone+'"><b>'+head+'</b></div>'
              + (res.quality_gate && !res.quality_gate.passed ? '<div class="note note-warn">数据质量门槛：'+esc2(res.quality_gate.reason)+'</div>' : '')
              + '<table class="tbl" style="margin-top:8px"><thead><tr><th>你的条件</th><th>实际值</th><th>阈值</th><th>结论</th><th>判定依据</th></tr></thead><tbody>'+rows+'</tbody></table>';
          })
          .catch(fail);
      };

      var cancel = el('r-cancel');
      if (cancel) cancel.onclick = function(){ location.hash = '#/alerts'; };

      var prev = el('r-preview');
      if (prev) prev.onclick = function(){
        el('r-result').innerHTML = '<div class="note note-info">邮件预览：条件成立时，邮件里会包含'
          + '「你的条件 / 实际数值 / 阈值 / 结论」对照表、当时的市场状态、数据来源与可靠性，'
          + '以及明确的「不构成买卖建议」说明。可以先用「测试一下」看到实际数值。</div>';
      };
    `;
  }

  // ---------------------------------------------------------------- 触发历史

  P.alertsEvents = async function (params) {
    await needCatalog();
    const days = (params && params.get('days')) || S.eventDays;
    S.eventDays = days;
    return _pageWith(await _eventsCardHtml(days));
  };

  function _eventsCard() {
    return card('最近的提醒记录',
      `<div id="ev-inline"><div class="muted">正在加载…</div></div>`,
      '每条记录都能展开看到「当时为什么触发」与「邮件发出去没有」');
  }

  async function _eventsCardHtml(days) {
    let data;
    try { data = await get('/alerts/events', { days: days, limit: 100 }); }
    catch (e) { return unavailable(String(e.message || e)); }

    const tabs = [['7', '近 7 天'], ['30', '近 30 天'], ['90', '近 90 天'], ['365', '近 1 年'], ['all', '全部']]
      .map(([v, t]) => `<a class="mini${String(days) === v ? ' active' : ''}" href="#/alerts-events?days=${v}">${t}</a>`).join(' ');

    if (!data.events.length) {
      return card('提醒记录',
        `<div style="margin-bottom:8px">${tabs}</div>` +
        note('info', '这段时间内没有任何提醒。规则会继续在后台监测，条件成立时你会收到邮件。'));
    }

    const rows = data.events.map(e => `<tr>
      <td>${esc(e.rule_name)}<div class="muted" style="font-size:12px">${e.event_type === 'recovered' ? '已恢复' : '触发'}</div></td>
      <td>${sevBadge(e.severity)}</td>
      <td class="muted">${tsLocal(e.trigger_time)}<br><span style="font-size:11px">${agoText(e.trigger_time)}</span></td>
      <td>${_metricSummary(e.metric_values)}</td>
      <td>${notifyBadge(e.notification_status)}${e.error_message ? '<div class="muted" style="font-size:11px">' + esc(String(e.error_message).slice(0, 60)) + '</div>' : ''}</td>
      <td>${qualityBadge(e.data_quality)}${e.used_fallback ? ' <span class="badge b-warn">备用源</span>' : ''}</td>
      <td class="act"><button class="mini" data-ev="${e.id}">详情</button></td>
    </tr>`).join('');

    return card('提醒记录',
      `<div style="margin-bottom:8px">${tabs}
         <span class="muted" style="margin-left:10px;font-size:12px">共 ${nf0(data.total)} 条</span>
       </div>
       <table class="tbl"><thead><tr>
         <th>规则</th><th>等级</th><th>触发时间</th><th>关键数值</th><th>邮件</th><th>数据质量</th><th></th>
       </tr></thead><tbody>${rows}</tbody></table>
       <div id="ev-detail" style="margin-top:10px"></div>`) +
      rawBlock('提醒记录原始数据', data);
  }

  function _metricSummary(vals) {
    if (!vals) return '<span class="muted">--</span>';
    return Object.entries(vals).slice(0, 3).map(([k, v]) => {
      const m = metricByCode(k);
      return `<span class="mono">${esc(m ? m.name_cn : k)}=${esc(String(v))}</span>`;
    }).join('<br>');
  }

  function bindEventDetails(root) {
    root.querySelectorAll('[data-ev]').forEach(b => {
      b.onclick = async () => {
        const box = root.querySelector('#ev-detail') || document.getElementById('ev-detail');
        try {
          const e = await get(`/alerts/events/${b.dataset.ev}`);
          box.innerHTML = _eventDetailHtml(e);
          if (box.scrollIntoView) box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } catch (err) { toast(String(err.message || err), true); }
      };
    });
  }

  function _eventDetailHtml(e) {
    const conds = (e.condition_result && e.condition_result.results) || [];
    const rows = conds.map(c => `<tr>
      <td>${esc(c.description || c.metric_code)}</td>
      <td class="mono">${c.actual === null || c.actual === undefined ? '--' : esc(String(c.actual))}</td>
      <td class="muted mono">${c.threshold === null || c.threshold === undefined ? '--' : esc(String(c.threshold))}${c.threshold_high ? ' ~ ' + esc(String(c.threshold_high)) : ''}</td>
      <td>${c.satisfied ? '<span class="badge b-ok">已满足</span>' : (c.available ? '<span class="badge b-mute">未满足</span>' : '<span class="badge b-warn">数据不可用</span>')}</td>
      <td class="muted">${esc(c.reason || '')}</td>
    </tr>`).join('');

    const ctx = e.market_context || {};
    const ctxRows = Object.entries(ctx).filter(([, v]) => v !== null && v !== undefined && v !== '')
      .map(([k, v]) => `<tr><td class="muted">${esc(k)}</td><td>${esc(String(v))}</td></tr>`).join('');

    const logs = (e.notification_logs || []).map(l => `<tr>
      <td>${esc(l.channel_type)}</td><td>${esc(l.target || '')}</td>
      <td>${notifyBadge(l.status)}</td><td class="mono">${nf0(l.attempt)}</td>
      <td class="muted">${tsLocal(l.created_at)}</td>
      <td class="muted" style="font-size:12px">${esc(String(l.error_message || l.smtp_response || '').slice(0, 90))}</td>
    </tr>`).join('');

    return card(`提醒详情 #${e.id} · ${esc(e.rule_name)}`,
      `<div class="stat-row" style="margin-bottom:10px">
        ${stat('触发时间', tsLocal(e.trigger_time), agoText(e.trigger_time) + ' · ' + esc(e.event_type))}
        ${stat('等级', SEV_CN[e.severity] || e.severity, '不代表行情好坏')}
        ${stat('邮件状态', NOTIFY_CN[e.notification_status] || e.notification_status,
        e.email_sent_at ? '已发送于 ' + tsLocal(e.email_sent_at) : (e.retry_count ? '已重试 ' + e.retry_count + ' 轮' : '未发送'),
        e.notification_status === 'FAILED' ? 'down' : '')}
        ${stat('数据来源', esc(e.provider || '未知'),
        (e.used_fallback ? '已切换备用源 · ' : '') + '质量 ' + esc(e.data_quality || '未知'))}
      </div>

      <h4>为什么触发</h4>
      <table class="tbl"><thead><tr>
        <th>你的条件</th><th>实际值</th><th>阈值</th><th>结论</th><th>判定依据</th>
      </tr></thead><tbody>${rows || '<tr><td colspan="5" class="muted">该事件没有附带逐条条件明细</td></tr>'}</tbody></table>

      ${ctxRows ? `<h4 style="margin-top:14px">当时的市场状态</h4>
        <table class="tbl"><tbody>${ctxRows}</tbody></table>` : ''}

      <h4 style="margin-top:14px">邮件发送过程</h4>
      <table class="tbl"><thead><tr>
        <th>渠道</th><th>收件人</th><th>结果</th><th>第几次</th><th>时间</th><th>响应 / 错误</th>
      </tr></thead><tbody>${logs || '<tr><td colspan="6" class="muted">还没有发送尝试记录（可能尚未配置邮件通道）</td></tr>'}</tbody></table>

      ${e.error_message ? note('warn', '<b>最后一次错误</b><br>' + esc(e.error_message)) : ''}

      <div class="form-actions" style="margin-top:12px">
        <button class="mini" id="ev-resend" data-ev="${e.id}">重新发送这封提醒</button>
      </div>

      <div class="muted" style="font-size:12px;margin-top:10px">
        数据来源链：${esc(e.provider_chain || '未知')} ·
        数据时间：${e.data_updated_at ? tsLocal(e.data_updated_at) : '未知'} ·
        事件记录时间：${tsLocal(e.created_at)}
      </div>` + rawBlock('事件原始数据', e));
  }

  function _pageWith(inner) {
    return `<div class="page-head"><h2>提醒记录</h2>
      <p class="muted">每一条触发都会留痕：什么时候触发、为什么触发、用的哪个数据源、邮件发出去没有。</p>
    </div>` + inner;
  }

  // ---------------------------------------------------------------- 邮件设置

  P.alertsSettings = async function () {
    await needCatalog();
    return `<div class="page-head"><h2>邮件通知设置</h2>
      <p class="muted">配置 SMTP 后，规则触发时会自动发邮件到这个邮箱。密码不会被保存在这里，也不会显示出来。</p>
    </div>` + _smtpCard(true);
  };

  function _smtpCard(full) {
    const s = S.stats;
    const cfg = (s && s.smtp) || {};
    return card('邮件通道（SMTP）',
      `<div class="form-grid">
        <label>SMTP 服务器 <input id="sm-host" value="${esc(cfg.host || '')}" placeholder="例如 smtp.qq.com" /></label>
        <label>端口 <input id="sm-port" type="number" value="${cfg.port || 465}" /></label>
        <label>加密方式
          <select id="sm-enc">
            <option value="ssl"${cfg.encryption === 'ssl' ? ' selected' : ''}>SSL（通常是 465）</option>
            <option value="starttls"${cfg.encryption === 'starttls' ? ' selected' : ''}>STARTTLS（通常是 587）</option>
            <option value="none"${cfg.encryption === 'none' ? ' selected' : ''}>不加密（仅内网）</option>
          </select>
        </label>
        <label>用户名（邮箱地址） <input id="sm-user" value="${esc(cfg.username || '')}" /></label>
        <label>授权码 / 密码 <input id="sm-pass" type="password" placeholder="${cfg.password_set ? '（已配置，留空则不修改）' : '请输入授权码'}" /></label>
        <label>发件人 <input id="sm-sender" value="${esc(cfg.sender || '')}" /></label>
        <label class="wide">收件人（多个用英文逗号分隔） <input id="sm-to" value="${esc((cfg.recipients || []).join(','))}" /></label>
      </div>
      <div class="form-actions">
        <button class="btn primary" id="sm-test">发送测试邮件</button>
        <button class="btn" id="sm-diag">只做连通性检查</button>
        <button class="btn" id="sm-send-digest">立即发送今日摘要</button>
      </div>
      <div id="sm-result" style="margin-top:12px"></div>
      ${full ? `<div class="muted" style="margin-top:12px;font-size:12px">
        <b>说明</b>：这里的填写只用于「测试邮件」的即时验证；要让后台持续发信，请把同样的配置写入服务器上的
        <code>.env</code>（SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_SENDER / SMTP_TO / SMTP_ENCRYPTION）。
        密码只从环境变量读取，不会落库、不会回显。<br>
        系统对连续失败会自动熔断（停止发送一段时间）以避免邮件风暴，恢复后可手动解除。
      </div>` : ''}`,
      '收不到提醒时，第一件事就是来这里发一封测试邮件');
  }

  function bindSmtp(root) {
    const host = root.querySelector('#sm-host');
    if (!host) return;
    const box = root.querySelector('#sm-result');

    const overrides = () => {
      const o = {
        smtp_host: host.value.trim(),
        smtp_port: Number(root.querySelector('#sm-port').value || 465),
        smtp_user: root.querySelector('#sm-user').value.trim(),
        smtp_sender: root.querySelector('#sm-sender').value.trim(),
        smtp_to: root.querySelector('#sm-to').value.trim(),
        smtp_encryption: root.querySelector('#sm-enc').value
      };
      const pw = root.querySelector('#sm-pass').value;
      if (pw) o.smtp_password = pw;
      return o;
    };

    const btn = root.querySelector('#sm-test');
    btn.onclick = async () => {
      btn.disabled = true; btn.textContent = '正在发送…';
      try {
        const r = await post('/alerts/notify/smtp/test', overrides());
        box.innerHTML = note(r.ok ? 'ok' : 'warn',
          '<b>' + esc(r.message) + '</b>' +
          (r.duration_ms ? '（耗时 ' + nf0(r.duration_ms) + ' ms）' : '')) +
          (r.smtp_response ? `<div class="muted" style="font-size:12px">SMTP 响应：${esc(r.smtp_response)}</div>` : '') +
          (r.error ? `<div class="muted" style="font-size:12px">错误原因：${esc(r.error)}</div>` : '') +
          rawBlock('测试邮件结果', r);
        toast(r.ok ? '测试邮件已发送' : '发送失败', !r.ok);
      } catch (e) {
        const msg = String(e.message || e);
        const auth = /401|403|令牌|权限/.test(msg);
        box.innerHTML = note('warn', '<b>测试未执行</b><br>' + esc(msg) +
          (auth ? '<br>请先设置写操作令牌。' : ''));
        toast('测试失败', true);
      } finally { btn.disabled = false; btn.textContent = '发送测试邮件'; }
    };

    const diag = root.querySelector('#sm-diag');
    if (diag) diag.onclick = async () => {
      diag.disabled = true;
      try {
        const r = await post('/alerts/notify/smtp/diagnose', overrides());
        box.innerHTML = rawBlock('连通性诊断结果', r);
        toast('诊断完成');
      } catch (e) { box.innerHTML = note('warn', esc(String(e.message || e))); }
      finally { diag.disabled = false; }
    };

    const dg = root.querySelector('#sm-send-digest');
    if (dg) dg.onclick = async () => {
      dg.disabled = true; dg.textContent = '发送中…';
      try {
        const r = await post('/alerts/digest/daily');
        box.innerHTML = note(r.sent ? 'ok' : 'warn',
          r.sent ? '摘要邮件已发送' : ('摘要未发送：' + esc(r.message || r.error || '未知原因'))) +
          rawBlock('摘要发送结果', r);
        toast(r.sent ? '已发送' : '未发送', !r.sent);
      } catch (e) { box.innerHTML = note('warn', esc(String(e.message || e))); }
      finally { dg.disabled = false; dg.textContent = '立即发送今日摘要'; }
    };
  }

  // ---------------------------------------------------------------- 渠道管理

  P.alertsChannels = async function () {
    await needCatalog();
    let data;
    try { data = await get('/alerts/channels'); } catch (e) { return unavailable(String(e.message || e)); }
    S.channels = data.channels;

    const rows = data.channels.map(c => `<tr>
      <td>${esc(c.channel_type)}</td>
      <td>${esc(c.target || '（用 .env 里的收件人）')}</td>
      <td>${c.rule_id ? '规则 #' + c.rule_id : '全局'}</td>
      <td>${c.enabled ? '<span class="badge b-ok">启用</span>' : '<span class="badge b-mute">停用</span>'}</td>
      <td class="mono">${nf0(c.consecutive_failures)}</td>
      <td class="muted">${c.last_success_at ? tsLocal(c.last_success_at) : '从未成功'}
        ${c.disabled_until ? '<br><span class="badge b-bad">熔断至 ' + tsLocal(c.disabled_until) + '</span>' : ''}</td>
      <td class="act">
        ${c.disabled_until ? `<button class="mini" data-ch-reset="${c.id}">解除熔断</button>` : ''}
        <button class="mini danger" data-ch-del="${c.id}">删除</button>
      </td>
    </tr>`).join('');

    const supported = (data.supported || []).map(s =>
      `<li>${esc(s.name)} ${s.implemented ? '<span class="badge b-ok">已实现</span>' : '<span class="badge b-mute">预留，尚未实现</span>'}</li>`
    ).join('');

    return `<div class="page-head"><h2>通知渠道</h2>
      <p class="muted">第一阶段已完整实现邮件通知。其余渠道在架构上已预留（新增渠道不需要改预警引擎）。</p>
    </div>` +
      card('已配置的渠道',
        `<table class="tbl"><thead><tr>
          <th>类型</th><th>目标</th><th>作用范围</th><th>状态</th><th>连续失败</th><th>最近成功</th><th>操作</th>
        </tr></thead><tbody>${rows || '<tr><td colspan="7" class="muted">还没有独立渠道记录。未单独配置时，系统直接使用 .env 里的 SMTP 收件人。</td></tr>'}</tbody></table>
         <div class="muted" style="margin-top:8px;font-size:12px">
           连续失败达到阈值后系统会自动熔断该渠道一段时间，避免一直重试造成邮件风暴。
         </div>`) +
      card('支持的渠道类型', `<ul class="muted">${supported}</ul>`) +
      rawBlock('渠道原始数据', data);
  };

  function bindChannelActions(root) {
    root.querySelectorAll('[data-ch-reset]').forEach(b => {
      b.onclick = async () => {
        try { await post(`/alerts/channels/${b.dataset.chReset}/reset`); toast('已解除熔断'); setTimeout(() => location.reload(), 600); }
        catch (e) { toast(String(e.message || e), true); }
      };
    });
    root.querySelectorAll('[data-ch-del]').forEach(b => {
      b.onclick = async () => {
        if (!confirm('删除这个通知渠道？')) return;
        try { await del(`/alerts/channels/${b.dataset.chDel}`); toast('已删除'); setTimeout(() => location.reload(), 600); }
        catch (e) { toast(String(e.message || e), true); }
      };
    });
  }

  // ---------------------------------------------------------------- 规则回测

  P.alertsBacktest = async function (params) {
    await needCatalog();
    const ruleId = params && params.get('rule') ? params.get('rule') : '';
    const rules = S.rules.length ? S.rules : ((await get('/alerts/rules', { limit: 500 })).rules || []);
    S.rules = rules;

    const opts = rules.map(r => `<option value="${r.id}"${String(ruleId) === String(r.id) ? ' selected' : ''}>${esc(r.name)}</option>`).join('');

    const head = `<div class="page-head"><h2>规则触发历史模拟</h2>
      <p class="muted">把规则放到过去的真实历史数据上跑一遍：它以前会在哪些时间点成立？成立之后市场又发生了什么？
        <b>结论只描述历史，不预测未来。</b></p>
    </div>`;

    if (!rules.length) return head + note('info', '还没有规则可以回测。请先在「智能预警中心」创建一条规则。');

    return head + card('选择要回测的规则', `
      <div class="form-grid">
        <label class="wide">规则
          <select id="bt-rule">${opts}</select>
        </label>
      </div>
      <div class="form-actions">
        <button class="btn primary" id="bt-run">开始回测</button>
      </div>
      <div id="bt-result" style="margin-top:12px"></div>`,
      '回测严格按时间顺序推进，不会用到当时还不知道的未来数据');
  };

  // ---------------------------------------------------------------- 页面挂载后处理（供 app.js 调用）

  P.__afterRender = function (path, root) {
    if (path === '/alerts') { bindRuleActions(root); bindSmtp(root); bindEventDetails(root); _loadInlineEvents(root); }
    else if (path === '/rules') { /* 条件编辑器已在脚本内自绑定 */ }
    else if (path === '/alerts-events') { bindEventDetails(root); }
    else if (path === '/alerts-settings') { bindSmtp(root); }
    else if (path === '/alerts-channels') { bindChannelActions(root); }
    else if (path === '/alerts-backtest') { _bindBacktest(root); }
  };

  async function _loadInlineEvents(root) {
    const box = root.querySelector('#ev-inline');
    if (!box) return;
    try {
      const data = await get('/alerts/events', { days: 7, limit: 20 });
      if (!data.events.length) {
        box.innerHTML = '<div class="muted">近 7 天没有触发记录。</div>';
        return;
      }
      box.innerHTML = `<table class="tbl"><thead><tr>
          <th>规则</th><th>等级</th><th>时间</th><th>邮件</th><th></th>
        </tr></thead><tbody>` +
        data.events.map(e => `<tr>
          <td>${esc(e.rule_name)}</td>
          <td>${sevBadge(e.severity)}</td>
          <td class="muted">${tsLocal(e.trigger_time)} <span style="font-size:11px">${agoText(e.trigger_time)}</span></td>
          <td>${notifyBadge(e.notification_status)}</td>
          <td class="act"><button class="mini" data-ev="${e.id}">详情</button></td>
        </tr>`).join('') + `</tbody></table>
        <div id="ev-detail" style="margin-top:10px"></div>`;
      bindEventDetails(box);
    } catch (e) {
      box.innerHTML = '<div class="muted">提醒记录加载失败：' + esc(String(e.message || e)) + '</div>';
    }
  }

  function _bindBacktest(root) {
    const btn = root.querySelector('#bt-run');
    if (!btn) return;
    btn.onclick = async () => {
      const id = root.querySelector('#bt-rule').value;
      const box = root.querySelector('#bt-result');
      box.innerHTML = '<div class="muted">正在用历史数据回测…（数据量大时可能需要一会儿）</div>';
      try {
        const r = await post(`/alerts/rules/${id}/backtest`);
        box.innerHTML = _backtestHtml(r) + rawBlock('回测原始数据', r);
      } catch (e) {
        box.innerHTML = note('warn', '<b>回测未完成</b><br>' + esc(String(e.message || e)) +
          '<br><span class="muted">提示：回测功能需要本地已有足够的历史数据（请先执行历史回填）。</span>');
      }
    };
  }

  function _backtestHtml(r) {
    if (!r || r.available === false) {
      return unavailable(r && r.message || '暂时无法回测');
    }
    const bands = (r.aftermath || []).map(a => `<tr>
      <td class="mono">${nf0(a.days)} 天</td>
      <td class="mono ${C.cls(a.avg_return_pct)}">${nf(a.avg_return_pct)}%</td>
      <td class="mono ${C.cls(a.max_gain_pct)}">${nf(a.max_gain_pct)}%</td>
      <td class="mono ${C.cls(a.max_drawdown_pct)}">${nf(a.max_drawdown_pct)}%</td>
      <td class="mono">${nf(a.volatility_pct)}%</td>
      <td class="muted">${nf0(a.sample_size)} 次样本</td>
    </tr>`).join('');

    return `<div class="stat-row">
        ${stat('历史触发次数', nf0(r.trigger_count), '过去成立过的次数')}
        ${stat('平均间隔', r.avg_interval_days != null ? nf(r.avg_interval_days) + ' 天' : '--', '两次触发之间')}
        ${stat('首次触发', r.first_trigger ? tsLocal(r.first_trigger) : '--')}
        ${stat('最近触发', r.last_trigger ? tsLocal(r.last_trigger) : '--')}
      </div>
      ${bands ? `<h4 style="margin-top:14px">触发之后，历史上发生了什么</h4>
        <table class="tbl"><thead><tr>
          <th>观察窗口</th><th>平均涨跌</th><th>最大涨幅</th><th>最大跌幅</th><th>波动率</th><th>样本数</th>
        </tr></thead><tbody>${bands}</tbody></table>` : ''}
      <div class="note note-info" style="margin-top:12px">
        <b>请这样理解这些数字</b><br>
        它们只说明「过去这次条件成立之后，市场曾经出现过什么情况」，<b>不代表未来会重复</b>。
        样本数越少，参考价值越低。系统不对未来涨跌做任何承诺。
      </div>`;
  }

  global.PA_ALERTS = P;

})(window);
