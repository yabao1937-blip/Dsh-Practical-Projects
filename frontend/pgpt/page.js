/* Page controller isolated from the application's shared store and model code. */
(() => {
    'use strict';
    const $ = id => document.getElementById(id), M = PGPTModel, D = PGPT_DATA, F = PGPT_MODELS;
    let mode = 'sample', charts = [], records = [], active = null;
    const custom = {}, historyKey = 'pgpt.training.history.v1';
    let historyRows = [];
    try { const saved = JSON.parse(localStorage.getItem(historyKey) || '[]'); if (Array.isArray(saved)) historyRows = saved.slice(0, 40).filter(r => r && typeof r.time === 'string'); } catch (_) { /* Storage is optional. */ }
    const fmt = (v, digits = 2) => Number.isFinite(v) ? v.toFixed(digits) : '--';
    const esc = s => String(s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    const percent = v => `${fmt(v)}<span class="stat-unit">%</span>`;
    const tolerance = () => { const v = Number($('pgpt-tolerance').value); return Number.isFinite(v) && v > 0 ? v : 0.8; };
    function metricRow(label, v) { return `<tr><td>${label}</td><td>${v.n}</td><td>${fmt(v.mae)}</td><td>${fmt(v.rmse)}</td><td>${fmt(v.r2, 3)}</td><td>${fmt(v.passRate === null ? null : v.passRate * 100, 1)}%</td></tr>`; }
    function render() {
        mode = $('pgpt-mode').value;
        records = M.aggregate(D.rows, mode);
        const baseline = F[mode], selected = custom[mode];
        active = selected ? selected.model : baseline.model;
        $('pgpt-range').value = selected ? selected.range : 'factory';
        const prediction = records.map(r => M.predict(active, r.x));
        const last = records.at(-1), latestDay = M.aggregate(D.rows, 'day').at(-1);
        $('pgpt-actual').innerHTML = percent(last.y);
        $('pgpt-predicted').innerHTML = percent(prediction.at(-1));
        $('pgpt-level').textContent = `精磁尾液位：${fmt(last.x[8])}%`;
        $('pgpt-cumulative').innerHTML = percent(latestDay.y);
        $('pgpt-count').textContent = `${latestDay.day} · ${latestDay.count}次有效采样算术平均`;
        const fitted = selected ? selected.metrics : baseline.historical;
        $('pgpt-fit').textContent = fmt(fitted.r2, 3);
        $('pgpt-fit-note').textContent = `${active.count}个训练样本；不代表向前预测能力`;
        $('pgpt-prediction-note').textContent = `${last.day} 历史工况计算值 · λ=${active.lambda}`;
        $('pgpt-note').textContent = `P-GPT · ${mode === 'sample' ? '班内逐采样' : '生产日均值'}独立岭回归 · 训练期 ${active.start}—${active.end}。一天一班，凌晨采样归原表生产日。${selected ? '当前为浏览器重训练模型，刷新页面后恢复冻结模型。' : '当前为6—7月冻结模型。'} 记录截止 ${last.day}，不是实时采集数据。`;
        $('pgpt-metrics').innerHTML = selected ? metricRow('当前模型历史拟合（训练内）', fitted) :
            metricRow('6—7月历史拟合（训练内）', baseline.historical) + metricRow('6—7月时间分块调参验证', baseline.tuning.validation) + metricRow('8—9月向前时间检验', baseline.forward) + metricRow('8—9月训练均值基线', baseline.forwardBaseline);
        $('pgpt-evaluation-note').textContent = selected ? '当前重训练模型已使用所选时段的标签，只展示训练内拟合结果；不再把这些记录标为向前检验。恢复冻结模型可查看原始评估。' : F.evaluationNote + (baseline.forward.rmse >= baseline.forwardBaseline.rmse ? ' 当前按日模型在向前检验中未超过训练均值基线，预测能力不足。' : ' 向前检验提升有限，当前结果仅供分析参考。');
        $('pgpt-trend-note').textContent = selected ? '当前为重训练计算值，不作为向前时间检验' : '6—7月为拟合值；8—9月为冻结模型检验值';
        $('pgpt-summary').textContent = `算法：标准化岭回归；λ=${active.lambda}；有效${records.length}条。日值为有效采样算术平均，非产量加权。灰分偏差 = 预测−实测。容差只影响下表判定，评估表固定±0.8个百分点。`;
        $('pgpt-rows').innerHTML = records.map((r, i) => {
            const delta = prediction[i] - r.y, pass = Math.abs(delta) <= tolerance();
            const phase = selected ? (r.day >= active.start && r.day <= active.end ? '重训练拟合' : '训练期外回算（非向前）') : r.day >= '2026-08-01' ? '向前检验' : '历史拟合';
            const label = mode === 'day' ? r.day : `${r.day} / ${r.time.replace('T', ' ')}（第${r.sequence}次）`;
            return `<tr><td>${label}</td><td>${fmt(r.y)}</td><td>${fmt(prediction[i])}</td><td>${delta >= 0 ? '+' : ''}${fmt(delta)}</td><td class="${pass ? 'pgpt-good' : 'pgpt-bad'}">${pass ? '合格' : '超差'}</td><td>${phase}</td><td>${r.count}</td></tr>`;
        }).reverse().join('');
        $('pgpt-history').innerHTML = historyRows.length ? historyRows.map(r => `<tr>${[r.time, r.mode, r.n, r.range, r.lambda, fmt(r.r2, 3)].map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('') : '<tr><td colspan="6">尚无浏览器重训练记录；冻结模型已加载</td></tr>';
        $('pgpt-inputs').innerHTML = D.features.map((name, j) => `<label>${esc(name)}<input type="number" step="any" min="0" ${j >= 2 && j <= 7 ? 'max="1"' : j === 0 || j === 8 ? 'max="100"' : ''} data-feature="${j}" value="${last.x[j] ?? ''}" placeholder="缺失时使用训练中位数"></label>`).join('');
        $('pgpt-result').textContent = '';
        const trendConfig = {type: 'line', data: {labels: records.map(r => mode === 'day' ? r.day.slice(5) : `${r.day.slice(5)}/${r.sequence}`), datasets: [
            {label: '实测灰分(%)', data: records.map(r => r.y), borderColor: '#60a5fa', pointRadius: 2},
            {label: selected ? '重训练计算值(%)' : '冻结模型计算值(%)', data: prediction, borderColor: '#f59e0b', pointRadius: 2}
        ]}, options: {responsive: true}};
        const factorConfig = {type: 'bar', data: {labels: ['原煤灰分', '带煤量', 'A', 'B', '401', '402', '473', '474', '液位'], datasets: [{label: '|标准化系数|', data: active.coefficients.map(Math.abs), backgroundColor: '#8b5cf6'}]}, options: {responsive: true}};
        if (!charts.length) charts = [new Chart($('pgpt-trend'), trendConfig), new Chart($('pgpt-factors'), factorConfig)];
        else [trendConfig, factorConfig].forEach((config, i) => { charts[i].data = config.data; charts[i].update(); });
        drawScatter(prediction);
    }
    function drawScatter(prediction) {
        const canvas = $('pgpt-scatter'), box = canvas.parentElement.getBoundingClientRect(), dpr = devicePixelRatio || 1;
        const w = Math.max(box.width - 40, 200), h = 290;
        canvas.width = w * dpr; canvas.height = h * dpr; canvas.style.width = `${w}px`; canvas.style.height = `${h}px`;
        const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
        const points = records.map((r, i) => ({x: r.x[8], y: r.y, p: prediction[i]})).filter(r => Number.isFinite(r.x));
        const xmin = Math.min(...points.map(r => r.x)) - 3, xmax = Math.max(...points.map(r => r.x)) + 3;
        const ymin = Math.floor(Math.min(...points.flatMap(r => [r.y, r.p]))) - 1, ymax = Math.ceil(Math.max(...points.flatMap(r => [r.y, r.p]))) + 1;
        const px = x => 45 + (x - xmin) / (xmax - xmin) * (w - 65), py = y => h - 40 - (y - ymin) / (ymax - ymin) * (h - 75);
        ctx.font = '12px Microsoft YaHei'; ctx.fillStyle = '#b0bdd0'; ctx.fillText('灰分(%)', 5, 16);
        for (let i = 0; i <= 4; i++) {
            const x = xmin + (xmax - xmin) * i / 4, y = ymin + (ymax - ymin) * i / 4;
            ctx.strokeStyle = '#1e3355'; ctx.beginPath(); ctx.moveTo(45, py(y)); ctx.lineTo(w - 20, py(y)); ctx.stroke();
            ctx.fillText(fmt(y, 1), 5, py(y) + 4); ctx.fillText(fmt(x, 0), px(x) - 8, h - 22);
        }
        ctx.fillText('精磁尾液位(%)', Math.max(50, w / 2 - 40), h - 4);
        for (const [key, color] of [['y', '#60a5fa'], ['p', '#f59e0b']]) {
            ctx.fillStyle = color; points.forEach(r => {ctx.beginPath(); ctx.arc(px(r.x), py(r[key]), 3, 0, Math.PI * 2); ctx.fill();});
        }
        ctx.fillStyle = '#60a5fa'; ctx.fillText('● 实测', w - 155, 16); ctx.fillStyle = '#f59e0b'; ctx.fillText('● 预测', w - 80, 16);
    }
    function retrain() {
        const range = $('pgpt-range').value;
        if (range === 'factory') { delete custom[mode]; render(); return; }
        try {
            let training = M.aggregate(D.rows, mode);
            if (range === '30d') {
                const start = new Date(`${training.at(-1).day}T00:00:00Z`); start.setUTCDate(start.getUTCDate() - 29);
                training = training.filter(r => r.day >= start.toISOString().slice(0, 10));
            }
            const selection = M.select(training), model = M.fit(training, selection.lambda);
            const metrics = M.metrics(training.map(r => r.y), training.map(r => M.predict(model, r.x)));
            custom[mode] = {model, metrics, range};
            historyRows.unshift({time: new Date().toLocaleString('zh-CN'), mode: mode === 'day' ? '按日' : '按采样', n: training.length, range: `${model.start}—${model.end}`, lambda: model.lambda, r2: metrics.r2});
            historyRows = historyRows.slice(0, 40);
            let storageError = false;
            try {localStorage.setItem(historyKey, JSON.stringify(historyRows));} catch (_) {storageError = true;}
            render();
            if (storageError) $('pgpt-note').textContent += ' 浏览器存储不可用，训练历史仅保留到本页关闭。';
        } catch (error) { $('pgpt-note').textContent = `重训练未完成：${error.message}`; }
    }
    $('pgpt-form').addEventListener('submit', e => {
        e.preventDefault();
        const x = [...document.querySelectorAll('[data-feature]')].map(el => el.value.trim() === '' ? null : Number(el.value));
        const missing = x.flatMap((v, j) => v === null ? [D.features[j]] : []);
        const outside = x.flatMap((v, j) => v !== null && (v < active.minimum[j] || v > active.maximum[j]) ? [D.features[j]] : []);
        const prediction = M.predict(active, x);
        $('pgpt-result').textContent = `预测灰分：${fmt(prediction)}%。${prediction < 0 || prediction > 100 ? '结果超出灰分物理范围，请勿采用。' : ''}${missing.length ? ` 缺失因素使用训练中位数：${missing.join('、')}。` : ''}${outside.length ? ` 超出训练范围：${outside.join('、')}，属于外推。` : ''}`;
    });
    $('pgpt-mode').addEventListener('change', render);
    $('pgpt-tolerance').addEventListener('change', () => { $('pgpt-tolerance').value = tolerance(); render(); });
    $('pgpt-refresh').addEventListener('click', render);
    $('pgpt-retrain').addEventListener('click', retrain);
    $('pgpt-restore').addEventListener('click', () => {delete custom[mode]; render();});
    $('pgpt-source').textContent = `${D.sources.map(s => s.file).join('；')}。去重${D.duplicateCount}条，保留${D.rows.length}条采样，剔除${D.rejectedTargets.length}条无效目标。${D.datePolicy} 年份2026取自同目录带完整日期的数据表。`;
    $('pgpt-quality').textContent = D.rejectedTargets.map(r => `${r.id}：${r.reason}（${r.value}）；${r.source.file} / ${r.source.sheet} / 第${r.source.row}行`).join('\n');
    $('pgpt-quality').style.whiteSpace = 'pre-wrap';
    let resizeTimer;
    window.addEventListener('resize', () => {clearTimeout(resizeTimer); resizeTimer = setTimeout(() => {charts.forEach(c => c.resize()); drawScatter(records.map(r => M.predict(active, r.x)));}, 150);});
    render();
})();
