/* ========================================
   页面二：数据采集与手工补录
   - 录入总精煤量/灰分/皮带秤/仪表等，含手动来源冻结提示
   ======================================== */

const CollectPage = {
    // 总精煤灰分的影响链：手动来源时，这些行全部标红冻结
    totalAshInfluence: {
        heavyAsh: '重介灰分 → 总精煤灰分（直接）',
        floatAsh: '浮精灰分 → 总精煤灰分（直接）',
        floatAmount: '浮精量 → 总精煤灰分（直接）',
        coarseAsh: '粗精煤泥灰分 → 总精煤灰分（直接）',
        coarseAmount: '粗精煤泥量 → 总精煤灰分（直接）',
        totalAmount: '总煤量 → 总精煤灰分（直接：分母与加权）',
        scale_501: '501皮带秤 → 总煤量 → 总精煤灰分（间接）',
        scale_502: '502皮带秤 → 总煤量 → 总精煤灰分（间接）',
        level_tail: '精磁尾液位 → 粗精煤泥灰分(模型因子) → 总精煤灰分（间接）',
    },
    instruments: [
        { id: 'ash_501', name: '501皮带灰分仪', metric: '灰分', unit: '%', min: 7, max: 9.5, value: 8.52, source: '自动', special: 'ash_501' },
        { id: 'ash_502', name: '502皮带灰分仪', metric: '灰分', unit: '%', min: 7, max: 9.5, value: 10.68, source: '自动', special: 'ash_502' },
        { id: 'density', name: '密度计', metric: '密度', unit: 'g/cm³', min: 1.35, max: 1.60, value: 1.450, source: '自动', special: 'density' },
        { id: 'scale_501', name: '501皮带秤', metric: '煤量', unit: 't/h', min: 100, max: 400, value: 268.5, source: '自动', special: 'scale_501' },
        { id: 'scale_502', name: '502皮带秤', metric: '煤量', unit: 't/h', min: 100, max: 400, value: 235.2, source: '自动', special: 'scale_502' },
        { id: 'level_tail', name: '精磁尾液位计', metric: '液位', unit: '%', min: 20, max: 80, value: 55, source: '自动', special: 'level_tail' },
        { id: 'coarse_ash', name: '粗精煤泥灰分', metric: '灰分', unit: '%', min: 5, max: 25, value: null, source: '默认(模型)', special: 'coarseAsh' },
        { id: 'coarse_amount', name: '粗精煤泥量', metric: '煤量', unit: 't/h', min: 0, max: 50, value: null, source: '默认(计算)', special: 'coarseAmount' },
        { id: 'float_ash', name: '浮精灰分仪', metric: '灰分', unit: '%', min: 8, max: 12, value: 9.85, source: '自动', special: 'floatAsh' },
        { id: 'float_amount', name: '浮精量', metric: '煤量', unit: 't/h', min: 0, max: 30, value: null, source: '录入', special: 'floatAmount' },
        // 任务四：汇总行放表格最下面（双击可修改，手动>录入>默认）
        { id: 'total_ash', name: '总精煤灰分', metric: '灰分', unit: '%', min: 8.5, max: 9.0, value: null, source: '自动', special: 'totalAsh' },
        { id: 'heavy_ash', name: '重介精煤灰分', metric: '灰分', unit: '%', min: 7, max: 9.5, value: null, source: '反推计算', special: 'heavyAsh' },
        { id: 'total_amount', name: '总精煤量', metric: '煤量', unit: 't/h', min: 200, max: 800, value: null, source: '自动', special: 'totalAmount' },
    ],

    categoryFields: {
        ash_meter: [
            { key: 'value', label: '灰分值(%)', type: 'number' },
            { key: 'belt', label: '皮带', type: 'select', options: ['501','502'] }
        ],
        density_meter: [
            { key: 'value', label: '密度值(g/cm³)', type: 'number' }
        ],
        belt_scale: [
            { key: 'value', label: '煤量(t/h)', type: 'number' },
            { key: 'belt', label: '皮带', type: 'select', options: ['501','502'] }
        ],
        magnetic_tail: [
            { key: 'level', label: '液位(%)', type: 'number' },
            { key: 'ash_content', label: '灰分(%)', type: 'number' },
            { key: 'moisture', label: '水分(%)', type: 'number' },
            { key: 'coal_amount', label: '煤量(t/h)', type: 'number' }
        ],
        float_ash: [
            { key: 'ash_content', label: '浮精灰分(%)', type: 'number' },
            { key: 'coal_amount', label: '浮精煤量(t/h)', type: 'number' },
            { key: 'filter_press', label: '压滤机运行', type: 'select', options: ['运行','停止'] }
        ],
        heavy_ash_sample: [
            { key: 'value', label: '重介精煤灰分采样值(%)', type: 'number' },
            { key: 'density', label: '采样时密度(g/cm³)', type: 'number' }
        ]
    },

    init() {
        this.renderTable();
        this.onCategoryChange();
        this.loadHistory();
        this._instrumentsCollapsed = false;
    },

    toggleInstruments() {
        const body = document.getElementById('instrument-panel-body');
        const icon = document.getElementById('instrument-toggle-icon');
        this._instrumentsCollapsed = !this._instrumentsCollapsed;
        if (this._instrumentsCollapsed) {
            body.style.display = 'none';
            icon.innerHTML = '&#9654; 展开';
        } else {
            body.style.display = '';
            icon.innerHTML = '&#9660; 收起';
        }
    },

    refreshAll() {
        this.renderTable();
        this.loadHistory();
        App.showToast('数据已刷新', 'success');
    },

    simulateUpdate() {
        this.instruments.forEach(inst => {
            const range = inst.max - inst.min;
            inst.value = +(inst.value + (Math.random() - 0.5) * range * 0.02).toFixed(3);
            inst.value = Math.max(inst.min, Math.min(inst.max, inst.value));
        });
        this.renderTable();
        document.getElementById('collect-refresh-time').textContent =
            '上次刷新：' + App.formatDate(new Date());
    },

    renderTable() {
        const tbody = document.getElementById('collect-tbody');
        tbody.innerHTML = this.instruments.map(inst => {
            const isTotal = !!inst.special;
            // 总灰分为"手动"来源时，其影响链上的行全部标红冻结
            const chain = this.totalAshInfluence[inst.special];
            const lockedFactor = !!App.store.totalAshManualOn && !!chain;
            // 可修改行（special）：由统一数据源解析（手动>录入/计算/模型>默认）
            let value = isTotal ? this._specialValue(inst.special) : inst.value;
            if (isTotal && value == null && inst.special === 'floatAsh') value = inst.value;   // 浮精灰分仪无数据时显示仪表默认
            const layer = isTotal ? this._specialLayer(inst.special) : null;
            const hasValue = value != null;
            const ok = hasValue && value >= inst.min && value <= inst.max;
            const statusClass = ok ? 'status-good' : 'status-warn';
            const statusText = hasValue ? (ok ? '正常' : '偏离') : '—';
            const statusIcon = hasValue ? (ok ? '&#10003;' : '&#9888;') : '';
            const valueCell = isTotal
                ? `<span class="total-edit-cell" title="${lockedFactor ? chain : '双击修改（手动值，清空恢复自动）'}" ondblclick="CollectPage.editTotalInput('${inst.special}', this)"${lockedFactor ? ' style="color:var(--accent-red);opacity:0.75"' : ''}>${hasValue ? value : '—'}</span>`
                : `<span style="font-weight:600;${ok ? '' : 'color:var(--accent-orange)'}">${inst.value}</span>`;
            // 总精煤灰分行：数据来源切换下拉（手动/计算）
            const sourceCell = (isTotal && inst.special === 'totalAsh')
                ? `<select id="totalash-source" onchange="CollectPage.onTotalAshSourceChange()" style="padding:3px 6px;border:1px solid var(--border-color);border-radius:4px;background:var(--bg-darker);color:var(--text-primary);font-size:12px">
                     <option value="calc"${!App.store.totalAshManualOn ? ' selected' : ''}>计算</option>
                     <option value="manual"${App.store.totalAshManualOn ? ' selected' : ''}>手动</option>
                   </select>`
                : isTotal
                    ? (lockedFactor
                        ? `<span style="color:var(--accent-red);font-size:12px" title="${chain}">冻结</span>`
                        : `<span style="color:var(--accent-cyan)">${layer}</span>`)
                    : `<span style="color:var(--accent-cyan)">${inst.source}</span>`;
            const actionCell = isTotal
                ? (lockedFactor
                    ? `<span style="color:var(--accent-red);font-size:12px" title="${chain}">已冻结</span>`
                    : '<span style="color:var(--text-muted);font-size:12px">双击修改</span>')
                : `<button class="link-btn" onclick="CollectPage.switchToManual('${inst.id}')">手工补录</button>`;
            return `<tr${lockedFactor ? ' style="background:rgba(239,68,68,0.07)"' : ''}>
                <td>${inst.name}${lockedFactor ? ' <span style="color:var(--accent-red);font-size:11px" title="' + chain + '">(冻结)</span>' : ''}</td>
                <td>${inst.metric}</td>
                <td>${valueCell}</td>
                <td>${inst.unit}</td>
                <td>${inst.min} ~ ${inst.max}</td>
                <td>${sourceCell}</td>
                <td style="color:var(--text-muted);font-size:12px">${App.formatDate(new Date())}</td>
                <td class="${statusClass}">${statusIcon} ${statusText}</td>
                <td>${actionCell}</td>
            </tr>`;
        }).join('');
    },

    // 总精煤灰分数据来源切换：手动→公式因素冻结；计算→清空手动值、解冻
    onTotalAshSourceChange() {
        const el = document.getElementById('totalash-source');
        const v = el ? el.value : 'calc';
        if (v === 'manual') {
            App.store.totalAshManualOn = true;
            App.saveStore();
            App._onExternalInput();
            this.renderTable();
            App.showToast('总灰分来源已切换为手动：公式因素已冻结（标红）', 'warning');
        } else {
            App.store.totalAshManualOn = false;
            App.saveStore();
            App.setAshInput('totalAsh', { manual: null });   // 恢复公式计算
            this.renderTable();
            App.showToast('总灰分来源已切换为计算：公式因素解冻', 'info');
        }
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
    },

    // 任务四：双击进入编辑（可修改行 手动值），清空=恢复自动/默认
    editTotalInput(special, el) {
        if (el.querySelector && el.querySelector('input')) return;
        // 总灰分为"手动"来源时，其影响链上的行全部冻结
        if (App.store.totalAshManualOn && this.totalAshInfluence[special]) {
            App.showToast(`总灰分当前为"手动"来源，${this.totalAshInfluence[special]}已冻结；将总灰分来源切换为"计算"后可编辑`, 'warning');
            return;
        }
        const inst = this.instruments.find(i => i.special === special);
        let cur = this._specialValue(special);
        if (cur == null && inst) cur = inst.value;
        const input = document.createElement('input');
        input.type = 'number';
        input.step = '0.1';
        input.min = '0';
        input.className = 'instrument-manual-input';
        input.value = cur == null ? '' : cur;
        input.onchange = () => this.commitTotalInput(special, input.value);
        input.onkeydown = e => { if (e.key === 'Enter') input.blur(); };
        input.onblur = () => this.commitTotalInput(special, input.value);
        el.innerHTML = '';
        el.appendChild(input);
        input.focus();
        input.select();
    },

    // 可修改行的取值解析（手动>录入/计算/模型>默认）
    _specialValue(special) {
        switch (special) {
            case 'totalAmount': return App.resolveTotalAmount();
            case 'totalAsh': return App.resolveTotalAsh();
            case 'floatAmount': return App.resolveAmount('floatAmount');
            case 'coarseAmount': return App.resolveCoarseAmount();
            case 'coarseAsh': return App.resolveCoarseAsh();
            case 'floatAsh': return App.resolveFloatAsh();
            case 'ash_501': case 'ash_502': case 'scale_501': case 'scale_502':
            case 'density': case 'level_tail': return App.resolveInstrument(special);
            case 'heavyAsh': return App.resolveHeavyAsh();
        }
        return null;
    },

    // 可修改行的来源层级显示
    _specialLayer(special) {
        switch (special) {
            case 'totalAmount': return App.totalInputLayer('totalAmount');
            case 'totalAsh': return App.totalInputLayer('totalAsh');
            case 'floatAmount': {
                const m = (App.store.amountInputs && App.store.amountInputs.floatAmount) || {};
                return (typeof m.manual === 'number' && isFinite(m.manual)) ? '手动' : '自动(表2)';
            }
            case 'coarseAmount': return App.coarseAmountLayer();
            case 'coarseAsh': return App.coarseAshLayer();
            case 'floatAsh': return App.floatAshLayer();
            case 'ash_501': case 'ash_502': case 'scale_501': case 'scale_502':
            case 'density': case 'level_tail': return App.instrumentLayer(special);
            case 'heavyAsh': return App.heavyAshLayer();
        }
        return null;
    },

    commitTotalInput(special, val) {
        const v = (val === '' || isNaN(parseFloat(val))) ? null : +parseFloat(val);
        switch (special) {
            case 'totalAmount': App.setAmountInput('totalAmount', { manual: v }); break;
            case 'totalAsh': App.setAshInput('totalAsh', { manual: v }); break;
            case 'floatAmount': App.setAmountInput('floatAmount', { manual: v, mode: v == null ? 'auto' : 'manual' }); break;
            case 'coarseAmount': App.setAmountInput('coarseAmount', { manual: v }); break;
            case 'coarseAsh': App.setCoarseAshInput({ manual: v }); break;
            case 'floatAsh': App.setFloatAshInput({ manual: v }); break;
            case 'ash_501': case 'ash_502': case 'scale_501': case 'scale_502':
            case 'density': case 'level_tail': App.setInstrumentInput(special, { manual: v }); break;
            case 'heavyAsh': App.setHeavyAshInput({ manual: v }); break;
        }
        this.renderTable();
    },

    switchToManual(instId) {
        // 切到手工补录表单并预填
        const inst = this.instruments.find(i => i.id === instId);
        if (!inst) return;
        const mapping = {
            ash_501: 'ash_meter', ash_502: 'ash_meter',
            density: 'density_meter',
            scale_501: 'belt_scale', scale_502: 'belt_scale',
            level_tail: 'magnetic_tail', float_ash: 'float_ash'
        };
        const cat = mapping[instId];
        if (cat) {
            document.getElementById('manual-category').value = cat;
            this.onCategoryChange();
        }
        App.showToast(`已切换为手工补录模式 - ${inst.name}`, 'warning');
    },

    onCategoryChange() {
        const cat = document.getElementById('manual-category').value;
        const fields = this.categoryFields[cat] || [];
        const container = document.getElementById('manual-form-fields');
        container.innerHTML = fields.map(f => {
            if (f.type === 'select') {
                const opts = f.options.map(o => `<option value="${o}">${o}</option>`).join('');
                return `<div class="form-group">
                    <label>${f.label}</label>
                    <select id="manual-field-${f.key}">${opts}</select>
                </div>`;
            }
            return `<div class="form-group">
                <label>${f.label}</label>
                <input type="${f.type}" id="manual-field-${f.key}" step="0.01" placeholder="请输入${f.label}">
            </div>`;
        }).join('');

        // 精磁尾液位：显示液位时间提示
        const timeHint = document.getElementById('manual-time-hint');
        if (timeHint) timeHint.remove();
        if (cat === 'magnetic_tail') {
            const hint = document.createElement('div');
            hint.id = 'manual-time-hint';
            hint.style.cssText = 'color:var(--accent-orange);font-size:12px;margin-top:-4px;margin-bottom:8px;padding:4px 8px;background:rgba(255,165,0,0.08);border-radius:4px;';
            hint.textContent = '提示：采样时间适用于灰分和水分，液位对应时间为采样时间提前1分30秒';
            container.parentElement.insertBefore(hint, container.nextSibling);
            // 监听时间变化，自动显示液位时间
            const timeInput = document.getElementById('manual-time');
            if (timeInput) {
                timeInput.oninput = () => this.updateLevelTimeHint(timeInput.value);
                timeInput.onchange = () => this.updateLevelTimeHint(timeInput.value);
            }
        }

        // 重介精煤灰分采样：密度字段预填当前密度计值
        if (cat === 'heavy_ash_sample') {
            const dEl = document.getElementById('manual-field-density');
            if (dEl) dEl.value = App.resolveDensity();
        }

        // 设置默认时间
        const now = new Date();
        const pad = n => String(n).padStart(2, '0');
        document.getElementById('manual-time').value =
            `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
    },

    submitManual() {
        const cat = document.getElementById('manual-category').value;
        // "总灰分修改"开启时公式因素锁定：采样录入暂缓
        if (cat === 'heavy_ash_sample' && App.store.totalAshManualOn) {
            App.showToast('"总灰分修改"开启中，采样录入已锁定；关闭开关后可采样', 'warning');
            return;
        }
        const fields = this.categoryFields[cat] || [];
        const values = {};
        let valid = true;
        fields.forEach(f => {
            const el = document.getElementById(`manual-field-${f.key}`);
            if (el) {
                values[f.key] = el.value;
                if (f.type === 'number' && !el.value) valid = false;
            }
        });
        if (!valid) {
            App.showToast('请填写所有必填字段', 'error');
            return;
        }
        const time = document.getElementById('manual-time').value.replace('T', ' ');
        const remark = document.getElementById('manual-remark').value;

        // 保存到手工录入记录
        const entry = {
            id: App.store.manualEntries.length + 1,
            timestamp: time,
            category: cat,
            values: values,
            remark: remark,
            operator: '操作员',
            status: '已录入'
        };
        App.store.manualEntries.push(entry);

        // 同时写入对应 store 数组（带系统标签）
        const system = '合并';
        switch (cat) {
            case 'heavy_ash_sample': {
                // 重介精煤灰分采样：记录 (时间, 密度, 灰分) 训练数据对，并作为当前实际重介灰分
                // 采样时录入的密度 = 此刻密度计实际读数 → 同步更新密度计（密度计纯手动录入）
                const ash = parseFloat(values.value) || 0;
                const rho = parseFloat(values.density) || App.resolveDensity() || 0;
                App.store.heavySamples = App.store.heavySamples || [];
                App.store.heavySamples.push({
                    id: App.store.heavySamples.length + 1,
                    timestamp: time,
                    rho: +rho.toFixed(3),
                    ash_content: +ash.toFixed(2),
                    source: '采样'
                });
                App.setHeavyAshInput({ manual: ash });
                if (rho > 0) App.setInstrumentInput('density', { manual: +rho.toFixed(3) });
                break;
            }
            case 'magnetic_tail':
                App.store.magneticTail.push({
                    id: App.store.magneticTail.length + 1,
                    timestamp: time,
                    system: system,
                    level: parseFloat(values.level) || 0,
                    ash_content: parseFloat(values.ash_content) || 0,
                    moisture: parseFloat(values.moisture) || 0,
                    coal_amount: parseFloat(values.coal_amount) || 0,
                    source: 'manual'
                });
                break;
            case 'float_ash': {
                const ash = parseFloat(values.ash_content) || 0;
                const amt = parseFloat(values.coal_amount) || 0;
                const press = values.filter_press === '运行' ? 1 : 0;
                const influence = (ash - 8.5) * amt / (250 + amt + 15);
                App.store.floatCoal.push({
                    id: App.store.floatCoal.length + 1,
                    timestamp: time,
                    system: system,
                    ash_content: ash,
                    coal_amount: amt,
                    filter_press_running: press,
                    influence_value: +influence.toFixed(3),
                    annotation: '正常'
                });
                break;
            }
            case 'ash_meter':
            case 'density_meter':
            case 'belt_scale':
                App.store.calcLogs.push({
                    id: App.store.calcLogs.length + 1,
                    timestamp: time,
                    calc_type: cat,
                    input_json: JSON.stringify(values),
                    output_json: '{}'
                });
                break;
        }

        // 持久化到 localStorage
        App.saveStore();

        // 新数据 → 自动执行重算目标密度
        App._onExternalInput();

        // 触发已初始化页面刷新
        if (typeof CoarsePage !== 'undefined' && CoarsePage.trendChart) {
            CoarsePage.refresh();
            CoarsePage.updateCharts();
        }
        if (typeof FloatPage !== 'undefined' && FloatPage.linkageChart) {
            FloatPage.refresh();
            FloatPage.updateCharts();
        }
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) {
            OverviewPage.refresh();
        }

        this.loadHistory();
        this.resetManual();

        // 重介精煤灰分采样：算实际总灰分并与期望对比，给出达标判定
        if (cat === 'heavy_ash_sample') {
            const ash = parseFloat(values.value) || 0;
            const rho = parseFloat(values.density) || App.resolveDensity() || 0;
            const tol = (App.store.ashTargetTol != null) ? App.store.ashTargetTol : 0.3;
            const actual = App.calcTotalAsh(
                ash, App.resolveAmount('denseAmount'),
                App.resolveFloatAsh(), App.resolveAmount('floatAmount'),
                App.resolveCoarseAsh(), App.resolveAmount('coarseAmount'));
            const target = App.store.ashTarget != null ? App.store.ashTarget : 8.50;
            if (actual != null) {
                const dev = actual - target;
                if (Math.abs(dev) <= tol) {
                    App.showToast(`✓ 采样已记录（ρ=${rho.toFixed(3)}，灰分=${ash.toFixed(2)}%）：实际总灰分 ${actual.toFixed(2)}%，与期望 ${target.toFixed(2)}% 偏差 ${dev >= 0 ? '+' : ''}${dev.toFixed(2)}% ≤ ±${tol}% —— 已达标`, 'success');
                } else {
                    App.showToast(`采样已记录（ρ=${rho.toFixed(3)}，灰分=${ash.toFixed(2)}%）：实际总灰分 ${actual.toFixed(2)}%，未达标（与期望 ${target.toFixed(2)}% 差 ${dev >= 0 ? '+' : ''}${dev.toFixed(2)}%，容差±${tol}%）`, 'warning');
                }
            } else {
                App.showToast(`采样已记录（灰分=${ash.toFixed(2)}%），数据不完整暂无法计算总灰分`, 'info');
            }
        } else {
            App.showToast('手工补录提交成功', 'success');
        }
    },

    resetManual() {
        const fields = document.querySelectorAll('#manual-form-fields input, #manual-form-fields select');
        fields.forEach(f => f.value = '');
        document.getElementById('manual-remark').value = '';
        const hint = document.getElementById('manual-time-hint');
        if (hint) hint.remove();
    },

    updateLevelTimeHint(timeValue) {
        if (!timeValue) return;
        const samplingTime = new Date(timeValue.replace('T', ' '));
        const levelTime = new Date(samplingTime.getTime() - 90 * 1000); // 减去90秒=1分30秒
        const pad = n => String(n).padStart(2, '0');
        const levelTimeStr = `${levelTime.getFullYear()}-${pad(levelTime.getMonth()+1)}-${pad(levelTime.getDate())} ${pad(levelTime.getHours())}:${pad(levelTime.getMinutes())}:${pad(levelTime.getSeconds())}`;
        let hint = document.getElementById('manual-time-hint');
        if (hint) {
            hint.textContent = `提示：采样时间适用于灰分和水分，液位对应时间为 ${levelTimeStr}（采样时间 -1分30秒）`;
        }
    },

    loadHistory() {
        const filter = document.getElementById('manual-history-filter').value;
        const searchEl = document.getElementById('manual-history-search');
        const keyword = searchEl ? searchEl.value.trim().toLowerCase() : '';
        let entries = App.store.manualEntries;
        if (filter !== 'all') {
            entries = entries.filter(e => e.category === filter);
        }
        if (keyword) {
            entries = entries.filter(e => {
                const vals = Object.values(e.values).join(' ').toLowerCase();
                return e.timestamp.toLowerCase().includes(keyword) || vals.includes(keyword) ||
                       (e.remark || '').toLowerCase().includes(keyword);
            });
        }
        const catNames = {
            ash_meter: '灰分仪', density_meter: '密度计', belt_scale: '皮带秤',
            magnetic_tail: '精磁尾液位', float_ash: '浮精灰分', heavy_ash_sample: '重介精煤灰分采样'
        };
        const tbody = document.getElementById('manual-history-tbody');
        if (entries.length === 0) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted)">暂无匹配记录</td></tr>';
            return;
        }
        const reversed = entries.slice().reverse();
        tbody.innerHTML = reversed.map((e, idx) => {
            const vals = e.values.display || Object.values(e.values).join(', ');
            return `<tr>
                <td>${reversed.length - idx}</td>
                <td style="font-size:12px">${e.timestamp}</td>
                <td>${catNames[e.category] || e.category}</td>
                <td>${vals}</td>
                <td>${e.operator}</td>
                <td class="status-good">${e.status}</td>
                <td><button class="link-btn" style="color:var(--accent-red)" onclick="CollectPage.deleteEntry(${e.id})">删除</button></td>
            </tr>`;
        }).join('');
    },

    deleteEntry(id) {
        // 从 manualEntries 中删除
        const idx = App.store.manualEntries.findIndex(e => e.id === id);
        if (idx === -1) return;
        const entry = App.store.manualEntries[idx];

        // 同时从对应的 store 数组中删除同期数据
        const time = entry.timestamp;
        const cat = entry.category;
        const removeRelated = (arr) => {
            const ri = arr.findIndex(d => d.timestamp === time && d.source === 'manual');
            if (ri !== -1) arr.splice(ri, 1);
        };
        switch (cat) {
            case 'magnetic_tail': removeRelated(App.store.magneticTail); break;
            case 'float_ash': removeRelated(App.store.floatCoal); break;
            case 'ash_meter':
            case 'density_meter':
            case 'belt_scale': removeRelated(App.store.calcLogs); break;
        }

        App.store.manualEntries.splice(idx, 1);
        App.saveStore();
        this.loadHistory();

        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
        if (typeof CoarsePage !== 'undefined' && CoarsePage.trendChart) { CoarsePage.refresh(); CoarsePage.updateCharts(); }
        if (typeof FloatPage !== 'undefined' && FloatPage.linkageChart) { FloatPage.refresh(); FloatPage.updateCharts(); }

        App.showToast('记录已删除', 'success');
    },

    clearHistory() {
        if (!confirm('确定清空所有补录历史记录？此操作不可恢复。')) return;
        App.store.manualEntries = [];
        App.saveStore();
        this.loadHistory();
        App.showToast('历史记录已清空', 'success');
    }
};
