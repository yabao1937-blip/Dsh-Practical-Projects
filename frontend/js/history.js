/* ========================================
   页面六：历史数据查询
   - 三表历史记录 + 模型训练历史查询
   ======================================== */

const HistoryPage = {
    init() {
        this.setDefaultDates();
    },

    setDefaultDates() {
        const now = new Date();
        const start = new Date(now.getTime() - 24 * 3600000);
        const pad = n => String(n).padStart(2, '0');
        const fmt = d => `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
        const elStart = document.getElementById('history-start-time');
        const elEnd = document.getElementById('history-end-time');
        if (elStart) elStart.value = fmt(start);
        if (elEnd) elEnd.value = fmt(now);
    },

    query() {
        const startTime = document.getElementById('history-start-time')?.value;
        const endTime = document.getElementById('history-end-time')?.value;
        const systemFilter = document.getElementById('history-system-filter')?.value || 'all';
        const categoryFilter = document.getElementById('history-category-filter')?.value || 'all';

        if (!startTime || !endTime) {
            App.showToast('请选择时间范围', 'warning');
            return;
        }

        const start = new Date(startTime).getTime();
        const end = new Date(endTime).getTime();

        if (start > end) {
            App.showToast('开始时间不能晚于结束时间', 'warning');
            return;
        }

        // Collect data based on category filter
        const results = { ash: [], density: [], coal: [], level: [], influence: [] };

        // 灰分/密度数据 → calcLogs
        if (categoryFilter === 'all' || categoryFilter === 'ash' || categoryFilter === 'density') {
            const logs = App.store.calcLogs.filter(l => {
                const t = new Date(l.timestamp).getTime();
                return l.calc_type === 'ash_density' && t >= start && t <= end &&
                    (systemFilter === 'all' || l.system === systemFilter);
            });
            logs.forEach(l => {
                let input = {};
                try {
                    input = typeof l.input_json === 'string' ? JSON.parse(l.input_json) : (l.input_json || {});
                } catch (e) {}

                if (categoryFilter === 'all' || categoryFilter === 'ash') {
                    if (input.ash_content !== undefined) {
                        results.ash.push({
                            timestamp: l.timestamp,
                            system: l.system || '--',
                            value: input.ash_content,
                            unit: '%'
                        });
                    }
                }
                if (categoryFilter === 'all' || categoryFilter === 'density') {
                    if (input.density !== undefined) {
                        results.density.push({
                            timestamp: l.timestamp,
                            system: l.system || '--',
                            value: input.density,
                            unit: 'g/cm³'
                        });
                    }
                }
            });
        }

        // 煤量数据 → coarseCoal / floatCoal
        if (categoryFilter === 'all' || categoryFilter === 'coal') {
            const filterData = (arr, source) => arr.filter(d => {
                const t = new Date(d.timestamp).getTime();
                return t >= start && t <= end &&
                    (systemFilter === 'all' || d.system === systemFilter);
            }).map(d => ({
                timestamp: d.timestamp,
                system: d.system || '--',
                coal_amount: d.coal_amount,
                source: source
            }));
            results.coal.push(...filterData(App.store.coarseCoal, '粗精煤泥'));
            results.coal.push(...filterData(App.store.floatCoal, '浮精'));
        }

        // 液位数据 → magneticTail
        if (categoryFilter === 'all' || categoryFilter === 'level') {
            const mt = App.store.magneticTail.filter(d => {
                const t = new Date(d.timestamp).getTime();
                return t >= start && t <= end &&
                    (systemFilter === 'all' || d.system === systemFilter);
            }).map(d => ({
                timestamp: d.timestamp,
                system: d.system || '--',
                level: d.level,
                ash_content: d.ash_content
            }));
            results.level.push(...mt);
        }

        // 影响值数据 → coarseCoal / floatCoal
        if (categoryFilter === 'all' || categoryFilter === 'influence') {
            const heavyAmt = 250;
            App.store.coarseCoal.forEach(d => {
                const t = new Date(d.timestamp).getTime();
                if (t >= start && t <= end && (systemFilter === 'all' || d.system === systemFilter)) {
                    const heavyAsh = App.getAshByTime(d.timestamp);
                    const baseTotal = App.calcTotalAsh(heavyAsh, heavyAmt, 0, 0, 0, 0);
                    const level = d.level !== undefined && d.level > 0 ? d.level :
                        (App.store.magneticTail.find(m => m.system === d.system && m.timestamp === d.timestamp)?.level || 0);
                    const predAsh = App.predictAsh(level);
                    const totalWith = App.calcTotalAsh(heavyAsh, heavyAmt, 0, 0, predAsh, d.coal_amount);
                    const inf = totalWith - baseTotal;
                    results.influence.push({
                        timestamp: d.timestamp,
                        system: d.system || '--',
                        source: '粗精煤泥',
                        influence: inf.toFixed(4),
                        ash: d.ash_content.toFixed(2)
                    });
                }
            });
            App.store.floatCoal.forEach(d => {
                const t = new Date(d.timestamp).getTime();
                if (t >= start && t <= end && (systemFilter === 'all' || d.system === systemFilter)) {
                    const heavyAsh = App.getAshByTime(d.timestamp);
                    const baseTotal = App.calcTotalAsh(heavyAsh, heavyAmt, 0, 0, 0, 0);
                    const totalWith = App.calcTotalAsh(heavyAsh, heavyAmt, d.ash_content, d.coal_amount, 0, 0);
                    const inf = totalWith - baseTotal;
                    results.influence.push({
                        timestamp: d.timestamp,
                        system: d.system || '--',
                        source: '浮精',
                        influence: inf.toFixed(4),
                        ash: d.ash_content.toFixed(2)
                    });
                }
            });
        }

        this.renderResults(results, categoryFilter);
        App.showToast('查询完成', 'success');
    },

    renderResults(results, categoryFilter) {
        const container = document.getElementById('history-results');
        if (!container) return;

        let html = '';

        // 灰分组
        if ((categoryFilter === 'all' || categoryFilter === 'ash') && results.ash.length > 0) {
            html += this._renderGroup('灰分数据', results.ash.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)),
                ['时间', '系统', '灰分(%)']);
        }

        // 密度组
        if ((categoryFilter === 'all' || categoryFilter === 'density') && results.density.length > 0) {
            html += this._renderGroup('密度数据', results.density.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp)),
                ['时间', '系统', '密度(g/cm³)']);
        }

        // 煤量组
        if ((categoryFilter === 'all' || categoryFilter === 'coal') && results.coal.length > 0) {
            const sorted = results.coal.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
            html += `
                <div class="panel" style="margin-bottom:12px">
                    <div class="panel-header"><h3>煤量数据</h3>
                        <span class="panel-header-info">共${sorted.length}条</span>
                    </div>
                    <div class="panel-body">
                        <div class="table-scroll" style="max-height:200px">
                            <table class="data-table">
                                <thead><tr><th>时间</th><th>系统</th><th>来源</th><th>煤量(t/h)</th></tr></thead>
                                <tbody>${sorted.map(d => `<tr><td style="font-size:12px">${d.timestamp}</td><td>${d.system}</td><td>${d.source}</td><td>${d.coal_amount?.toFixed(1) || '--'}</td></tr>`).join('')}</tbody>
                            </table>
                        </div>
                    </div>
                </div>`;
        }

        // 液位组
        if ((categoryFilter === 'all' || categoryFilter === 'level') && results.level.length > 0) {
            const sorted = results.level.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
            html += `
                <div class="panel" style="margin-bottom:12px">
                    <div class="panel-header"><h3>精磁尾液位数据</h3>
                        <span class="panel-header-info">共${sorted.length}条</span>
                    </div>
                    <div class="panel-body">
                        <div class="table-scroll" style="max-height:200px">
                            <table class="data-table">
                                <thead><tr><th>时间</th><th>系统</th><th>液位(%)</th><th>灰分(%)</th></tr></thead>
                                <tbody>${sorted.map(d => `<tr><td style="font-size:12px">${d.timestamp}</td><td>${d.system}</td><td>${d.level?.toFixed(2) || '--'}</td><td>${d.ash_content?.toFixed(2) || '--'}</td></tr>`).join('')}</tbody>
                            </table>
                        </div>
                    </div>
                </div>`;
        }

        // 影响值组
        if ((categoryFilter === 'all' || categoryFilter === 'influence') && results.influence.length > 0) {
            const sorted = results.influence.sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
            html += `
                <div class="panel" style="margin-bottom:12px">
                    <div class="panel-header"><h3>影响值数据</h3>
                        <span class="panel-header-info">共${sorted.length}条</span>
                    </div>
                    <div class="panel-body">
                        <div class="table-scroll" style="max-height:200px">
                            <table class="data-table">
                                <thead><tr><th>时间</th><th>系统</th><th>来源</th><th>影响值(%)</th><th>灰分(%)</th></tr></thead>
                                <tbody>${sorted.map(d => `<tr><td style="font-size:12px">${d.timestamp}</td><td>${d.system}</td><td>${d.source}</td><td>${d.influence > 0 ? '+' : ''}${d.influence}</td><td>${d.ash}</td></tr>`).join('')}</tbody>
                            </table>
                        </div>
                    </div>
                </div>`;
        }

        if (!html) {
            html = '<div class="panel"><div class="panel-body" style="text-align:center;color:var(--text-muted);padding:40px">未找到符合条件的数据</div></div>';
        }

        // Summary stats
        const totalCount = results.ash.length + results.density.length + results.coal.length + results.level.length + results.influence.length;
        const summaryEl = document.getElementById('history-summary');
        if (summaryEl) {
            summaryEl.innerHTML = `
                <div class="import-preview-summary">
                    <div class="summary-item"><span class="summary-label">查询结果：</span><span class="summary-val">${totalCount}条</span></div>
                    <div class="summary-item"><span class="summary-label">灰分：</span><span class="summary-val">${results.ash.length}条</span></div>
                    <div class="summary-item"><span class="summary-label">密度：</span><span class="summary-val">${results.density.length}条</span></div>
                    <div class="summary-item"><span class="summary-label">煤量：</span><span class="summary-val">${results.coal.length}条</span></div>
                    <div class="summary-item"><span class="summary-label">液位：</span><span class="summary-val">${results.level.length}条</span></div>
                    <div class="summary-item"><span class="summary-label">影响值：</span><span class="summary-val">${results.influence.length}条</span></div>
                </div>`;
        }

        container.innerHTML = html;
    },

    _renderGroup(title, data, headers) {
        return `
            <div class="panel" style="margin-bottom:12px">
                <div class="panel-header"><h3>${title}</h3>
                    <span class="panel-header-info">共${data.length}条</span>
                </div>
                <div class="panel-body">
                    <div class="table-scroll" style="max-height:200px">
                        <table class="data-table">
                            <thead><tr>${headers.map(h => `<th>${h}</th>`).join('')}</tr></thead>
                            <tbody>${data.map(d => {
                                const isAsh = headers.includes('灰分(%)');
                                const isDensity = headers.includes('密度(g/cm³)');
                                return `<tr>
                                    <td style="font-size:12px">${d.timestamp}</td>
                                    <td>${d.system}</td>
                                    <td>${isAsh ? (d.value?.toFixed(2) || '--') : isDensity ? (d.value?.toFixed(3) || '--') : '--'}</td>
                                </tr>`;
                            }).join('')}</tbody>
                        </table>
                    </div>
                </div>
            </div>`;
    },

    exportResults() {
        const startTime = document.getElementById('history-start-time')?.value;
        const endTime = document.getElementById('history-end-time')?.value;
        if (!startTime || !endTime) {
            App.showToast('请先查询数据', 'warning');
            return;
        }
        // Get visible data from tables
        const tables = document.querySelectorAll('#history-results .data-table');
        if (tables.length === 0) {
            App.showToast('无数据可导出', 'warning');
            return;
        }

        let allRows = [];
        tables.forEach(table => {
            const caption = table.closest('.panel')?.querySelector('h3')?.textContent || '数据';
            allRows.push([`=== ${caption} ===`]);
            const rows = table.querySelectorAll('tr');
            rows.forEach((row, idx) => {
                const cells = row.querySelectorAll('th, td');
                const rowData = Array.from(cells).map(c => c.textContent.trim());
                if (idx === 0) {
                    allRows.push(rowData);
                } else {
                    allRows.push(rowData);
                }
            });
            allRows.push([]);
        });

        const ws = XLSX.utils.aoa_to_sheet(allRows);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '历史数据');
        const now = new Date();
        const pad = n => String(n).padStart(2, '0');
        const ts = `${now.getFullYear()}${pad(now.getMonth()+1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}`;
        XLSX.writeFile(wb, `历史数据查询_${ts}.csv`);
        App.showToast('数据已导出', 'success');
    }
};
