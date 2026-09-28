/* pages-setup.js —— 首次启动的安装引导：选数据库 -> 连接自检 -> 落盘完成
 *
 * 为什么要把它做成必经的一步：
 * SQLite 是本机文件，装在三台机器上就是三份互不相干的数据库。
 * 想让多台设备看到同一份数据，就必须把库放到一台大家都能连的 PostgreSQL / MySQL 上。
 * 与其让人去翻文档改 .env，不如第一次打开就问清楚，并且**没测通不许保存**。
 */
(function (global) {
  'use strict';
  const P = {};
  const { get, post, note, rawBlock, table, esc, toast } = C;

  const PORTS = { sqlite: '', postgresql: '5432', mysql: '3306' };
  const PLACEHOLDER = {
    postgresql: { host: '192.168.1.10', db: 'btc_research', user: 'btc' },
    mysql: { host: '192.168.1.10', db: 'btc_research', user: 'btc' },
  };

  function fieldRow(label, inner, help) {
    return `<div class="form-row"><label>${esc(label)}</label>` +
      `<div style="flex:1;min-width:200px">${inner}${help ? `<div class="muted" style="font-size:11px">${help}</div>` : ''}</div></div>`;
  }

  function dialectCards(dialects, selected) {
    return dialects.map(d => {
      const on = d.dialect === selected;
      const ready = d.driver_installed;
      return `<div class="db-choice${on ? ' on' : ''}" data-dialect="${esc(d.dialect)}">
        <div class="db-choice-head">
          <span class="db-choice-name">${esc(d.label)}</span>
          ${ready
          ? '<span class="badge b-ok">驱动就绪</span>'
          : `<span class="badge b-warn" title="${esc(d.install_hint)}">缺驱动</span>`}
        </div>
        <div class="muted" style="font-size:12px">${esc(d.tagline)}</div>
        <div class="muted" style="font-size:11px;margin-top:4px">适合：${esc(d.best_for)}</div>
      </div>`;
    }).join('');
  }

  function sqliteForm(d, status) {
    const cur = String(d.file_path || status.default_file || 'data/btc.db');
    return fieldRow('数据文件',
      `<input class="inp" id="db-file" value="${esc(cur)}" placeholder="data/btc.db" autocomplete="off">`,
      '相对路径以程序根目录为准；换不了机器同步，但零依赖、开箱即用。');
  }

  function serverForm(d, status, ph) {
    return [
      fieldRow('主机地址',
        `<input class="inp" id="db-host" value="${esc(d.host)}" placeholder="${esc(ph.host)}" autocomplete="off">`,
        '填 IP 最稳妥；用域名时要保证这台机器能解析到它。'),
      fieldRow('端口',
        `<input class="inp" id="db-port" value="${esc(d.port || PORTS[d.dialect])}" style="max-width:140px" autocomplete="off">`,
        `默认 ${PORTS[d.dialect]}；连不上时先用 telnet 试一下这个端口通不通。`),
      fieldRow('数据库名',
        `<input class="inp" id="db-name" value="${esc(d.database)}" placeholder="${esc(ph.db)}" autocomplete="off">`,
        '必须是**已经建好的空库** —— 本向导只负责连库，不负责建库。'),
      fieldRow('用户名',
        `<input class="inp" id="db-user" value="${esc(d.username)}" placeholder="${esc(ph.user)}" autocomplete="off">`),
      fieldRow('密码',
        `<input class="inp" id="db-pass" type="password" value="${esc(d.password)}" autocomplete="new-password">`,
        '只用于本次测试与写入本机 .env，不会上传到任何地方。'),
      d.dialect === 'postgresql'
        ? fieldRow('SSL 模式',
          `<select class="inp" id="db-ssl" style="max-width:200px">
             ${['', 'disable', 'require', 'prefer'].map(v =>
    `<option value="${v}"${d.sslmode === v ? ' selected' : ''}>${v === '' ? '不指定（跟随驱动默认）' : v}</option>`).join('')}
           </select>`,
          '多数内网 PostgreSQL 没开 SSL，连不上且报 SSL 相关错误时选 disable。')
        : '',
    ].join('');
  }

  P.setupWizard = async function () {
    const status = await get('/system/setup/status');
    const dialects = status.dialects || [];
    const start = 'postgresql';
    const d = { dialect: start, host: '', port: '', database: '', username: '', password: '', file_path: '', sslmode: '' };

    let html = `<div class="setup-wrap">
      <div class="setup-head">
        <div class="setup-brand"><span class="brand-mark">₿</span></div>
        <div>
          <h2 class="page-title">安装引导：先选一个数据库</h2>
          <div class="page-sub">这一步决定数据存在哪。选好后我们会真连一次，通了才让你继续。</div>
        </div>
      </div>

      <div class="note note-info">
        <b>为什么要选？</b><br>
        <b>SQLite</b> 是本机上的一个文件，装在哪台机器就在哪台机器上，多台设备各存一份、互不相干。<br>
        <b>PostgreSQL / MySQL</b> 是独立服务，多台设备连同一个库 —— 这才是「几台机器看到的数据完全一致」的做法。<br>
        已经跑了一段时间的 SQLite 库也能直接用：选 SQLite 并把路径指向它即可，原有数据不会被清掉。
      </div>

      <div class="card">
        <h3 class="card-title">第 1 步 · 数据库类型
          <span class="hint">点一下卡片切换；标着「缺驱动」的需要先在本机装一个 Python 包</span></h3>
        <div class="db-choices" id="db-choices">${dialectCards(dialects, start)}</div>
        <div id="db-driver-hint"></div>
      </div>

      <div class="card">
        <h3 class="card-title">第 2 步 · 连接参数
          <span class="hint">填完点「测试连接」，我们会由服务端真连一次</span></h3>
        <div id="db-form"></div>
        <details class="raw" style="margin-top:6px">
          <summary>高级：我手上已经有完整连接串</summary>
          <div style="padding-top:8px">
            <input class="inp" id="db-raw" placeholder="postgresql+asyncpg://user:pass@host:5432/dbname" autocomplete="off">
            <div class="muted" style="font-size:11px;margin-top:4px">
              填了就以它为准（上面那些格子会被忽略）。
              也认 <code>postgres://</code>、<code>mysql://</code>、<code>postgresql+psycopg://</code> 这类写法。
            </div>
          </div>
        </details>
        <div class="form-row" style="margin-top:10px">
          <label>连接串</label>
          <div style="flex:1"><code id="db-preview" class="mono muted">—</code></div>
        </div>
      </div>

      <div class="card">
        <h3 class="card-title">第 3 步 · 连接自检</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-primary" id="db-test">测试连接</button>
          <button class="btn" id="db-save" disabled>自检通过后才能真正安装</button>
        </div>
        <div id="db-result"></div>
      </div>

      <div class="muted" style="font-size:11px">
        配置文件写在：<code>${esc(status.env_file)}</code> · Python ${esc(status.python)}
        ${rawBlock('安装引导原始数据', status)}
      </div>
    </div>`;

    requestAnimationFrame(() => {
      const form = document.getElementById('db-form');
      const preview = document.getElementById('db-preview');
      const rawInput = document.getElementById('db-raw');
      const out = document.getElementById('db-result');
      const saveBtn = document.getElementById('db-save');
      const hintBox = document.getElementById('db-driver-hint');
      let lastOk = false;
      let lastUrl = '';

      const cur = () => dialects.find(x => x.dialect === d.dialect) || dialects[0] || {};

      const paintForm = () => {
        const ph = PLACEHOLDER[d.dialect] || { host: '127.0.0.1', db: 'btc_research', user: 'btc' };
        form.innerHTML = d.dialect === 'sqlite'
          ? sqliteForm(d, cur())
          : serverForm(d, cur(), ph);
        ['db-file', 'db-host', 'db-port', 'db-name', 'db-user', 'db-pass', 'db-ssl'].forEach(id => {
          const el = document.getElementById(id);
          if (!el) return;
          el.addEventListener('input', () => { readForm(); paintHint(); });
          el.addEventListener('change', () => { readForm(); paintHint(); });
        });
        paintHint();
      };

      const paintHint = () => {
        const info = cur();
        hintBox.innerHTML = info.driver_installed
          ? ''
          : note('warn', `<b>${esc(info.label)} 的异步驱动还没装</b>，先在服务端这台机器上执行：<br>
              <code>${esc(info.install_hint)}</code><br>
              <span class="muted">便携版用户：已经预装好了，看到这条说明装包时被精简掉了，执行上面那条命令即可。</span>`);
        preview.textContent = lastUrl || '（请填写参数）';
      };

      const readForm = () => {
        if (d.dialect === 'sqlite') {
          d.file_path = (document.getElementById('db-file') || {}).value || '';
        } else {
          d.host = (document.getElementById('db-host') || {}).value || '';
          d.port = (document.getElementById('db-port') || {}).value || '';
          d.database = (document.getElementById('db-name') || {}).value || '';
          d.username = (document.getElementById('db-user') || {}).value || '';
          d.password = (document.getElementById('db-pass') || {}).value || '';
          d.sslmode = (document.getElementById('db-ssl') || {}).value || '';
        }
      };

      const payload = () => {
        readForm();
        const raw = ((rawInput && rawInput.value) || '').trim();
        if (raw) return { database_url: raw };
        const body = { dialect: d.dialect };
        if (d.dialect === 'sqlite') body.file_path = d.file_path || 'data/btc.db';
        else Object.assign(body, {
          host: d.host, port: d.port, database: d.database,
          username: d.username, password: d.password, sslmode: d.sslmode,
        });
        return body;
      };

      document.getElementById('db-choices').addEventListener('click', e => {
        const card = e.target.closest('.db-choice');
        if (!card) return;
        d.dialect = card.dataset.dialect;
        document.querySelectorAll('.db-choice').forEach(c => c.classList.toggle('on', c === card));
        paintForm();
        resetGate();
      });

      if (rawInput) rawInput.addEventListener('input', () => { resetGate(); paintHint(); });

      const resetGate = () => {
        lastOk = false;
        saveBtn.disabled = true;
        saveBtn.textContent = '自检通过后才能真正安装';
        preview.textContent = '（待生成）';
      };

      const renderProbe = (r) => {
        const rows = (r.checks || []).map(c => [
          esc(c.name),
          c.ok ? '<span class="badge b-ok">通过</span>' : '<span class="badge b-bad">失败</span>',
          `<span class="muted">${esc(c.detail || '')}</span>`,
        ]);
        out.innerHTML =
          (r.ok ? note('ok', `✅ ${esc(r.summary || '连接正常')}`)
            : note('bad', `❌ ${esc(r.summary || '连接失败')}`)) +
          table(['检查项', '结果', '说明'], rows) +
          (r.hint ? note('warn', esc(r.hint)) : '') +
          (r.ok ? '' : `<div class="muted" style="font-size:11px;margin-top:6px">
             自检没过之前不会写入任何配置 —— 这一步改坏了大不了重填，不用担心把服务搞挂。
           </div>`);
        lastOk = !!r.ok;
        saveBtn.disabled = !lastOk;
        saveBtn.textContent = lastOk ? '保存并完成安装' : '自检通过后才能真正安装';
        preview.textContent = r.masked_url || '—';
        lastUrl = r.masked_url || '';
      };

      document.getElementById('db-test').onclick = async () => {
        out.innerHTML = `<div class="loading"><span class="spin"></span> 服务端正在连接目标数据库…</div>`;
        try {
          const r = await post('/system/setup/test', payload());
          renderProbe(r);
        } catch (e) {
          out.innerHTML = note('bad', `自检请求失败：${esc(String(e.message || e))}`);
        }
      };

      saveBtn.onclick = async () => {
        if (!lastOk) return;
        saveBtn.disabled = true;
        out.innerHTML = `<div class="loading"><span class="spin"></span> 正在写入配置、建表、初始化…</div>`;
        try {
          const r = await post('/system/setup/complete', payload());
          if (!r.completed) {
            renderProbe(r.probe || {});
            toast('安装未完成：' + ((r.probe || {}).summary || '请先看上面的报错'), true);
            return;
          }
          out.innerHTML = note('ok',
            `<b>安装完成：${esc(r.dialect_label || r.dialect)}</b><br>
             连接串（已隐藏密码）：<code>${esc(r.masked_url || '')}</code><br>
             配置写入：${esc((r.written_files || []).join('、') || '—')}<br>
             <span class="muted">${esc(r.hint || '')}</span>`) +
            `<div class="loading"><span class="spin"></span> 正在进入平台…</div>`;
          toast('安装完成，正在进入平台');
          setTimeout(() => location.reload(), 1400);
        } catch (e) {
          out.innerHTML = note('bad', `安装失败：${esc(String(e.message || e))}`);
          saveBtn.disabled = false;
        }
      };

      paintForm();
    });

    return html;
  };

  global.PSU = P;
})(window);
