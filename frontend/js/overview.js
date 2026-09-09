/* ========================================
   页面一：密度推荐总览
   - 重介灰分/实测密度/总精煤灰分卡片 + 建议密度指示
   - 调用 App 取值链与 computeDensityGuidance 渲染
   ======================================== */

const OverviewPage = {
    chart: null,
    _adjustedAsh: {},  // #11 存储手动调整值: { '401_target': 8.50, '401_actual': 9.20, ... }

    init() {
        // 密度指导参数回显（钳制幅度/达标容差，持久化值）
        const g0 = App.getDensityGuide();
        const msEl = document.getElementById('density-maxstep');
        if (msEl) msEl.value = g0.maxStep;
        const tolEl = document.getElementById('ash-target-tol');
        if (tolEl) tolEl.value = (App.store.ashTargetTol != null) ? App.store.ashTargetTol : 0.3;
        // 双击恢复：实际灰分→清空在线仪表总灰分手动值(回公式计算)；目标灰分→恢复默认8.50
        const actualEl0 = document.getElementById('actual-ash-total');
        if (actualEl0) {
            actualEl0.title = '双击清空手动值（重介版：恢复默认8.50；总灰分版：恢复公式计算）';
            actualEl0.style.cursor = 'pointer';
            actualEl0.addEventListener('dblclick', (e) => {
                e.stopPropagation();
                if (App.store.guideScheme === 'heavy') {
                    App.setHeavyAshInput({ manual: null });
                    App.showToast('重介精煤灰分手动值已清空，恢复默认8.50', 'info');
                } else {
                    App.setAshInput('totalAsh', { manual: null });
                    App.showToast('总精煤灰分手动值已清空，恢复公式计算', 'info');
                }
            });
        }
        const targetEl0 = document.getElementById('target-ash-total');
        if (targetEl0) {
            targetEl0.title = '双击恢复默认8.50%';
            targetEl0.style.cursor = 'pointer';
            targetEl0.addEventListener('dblclick', (e) => {
                e.stopPropagation();
                App.store.ashTarget = 8.50;
                App.saveStore();
                App._onExternalInput();
                this.updateCards();
                App.showToast('目标灰分已恢复默认8.50%', 'info');
            });
        }
        this.initTrendChart();
        this.updateCards();
        this.refreshTrend();
    },

    refresh() {
        this.updateCards();
        if (this.chart) this.refreshTrend();
    },

    // #11 箭头按钮调整灰分值：与在线仪表手动调节同一"手动层"，相互覆盖、后写生效
    adjustAsh(sysId, type, delta) {
        if (sysId === 'total' && type === 'target') {
            const cur = (App.store.ashTarget != null) ? App.store.ashTarget : 8.50;
            App.store.ashTarget = +(cur + delta).toFixed(2);
            App.saveStore();
            App._onExternalInput();   // 目标变化 → 自动执行重算目标密度
        } else if (sysId === 'total' && type === 'actual') {
            // 按当前版本写入对应手动层：总灰分版→在线仪表总灰分；重介版→重介精煤灰分
            if (App.store.guideScheme === 'heavy') {
                const cur = App.getHeavyAsh();
                const base = (typeof cur === 'number' && isFinite(cur)) ? cur : 8.50;
                App.setHeavyAshInput({ manual: +(base + delta).toFixed(2) });
            } else {
                const cur = App.resolveTotalAsh();
                const base = (typeof cur === 'number' && isFinite(cur)) ? cur : 8.50;
                App.setAshInput('totalAsh', { manual: +(base + delta).toFixed(2) });   // 写同一手动层，与在线仪表互相覆盖
            }
        }
        // 重新计算偏差和推荐
        this.updateCards();
    },

    // #12 置信度统一计算：卡片角标与详情弹窗共用，保证一致
    // 模型口径：密度回归模型 + 粗精煤泥生产模型（取最优 R²）
    computeConfidence(target, actual) {
        const models = App.store.regressionModels || [];
        const cm = App.store.coarseModel;
        const hasModel = models.length > 0 || !!cm;
        let bestR2 = 0;
        if (models.length > 0) bestR2 = Math.max(...models.map(m => m.r_squared || 0));
        if (cm && cm[cm.production]) {
            const mm = cm[cm.production].metrics;
            const r2 = (mm && typeof mm.r2 === 'number') ? mm.r2
                     : (typeof cm[cm.production].r2 === 'number' ? cm[cm.production].r2 : 0);
            bestR2 = Math.max(bestR2, r2);
        }
        const absDev = (actual == null) ? Infinity : Math.abs(actual - target);
        const hasCoarseData = (App.store.coarseCoal || []).length > 0;
        const hasFloatData = (App.store.floatCoal || []).length > 0;
        const hasMagnetic = (App.store.magneticTail || []).length > 0;
        if (hasModel && bestR2 > 0.8 && absDev <= 0.15 && hasCoarseData && hasFloatData && hasMagnetic) {
            return { level: '高', className: 'high', color: 'var(--accent-green)',
                     reason: '模型拟合度高(R²>0.8)、偏差小、数据源完整' };
        }
        if (hasModel && bestR2 > 0.5 && hasCoarseData) {
            return { level: '中', className: 'medium', color: 'var(--accent-orange)',
                     reason: '模型存在但拟合度一般或部分数据源缺失' };
        }
        return { level: '低', className: 'low', color: 'var(--accent-red)',
                 reason: '模型未训练或拟合度差，建议补充数据后重新训练' };
    },

    updateCards() {
        const now = App.formatDate(new Date());
        const defaultTarget = (App.store.ashTarget != null) ? App.store.ashTarget : 8.50;

        // 从 store 计算各系统的实际灰分（最新数据）
        const coarseData = App.store.coarseCoal;
        const floatData = App.store.floatCoal;
        const hasCoarse = coarseData.length > 0;
        const hasFloat = floatData.length > 0;

        // 取最新粗精煤泥和浮精数据
        const latestCoarse = hasCoarse ? coarseData[coarseData.length - 1] : null;
        const latestFloat = hasFloat ? floatData[floatData.length - 1] : null;

        // 计算总灰分（粗精煤泥灰分采用多因素模型"前馈预测"值，先于化验值给出，实现密度精准预判）
        // 三个量走"录入+输入"：由 resolveAmount 解析（录入/自动/计算）
        const heavyAsh = App.getHeavyAsh();        // 任务三：反推重介精煤灰分（默认8.50兜底）
        const heavyAmt = App.resolveAmount('denseAmount');        // 重介精煤量（录入或计算）
        const coarseAmt = App.resolveAmount('coarseAmount');      // 粗精煤泥量（录入）
        // 粗精灰分/浮精灰分：与在线仪表统一解析（时间最新 + 手动覆盖），全链路相通
        const coarseAsh = App.resolveCoarseAsh() ?? 0;
        const floatAsh = App.resolveFloatAsh() ?? 0;
        const floatAmt = App.resolveAmount('floatAmount');        // 浮精量（录入或自动取表2）

        // 实际总灰分 = 在线仪表"总精煤灰分"同源值（手动化验 > 公式计算 > 录入 > 默认）
        const computedActual = App.resolveTotalAsh();

        const systems = [
            { id: 'total', belt: '501+502', density: App.resolveDensity() },
        ];

        systems.forEach(sys => {
            // 目标=期望总灰分（store.ashTarget）；实际=在线仪表总精煤灰分（同一手动层，箭头与在线仪表互相覆盖）
            const target = defaultTarget;
            const actual = computedActual;

            // 建议密度：按当前版本（总灰分版/重介精煤灰分版）计算
            const guide = App.computeDensityGuidance(target);
            const heavyMode = (guide.scheme === 'heavy') && guide.valid && guide.targetHeavy != null;
            const tol = (App.store.ashTargetTol != null) ? App.store.ashTargetTol : 0.1;

            // 版本口径统一：重介版卡片整张切换为重介口径（目标重介灰分/实测重介灰分/重介偏差），保证 实际−目标=偏差
            const targetShow = heavyMode ? guide.targetHeavy : target;
            const actualShow = heavyMode ? guide.heavyAsh : actual;
            const dev = heavyMode ? guide.deltaAHeavy : (guide.valid ? guide.deltaA : (actual !== null ? +(actual - target).toFixed(2) : null));
            const tolShow = heavyMode ? +(tol * guide.totalAmt / guide.heavyAmt).toFixed(3) : tol;
            const hasData = heavyMode ? true : (actual !== null);

            const tLabelEl = document.getElementById(`target-ash-label-${sys.id}`);
            if (tLabelEl) tLabelEl.textContent = heavyMode ? '目标重介灰分' : '目标灰分';
            const aLabelEl = document.getElementById(`actual-ash-label-${sys.id}`);
            if (aLabelEl) aLabelEl.textContent = heavyMode ? '实测重介灰分' : '实际灰分';

            document.getElementById(`density-${sys.id}`).textContent = guide.rhoNew.toFixed(3);
            const subEl = document.getElementById(`density-sub-${sys.id}`);
            if (subEl) {
                subEl.textContent = guide.valid
                    ? `密度计 ${guide.rhoCur.toFixed(3)} · 目标密度 ${guide.rhoNew.toFixed(3)} · ${guide.direction === 'down' ? '降密' : guide.direction === 'up' ? '提密' : '达标保持'}`
                    : `密度计 ${guide.rhoCur.toFixed(3)}`;
            }

            // 置信度与详情弹窗共用同一计算口径
            const conf = this.computeConfidence(target, actual);
            document.getElementById(`conf-${sys.id}`).textContent = `置信度：${conf.level}`;
            document.getElementById(`conf-${sys.id}`).className = `confidence-tag ${conf.className}`;
            // 版本标签（置信度旁）
            const schemeEl = document.getElementById(`scheme-${sys.id}`);
            if (schemeEl) {
                schemeEl.textContent = guide.scheme === 'heavy' ? '重介精煤版' : '总灰分版';
                schemeEl.title = guide.scheme === 'heavy' ? '当前：重介精煤灰分版（偏差=实测重介灰分−目标重介灰分）' : '当前：总灰分版（偏差=实际总灰分−目标总灰分）';
            }
            document.getElementById(`target-ash-${sys.id}`).textContent = (heavyMode ? targetShow : target).toFixed(2) + '%';
            const actualEl = document.getElementById(`actual-ash-${sys.id}`);
            if (actualEl) {
                actualEl.textContent = hasData ? actualShow.toFixed(2) + '%' : '--';
                actualEl.style.color = '';
                actualEl.style.textDecoration = '';
                actualEl.title = heavyMode ? '重介精煤灰分实测值（采样/手写，箭头调整）' : '与在线仪表"总精煤灰分"同源（箭头与在线仪表手动互相覆盖）';
            }
            const devEl = document.getElementById(`deviation-${sys.id}`);
            if (hasData) {
                devEl.textContent = (dev > 0 ? '+' : '') + dev.toFixed(2) + '%';
                devEl.className = 'ash-val' + (Math.abs(dev) > (heavyMode ? tolShow : 0.1) ? ' warn' : '');
            } else {
                devEl.textContent = '--';
                devEl.className = 'ash-val';
            }

            // 方向指示
            let direction = 'stable', dirText = '保持稳定', effect = '暂无数据';
            if (hasData) {
                if (Math.abs(dev) <= tolShow) {
                    direction = 'stable'; dirText = '保持稳定';
                    effect = '当前密度合适，无需调整';
                } else if (dev > 0) {
                    // 灰分偏高 → 下调密度（分选密度↓ → 精煤灰分↓）
                    direction = 'down'; dirText = '建议下调';
                    effect = heavyMode ? '重介灰分偏高，适度降密使偏差回归目标' : '适度降密可降低灰分，使偏差回归目标范围';
                } else {
                    // 灰分偏低 → 上调密度（分选密度↑ → 精煤灰分↑、产率↑）
                    direction = 'up'; dirText = '建议上调';
                    effect = heavyMode ? '重介灰分偏低，适度提密提升回收率' : '适度提密可提升回收率，灰分回归目标值';
                }
            }
            document.getElementById(`effect-${sys.id}`).textContent = effect;
            // 达标提示（当前版本口径）
            const effectEl = document.getElementById(`effect-${sys.id}`);
            if (effectEl) {
                if (hasData && Math.abs(dev) <= tolShow) {
                    effectEl.innerHTML = heavyMode
                        ? `<span style="color:var(--accent-green)">✓ 已达标：实测重介灰分 ${actualShow.toFixed(2)}% 与目标重介灰分 ${targetShow.toFixed(2)}% 偏差 ${dev > 0 ? '+' : ''}${dev.toFixed(2)}% ≤ ±${tolShow}%（等效总灰分容差±${tol}%）</span>`
                        : `<span style="color:var(--accent-green)">✓ 已达标：实际总灰分 ${actual.toFixed(2)}% 与期望 ${target.toFixed(2)}% 偏差 ${dev > 0 ? '+' : ''}${dev.toFixed(2)}% ≤ ±${tol}%</span>`;
                } else {
                    effectEl.textContent = effect;
                }
            }
            document.getElementById(`source-${sys.id}`).textContent = hasData ? (heavyMode ? '重介灰分实测值' : '在线仪表(总精煤灰分)') : '--';
            document.getElementById(`update-time-${sys.id}`).textContent = hasData ? now : '--';

            const dirEl = document.getElementById(`direction-${sys.id}`);
            const arrow = direction === 'up' ? '&#9650;' : direction === 'down' ? '&#9660;' : '&#9644;';
            dirEl.innerHTML = `<span class="direction-arrow ${direction}">${arrow}</span><span class="direction-text">${dirText}</span>`;
        });

        // 状态汇总
        const models = App.store.regressionModels;
        const lastModel = models.length > 0 ? models[0].created_at : '--';
        document.getElementById('status-last-train').textContent = lastModel;

        // 模型状态
        const modelStatusEl = document.getElementById('status-model-state');
        if (modelStatusEl) {
            if (models.length > 0) {
                const best = models.reduce((a, b) => a.r_squared > b.r_squared ? a : b);
                modelStatusEl.textContent = `已训练(${models.length}个, R²=${best.r_squared.toFixed(3)})`;
                modelStatusEl.className = 'status-val good';
            } else {
                modelStatusEl.textContent = '未训练';
                modelStatusEl.className = 'status-val warn';
            }
        }

        // 数据记录总数
        const integrityEl = document.getElementById('status-data-integrity');
        if (integrityEl) {
            const total = App.store.magneticTail.length + App.store.coarseCoal.length +
                          App.store.floatCoal.length + App.store.manualEntries.length;
            integrityEl.textContent = total > 0 ? `共${total}条记录` : '暂无数据';
            integrityEl.className = 'status-val ' + (total > 0 ? 'good' : 'warn');
        }

        document.getElementById('status-manual-count').textContent = App.store.manualEntries.length + '条';
    },

    initTrendChart() {
        const ctx = document.getElementById('chart-overview-trend').getContext('2d');
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    { label: '合并系统', data: [], borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)', tension: 0.3, pointRadius: 3, borderWidth: 2, fill: false },
                    { label: '目标线(8.50%)', data: [], borderColor: '#8b5cf6', borderDash: [5, 5], pointRadius: 0, borderWidth: 1.5, fill: false }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#b0bdd0', font: { size: 12 } } },
                    tooltip: { backgroundColor: '#1e3355', titleColor: '#f0f4fa', bodyColor: '#b0bdd0', borderColor: '#2a4470', borderWidth: 1 }
                },
                scales: {
                    x: { ticks: { color: '#7b8da6', maxTicksLimit: 12 }, grid: { color: 'rgba(255,255,255,0.04)' } },
                    y: { ticks: { color: '#7b8da6' }, grid: { color: 'rgba(255,255,255,0.04)' },
                         title: { display: true, text: '灰分(%)', color: '#7b8da6' } }
                }
            }
        });
    },

    refreshTrend() {
        if (!this.chart) return;

        const systems = ['total'];
        // #13 数据源改为 calcLogs（灰分/密度数据）
        const calcLogs = App.store.calcLogs.filter(l => l.calc_type === 'ash_density');

        // 解析每条记录的 system, ash_content, timestamp
        const parsed = [];
        calcLogs.forEach(l => {
            let ash = null;
            try {
                const input = typeof l.input_json === 'string' ? JSON.parse(l.input_json) : l.input_json;
                ash = input.ash_content;
            } catch (e) {}
            if (ash === null || ash === undefined) return;
            parsed.push({ system: 'total', ash_content: ash, timestamp: l.timestamp });
        });

        // 按系统筛选数据
        const systemData = {};
        systems.forEach(sys => {
            systemData[sys] = parsed.filter(d => d.system === sys)
                .sort((a, b) => new Date(a.timestamp) - new Date(b.timestamp));
        });

        const hasData = systems.some(sys => systemData[sys].length > 0);
        if (!hasData) {
            this.chart.data.labels = [];
            this.chart.data.datasets.forEach(ds => ds.data = []);
            this.chart.update();
            return;
        }

        // 收集所有时间戳并排序
        const allTimestamps = new Set();
        systems.forEach(sys => {
            systemData[sys].forEach(d => allTimestamps.add(d.timestamp));
        });
        const sortedTimes = Array.from(allTimestamps).sort();

        // 用 Map 加速查找：key = "system|timestamp"
        const lookup = new Map();
        parsed.forEach(d => {
            lookup.set(`${d.system}|${d.timestamp}`, d.ash_content);
        });

        const labels = [];
        const sysArrays = systems.map(() => []);
        const targetArr = [];

        sortedTimes.forEach(ts => {
            const dt = new Date(ts);
            labels.push(App.formatShortDate(dt));

            systems.forEach((sys, i) => {
                const val = lookup.get(`${sys}|${ts}`);
                sysArrays[i].push(val !== undefined ? val : null);
            });

            targetArr.push(8.50);
        });

        this.chart.data.labels = labels;
        systems.forEach((_, i) => {
            this.chart.data.datasets[i].data = sysArrays[i];
        });
        this.chart.data.datasets[1].data = targetArr;
        this.chart.update();
    },

    showCardDetail(sysId) {
        App.backCalcHeavyAsh();   // 反推重介灰分仅展示用（不参与密度建议）
        const defaultTarget = (App.store.ashTarget != null) ? App.store.ashTarget : 8.50;
        const heavyAsh = App.getHeavyAsh();      // 静态初始值（手写/导入/采样，默认8.50）
        const heavyAmt = App.resolveAmount('denseAmount');      // 重介精煤量（录入或计算）
        const coarseData = App.store.coarseCoal;
        const floatData = App.store.floatCoal;
        const latestCoarse = coarseData.length > 0 ? coarseData[coarseData.length - 1] : null;
        const latestFloat = floatData.length > 0 ? floatData[floatData.length - 1] : null;
        const coarseAmt = App.resolveAmount('coarseAmount');    // 粗精煤泥量（录入）
        // 粗精灰分/浮精灰分：与在线仪表统一解析（时间最新 + 手动覆盖），全链路相通
        const coarseAsh = App.resolveCoarseAsh() ?? 0;
        const floatAsh = App.resolveFloatAsh() ?? 0;
        const floatAmt = App.resolveAmount('floatAmount');      // 浮精量（录入或自动取表2）

        const systems = { 'total': { belt: '501+502', density: App.resolveDensity() } };
        const sys = systems[sysId];

        const target = defaultTarget;
        const amountsOk = heavyAmt != null && floatAmt != null && coarseAmt != null;
        const totalAmt = amountsOk ? heavyAmt + floatAmt + coarseAmt : 0;
        // 密度指导（与卡片同一口径）；实际总灰分与在线仪表同源
        const g = App.computeDensityGuidance(target);
        const numerator = amountsOk ? heavyAsh * heavyAmt + floatAsh * floatAmt + coarseAsh * coarseAmt : 0;
        const actualTotal = totalAmt > 0 ? numerator / totalAmt : 0;
        const actual = App.resolveTotalAsh();
        const dev = +(actual - target).toFixed(2);

        // #12 置信度分析（与卡片角标共用同一计算口径，保证一致）
        const conf = this.computeConfidence(target, actual);
        const confLevel = conf.level, confColor = conf.color, confReason = conf.reason;
        const absDev = Math.abs(dev);
        // 数据完整度/模型状态展示用统计（与置信度判定无关）
        const models = App.store.regressionModels;
        const hasModel = models.length > 0;
        const bestR2 = hasModel ? Math.max(...models.map(m => m.r_squared)) : 0;
        const hasCoarseData = coarseData.length > 0;
        const hasFloatData = floatData.length > 0;
        const hasCalcLogs = App.store.calcLogs.length > 0;
        const hasMagnetic = App.store.magneticTail.length > 0;

        const html = `
            <div style="line-height:2.2;font-size:14px">
                <h4 style="margin:0 0 12px;color:var(--accent-blue)">合并系统（501+502皮带）密度推荐计算</h4>
                <p><strong>一、基础参数</strong></p>
                <table class="data-table" style="margin:4px 0 12px">
                    <tr><td>目标灰分</td><td>${target}%</td></tr>
                    <tr><td>重介精煤灰分</td><td>${heavyAsh}%${App.heavyAshLayer() === '手动(采样)' ? '（手动/采样）' : '（默认8.50）'}</td></tr>
                    <tr><td>重介精煤量(录入/计算)</td><td>${heavyAmt == null ? '—' : heavyAmt + ' t/h'}</td></tr>
                    <tr><td>粗精灰分(化验实测)</td><td>${latestCoarse ? latestCoarse.ash_content.toFixed(2) + '%' : '暂无数据'}</td></tr>
                    <tr><td>粗精灰分(前馈预测)</td><td>${latestCoarse ? coarseAsh.toFixed(2) + '%' : '暂无数据'}</td></tr>
                    <tr><td>粗精煤泥量(录入)</td><td>${coarseAmt == null ? '—' : coarseAmt + ' t/h'}</td></tr>
                    <tr><td>最新浮精灰分</td><td>${latestFloat ? floatAsh + '%' : '暂无数据'}</td></tr>
                    <tr><td>浮精量(录入/自动)</td><td>${floatAmt == null ? '—' : floatAmt + ' t/h'}</td></tr>
                </table>
                <p><strong>二、总灰分计算</strong></p>
                <p style="color:var(--text-secondary)">公式：总灰分 = (重介灰分×重介量 + 浮精灰分×浮精量 + 粗精灰分×粗精量) / 总精煤量</p>
                <p>代入数值：</p>
                ${amountsOk ? `
                <p style="padding-left:16px;color:var(--accent-blue)">
                    总灰分 = (${heavyAsh}×${heavyAmt} + ${floatAsh}×${floatAmt} + ${coarseAsh}×${coarseAmt}) / (${heavyAmt} + ${floatAmt} + ${coarseAmt})
                </p>
                <p style="padding-left:16px;color:var(--accent-blue)">
                    = (${(heavyAsh * heavyAmt).toFixed(2)} + ${(floatAsh * floatAmt).toFixed(2)} + ${(coarseAsh * coarseAmt).toFixed(2)}) / ${totalAmt}
                </p>
                <p style="padding-left:16px;color:var(--accent-blue)">
                    = ${numerator.toFixed(2)} / ${totalAmt} = <strong>${actualTotal.toFixed(4)}%</strong>
                </p>` : `<p style="padding-left:16px;color:var(--accent-orange)">量数据不完整，请先在"量数据（录入 + 输入）"面板补齐三个量</p>`}
                <p><strong>三、偏差计算</strong></p>
                <p style="color:var(--text-secondary)">偏差 = 实际总灰分 - 目标灰分</p>
                <p>代入数值：${amountsOk ? `${actual.toFixed(4)} - ${target} = <strong style="color:${Math.abs(dev) > 0.1 ? 'var(--accent-red)' : 'var(--accent-green)'}">${dev > 0 ? '+' : ''}${dev}%</strong>` : '量数据不完整，暂无法计算偏差'}</p>
                <p><strong>四、推荐密度（${g.scheme === 'heavy' ? '重介精煤灰分版' : '总灰分版'}）</strong></p>
                <table class="data-table" style="margin:4px 0 12px">
                    <tr><td>控制版本</td><td>${g.scheme === 'heavy' ? '<span style="color:var(--accent-blue)">重介精煤灰分版</span>（总览页按钮切换）' : '总灰分版（总览页按钮切换）'}</td></tr>
                    <tr><td>密度计当前值</td><td>${g.rhoCur.toFixed(3)} g/cm³</td></tr>
                    ${g.scheme === 'heavy' ? `
                    <tr><td>实测重介精煤灰分</td><td>${g.heavyAsh.toFixed(2)}%</td></tr>
                    <tr><td>目标重介精煤灰分</td><td>${g.targetHeavy != null ? g.targetHeavy.toFixed(2) + '%' : '—'}</td></tr>
                    <tr><td>重介灰分偏差</td><td>${(g.deltaAHeavy > 0 ? '+' : '') + g.deltaAHeavy.toFixed(2)}%</td></tr>` : `
                    <tr><td>实际总灰分(在线仪表)</td><td>${g.valid ? g.actualTotal.toFixed(2) + '%' : '—'}</td></tr>`}
                    <tr><td>期望总灰分</td><td>${g.targetTotal.toFixed(2)}%</td></tr>
                    <tr><td>${g.scheme === 'heavy' ? '等效总灰分偏差' : '总灰分偏差'} ΔA</td><td>${g.valid ? (g.deltaA > 0 ? '+' : '') + g.deltaA.toFixed(2) + '%' : '—'}</td></tr>
                    <tr><td>调整规则(专家经验)</td><td>偏差≤0.05%不调；0.15%→调0.01；0.25%→调0.02；区间线性插值，0.25%以上按斜率0.1外推</td></tr>
                    <tr><td>预测增益 K</td><td>${g.K.toFixed(4)}（${g.kSource === 'data' ? '表3配对数据驱动' + (g.kInfo ? '，n=' + g.kInfo.n + '，' + (g.kInfo.source === 'per_system' ? '分系统加权' : '合并回归') : '') : '专家经验反推'}，仅用于调密后重介灰分预测）</td></tr>
                    <tr><td>密度修正量</td><td>${g.valid ? (g.deltaRho > 0 ? '+' : '') + g.deltaRho.toFixed(3) : '—'} g/cm³（建议值，人工执行后录入实际密度）</td></tr>
                    <tr><td>目标密度</td><td><strong>${g.rhoNew.toFixed(3)} g/cm³</strong>（范围1.35~1.60）</td></tr>
                    <tr><td>重介灰分预测(调密后)</td><td>${g.valid && Math.abs(g.deltaRho) > 1e-9 ? (g.heavyAsh + (g.rhoNew - g.rhoCur) / (g.K || 0.03)).toFixed(2) + '%（仅预测，不参与计算，请采样验证）' : '—'}</td></tr>
                    <tr><td>达标判定</td><td>${g.valid && Math.abs(g.deltaA) <= g.deadband ? '<span style="color:var(--accent-green)">✓ 已达标（' + (g.scheme === 'heavy' ? '重介口径±' + (g.deadband * g.totalAmt / g.heavyAmt).toFixed(3) + '%，等效总灰分±' + g.deadband : '容差±' + g.deadband) + '%）</span>' : '<span style="color:var(--accent-orange)">未达标</span>'}</td></tr>
                    <tr><td>重介灰分反推值(仅展示)</td><td>${App.store.heavyAshBackcalc != null ? App.store.heavyAshBackcalc.toFixed(2) + '%' : '—'}</td></tr>
                </table>
                <p style="color:var(--text-secondary)">${g.reason}</p>
                <hr style="border-color:var(--border-color);margin:16px 0">
                <p><strong>五、置信度分析</strong></p>
                <table class="data-table" style="margin:4px 0 12px">
                    <tr><td>置信等级</td><td style="color:${confColor};font-weight:600">${confLevel}</td></tr>
                    <tr><td>判定原因</td><td>${confReason}</td></tr>
                    <tr><td>数据完整度</td><td>
                        粗精煤泥：${hasCoarseData ? coarseData.length + '条' : '<span style="color:var(--accent-red)">无数据</span>'} |
                        浮精：${hasFloatData ? floatData.length + '条' : '<span style="color:var(--accent-red)">无数据</span>'} |
                        灰分密度：${hasCalcLogs ? App.store.calcLogs.length + '条' : '<span style="color:var(--accent-red)">无数据</span>'} |
                        精磁尾：${hasMagnetic ? App.store.magneticTail.length + '条' : '<span style="color:var(--accent-red)">无数据</span>'}
                    </td></tr>
                    <tr><td>密度模型状态</td><td>${hasModel ? `${models.length}个模型，最佳R²=${bestR2.toFixed(4)}` : '<span style="color:var(--accent-red)">未训练</span>'}</td></tr>
                    <tr><td>偏差程度</td><td style="color:${absDev > 0.15 ? 'var(--accent-red)' : absDev > 0.05 ? 'var(--accent-orange)' : 'var(--accent-green)'}">|${dev}%| = ${absDev.toFixed(2)}%</td></tr>
                </table>
                <p style="color:var(--text-secondary);font-size:12px">综合判断：${confLevel === '高' ? '推荐密度可靠性高，可直接参考执行' : confLevel === '中' ? '推荐密度有一定参考价值，建议结合现场经验综合判断' : '推荐密度仅供参考，建议优先补充数据并训练模型后再做决策'}</p>
            </div>
        `;
        App.openModal(`${sysId}系统 - 计算详情`, html, '<button class="btn" onclick="App.closeModal()">关闭</button>');
    }
};
