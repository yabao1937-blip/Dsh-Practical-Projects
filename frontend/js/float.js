/* ========================================
   页面四：浮精影响分析
   - 浮精灰分/量对总精煤灰分的影响链路
   ======================================== */

const FloatPage = {
    linkageChart: null,
    distChart: null,

    init() {
        this.initTimeRange();
        this.refresh();
        this.initCharts();
    },

    // 同一批化验数据按不同生产系统重复导入时，会在同一采样时间产生多条内容相同的记录，
    // 因此同一时刻只保留一条（首次导入的那条）用于展示/统计，避免同一时间重复显示。
    _uniqueByTime(data) {
        const seen = new Set();
        return data.filter(d => {
            const key = String(d.timestamp);
            if (seen.has(key)) return false;
            seen.add(key);
            return true;
        });
    },

    // 取时间上最新的一条记录（存储顺序不一定按时间）
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

    // 时间轴刻度标签：先把每个数据点格式化成"能塞得下"的短标签（跨天只写 MM-DD），
    // 再按可用的像素宽度自动抽取刻度间隔（1/2/3/7/14/30 天…），密集处必然被跳过，
    // 所以不会出现多个标签叠在同一处。原因：浮精化验数据是"每天 1 个点、有时隔几十天再采"
    // 的稀疏数据，若每个点都标完整时间，靠得近的点标签会横向挤在一起糊成一片。
    // indexBased=true 表示目标图的 X 位置按数据序号等距（柱状图），此时像素位置也按序号算——
    // 否则"按时间比例算出来的位置"与图上实际位置不一致，抽出来的刻度会与实际布局错位。
    _timeTickLabels(data, times, chartPx, indexBased) {
        const pad = n => String(n).padStart(2, '0');
        const n = data.length;
        const out = new Array(n).fill('');

        let t0 = null, t1 = null;
        for (let i = 0; i < n; i++) {
            const t = times[i];
            if (typeof t !== 'number' || !isFinite(t)) continue;
            if (t0 === null) t0 = t;
            t1 = t;
        }
        if (t0 === null) return out;                       // 时间戳无法解析：交给通用刻度兜底
        const span = t1 - t0;
        const w = chartPx || 640;
        const DAY = 86400000;
        const HOUR = 3600000;
        // 某点在目标图上的像素位置：柱状图按序号等距（与 chart-lite._drawBar 的柱心一致），
        // 折线图按时间比例（与 chart-lite._drawLine 的 xTimes 布局一致）
        const xAt = (i, t) => indexBased
            ? ((i + 0.5) / (n || 1)) * w
            : ((t - t0) / (span || 1)) * w;
        if (span < DAY) {                                  // 数据集中在 1 天内：按"时:分"标刻度
            const hm = t => {
                const d = new Date(t);
                return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
            };
            const candsH = [HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR];
            const need0 = 56, gap0 = 32;
            const pxPerHour = w / (span / HOUR);
            for (let ci = 0; ci < candsH.length; ci++) {
                const dt = candsH[ci];
                if (dt * pxPerHour / HOUR < need0) continue;   // 间隔太密，换更大的（candsH 单位是"小时"）
                let lastT = -Infinity, lastX = -Infinity;
                for (let i = 0; i < n; i++) {
                    const t = times[i];
                    if (typeof t !== 'number' || !isFinite(t)) continue;
                    if (t - lastT < dt) continue;
                    const x = xAt(i, t);
                    if (x - lastX < gap0) continue;
                    out[i] = hm(t);
                    lastT = t;
                    lastX = x;
                }
                return out;
            }
            if (n) out[0] = hm(t0);
            return out;
        }

        const labelOf = t => {
            const d = new Date(t);
            let s = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
            if (span <= 3 * DAY) s += ` ${pad(d.getHours())}:${pad(d.getMinutes())}`;
            return s;
        };

        // 候选间隔（天）：1/2/3/7/14/30；跨度超过 3 个月再放大到 6/12/18/24 天
        const cands = [1, 2, 3, 7, 14, 30];
        if (span > 92 * DAY) cands.push(60, 90);
        // 从最小的刻度间隔往上试，第一个"放得下"的就用它
        const needPx = 88;                                 // 相邻刻度标签之间的最小像素距离
        const minGapPx = 76;                               // "MM-DD" 约 36px 宽 + 40px 呼吸位
        const pxPerDay = w / (span / DAY);
        for (let ci = 0; ci < cands.length; ci++) {
            const dt = cands[ci] * DAY;
            if (dt * pxPerDay / DAY < needPx) continue;    // 间隔太密，换更大的（cands 单位是"天"）
            let lastLabelT = -Infinity, lastLabelX = -Infinity;
            for (let i = 0; i < n; i++) {
                const t = times[i];
                if (typeof t !== 'number' || !isFinite(t)) continue;
                if (t - lastLabelT < dt) continue;
                const x = xAt(i, t);
                if (x - lastLabelX < minGapPx) continue;   // 双保险：离上一个标签太近仍跳过
                out[i] = labelOf(t);
                lastLabelT = t;
                lastLabelX = x;
            }
            return out;
        }
        // 兜底：跨度很短但点多时，按像素均匀地隔几个点标一个
        const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(w / needPx))));
        for (let i = 0; i < n; i += step) out[i] = labelOf(times[i]);
        return out;
    },

    initTimeRange() {
        const now = new Date();
        const start = new Date(now.getTime() - 24 * 3600000);
        const pad = n => String(n).padStart(2, '0');
        const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        document.getElementById('float-start-time').value = fmt(start);
        document.getElementById('float-end-time').value = fmt(now);
    },

    query() {
        this.refresh();
        this.updateCharts();
        this.renderTable();
        App.showToast('查询完成', 'success');
    },

    refresh() {
        this.updateCards();
        this.renderTable();
    },

    updateCards() {
        const data = this._uniqueByTime(App.store.floatCoal);
        if (data.length === 0) {
            const fAshEmpty = App.resolveFloatAsh();
            document.getElementById('float-current-ash').innerHTML =
                fAshEmpty == null ? '--<span class="stat-unit">%</span>' : `${fAshEmpty.toFixed(2)}<span class="stat-unit">%</span>`;
            const fAmtEmpty = App.resolveAmount('floatAmount');
            document.getElementById('float-current-amount').innerHTML =
                (fAmtEmpty == null ? '--' : fAmtEmpty.toFixed(1)) + '<span class="stat-unit">t/h</span>';
            this.syncAmountInput();
            document.getElementById('float-fixed-influence').innerHTML = '--<span class="stat-unit">%</span>';
            document.getElementById('float-dynamic-range').innerHTML = '--<span class="stat-unit">%</span>';
            return;
        }
        // 当前值应取“时间上最新”的一条，而不是数组最后一条
        const latest = this._latestByTime(data);
        // 浮精煤量：与"量数据（录入+输入）"面板同步（自动=最新记录煤量，手动=面板录入值）
        const fAmt = this.syncAmountInput();
        const fAmtVal = fAmt == null ? 0 : fAmt;

        const fAsh = App.resolveFloatAsh();
        document.getElementById('float-current-ash').innerHTML =
            fAsh == null ? '--<span class="stat-unit">%</span>' : `${fAsh.toFixed(2)}<span class="stat-unit">%</span>`;
        document.getElementById('float-current-amount').innerHTML =
            `${fAmt == null ? '--' : fAmt.toFixed(1)}<span class="stat-unit">t/h</span>`;

        // 固定影响值计算（#2 动态重介灰分, #3 去掉粗精项）
        const heavyAsh = App.getAshByTime(latest.timestamp) ?? 8.50;   // 超窗记缺失回退默认
        const heavyAmt = 250;
        const totalWithout = App.calcTotalAsh(heavyAsh, heavyAmt, 0, 0, 0, 0);
        const totalWith = App.calcTotalAsh(heavyAsh, heavyAmt, latest.ash_content, fAmtVal, 0, 0);
        const fixedInfluence = totalWith - totalWithout;
        document.getElementById('float-fixed-influence').innerHTML =
            `${fixedInfluence > 0 ? '+' : ''}${fixedInfluence.toFixed(2)}<span class="stat-unit">%</span>`;

        // 动态波动范围（标准差）（#4 使用动态重介灰分 + 去掉粗精项）
        const influences = data.map(d => {
            const hAsh = App.getAshByTime(d.timestamp) ?? 8.50;   // 超窗记缺失回退默认
            const tw = App.calcTotalAsh(hAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
            const base = App.calcTotalAsh(hAsh, heavyAmt, 0, 0, 0, 0);
            return tw - base;
        });
        const mean = influences.reduce((s, v) => s + v, 0) / influences.length;
        const std = Math.sqrt(influences.reduce((s, v) => s + (v - mean) ** 2, 0) / influences.length);
        document.getElementById('float-dynamic-range').innerHTML =
            `±${std.toFixed(3)}<span class="stat-unit">%</span>`;
    },

    // 浮精煤量输入框与"量数据（录入+输入）"面板双向同步（同粗精煤泥量卡片格式，可输入）
    syncAmountInput() {
        const input = document.getElementById('float-amount-input');
        const v = App.resolveAmount('floatAmount');
        // 外部修改时回填输入框（用户正在输入时不打断）
        if (input && v !== null && document.activeElement !== input && parseFloat(input.value) !== v) {
            input.value = v;
        }
        return v;
    },

    onAmountChange() {
        const input = document.getElementById('float-amount-input');
        const v = input ? parseFloat(input.value) : NaN;
        const val = isNaN(v) ? null : v;
        if (input) input.value = isNaN(v) ? '' : v;
        // 写入量数据统一数据源（mode=manual），自动联动刷新 总览/粗精/浮精
        App.setAmountInput('floatAmount', { mode: 'manual', manual: val });
        this.updateCards();
        App.showToast(`浮精煤量已设为 ${val === null ? '--' : val.toFixed(1)} t/h`, 'info');
    },

    initCharts() {
        // 联动趋势图
        const ctx1 = document.getElementById('chart-float-linkage').getContext('2d');
        this.linkageChart = new Chart(ctx1, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: '浮精灰分(%)',
                        data: [],
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6,182,212,0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 3,
                        borderWidth: 2
                    },
                    {
                        label: '总精煤灰分(%)',
                        data: [],
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59,130,246,0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 3,
                        borderWidth: 2
                    },
                    {
                        label: '浮精影响值(%)',
                        data: [],
                        borderColor: '#f59e0b',
                        tension: 0.3,
                        pointRadius: 3,
                        borderWidth: 2,
                        yAxisID: 'y1'
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#b0bdd0', font: { size: 12 } } },
                    tooltip: { backgroundColor: '#1e3355', titleColor: '#f0f4fa', bodyColor: '#b0bdd0' }
                },
                scales: {
                    x: { ticks: { color: '#7b8da6', maxTicksLimit: 12 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                    y: { position: 'left', ticks: { color: '#7b8da6' }, grid: { color: 'rgba(255,255,255,0.04)' },
                         title: { display: true, text: '灰分(%)', color: '#7b8da6' } },
                    y1: { position: 'right', ticks: { color: '#f59e0b' }, grid: { drawOnChartArea: false },
                          title: { display: true, text: '影响值(%)', color: '#f59e0b' } }
                }
            }
        });

        // 影响值分布柱状图
        const ctx2 = document.getElementById('chart-float-distribution').getContext('2d');
        this.distChart = new Chart(ctx2, {
            type: 'bar',
            data: {
                labels: [],
                datasets: [{
                    label: '浮精影响值(%)',
                    data: [],
                    backgroundColor: [],
                    borderRadius: 4,
                    maxBarThickness: 30
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#b0bdd0', font: { size: 11 } } },
                    tooltip: { backgroundColor: '#1e3355', titleColor: '#f0f4fa', bodyColor: '#b0bdd0' }
                },
                scales: {
                    x: { ticks: { color: '#7b8da6', maxTicksLimit: 12 }, grid: { display: false } },
                    y: { ticks: { color: '#7b8da6' }, grid: { color: 'rgba(255,255,255,0.04)' } }
                }
            }
        });

        this.updateCharts();
    },

    updateCharts() {
        if (!this.linkageChart || !this.distChart) return;
        // 按真实时间排序并按采样时间去重（同批数据跨系统重复导入时同一时刻只显示一次）；
        // 同时把每点时间戳交给图表，使 X 轴按真实时间间隔比例分布（与数据条数无关）
        const data = this._uniqueByTime(this._sortByTime(App.store.floatCoal));
        const heavyAmt = 250;
        const pad = n => String(n).padStart(2, '0');
        // 完整时间既用于悬停提示，也用于 X 轴定位（按真实时间间隔比例分布，与数据条数无关）
        const times = [];
        const labels = data.map(d => {
            const dt = new Date(d.timestamp);
            const t = dt.getTime();
            times.push(isFinite(t) ? t : null);
            return isFinite(t)
                ? `${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`
                : String(d.timestamp || '');
        });
        // X 轴刻度标签：不逐点标完整时间（密集段会叠成一团），只标合适的时间间隔
        const chartPx = Math.max(360, ((this.linkageChart && this.linkageChart.width) || 900) - 130);
        this.linkageChart.xTimes = times;
        this.linkageChart.xTickLabels = this._timeTickLabels(data, times, chartPx);
        const floatAsh = data.map(d => d.ash_content);
        const totalAsh = data.map(d => {
            const hAsh = App.getAshByTime(d.timestamp) ?? 8.50;   // 超窗记缺失回退默认
            return App.calcTotalAsh(hAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
        });
        const influenceValues = data.map(d => this._influenceOf(d, heavyAmt));

        this.linkageChart.data.labels = labels;
        this.linkageChart.data.datasets[0].data = floatAsh;
        this.linkageChart.data.datasets[1].data = totalAsh;
        this.linkageChart.data.datasets[2].data = influenceValues;
        this.linkageChart.update('none');

        // 分布图（最近12个时段）
        const recentLabels = labels.slice(-12);
        const recentTimes = times.slice(-12);
        const recentInfluence = influenceValues.slice(-12);
        const colors = recentInfluence.map(v =>
            v > 0.15 ? '#ef4444' : v > 0.05 ? '#f59e0b' : '#10b981'
        );
        // 柱状图标签原先无条件画在每根柱下方（无防重叠处理），所以同样只标合适的时间间隔。
        // 第 4 个参数 true = 柱状图按序号等距，像素位置须与 _drawBar 的柱心口径一致。
        const distPx = Math.max(200, ((this.distChart && this.distChart.width) || 380) - 120);
        this.distChart.xTickLabels = this._timeTickLabels(recentLabels, recentTimes, distPx, true);
        this.distChart.data.labels = recentLabels;
        this.distChart.data.datasets[0].data = recentInfluence;
        this.distChart.data.datasets[0].backgroundColor = colors;
        this.distChart.update('none');
    },

    // 浮精影响值：口径统一在 App.influenceOfFloat（图表/表格/详情/存量重算同一份公式）。
    _influenceOf(d, heavyAmt) {
        return App.influenceOfFloat(d);
    },

    renderTable() {
        const data = this._uniqueByTime(App.store.floatCoal);
        const tbody = document.getElementById('float-tbody');
        tbody.innerHTML = data.slice().reverse().slice(0, 30).map(d => {
            const pressTag = d.filter_press_running ?
                '<span class="annotate-tag press-filter">压滤机运行</span>' :
                '<span class="annotate-tag normal">正常</span>';
            const annTag = d.annotation && d.annotation !== '正常' ?
                `<span class="annotate-tag abnormal">${d.annotation}</span>` : '';
            const inf = this._influenceOf(d);   // 与图表同源（不再用导入时存下的旧口径值）
            const infClass = inf > 0.1 ? 'text-up' : inf < -0.1 ? 'text-down' : '';
            return `<tr>
                <td style="font-size:12px">${d.timestamp}</td>
                <td>${d.ash_content.toFixed(2)}</td>
                <td>${d.coal_amount.toFixed(1)}</td>
                <td>${pressTag}</td>
                <td class="${infClass}">${inf > 0 ? '+' : ''}${inf.toFixed(3)}</td>
                <td>${annTag || pressTag}</td>
                <td>
                    <button class="link-btn" onclick="FloatPage.showDetail(${d.id})">详情</button>
                    <button class="link-btn" onclick="FloatPage.annotateEntry(${d.id})">标注</button>
                </td>
            </tr>`;
        }).join('');
    },

    showDetail(id) {
        const d = App.store.floatCoal.find(x => x.id === id);
        if (!d) return;
        const html = `
            <div style="line-height:2">
                <p><strong>时间：</strong>${d.timestamp}</p>
                <p><strong>浮精灰分：</strong>${d.ash_content.toFixed(2)}%</p>
                <p><strong>浮精煤量：</strong>${d.coal_amount.toFixed(1)} t/h</p>
                <p><strong>压滤机状态：</strong>${d.filter_press_running ? '运行中' : '停止'}</p>
                <p><strong>影响值：</strong>${this._influenceOf(d).toFixed(3)}%</p>
                <p><strong>工况标注：</strong>${d.annotation || '无'}</p>
                <hr style="border-color:var(--border-color);margin:12px 0">
                <p><strong>计算方式：</strong></p>
                <p>总灰分 = (重介灰分×重介量 + 浮精灰分×浮精量) / (重介量 + 浮精量)</p>
                <p>影响值 = 含浮精总灰分 - 不含浮精总灰分（重介灰分动态获取）</p>
                <p><strong>数据来源：</strong>自动采集 / 手工录入</p>
                <p><strong>更新时间：</strong>${App.formatDate(new Date())}</p>
            </div>
        `;
        App.openModal('浮精数据详情', html, '<button class="btn" onclick="App.closeModal()">关闭</button>');
    },

    showAnnotateModal() {
        const data = this._uniqueByTime(App.store.floatCoal);
        const opts = data.slice().reverse().slice(0, 10).map(d =>
            `<option value="${d.id}">${d.timestamp} - 灰分:${d.ash_content.toFixed(2)}%</option>`
        ).join('');
        const html = `
            <div class="annotate-form">
                <div class="form-group">
                    <label>选择记录</label>
                    <select id="annotate-record-id">${opts}</select>
                </div>
                <div class="form-group">
                    <label>异常工况类型</label>
                    <select id="annotate-type">
                        <option value="压滤机卸料">压滤机卸料</option>
                        <option value="浮选药剂异常">浮选药剂异常</option>
                        <option value="入浮浓度波动">入浮浓度波动</option>
                        <option value="设备故障">设备故障</option>
                        <option value="其他">其他</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>备注</label>
                    <input type="text" id="annotate-remark" placeholder="可选填写备注">
                </div>
            </div>
        `;
        App.openModal('标注异常工况', html,
            '<button class="btn" onclick="App.closeModal()">取消</button>' +
            '<button class="btn btn-primary" onclick="FloatPage.submitAnnotation()">确认标注</button>'
        );
    },

    annotateEntry(id) {
        document.getElementById('annotate-record-id')?.remove();
        this.showAnnotateModal();
        // 预选该记录
        setTimeout(() => {
            const sel = document.getElementById('annotate-record-id');
            if (sel) sel.value = id;
        }, 50);
    },

    submitAnnotation() {
        const id = parseInt(document.getElementById('annotate-record-id')?.value);
        const type = document.getElementById('annotate-type')?.value;
        if (!id || !type) return;
        const d = App.store.floatCoal.find(x => x.id === id);
        if (d) {
            d.annotation = type;
            App.saveStore();
            this.renderTable();
            App.closeModal();
            App.showToast('工况标注已保存', 'success');
        }
    },

    showCardDetail(cardType) {
        const data = this._uniqueByTime(App.store.floatCoal);
        const heavyAmt = 250;
        const latest = data.length > 0 ? data[data.length - 1] : null;
        const heavyAsh = latest ? (App.getAshByTime(latest.timestamp) ?? 8.50) : 8.50;
        let html = '';

        if (cardType === 'ash') {
            const fAmtAsh = App.resolveAmount('floatAmount');
            html = `
                <div style="line-height:2.2;font-size:14px">
                    <h4 style="margin:0 0 12px;color:var(--accent-blue)">当前浮精灰分</h4>
                    <p><strong>一、数据来源</strong></p>
                    <p>最新一条浮精化验数据（直接取值）</p>
                    <p><strong>二、当前值</strong></p>
                    <table class="data-table" style="margin:4px 0 12px">
                        <tr><td>浮精灰分</td><td>${latest ? latest.ash_content.toFixed(2) + '%' : '暂无数据'}</td></tr>
                        <tr><td>浮精煤量(量数据同步)</td><td>${fAmtAsh == null ? '暂无数据' : fAmtAsh.toFixed(1) + ' t/h'}</td></tr>
                        <tr><td>压滤机状态</td><td>${latest ? (latest.filter_press_running ? '运行中' : '停止') : '--'}</td></tr>
                        <tr><td>采样时间</td><td>${latest ? latest.timestamp : '--'}</td></tr>
                    </table>
                </div>`;
        } else if (cardType === 'amount') {
            const fAmtCard = App.resolveAmount('floatAmount');
            const fAmtMode = (App.store.amountInputs && App.store.amountInputs.floatAmount)
                ? (App.store.amountInputs.floatAmount.mode || 'auto') : 'auto';
            html = `
                <div style="line-height:2.2;font-size:14px">
                    <h4 style="margin:0 0 12px;color:var(--accent-blue)">浮精煤量</h4>
                    <p><strong>一、数据来源</strong></p>
                    <p>${fAmtMode === 'manual' ? '量数据面板人工录入（与总览页同步）' : '最新一条浮精数据中的煤量字段（皮带秤读数，直接取值）'}</p>
                    <p><strong>二、当前值</strong></p>
                    <table class="data-table" style="margin:4px 0 12px">
                        <tr><td>浮精煤量</td><td>${fAmtCard == null ? '暂无数据' : fAmtCard.toFixed(1) + ' t/h'}</td></tr>
                        <tr><td>采样时间</td><td>${latest ? latest.timestamp : '--'}</td></tr>
                    </table>
                </div>`;
        } else if (cardType === 'influence') {
            const totalWithout = App.calcTotalAsh(heavyAsh, heavyAmt, 0, 0, 0, 0);
            const fAsh = latest ? latest.ash_content : 0;
            const fAmt = App.resolveAmount('floatAmount');   // 与量数据面板同步
            const fAmtVal = fAmt == null ? 0 : fAmt;
            const totalWith = App.calcTotalAsh(heavyAsh, heavyAmt, fAsh, fAmtVal, 0, 0);
            const influence = totalWith - totalWithout;
            html = `
                <div style="line-height:2.2;font-size:14px">
                    <h4 style="margin:0 0 12px;color:var(--accent-blue)">固定影响值</h4>
                    <p><strong>一、公式</strong></p>
                    <p style="color:var(--text-secondary)">影响值 = 含浮精总灰分 - 不含浮精总灰分</p>
                    <p><strong>二、基准总灰分（不含浮精）</strong></p>
                    <p style="padding-left:16px;color:var(--text-secondary)">基准灰分 = 重介灰分 = ${heavyAsh.toFixed(2)}%（从灰分密度数据动态获取）</p>
                    <p style="padding-left:16px">= <strong>${totalWithout.toFixed(4)}%</strong></p>
                    <p><strong>三、含浮精总灰分</strong></p>
                    <p style="padding-left:16px;color:var(--text-secondary)">总灰分 = (重介灰分×重介量 + 浮精灰分×浮精量) / (重介量 + 浮精量)</p>
                    <p style="padding-left:16px">= (${heavyAsh.toFixed(2)}×${heavyAmt} + ${fAsh.toFixed(2)}×${fAmtVal.toFixed(1)}) / (${heavyAmt} + ${fAmtVal.toFixed(1)})</p>
                    <p style="padding-left:16px">= <strong>${totalWith.toFixed(4)}%</strong></p>
                    <p><strong>四、影响值</strong></p>
                    <p>${totalWith.toFixed(4)} - ${totalWithout.toFixed(4)} = <strong>${influence > 0 ? '+' : ''}${influence.toFixed(4)}%</strong></p>
                    <table class="data-table" style="margin:8px 0">
                        <tr><td>重介灰分</td><td>${heavyAsh.toFixed(2)}%（动态）</td><td>重介量</td><td>${heavyAmt} t/h</td></tr>
                        <tr><td>浮精灰分</td><td>${fAsh.toFixed(2)}%</td><td>浮精量(量数据同步)</td><td>${fAmtVal.toFixed(1)} t/h</td></tr>
                    </table>
                </div>`;
        } else if (cardType === 'range') {
            if (data.length === 0) {
                html = '<div style="line-height:2;font-size:14px"><p>暂无数据，无法计算动态波动范围</p></div>';
            } else {
                const influences = data.map(d => {
                    const hAsh = App.getAshByTime(d.timestamp) ?? 8.50;   // 超窗记缺失回退默认
                    const base = App.calcTotalAsh(hAsh, heavyAmt, 0, 0, 0, 0);
                    const withF = App.calcTotalAsh(hAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
                    return withF - base;
                });
                const mean = influences.reduce((s, v) => s + v, 0) / influences.length;
                const variance = influences.reduce((s, v) => s + (v - mean) ** 2, 0) / influences.length;
                const std = Math.sqrt(variance);
                html = `
                    <div style="line-height:2.2;font-size:14px">
                        <h4 style="margin:0 0 12px;color:var(--accent-blue)">动态波动范围（标准差）</h4>
                        <p><strong>一、公式</strong></p>
                        <p style="color:var(--text-secondary)">σ = sqrt( Σ(xi - x̄)² / n )</p>
                        <p><strong>二、计算过程</strong></p>
                        <p>1. 计算每条数据的影响值（含浮精总灰分 - 基准总灰分）</p>
                        <p>2. 求影响值均值 x̄ = <strong>${mean.toFixed(4)}%</strong>（共${influences.length}条）</p>
                        <p>3. 求方差 σ² = Σ(xi - x̄)² / n = <strong>${variance.toFixed(6)}</strong></p>
                        <p>4. 求标准差 σ = sqrt(${variance.toFixed(6)}) = <strong>±${std.toFixed(3)}%</strong></p>
                        <p><strong>三、含义</strong></p>
                        <p style="color:var(--text-secondary)">表示浮精对总灰分影响值的波动程度，值越小说明越稳定</p>
                    </div>`;
            }
        }

        App.openModal('计算详情', html, '<button class="btn" onclick="App.closeModal()">关闭</button>');
    }
};
