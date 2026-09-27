/* charts.js —— ECharts 封装（本地化 vendor，零外部 CDN 依赖） */
(function (global) {
  'use strict';

  const instances = new Map();

  function palette() {
    return { up: '#d93025', down: '#188038', accent: '#1a73e8', muted: '#7b848d', line: '#e3e6ea' };
  }

  function h(tag, cls, height) {
    return `<div class="chart ${cls || ''}" id="${tag}" ${height ? 'style="height:' + height + 'px"' : ''}></div>`;
  }

  function mount(id, option) {
    const el = document.getElementById(id);
    if (!el || typeof echarts === 'undefined') return;
    let chart = instances.get(id);
    if (chart) { chart.dispose(); instances.delete(id); }
    chart = echarts.init(el, null, { renderer: 'canvas' });
    chart.setOption(option, true);
    instances.set(id, chart);
    return chart;
  }

  function resizeAll() {
    instances.forEach(c => { try { c.resize(); } catch (_) { } });
  }
  window.addEventListener('resize', resizeAll);

  const baseGrid = { left: 58, right: 22, top: 34, bottom: 30 };

  /** K 线图 + 均线 + 成交量 */
  function candleChart(id, rows, mas) {
    if (!rows || !rows.length) return;
    const P = palette();
    const dates = rows.map(r => C.dateOf(r.ts));
    const ohlc = rows.map(r => [r.open, r.close, r.low, r.high]);
    const vol = rows.map(r => Number(r.volume || 0).toFixed(2));
    const series = [
      {
        name: 'K线', type: 'candlestick', data: ohlc,
        itemStyle: { color: P.up, color0: P.down, borderColor: P.up, borderColor0: P.down }
      }
    ];
    (mas || []).forEach((m, i) => {
      series.push({
        name: 'MA' + m, type: 'line', data: rows.map(r => r['ma' + m] ?? null),
        smooth: true, symbol: 'none', lineStyle: { width: 1.2 }
      });
    });
    mount(id, {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e3e6ea', textStyle: { color: '#1a1d21', fontSize: 12 }
      },
      legend: { data: ['K线'].concat((mas || []).map(m => 'MA' + m)), top: 0, textStyle: { fontSize: 11 } },
      grid: [{ left: 58, right: 22, top: 32, height: '62%' }, { left: 58, right: 22, top: '76%', height: '16%' }],
      xAxis: [
        { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#e3e6ea' } }, axisLabel: { fontSize: 10, color: '#7b848d' } },
        { type: 'category', gridIndex: 1, data: dates, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { lineStyle: { color: '#e3e6ea' } } }
      ],
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: '#f0f2f4' } }, axisLabel: { fontSize: 10, color: '#7b848d' } },
        { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } }
      ],
      dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 40, end: 100 }],
      series: series.concat([{ name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: vol, itemStyle: { color: '#d2e3fc' } }])
    });
  }

  /** 通用折线图 */
  function lineChart(id, dates, series, opts) {
    opts = opts || {};
    const P = palette();
    const yAxis = [{ scale: !!opts.scale, splitLine: { lineStyle: { color: '#f0f2f4' } }, axisLabel: { fontSize: 10, color: '#7b848d' }, name: opts.yName || '' }];
    if (opts.y2) yAxis.push({ scale: true, splitLine: { show: false }, axisLabel: { fontSize: 10, color: '#7b848d' }, name: opts.y2.name || '' });
    mount(id, {
      animation: false,
      backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e3e6ea', textStyle: { color: '#1a1d21', fontSize: 12 } },
      legend: { top: 0, textStyle: { fontSize: 11 }, data: series.map(s => s.name) },
      grid: Object.assign({}, baseGrid, { top: 34 }),
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLabel: { fontSize: 10, color: '#7b848d' }, axisLine: { lineStyle: { color: '#e3e6ea' } } },
      yAxis,
      series: series.map((s, i) => Object.assign({
        type: 'line', smooth: true, symbol: 'none',
        lineStyle: { width: 1.6 },
        areaStyle: s.area ? { opacity: .12 } : undefined,
        markLine: s.markLine || undefined,
        itemStyle: { color: s.color || [P.accent, P.up, '#f9ab00', '#9334e6', P.down][i % 5] }
      }, s))
    });
  }

  /** 柱状图（年度收益等） */
  function barChart(id, labels, values, colorPositive) {
    const P = palette();
    mount(id, {
      animation: false, backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e3e6ea', textStyle: { color: '#1a1d21', fontSize: 12 } },
      grid: baseGrid,
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 10, color: '#7b848d' }, axisLine: { lineStyle: { color: '#e3e6ea' } } },
      yAxis: { splitLine: { lineStyle: { color: '#f0f2f4' } }, axisLabel: { fontSize: 10, color: '#7b848d' } },
      series: [{
        type: 'bar', data: values, barMaxWidth: 34, label: { show: false },
        itemStyle: {
          color: p => {
            const v = p.value;
            if (typeof v !== 'number') return P.accent;
            if (colorPositive === false) return P.accent;
            return v >= 0 ? P.up : P.down;
          }
        }
      }]
    });
  }

  /** 概率区间图（预测：p10 ~ p90） */
  function rangeChart(id, labels, lows, highs, mids) {
    const P = palette();
    mount(id, {
      animation: false, backgroundColor: 'transparent',
      tooltip: { trigger: 'axis', backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e3e6ea', textStyle: { color: '#1a1d21', fontSize: 12 } },
      legend: { top: 0, textStyle: { fontSize: 11 }, data: ['悲观 P10', '中位 P50', '乐观 P90'] },
      grid: Object.assign({}, baseGrid, { top: 34 }),
      xAxis: { type: 'category', data: labels, axisLabel: { fontSize: 10, color: '#7b848d' }, axisLine: { lineStyle: { color: '#e3e6ea' } } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: '#f0f2f4' } }, axisLabel: { fontSize: 10, color: '#7b848d' } },
      series: [
        { name: '悲观 P10', type: 'line', data: lows, symbol: 'none', lineStyle: { width: 1, type: 'dashed', color: P.down }, areaStyle: { opacity: 0 } },
        { name: '乐观 P90', type: 'line', data: highs, symbol: 'none', lineStyle: { width: 1, color: P.up }, areaStyle: { opacity: .10, color: P.up } },
        { name: '中位 P50', type: 'line', data: mids, symbol: 'none', lineStyle: { width: 2, color: P.accent } }
      ]
    });
  }

  /** 饼图（概率分布 / 构成） */
  function pieChart(id, data, colors) {
    mount(id, {
      animation: false, backgroundColor: 'transparent',
      tooltip: { trigger: 'item', backgroundColor: 'rgba(255,255,255,.97)', borderColor: '#e3e6ea', textStyle: { color: '#1a1d21', fontSize: 12 } },
      legend: { bottom: 0, textStyle: { fontSize: 11 } },
      series: [{
        type: 'pie', radius: ['42%', '66%'], center: ['50%', '45%'],
        label: { formatter: '{b} {d}%', fontSize: 11 },
        data: data, color: colors
      }]
    });
  }

  global.CH = { h, mount, candleChart, lineChart, barChart, rangeChart, pieChart, resizeAll };
})(window);
