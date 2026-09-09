/* ========================================
   页面三：粗精煤泥灰分多因素实时分析
   - 预测模型：MLR / PLS（见 app.js），按 Q² 选生产模型
   - 展现：预测vs实测散点(±容差带) + 因子权重 + 近10天公式校验
   ======================================== */

const CoarsePage = {
    trendChart: null,
    factorChart: null,
    scatterChart: null,
    viewModel: 'production',   // 'production' | 'mlr' | 'pls'
    rollTimer: null,

    init() {
        this.refresh();
        this.initCharts();
        this.initControls();
        this.syncControls();
        this.startAutoRoll();
    },

    // 解析当前查看的算法 -> 'mlr' | 'pls'
    _which() {
        if (this.viewModel === 'production') {
            return App.store.coarseModel ? App.store.coarseModel.production : 'pls';
        }
        return this.viewModel;
    },

    _pred(rec) { return App.predictCoarseAsh(rec, this._which()); },

    // 统一获取液位：优先取 coarseCoal 自身 level，否则从 magneticTail 匹配
    _getLevel(d) {
        if (d.level !== undefined && d.level !== null && d.level > 0) return d.level;
        const mtData = App.store.magneticTail;
        // 任务四：就近匹配加时间窗（同系统、不晚于该记录且不超过5分钟），超窗按缺失(0)处理
        const t = new Date(d.timestamp).getTime();
        const mt = mtData.find(m => m.system === d.system && m.timestamp === d.timestamp)
                 || mtData.slice().reverse().find(m => m.system === d.system
                        && new Date(m.timestamp) <= new Date(d.timestamp)
                        && (t - new Date(m.timestamp).getTime()) <= 5 * 60000);
        return mt ? mt.level : 0;
    },

    // 同一批化验数据按不同生产系统重复导入时，会在同一采样时间产生多条内容相同的记录，
    // 生产系统对煤泥灰分影响很小，因此同一时刻只保留一条（首次导入的那条）用于展示/统计。
    _uniqueByTime(data) {
        const seen = new Set();
        return data.filter(d => {
            const key = String(d.timestamp);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    },

    // 取时间上最新的一条记录（数据并非总按时间排序存储，不能用数组末尾代替“最新”）
    _latestByTime(data) {
        let latest = null, latestT = -Infinity;
        (data || []).forEach(d => {
            const t = new Date(d.timestamp).getTime();
            if (isFinite(t) && t > latestT) { latestT = t; latest = d; }
        });
        return latest || (data && data.length ? data[data.length - 1] : null);
    },

    // 按时间升序排序（无法解析的时间戳排到最后）
    _sortByTime(data) {
        return (data || []).slice().sort((a, b) => {
            const ta = new Date(a.timestamp).getTime();
            const tb = new Date(b.timestamp).getTime();
            if (!isFinite(ta)) return 1;
            if (!isFinite(tb)) return -1;
            return ta - tb;
        });
    },

    // 班次筛选：白班 08:00~20:00，夜班 20:00~次日08:00
    _getShiftData(data) {
        if (data.length === 0) return [];
        const now = new Date();
        const hour = now.getHours();
        let shiftStart;
        if (hour >= 8 && hour < 20) {
            shiftStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 0, 0);
        } else if (hour >= 20) {
            shiftStart = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 20, 0, 0);
        } else {
            shiftStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1, 20, 0, 0);
        }
        return data.filter(d => new Date(d.timestamp) >= shiftStart);
    },

    // 取最近 N 天（以数据中最大时间戳为基准）的有效记录
    _recentRecords(data, days = 10) {
        data = this._uniqueByTime(data);
        const valid = data.filter(d => typeof d.ash_content === 'number' && d.ash_content > 0);
        if (valid.length === 0) return [];
        const maxTs = Math.max(...valid.map(d => new Date(d.timestamp).getTime()));
        const from = maxTs - days * 86400000;
        let rec = valid.filter(d => new Date(d.timestamp).getTime() >= from);
        return rec.length ? rec : valid;   // 不足时回退全部
    },

    // 实时拟合指标（基于当前查看的模型 + 当前容差）
    _liveMetrics(data) {
        const recs = data.filter(d => typeof d.ash_content === 'number' && d.ash_content > 0);
        if (recs.length < 2) return null;
        const y = recs.map(d => d.ash_content);
        const yh = recs.map(d => this._pred(d));
        return App._metrics(y, yh, App.MLR_FEATURES.length);
    },

    refresh() {
        this.updateCards();
        this.updateAmountCard();
        this.updateCharts();
        this.updateScatter();
        this.updateFactorChart();
        this.renderTable();
        this.updateModelSummary();
    },

    // 卡片5：粗精煤泥量手动输入（t/h），与"量数据（录入+输入）"面板共用同一数据源，参与累计灰分按煤量加权
    updateAmountCard() {
        const disp = document.getElementById('coarse-amount-display');
        const input = document.getElementById('coarse-amount-input');
        const v = App.resolveAmount('coarseAmount');
        if (disp) disp.innerHTML = v !== null
            ? `${v.toFixed(1)}<span class="stat-unit">t/h</span>` : '--<span class="stat-unit">t/h</span>';
        // 与量数据面板双向同步：外部修改时回填输入框（用户正在输入时不打断）
        if (input && v !== null && document.activeElement !== input && parseFloat(input.value) !== v) {
            input.value = v;
        }
    },

    onAmountChange() {
        const input = document.getElementById('coarse-amount-input');
        const v = input ? parseFloat(input.value) : NaN;
        const val = isNaN(v) ? null : v;
        if (input) input.value = isNaN(v) ? '' : v;
        // 写入量数据统一数据源，自动联动刷新 总览/粗精/浮精
        App.setAmountInput('coarseAmount', { mode: 'manual', manual: val });
        this.updateAmountCard();
        App.showToast(`粗精煤泥量已设为 ${val === null ? '--' : val.toFixed(1)} t/h`, 'info');
    },

    updateCards() {
        const data = this._uniqueByTime(App.store.coarseCoal);
        // 当前值应取“时间上最新”的一条，而不是数组最后一条（存储顺序不一定按时间）
        const latest = this._latestByTime(data);
        const which = this._which();

        // 卡片1：当前实测灰分
        document.getElementById('coarse-current-ash').innerHTML = latest
            ? `${latest.ash_content.toFixed(2)}<span class="stat-unit">%</span>` : '--';
        // 当前液位：在线仪表精磁尾液位计为权威值（手动>录入>默认）
        const curLevel = App.resolveInstrument('level_tail');
        document.getElementById('coarse-level').textContent = curLevel != null ? curLevel.toFixed(2) : '--';

        // 卡片2：多因素预测灰分（在线仪表液位为手动值时覆盖最新记录的液位特征）
        const lvlOverride = App.instrumentLayer('level_tail') === '手动' ? App.resolveInstrument('level_tail') : null;
        const pred = latest ? (lvlOverride != null ? App.predictCoarseAsh({ ...latest, level: lvlOverride }, this._which()) : this._pred(latest)) : null;
        document.getElementById('coarse-predicted-ash').innerHTML = (pred !== null && !isNaN(pred))
            ? `${pred.toFixed(2)}<span class="stat-unit">%</span>` : '--';
        const devEl = document.getElementById('coarse-predicted-dev');
        if (devEl) {
            if (latest && pred !== null) {
                const dev = pred - latest.ash_content;
                devEl.innerHTML = `偏差 ${dev > 0 ? '+' : ''}${dev.toFixed(2)}% · 算法 ${which.toUpperCase()}`;
            } else { devEl.textContent = '算法 ' + which.toUpperCase(); }
        }

        // 卡片3：累计灰分（本班次按煤量加权的预测灰分）
        let shiftData = this._getShiftData(data);
        let cumAsh = 0, shiftInfo = '暂无班次数据';
        if (shiftData.length === 0 && data.length > 0) shiftData = data;
        shiftData = this._sortByTime(shiftData);   // 按时间排序，保证“末条=最新”与范围显示正确
        if (shiftData.length > 0) {
            // 手动输入的粗精煤泥量（与"量数据"面板同步）作为最新一条记录的煤量权重参与加权
            const inputAmt = App.resolveAmount('coarseAmount') || 0;            let sumW = 0, sumAmt = 0;
            shiftData.forEach((d, i) => {
                const p = this._pred(d);
                const amt = (i === shiftData.length - 1 && inputAmt > 0) ? inputAmt : (d.coal_amount || 0);
                sumW += p * amt; sumAmt += amt;
            });
            cumAsh = sumAmt > 0 ? sumW / sumAmt : shiftData.reduce((s, d) => s + this._pred(d), 0) / shiftData.length;
            const first = shiftData[0], last = shiftData[shiftData.length - 1];
            const fmt = t => (t || '').length > 16 ? t.slice(5, 16) : (t || '--');
            shiftInfo = `${fmt(first.timestamp)} ~ ${fmt(last.timestamp)}${inputAmt > 0 ? ' · 末条煤量' + inputAmt.toFixed(1) + 't/h' : ''}`;
        }
        document.getElementById('coarse-cumulative-ash').innerHTML =
            `${isNaN(cumAsh) ? '--' : cumAsh.toFixed(2)}<span class="stat-unit">%</span>`;
        const cumSub = document.getElementById('coarse-cumulative-sub');
        if (cumSub) cumSub.textContent = shiftInfo;

        // 卡片4：模型拟合度（R² + ±容差合格率）
        const m = this._liveMetrics(data);
        const tol = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
        document.getElementById('coarse-fit-r2').innerHTML = m
            ? `${m.r2.toFixed(3)}` : '--';
        const fitSub = document.getElementById('coarse-fit-sub');
        if (fitSub) fitSub.innerHTML = m
            ? `合格率 <strong>${m.passRate.toFixed(0)}%</strong> (±${tol}%) · MAE ${m.mae.toFixed(2)}` : '需先导入数据';
    },

    initCharts() {
        // 趋势图：实测 / 多因素预测 / 目标
        const ctx1 = document.getElementById('chart-coarse-trend').getContext('2d');
        this.trendChart = new Chart(ctx1, {
            type: 'line',
            data: { labels: [], datasets: [
                { label: '实测灰分(%)', data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', fill: true, tension: 0.3, pointRadius: 3, borderWidth: 2 },
                { label: '多因素预测(%)', data: [], borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.06)', fill: false, tension: 0.3, pointRadius: 2, borderWidth: 2, borderDash: [4, 3] },
                { label: '目标灰分', data: [], borderColor: '#8b5cf6', borderDash: [5, 5], pointRadius: 0, borderWidth: 1.5, fill: false }
            ] },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { labels: { color: '#b0bdd0', font: { size: 12 } } },
                           tooltip: { backgroundColor: '#1e3355', titleColor: '#f0f4fa', bodyColor: '#b0bdd0' } },
                scales: {
                    x: { ticks: { color: '#7b8da6', maxTicksLimit: 24 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                    y: { ticks: { color: '#7b8da6', precision: 0 }, grid: { color: 'rgba(255,255,255,0.04)' },
                         title: { display: true, text: '灰分(%)', color: '#7b8da6' } }
                }
            }
        });

        // 因子权重图：|标准化系数| 条形（颜色/箭头表方向）
        const ctx2 = document.getElementById('chart-coarse-factors').getContext('2d');
        this.factorChart = new Chart(ctx2, {
            type: 'bar',
            data: { labels: [], datasets: [{ label: '影响权重(|标准化系数|)', data: [], backgroundColor: [] }] },
            options: {
                responsive: true, maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#b0bdd0', font: { size: 11 } } },
                           tooltip: { backgroundColor: '#1e3355', titleColor: '#f0f4fa', bodyColor: '#b0bdd0' } },
                scales: { x: { ticks: { color: '#7b8da6', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.04)' } },
                          y: { ticks: { color: '#7b8da6' }, grid: { color: 'rgba(255,255,255,0.04)' },
                               title: { display: true, text: '|标准化系数|', color: '#7b8da6' } } }
            }
        });

        // 灰分 vs 液位：自绘数值 X 轴散点图（实际值 + 多因素预测值，每条记录一点）
        const canvas3 = document.getElementById('chart-coarse-regression');
        this._initLevelTooltip(canvas3);
        if (!this._levelResizeBound) { this._levelResizeBound = true; window.addEventListener('resize', () => this._drawLevelChart()); }
        this._drawLevelChart();
        this.updateCharts();
        this.updateFactorChart();
    },

    updateCharts() {
        if (!this.trendChart) return;
        // 按真实时间排序并去重（同批数据跨系统重复导入时同一时刻只显示一个点）；
        // 同时把每点时间戳交给图表，使 X 轴按真实时间间隔比例分布（与数据条数无关）
        const data = this._uniqueByTime(this._sortByTime(App.store.coarseCoal));
        const pad = n => String(n).padStart(2, '0');
        const times = [];
        const labels = data.map(d => {
            const dt = new Date(d.timestamp);
            const t = dt.getTime();
            times.push(isFinite(t) ? t : null);
            return isFinite(t)
                ? `${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`
                : String(d.timestamp || '');
        });
        this.trendChart.xTimes = null;   // 序号等距分布：同一日多个采样点分开显示，时间经X轴标签体现
        // 横轴以天为单位：每天第一条记录处标注 MM-DD；各点完整时间在悬停提示中显示
        const dayTicks = labels.map((lb, i) => {
            const day = data[i] ? String(data[i].timestamp).slice(5, 10) : '';
            const prev = i > 0 && data[i - 1] ? String(data[i - 1].timestamp).slice(5, 10) : null;
            return (day && day !== prev) ? day : '';
        });
        this.trendChart.xTickLabels = dayTicks;

        this.trendChart.data.labels = labels;
        this.trendChart.data.datasets[0].data = data.map(d => d.ash_content);
        this.trendChart.data.datasets[1].data = data.map(d => +this._pred(d).toFixed(3));
        this.trendChart.data.datasets[2].data = data.map(() => 13.0);
        this.trendChart.update('none');
    },

    updateScatter() { this._drawLevelChart(); },

    // 自定义数值 X 轴散点图：X=精磁尾液位(线性比例) Y=灰分，实际值+预测值各一条记录一点
    // （chart-lite 折线 X 轴按索引等距、无法按数值线性映射，故此图自绘 canvas）
    _drawLevelChart() {
        const canvas = document.getElementById('chart-coarse-regression');
        if (!canvas) return;
        const parent = canvas.parentElement;
        const dpr = window.devicePixelRatio || 1;
        const w = (parent && parent.clientWidth > 0) ? parent.clientWidth : (canvas.clientWidth || 800);
        const h = (parent && parent.clientHeight > 0) ? parent.clientHeight : 340;
        if (w <= 0 || h <= 0) return;
        canvas.style.width = '100%'; canvas.style.height = '100%';
        const W = Math.round(w * dpr), H = Math.round(h * dpr);
        if (canvas.width !== W || canvas.height !== H) { canvas.width = W; canvas.height = H; }
        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        const FONT = '12px Microsoft YaHei, sans-serif';

        const valid = this._uniqueByTime(App.store.coarseCoal)
            .filter(d => typeof d.ash_content === 'number' && d.ash_content > 0 && this._getLevel(d) > 0);
        if (valid.length === 0) {
            ctx.fillStyle = '#7b8da6'; ctx.font = FONT; ctx.textAlign = 'center';
            ctx.fillText('暂无数据，请先导入多因素历史数据', w / 2, h / 2);
            this._levelPts = []; return;
        }
        const pts = valid.map(d => ({ lv: this._getLevel(d), act: d.ash_content, pred: this._pred(d) }));
        const lvs = pts.map(p => p.lv);
        const ys = pts.flatMap(p => [p.act, p.pred]);
        const xMin = Math.floor(Math.min(...lvs)) - 1, xMax = Math.ceil(Math.max(...lvs)) + 1;
        let yMin = Math.min(...ys), yMax = Math.max(...ys);
        const yr = (yMax - yMin) || 1; yMin -= yr * 0.1; yMax += yr * 0.1;
        const pad = { t: 26, r: 28, b: 46, l: 56 };
        const cw = w - pad.l - pad.r, ch = h - pad.t - pad.b;
        const toX = lv => pad.l + ((lv - xMin) / (xMax - xMin)) * cw;
        const toY = v => pad.t + ch - ((v - yMin) / (yMax - yMin)) * ch;

        // 网格 + Y 轴刻度
        ctx.strokeStyle = 'rgba(255,255,255,0.06)'; ctx.lineWidth = 1;
        ctx.fillStyle = '#7b8da6'; ctx.font = '11px Microsoft YaHei, sans-serif'; ctx.textAlign = 'right';
        for (let i = 0; i <= 5; i++) {
            const v = yMin + (yMax - yMin) * i / 5; const y = toY(v);
            ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
            ctx.fillText(v.toFixed(1), pad.l - 6, y + 4);
        }
        // X 轴刻度
        ctx.textAlign = 'center';
        for (let i = 0; i <= 6; i++) {
            const lv = xMin + (xMax - xMin) * i / 6; ctx.fillText(lv.toFixed(0), toX(lv), h - pad.b + 16);
        }
        // 轴标题
        ctx.save(); ctx.translate(14, pad.t + ch / 2); ctx.rotate(-Math.PI / 2);
        ctx.textAlign = 'center'; ctx.font = FONT; ctx.fillStyle = '#7b8da6';
        ctx.fillText('灰分(%)', 0, 0); ctx.restore();
        ctx.textAlign = 'center'; ctx.fillText('精磁尾液位(%)', pad.l + cw / 2, h - 8);

        // 散点：预测在下层(橙)，实际在上层(蓝)，每条记录一点 → 数值不丢失
        ctx.fillStyle = 'rgba(245,158,11,0.6)';
        pts.forEach(p => { ctx.beginPath(); ctx.arc(toX(p.lv), toY(p.pred), 2.6, 0, Math.PI * 2); ctx.fill(); });
        ctx.fillStyle = '#3b82f6';
        pts.forEach(p => { ctx.beginPath(); ctx.arc(toX(p.lv), toY(p.act), 3, 0, Math.PI * 2); ctx.fill(); });

        // 图例
        ctx.font = FONT; ctx.textAlign = 'left';
        [['#3b82f6', '实际灰分'], ['rgba(245,158,11,0.95)', '预测灰分']].forEach((it, i) => {
            const lx = pad.l + 8 + i * 110, ly = pad.t + 6;
            ctx.fillStyle = it[0]; ctx.beginPath(); ctx.arc(lx, ly, 4, 0, Math.PI * 2); ctx.fill();
            ctx.fillStyle = '#b0bdd0'; ctx.fillText(it[1], lx + 9, ly + 4);
        });

        this._levelGeom = { w, h };
        this._levelPts = pts.map(p => ({ x: toX(p.lv), y: toY(p.act), lv: p.lv, act: p.act, pred: p.pred }));

        // 角标
        const m = this._liveMetrics(this._uniqueByTime(App.store.coarseCoal));
        const tol = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
        const cap = document.getElementById('regression-r2-text');
        const which = this._which().toUpperCase();
        if (cap) cap.textContent = m
            ? `R²=${m.r2.toFixed(3)} | RMSE=${m.rmse.toFixed(2)} | MAE=${m.mae.toFixed(2)} | ±${tol}%合格=${m.passRate.toFixed(0)}% | ${which} | ${pts.length}点(每点=1条记录)`
            : (pts.length + '个数据点');
        const fEl = document.getElementById('regression-formula-text');
        if (fEl) fEl.textContent = `X:精磁尾液位 Y:灰分 | 算法：${which}（${this.viewModel === 'production' ? '生产模型' : '对比查看'}）`;
    },

    _initLevelTooltip(canvas) {
        if (!canvas || canvas._lvlBound) return;
        canvas._lvlBound = true;
        canvas.addEventListener('mousemove', (e) => {
            if (!this._levelPts || !this._levelPts.length) return;
            const rect = canvas.getBoundingClientRect();
            const mx = e.clientX - rect.left, my = e.clientY - rect.top;
            let best = null, bd = 16;
            this._levelPts.forEach(p => { const d = Math.hypot(p.x - mx, p.y - my); if (d < bd) { bd = d; best = p; } });
            this._levelHover = best || null;
            this._drawLevelHover();
        });
        canvas.addEventListener('mouseleave', () => { this._levelHover = null; this._drawLevelHover(); });
    },

    _drawLevelHover() {
        this._drawLevelChart();
        const h = this._levelHover; if (!h || !this._levelGeom) return;
        const canvas = document.getElementById('chart-coarse-regression');
        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1; ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(h.x, h.y, 6, 0, Math.PI * 2); ctx.stroke();
        const txt = `液位 ${h.lv.toFixed(1)}% · 实际 ${h.act.toFixed(2)}% · 预测 ${h.pred.toFixed(2)}%`;
        ctx.font = '12px Microsoft YaHei, sans-serif';
        const tw = ctx.measureText(txt).width + 16;
        const bx = Math.min(h.x + 8, this._levelGeom.w - tw - 2), by = Math.max(h.y - 28, 2);
        ctx.fillStyle = '#1e3355'; ctx.fillRect(bx, by, tw, 22);
        ctx.fillStyle = '#f0f4fa'; ctx.textAlign = 'left'; ctx.fillText(txt, bx + 8, by + 15);
    },

    // 因子权重条形：|标准化系数|，正绿负红，标签带↑↓
    updateFactorChart() {
        if (!this.factorChart) return;
        const model = App.getCoarseModel(this._which());
        const stdCoef = model.stdCoef || [];
        const feats = App.MLR_FEATURES;
        const items = feats.map((f, i) => ({ name: App.FEATURE_LABELS[f] || f, val: stdCoef[i] || 0 }))
                           .sort((a, b) => Math.abs(b.val) - Math.abs(a.val));
        this.factorChart.data.labels = items.map(it => `${it.name}${it.val >= 0 ? '↑' : '↓'}`);
        this.factorChart.data.datasets[0].data = items.map(it => +Math.abs(it.val).toFixed(3));
        this.factorChart.data.datasets[0].backgroundColor = items.map(it => it.val >= 0 ? '#10b981' : '#ef4444');
        this.factorChart.update();
    },

    // 近10天公式校验表
    renderTable() {
        const tol = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
        const recs = this._recentRecords(App.store.coarseCoal, 10)
            .slice().sort((a, b) => new Date(b.timestamp) - new Date(a.timestamp)).slice(0, 50);
        const tbody = document.getElementById('coarse-tbody');
        if (!tbody) return;
        if (recs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">暂无数据，请先导入多因素历史数据</td></tr>';
            return;
        }
        tbody.innerHTML = recs.map(d => {
            const pred = this._pred(d);
            const dev = pred - d.ash_content;
            const ok = Math.abs(dev) <= tol;
            return `<tr>
                <td style="font-size:12px">${d.timestamp}</td>
                <td>${d.ash_content.toFixed(2)}</td>
                <td>${pred.toFixed(2)}</td>
                <td class="${ok ? '' : 'text-warn'}">${dev > 0 ? '+' : ''}${dev.toFixed(2)}</td>
                <td><span class="annotate-tag ${ok ? 'normal' : 'press-filter'}">${ok ? '合格 ✓' : '超差 ✗'}</span></td>
                <td style="font-size:12px">${d.system || '-'}</td>
            </tr>`;
        }).join('');
    },

    // 校验面板摘要 + 重训练历史
    updateModelSummary() {
        const tol = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
        const recent = this._recentRecords(App.store.coarseCoal, 10);
        const m = this._liveMetrics(recent);
        const recent30 = this._recentRecords(App.store.coarseCoal, 30);
        const m30 = this._liveMetrics(recent30);
        const el = document.getElementById('coarse-model-summary');
        const cm = App.store.coarseModel;
        const prod = cm ? cm[cm.production] : null;
        const selBasis = (prod && prod.metrics && prod.metrics.q2Time != null) ? '时间Q²选优' : 'Q²选优';
        const prodTxt = cm ? `${cm.production.toUpperCase()} (${selBasis})` : '出厂默认 PLS';
        const rangeLabel = r => (r === 'jun_jul' ? '6-7月' : r === '30d' ? '近30天' : r === 'all' ? '全部' : (r || '6-7月'));
        const trainedTxt = cm ? `${cm.trainedAt}（${rangeLabel(cm.range)}，n=${cm.n}）` : '未训练（使用出厂模型）';
        let html = `<div class="summary-item"><span class="summary-label">近10天样本：</span><span class="summary-val">${recent.length}</span></div>`;
        // 日常参考：近30天窗口样本量与合格率（口径策略：默认出厂6-7月，日常对照近30天）
        html += `<div class="summary-item"><span class="summary-label">近30天样本：</span><span class="summary-val">${recent30.length}</span></div>`;
        if (m30) {
            html += `<div class="summary-item"><span class="summary-label">近30天合格率：</span><span class="summary-val" style="color:${m30.passRate >= 60 ? 'var(--accent-green)' : m30.passRate >= 30 ? 'var(--accent-orange)' : 'var(--accent-red)'}">${m30.passRate.toFixed(0)}%</span></div>`;
        }
        if (m) {
            html += `<div class="summary-item"><span class="summary-label">R²：</span><span class="summary-val">${m.r2.toFixed(3)}</span></div>`;
            // 三档容差合格率（±0.8/±1.0/±1.5%），供工艺确认可接受偏差口径
            const p1 = (m.passRate1 !== undefined) ? m.passRate1 : m.passRate;
            const p15 = (m.passRate15 !== undefined) ? m.passRate15 : m.passRate;
            html += `<div class="summary-item"><span class="summary-label">合格率±0.8/1.0/1.5%：</span><span class="summary-val" style="color:${m.passRate >= 60 ? 'var(--accent-green)' : m.passRate >= 30 ? 'var(--accent-orange)' : 'var(--accent-red)'}">${m.passRate.toFixed(0)}/${p1.toFixed(0)}/${p15.toFixed(0)}%</span></div>`;
            html += `<div class="summary-item"><span class="summary-label">MAE：</span><span class="summary-val">${m.mae.toFixed(2)}%</span></div>`;
            html += `<div class="summary-item"><span class="summary-label">RMSE：</span><span class="summary-val">${m.rmse.toFixed(2)}%</span></div>`;
        }
        html += `<div class="summary-item"><span class="summary-label">生产算法：</span><span class="summary-val">${prodTxt}</span></div>`;
        html += `<div class="summary-item"><span class="summary-label">最近训练：</span><span class="summary-val" style="font-size:12px">${trainedTxt}</span></div>`;
        if (el) el.innerHTML = html;

        // 重训练历史 mini 表
        const histEl = document.getElementById('coarse-history-tbody');
        if (histEl) {
            const hist = (App.store.coarseModelHistory || []).slice().reverse().slice(0, 8);
            if (hist.length === 0) {
                histEl.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">暂无重训练记录</td></tr>';
            } else {
                const rangeLabel = r => (r === 'jun_jul' ? '6-7月' : r === '30d' ? '近30天' : r === 'all' ? '全部' : (r || '6-7月'));
                const q2t = v => (v != null) ? ` Q²时${(+v).toFixed(3)}` : '';
                histEl.innerHTML = hist.map(h => `<tr>
                    <td style="font-size:12px">${h.trainedAt}</td>
                    <td>${h.n}</td>
                    <td>${rangeLabel(h.range)}</td>
                    <td>MLR: R²${h.mlr.r2.toFixed(3)} 合格${h.mlr.passRate.toFixed(0)}% Q²${h.mlr.q2.toFixed(3)}${q2t(h.mlr.q2Time)}</td>
                    <td>PLS(A${h.pls.A}): R²${h.pls.r2.toFixed(3)} 合格${h.pls.passRate.toFixed(0)}% Q²${h.pls.q2.toFixed(3)}${q2t(h.pls.q2Time)}</td>
                    <td><span class="annotate-tag normal">${h.production.toUpperCase()}</span></td>
                </tr>`).join('');
            }
        }
    },

    // ---------- 控件 ----------
    initControls() {
        // 容差、算法视图、重训练、5分钟滚动 由 index.html 直接 onclick/onchange 调用下方方法
    },
    syncControls() {
        const tolEl = document.getElementById('coarse-tol');
        if (tolEl) tolEl.value = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
        const vmEl = document.getElementById('coarse-view-model');
        if (vmEl) vmEl.value = this.viewModel;
        const rollEl = document.getElementById('coarse-auto-roll');
        if (rollEl) rollEl.checked = !!this.rollTimer;
        const trEl = document.getElementById('coarse-train-range');
        if (trEl) trEl.value = App.store.coarseTrainRange || 'jun_jul';
    },
    onTrainRangeChange() {
        const el = document.getElementById('coarse-train-range');
        const v = (el && el.value) || 'jun_jul';
        App.store.coarseTrainRange = v;
        App.saveStore();
        this.retrain();
    },
    onTolChange() {
        const v = parseFloat(document.getElementById('coarse-tol').value);
        if (!isNaN(v) && v > 0) {
            App.store.coarseTolerance = v;
            App.saveStore();
            this.refresh();
            App.showToast(`容差已设为 ±${v}%`, 'info');
        }
    },
    onModelViewChange() {
        this.viewModel = document.getElementById('coarse-view-model').value;
        this.refresh();
    },
    async retrain() {
        const range = App.store.coarseTrainRange || 'jun_jul';
        const ok = await App.retrainCoarseModelAsync(range);
        if (ok) {
            const cm = App.store.coarseModel;
            const label = App.filterTrainRows(range).length;
            App.showToast(`重训练完成（${range === 'jun_jul' ? '六月至七月' : range === '30d' ? '最近30天' : '全部数据'}，n=${label}）：${cm.production.toUpperCase()} R²=${cm[cm.production].metrics.r2.toFixed(3)} 合格率=${cm[cm.production].metrics.passRate.toFixed(0)}%`, 'success');
        } else {
            App.showToast('训练失败，数据不足', 'error');
        }
        this.refresh();
    },
    toggleAutoRoll() {
        const rollEl = document.getElementById('coarse-auto-roll');
        if (rollEl && rollEl.checked) this.startAutoRoll();
        else this.stopAutoRoll();
    },
    startAutoRoll() {
        this.stopAutoRoll();
        this.rollTimer = setInterval(() => {
            App.refreshCoarsePredictions();
            this.updateCards();
            this.updateScatter();
            this.renderTable();
            const el = document.getElementById('coarse-update-time');
            if (el) el.textContent = '滚动更新：' + App.formatDate(new Date());
        }, 5 * 60 * 1000);
        const el = document.getElementById('coarse-auto-roll'); if (el) el.checked = true;
    },
    stopAutoRoll() {
        if (this.rollTimer) { clearInterval(this.rollTimer); this.rollTimer = null; }
        const el = document.getElementById('coarse-auto-roll'); if (el) el.checked = false;
    },

    switchView() { this.updateCharts(); this.updateCards(); },

    // ---------- 卡片详情 ----------
    showCardDetail(type) {
        const data = this._uniqueByTime(App.store.coarseCoal);
        const latest = this._latestByTime(data);
        const cm = App.store.coarseModel;
        const which = this._which();
        const m = this._liveMetrics(data);
        let html = '';
        if (type === 'ash') {
            html = `<div style="line-height:2;font-size:14px">
                <h4 style="margin:0 0 12px;color:var(--accent-blue)">当前实测灰分</h4>
                <p>取最新一条粗精煤泥化验数据（315灰分），直接读值。</p>
                <table class="data-table" style="margin:4px 0 12px">
                    <tr><td>实测灰分</td><td>${latest ? latest.ash_content.toFixed(2) + '%' : '暂无'}</td></tr>
                    <tr><td>煤量</td><td>${latest ? (latest.coal_amount||0).toFixed(1) + ' t/h' : '-'}</td></tr>
                    <tr><td>精磁尾液位</td><td>${latest ? this._getLevel(latest).toFixed(2) + '%' : '-'}</td></tr>
                    <tr><td>原煤灰分</td><td>${latest && latest.raw_ash != null ? latest.raw_ash.toFixed(2) + '%' : '-'}</td></tr>
                    <tr><td>采样时间</td><td>${latest ? latest.timestamp : '-'}</td></tr>
                </table></div>`;
        } else if (type === 'predict') {
            const pred = latest ? this._pred(latest) : null;
            const dev = (pred !== null && latest) ? pred - latest.ash_content : 0;
            html = `<div style="line-height:2;font-size:14px">
                <h4 style="margin:0 0 12px;color:var(--accent-blue)">多因素预测灰分（${which.toUpperCase()}）</h4>
                <p style="color:var(--text-secondary)">将原煤灰分、带煤量、系统组合、脱粉、停机、液位等扰动因子全部纳入前馈计算。</p>
                <p>预测灰分 = ${pred !== null ? pred.toFixed(2) + '%' : '--'}　实测 = ${latest ? latest.ash_content.toFixed(2) + '%' : '--'}　偏差 = ${dev > 0 ? '+' : ''}${dev.toFixed(2)}%</p>
                <p style="color:var(--text-secondary);font-size:12px">生产模型：${cm ? cm.production.toUpperCase() + '（按留一交叉验证 Q² 选优）' : '出厂默认 PLS（待训练）'}</p></div>`;
        } else if (type === 'cumulative') {
            let shiftData = this._getShiftData(data);
            if (shiftData.length === 0 && data.length) shiftData = data;
            const inputAmt = App.resolveAmount('coarseAmount') || 0;            let rows = '', sumW = 0, sumAmt = 0;
            shiftData.forEach((d, i) => { const p = this._pred(d);
                const amt = (i === shiftData.length - 1 && inputAmt > 0) ? inputAmt : (d.coal_amount || 0);
                sumW += p * amt; sumAmt += amt;
                rows += `<tr><td>${d.timestamp}</td><td>${p.toFixed(2)}%</td><td>${amt.toFixed(1)}</td></tr>`; });
            const cum = sumAmt > 0 ? sumW / sumAmt : 0;
            html = `<div style="line-height:2;font-size:14px">
                <h4 style="margin:0 0 12px;color:var(--accent-blue)">累计灰分（本班次按煤量加权）</h4>
                <p style="color:var(--text-secondary)">累计灰分 = Σ(预测灰分×煤量) / Σ(煤量)${inputAmt > 0 ? '（末条煤量采用手动输入 ' + inputAmt.toFixed(1) + ' t/h）' : ''}</p>
                <table class="data-table" style="margin:4px 0 12px"><tr><th>时间</th><th>预测灰分</th><th>煤量</th></tr>${rows || '<tr><td colspan="3">暂无数据</td></tr>'}</table>
                <p>累计灰分 = <strong>${cum.toFixed(2)}%</strong>（共 ${shiftData.length} 条）</p></div>`;
        } else if (type === 'fit') {
            const tol = (App.store.coarseTolerance !== undefined) ? App.store.coarseTolerance : 0.8;
            html = `<div style="line-height:2;font-size:14px">
                <h4 style="margin:0 0 12px;color:var(--accent-blue)">模型拟合度</h4>
                <p>R² = <strong>${m ? m.r2.toFixed(3) : '--'}</strong>（决定系数，越接近1越好）</p>
                <p>±${tol}% 合格率 = <strong>${m ? m.passRate.toFixed(0) + '%' : '--'}</strong>　MAE = ${m ? m.mae.toFixed(2) + '%' : '--'}　RMSE = ${m ? m.rmse.toFixed(2) + '%' : '--'}</p>
                <p style="color:var(--accent-orange);font-size:12px">说明：±${tol}% 是目标容差带。当前历史数据噪声地板较高（瞬时点样 vs 班级原煤灰分），合格率为真实值，随数据累积与时间对齐改善而上升，可在“重训练历史”中追踪。</p></div>`;
        }
        App.openModal('计算详情', html, '<button class="btn" onclick="App.closeModal()">关闭</button>');
    }
};
