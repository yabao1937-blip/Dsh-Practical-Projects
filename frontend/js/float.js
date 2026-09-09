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
        const times = [];
        const labels = data.map(d => {
            const dt = new Date(d.timestamp);
            const t = dt.getTime();
            times.push(isFinite(t) ? t : null);
            return isFinite(t)
                ? `${pad(dt.getMonth()+1)}-${pad(dt.getDate())} ${pad(dt.getHours())}:${pad(dt.getMinutes())}`
                : String(d.timestamp || '');
        });
        this.linkageChart.xTimes = times;
        this.distChart.xTimes = times;
        const floatAsh = data.map(d => d.ash_content);
        const totalAsh = data.map(d => {
            const hAsh = App.getAshByTime(d.timestamp) ?? 8.50;   // 超窗记缺失回退默认
            return App.calcTotalAsh(hAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
        });
        const influenceValues = data.map(d => {
            const hAsh = App.getAshByTime(d.timestamp) ?? 8.50;   // 超窗记缺失回退默认
            const base = App.calcTotalAsh(hAsh, heavyAmt, 0, 0, 0, 0);
            const withF = App.calcTotalAsh(hAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
            return +(withF - base).toFixed(3);
        });

        this.linkageChart.data.labels = labels;
        this.linkageChart.data.datasets[0].data = floatAsh;
        this.linkageChart.data.datasets[1].data = totalAsh;
        this.linkageChart.data.datasets[2].data = influenceValues;
        this.linkageChart.update('none');

        // 分布图（最近12个时段）
        const recentLabels = labels.slice(-12);
        const recentInfluence = influenceValues.slice(-12);
        const colors = recentInfluence.map(v =>
            v > 0.15 ? '#ef4444' : v > 0.05 ? '#f59e0b' : '#10b981'
        );
        this.distChart.data.labels = recentLabels;
        this.distChart.data.datasets[0].data = recentInfluence;
        this.distChart.data.datasets[0].backgroundColor = colors;
        this.distChart.update('none');
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
            const infClass = d.influence_value > 0.1 ? 'text-up' : d.influence_value < -0.1 ? 'text-down' : '';
            return `<tr>
                <td style="font-size:12px">${d.timestamp}</td>
                <td>${d.ash_content.toFixed(2)}</td>
                <td>${d.coal_amount.toFixed(1)}</td>
                <td>${pressTag}</td>
                <td class="${infClass}">${d.influence_value > 0 ? '+' : ''}${d.influence_value.toFixed(3)}</td>
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
                <p><strong>影响值：</strong>${d.influence_value.toFixed(3)}%</p>
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
