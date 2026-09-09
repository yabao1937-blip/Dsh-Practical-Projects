/* ========================================
   全局核心模块：数据 store + 持久化 + 算法/训练引擎
   - store：内存数据模型（coarseCoal/floatCoal/calcLogs/coarseModel 等）
   - saveStore/loadStore：localStorage 持久化，http 下额外镜像 PUT /api/v1/state
   - 算法：取值链 resolve*、总灰分 calcTotalAsh、密度建议 computeDensityGuidance、
           粗灰预测 predictCoarseAsh、推测简报 buildHourlyBrief
   - 训练：trainMlr/trainPls（本地回退）+ retrainCoarseModelAsync（走后端 sklearn）
   ======================================== */

const App = {
    // 数据存储（模拟数据库）
    store: {
        magneticTail: [],
        rawSlime: [],
        coarseCoal: [],
        floatCoal: [],
        regressionModels: [],
        calcLogs: [],
        manualEntries: [],
        importLogs: [],
        alerts: [],
        // 多因素粗精煤泥灰分模型（MLR + PLS）
        coarseModel: null,            // {mlr, pls, production, trainedAt, n, tolerance}
        coarseModelHistory: [],       // 每次重训练的拟合度快照（持续优化追踪）
        coarseTolerance: 0.8,         // 合格判定容差（±%），可在粗精煤泥页调整
        coarseTrainRange: 'jun_jul',  // 训练数据范围：jun_jul=仅6-7月(出厂口径) | 30d=近30天 | all=全部
        // 量数据（录入+输入）：总精煤量/浮精量/粗精煤泥量/重介精煤量
        // mode: manual=人工录入 | auto=数据源自动 | calc=系统计算
        amountInputs: {
            totalAmount:  { mode: 'manual', manual: null },  // 总精煤量(501+502皮带) t/h
            floatAmount:  { mode: 'auto',   manual: null },  // 浮精量 t/h（自动取表2，可切录入）
            coarseAmount: { mode: 'manual', manual: null },  // 粗精煤泥量 t/h（三表暂无数据源，接口预留）
            denseAmount:  { mode: 'calc',   manual: null },  // 重介精煤量 t/h = 总 − 浮 − 粗
        },
        // 任务三：灰分数据（总精煤灰分来源 + 反推重介精煤灰分）
        ashInputs: {
            totalAsh: { mode: 'auto', manual: null },  // 总精煤灰分：auto=501/502皮带灰分加权 | manual=化验录入
        },
        heavyAshBackcalc: null,   // 反推的重介精煤灰分（任务三公式），null=未反推
        heavyAshInput: { manual: null },   // 重介精煤灰分手动覆盖（采样值，双击修改）
        heavySamples: [],          // 重介精煤灰分采样记录 [{id, timestamp, rho, ash_content}]（训练数据积累）
        ashTargetTol: 0.1,         // 总灰分达标容差（±%），页面可配置（专家经验版默认±0.1）
        guideScheme: 'total',      // 密度指导版本：total=总灰分版 | heavy=重介精煤灰分版（总览页可切换）
        totalAshManualOn: false,   // 总灰分修改开关：默认关=总灰分公式计算级别（不可手动修改）；开=手动化验值优先，公式因素锁定
        ashTarget: 8.50,                   // 目标总精煤灰分（卡片箭头调整后持久化，密度自动执行读取）
        densityGuide: { maxStep: 0.01, deadband: 0.05 },   // 密度指导钳制幅度/死区（页面可调）
        densityAutoOn: true,               // 密度自动执行开关：默认开启，持久化（刷新不变）
        // 在线仪表可修改项（手动 > 录入/计算/模型 > 默认）
        coarseAshInput: { manual: null },   // 粗精煤泥灰分：默认=模型预测
        coarseAshEma: null,                 // 模型预测值EMA平滑状态 {v, t}（压单点噪声）
        autoState: {},                      // 手动有效期机制：各键自动层最近一次数值变化 {v, t}（自动变化后接管更早的手工值）
        floatAshInput:  { manual: null },   // 浮精灰分：默认=仪表9.85 / 录入=表2
        coarseCalc: { screen315: null, waterUnder: null },  // 粗精煤泥量计算：315筛子量−筛下水量（后续导入表提供）
        // 在线仪表权威值（手动 > 录入 > 默认）：其他页面统一从这里取数
        instrumentInputs: {
            ash_501:   { manual: null }, ash_502:   { manual: null },
            scale_501: { manual: null }, scale_502: { manual: null },
            density:   { manual: null }, level_tail: { manual: null },
        },
    },

    charts: {},

    // 页面初始化状态（延迟初始化，解决 display:none 下图表无法渲染）
    pageInited: {},

    // 初始化
    init() {
        this.loadStore();
        if (this.store.__merged !== 2) {
            this.applySeedStore();
        }
        // 老数据无 amountInputs 时补默认（录入+输入）
        if (!this.store.amountInputs) {
            this.store.amountInputs = {
                totalAmount:  { mode: 'manual', manual: 450 },  // 总精煤量 501+502 皮带 t/h
                floatAmount:  { mode: 'auto',   manual: null }, // 浮精量，自动取表2最新值
                coarseAmount: { mode: 'manual', manual: 40 },   // 粗精煤泥量 t/h
                denseAmount:  { mode: 'calc',   manual: null }, // 重介精煤量 = 总 − 浮 − 粗
            };
            this.saveStore();
        }
        // 老数据无 ashInputs 时补默认（任务三）
        if (!this.store.ashInputs) {
            this.store.ashInputs = { totalAsh: { mode: 'auto', manual: null } };
            this.saveStore();
        }
        // 任务四：录入层字段补齐（手动>录入>默认）
        if (this.store.amountInputs && this.store.amountInputs.totalAmount && this.store.amountInputs.totalAmount.entry === undefined) {
            this.store.amountInputs.totalAmount.entry = null;
            this.saveStore();
        }
        if (this.store.ashInputs && this.store.ashInputs.totalAsh && this.store.ashInputs.totalAsh.entry === undefined) {
            this.store.ashInputs.totalAsh.entry = null;
            this.saveStore();
        }
        // 在线仪表可修改项补齐
        if (!this.store.coarseAshInput) { this.store.coarseAshInput = { manual: null }; this.saveStore(); }
        if (!this.store.heavyAshInput) { this.store.heavyAshInput = { manual: null }; this.saveStore(); }
        if (this.store.ashTarget == null) { this.store.ashTarget = 8.50; this.saveStore(); }
        if (!this.store.coarseTrainRange) { this.store.coarseTrainRange = 'jun_jul'; this.saveStore(); }
        // 出厂烘焙模型无 range 字段时回填当前训练范围
        if (this.store.coarseModel && !this.store.coarseModel.range) {
            this.store.coarseModel.range = this.store.coarseTrainRange || 'jun_jul';
            this.saveStore();
        }
        // 手动有效期机制状态补齐
        if (!this.store.autoState) { this.store.autoState = {}; this.saveStore(); }
        // 采样记录与达标容差补齐
        if (!this.store.heavySamples) { this.store.heavySamples = []; this.saveStore(); }
        if (this.store.ashTargetTol == null) { this.store.ashTargetTol = 0.1; this.saveStore(); }
        // 旧默认0.3迁移为专家经验版默认0.1（专家调整表在0.15%~0.25%区间内）
        if (this.store.ashTargetTol === 0.3) { this.store.ashTargetTol = 0.1; this.saveStore(); }
        // 密度计：纯手动录入（每次采样时录入实际密度计数值），系统只给"建议密度"展示，不自动写入密度计
        if (this.store.densityAutoOn == null) { this.store.densityAutoOn = false; this.saveStore(); }
        if (!this.store.guideScheme) { this.store.guideScheme = 'total'; this.saveStore(); }
        this._syncGuideSchemeBtn();
        // 总灰分修改开关：默认关=公式计算级别；关闭状态下清掉遗留的手动总灰分
        if (this.store.totalAshManualOn == null) { this.store.totalAshManualOn = false; this.saveStore(); }
        if (!this.store.totalAshManualOn) this.setAshInput('totalAsh', { manual: null });
        this._syncTotalAshBtn();
        if (!this.store.floatAshInput) { this.store.floatAshInput = { manual: null }; this.saveStore(); }
        if (!this.store.coarseCalc) { this.store.coarseCalc = { screen315: null, waterUnder: null }; this.saveStore(); }
        if (!this.store.instrumentInputs) {
            this.store.instrumentInputs = {
                ash_501: { manual: null }, ash_502: { manual: null },
                scale_501: { manual: null }, scale_502: { manual: null },
                density: { manual: null }, level_tail: { manual: null },
            };
            this.saveStore();
        }
        this.bindNav();        this.startClock();

        this.checkAlerts();
        // 只初始化首页（当前可见页）
        this.initPage('page-overview');
    },

    // 持久化：保存到 localStorage（http 访问时额外镜像到后端 /state）
    saveStore() {
        try {
            localStorage.setItem('dmcs_store', JSON.stringify(this.store));
        } catch (e) {
            console.warn('localStorage 保存失败:', e);
        }
        try {
            if (window.Api && window.location.protocol.startsWith('http')) {
                window.Api.putState(this.store);
            }
        } catch (e) { /* 后端未启动时静默 */ }
    },

    // 一次性数据迁移：清空旧数据并载入合并系统种子数据（__merged 标记版本）
    applySeedStore() {
        const seed = window.DMCS_SEED_STORE;
        if (seed) {
            this.store = JSON.parse(JSON.stringify(seed));
            this.saveStore();
        }
    },

    // 持久化：从 localStorage 加载
    loadStore() {
        try {
            const raw = localStorage.getItem('dmcs_store');
            if (raw) {
                const data = JSON.parse(raw);
                // 迁移标记必须保留：默认store不含该字段，漏掉会导致每次刷新都重新种入种子数据
                if (data.__merged !== undefined) this.store.__merged = data.__merged;
                // 合并到默认 store，保留新增字段兼容性
                const keys = Object.keys(this.store);
                keys.forEach(k => {
                    if (data[k] !== undefined) {
                        this.store[k] = data[k];
                    }
                });
                return true;
            }
        } catch (e) {
            console.warn('localStorage 加载失败:', e);
        }
        return false;
    },

    // 导出数据备份为 Excel/CSV 文件
    exportData() {
        const now = new Date();
        const pad = n => String(n).padStart(2, '0');
        const ts = `${now.getFullYear()}${pad(now.getMonth()+1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
        const fileName = `密控系统数据备份_${ts}.csv`;

        // 每个 store 数组对应一个 Sheet（xlsx-lite 只输出 CSV，所以合并到一个文件）
        const sheetNames = {
            magneticTail: '精磁尾',
            rawSlime: '原煤煤泥',
            coarseCoal: '粗煤泥',
            floatCoal: '浮选',
            regressionModels: '回归模型',
            calcLogs: '计算日志',
            manualEntries: '手工录入',
            importLogs: '导入日志',
            alerts: '告警'
        };

        // 用第一个有数据的数组生成 CSV
        let allRows = [];
        Object.keys(sheetNames).forEach(key => {
            const arr = this.store[key];
            if (arr && arr.length > 0) {
                // 写入分节标题
                allRows.push([`=== ${sheetNames[key]} ===`]);
                if (arr.length > 0) {
                    const headers = Object.keys(arr[0]);
                    allRows.push(headers);
                    arr.forEach(item => {
                        allRows.push(headers.map(h => item[h] !== undefined ? item[h] : ''));
                    });
                }
                allRows.push([]); // 空行分隔
            }
        });

        if (allRows.length === 0) {
            this.showToast('暂无数据可导出', 'warning');
            return;
        }

        const ws = XLSX.utils.aoa_to_sheet(allRows);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '数据备份');
        XLSX.writeFile(wb, fileName);
        this.showToast('数据备份已导出: ' + fileName, 'success');
    },

    // 导入数据备份文件
    importBackup() {
        const input = document.createElement('input');
        input.type = 'file';
        input.accept = '.csv,.xlsx,.xls';
        input.onchange = (e) => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = async (ev) => {
                try {
                    const wb = await XLSX.read(ev.target.result, { type: 'array' });
                    const ws = wb.Sheets[wb.SheetNames[0]];
                    const raw = XLSX.utils.sheet_to_json(ws, { header: 1 });

                    // 解析备份格式：按 === 分节 === 分隔各数组
                    const sections = {};
                    let currentSection = null;
                    let currentHeaders = null;

                    raw.forEach(row => {
                        if (!row || row.length === 0) {
                            currentHeaders = null;
                            return;
                        }
                        const firstCell = String(row[0] || '');
                        const match = firstCell.match(/^===\s*(.+?)\s*===$/);
                        if (match) {
                            currentSection = match[1].trim();
                            currentHeaders = null;
                            sections[currentSection] = [];
                            return;
                        }
                        if (currentSection) {
                            if (!currentHeaders) {
                                currentHeaders = row.map(c => String(c || ''));
                            } else {
                                const obj = {};
                                currentHeaders.forEach((h, i) => {
                                    obj[h] = row[i] !== undefined && row[i] !== '' ? row[i] : undefined;
                                });
                                sections[currentSection].push(obj);
                            }
                        }
                    });

                    // 映射中文 section 名到 store key
                    const sectionMap = {
                        '精磁尾': 'magneticTail',
                        '原煤煤泥': 'rawSlime',
                        '粗煤泥': 'coarseCoal',
                        '浮选': 'floatCoal',
                        '回归模型': 'regressionModels',
                        '计算日志': 'calcLogs',
                        '手工录入': 'manualEntries',
                        '导入日志': 'importLogs',
                        '告警': 'alerts'
                    };

                    Object.keys(sections).forEach(secName => {
                        const key = sectionMap[secName];
                        if (key && sections[secName].length > 0) {
                            this.store[key] = sections[secName];
                        }
                    });

                    this.saveStore();
                    this.refreshAllPages();
                    this.showToast('数据备份导入成功，共恢复 ' +
                        Object.keys(sections).filter(s => sections[s].length > 0).length + ' 个数据表', 'success');
                } catch (err) {
                    this.showToast('备份文件解析失败: ' + err.message, 'error');
                }
            };
            reader.readAsArrayBuffer(file);
        };
        input.click();
    },

    // 刷新所有已初始化页面
    refreshAllPages() {
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) {
            OverviewPage.refresh();
        }
        if (typeof CoarsePage !== 'undefined' && CoarsePage.trendChart) {
            CoarsePage.refresh();
            CoarsePage.updateCharts();
        }
        if (typeof FloatPage !== 'undefined') {
            FloatPage.refresh();
            if (FloatPage.linkageChart) {
                FloatPage.updateCharts();
            }
        }
        if (typeof CollectPage !== 'undefined') {
            CollectPage.loadHistory();
        }
        if (typeof ImportPage !== 'undefined') {
            ImportPage.loadLogs();
        }
        if (typeof HistoryPage !== 'undefined') {
            // History page refreshes on demand via query button
        }
    },

    // 导航切换
    bindNav() {
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const pageId = item.dataset.page;
                document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
                item.classList.add('active');
                document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
                document.getElementById(pageId).classList.add('active');
                // 延迟初始化或刷新图表
                setTimeout(() => this.initPage(pageId), 50);
            });
        });
    },

    // 按需初始化页面
    initPage(pageId) {
        if (!this.pageInited[pageId]) {
            this.pageInited[pageId] = true;
            switch (pageId) {
                case 'page-overview':
                    if (typeof OverviewPage !== 'undefined') OverviewPage.init();
                    break;
                case 'page-collect':
                    if (typeof CollectPage !== 'undefined') CollectPage.init();
                    break;
                case 'page-coarse':
                    if (typeof CoarsePage !== 'undefined') CoarsePage.init();
                    break;
                case 'page-float':
                    if (typeof FloatPage !== 'undefined') FloatPage.init();
                    break;
                case 'page-import':
                    if (typeof ImportPage !== 'undefined') ImportPage.init();
                    break;
                case 'page-history':
                    if (typeof HistoryPage !== 'undefined') HistoryPage.init();
                    break;
            }
        } else {
            // 已初始化的页面，切换回来时重新加载数据并刷新图表
            switch (pageId) {
                case 'page-overview':
                    if (OverviewPage.chart) {
                        OverviewPage.refresh();
                        OverviewPage.chart.resize();
                    }
                    break;
                case 'page-collect':
                    if (typeof CollectPage !== 'undefined') {
                        CollectPage.renderTable();
                        CollectPage.loadHistory();
                    }
                    break;
                case 'page-coarse':
                    if (CoarsePage.trendChart) {
                        CoarsePage.refresh();
                        CoarsePage.updateCharts();
                        CoarsePage.trendChart.resize();
                        if (CoarsePage.factorChart) CoarsePage.factorChart.resize();
                        if (CoarsePage._drawLevelChart) CoarsePage._drawLevelChart();
                    }
                    break;
                case 'page-float':
                    if (FloatPage.linkageChart) {
                        FloatPage.refresh();
                        FloatPage.updateCharts();
                        FloatPage.linkageChart.resize();
                        if (FloatPage.distChart) FloatPage.distChart.resize();
                    }
                    break;
                case 'page-import':
                    if (typeof ImportPage !== 'undefined') {
                        ImportPage.loadLogs();
                    }
                    break;
                case 'page-history':
                    if (typeof HistoryPage !== 'undefined') {
                        HistoryPage.setDefaultDates();
                    }
                    break;
            }
        }
    },

    // 时钟
    startClock() {
        const update = () => {
            const now = new Date();
            document.getElementById('sys-time').textContent = now.toLocaleString('zh-CN', {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
        };
        update();
        setInterval(update, 1000);
    },

    // 生成测试数据（已移除，保留方法名不报错）
    seedData() {
    },

    // 清除所有数据
    clearData() {
        if (!confirm('确定清除所有数据？此操作不可恢复。')) return;
        localStorage.removeItem('dmcs_store');
        this.store = {
            magneticTail: [], rawSlime: [], coarseCoal: [], floatCoal: [],
            regressionModels: [], calcLogs: [], manualEntries: [],
            importLogs: [], alerts: [],
            coarseModel: null, coarseModelHistory: [], coarseTolerance: 0.8,
            coarseTrainRange: 'jun_jul',
            coarseAshEma: null,
            autoState: {},
            amountInputs: {
                totalAmount:  { mode: 'manual', manual: null, entry: null },
                floatAmount:  { mode: 'auto',   manual: null },
                coarseAmount: { mode: 'manual', manual: null },
                denseAmount:  { mode: 'calc',   manual: null },
            },
            ashInputs: { totalAsh: { mode: 'auto', manual: null, entry: null } },
            heavyAshBackcalc: null,
            heavyAshInput: { manual: null },
            heavySamples: [],
            ashTargetTol: 0.1,
            guideScheme: 'total',
            ashTarget: 8.50,
            densityGuide: { maxStep: 0.01, deadband: 0.05 },
            densityAutoOn: true,
            coarseAshInput: { manual: null },
            floatAshInput: { manual: null },
            coarseCalc: { screen315: null, waterUnder: null },
            instrumentInputs: {
                ash_501: { manual: null }, ash_502: { manual: null },
                scale_501: { manual: null }, scale_502: { manual: null },
                density: { manual: null }, level_tail: { manual: null },
            },
        };
        this.refreshAllPages();
        this.checkAlerts();
        this.showToast('所有数据已清除', 'success');
    },

    // 检查告警（从实际 store 数据动态计算）
    checkAlerts() {
        this.store.alerts = [];
        const target = 8.50;
        const threshold = 0.15;

        const coarseData = this.store.coarseCoal;
        const floatData = this.store.floatCoal;
        const hasData = coarseData.length > 0 || floatData.length > 0;

        // 清除旧高亮
        const card = document.getElementById('card-total');
        if (card) card.classList.remove('alert-active');

        if (hasData) {
            // 任务三：重介精煤灰分用反推值（默认8.50兜底），三量走量数据面板
            const heavyAsh = this.getHeavyAsh();
            const heavyAmt = this.resolveAmount('denseAmount');
            const floatAmt = this.resolveAmount('floatAmount');
            const coarseAmt = this.resolveAmount('coarseAmount');
            const latestCoarse = coarseData.length > 0 ? coarseData[coarseData.length - 1] : null;
            const latestFloat = floatData.length > 0 ? floatData[floatData.length - 1] : null;
            const coarseAsh = latestCoarse ? (this.predictCoarseAsh(latestCoarse) || latestCoarse.ash_content) : 0;
            const floatAsh = latestFloat ? latestFloat.ash_content : 0;
            const amountsOk = heavyAmt != null && floatAmt != null && coarseAmt != null;
            const actual = amountsOk ? this.calcTotalAsh(heavyAsh, heavyAmt, floatAsh, floatAmt, coarseAsh, coarseAmt) : null;
            const deviation = amountsOk ? +(actual - target).toFixed(2) : null;

            if (amountsOk && Math.abs(deviation) > threshold) {
                this.store.alerts.push({
                    system: '合并',
                    level: '严重',
                    message: `合并系统灰分偏差 ${deviation > 0 ? '+' : ''}${deviation.toFixed(2)}%，超出告警阈值(±${threshold}%)`,
                    time: this.formatDate(new Date())
                });
                const card = document.getElementById('card-total');
                if (card) card.classList.add('alert-active');
            }
        }

        const count = this.store.alerts.length;
        document.getElementById('alert-count').textContent = count;
        document.getElementById('status-alert-count').textContent = count;
        const zone = document.getElementById('overview-alerts');
        if (zone) {
            if (count > 0) {
                zone.innerHTML = this.store.alerts.map(a =>
                    `<div class="alert-item">
                        <span class="alert-level">[${a.level}]</span>
                        <span class="alert-msg">${a.message}</span>
                        <span class="alert-time">${a.time}</span>
                    </div>`
                ).join('');
            } else {
                zone.innerHTML = '';
            }
        }
    },

    // 训练回归模型（从精磁尾液位数据训练）
    async trainModels() {
        try {
        const data = this.store.magneticTail;
        if (data.length < 5) {
            this.showToast('精磁尾液位数据不足（至少需要5条），请先录入精磁尾数据或加载测试数据', 'warning');
            return;
        }

        const xArr = data.map(d => d.level);
        const yArr = data.map(d => d.ash_content);
        const models = [];

        // 线性回归 (degree=1)
        const lr = this.polyRegression(xArr, yArr, 1);
        if (lr) {
            models.push({
                id: 1, model_type: 'linear',
                params: lr.params.map(p => +p.toFixed(6)),
                r_squared: +lr.rSquared.toFixed(6),
                rmse: +lr.rmse.toFixed(4),
                created_at: this.formatDate(new Date()),
                data_points: data.length
            });
        }

        // 二次多项式 (degree=2)
        if (data.length >= 6) {
            const pr2 = this.polyRegression(xArr, yArr, 2);
            if (pr2) {
                models.push({
                    id: 2, model_type: 'poly2',
                    params: pr2.params.map(p => +p.toFixed(6)),
                    r_squared: +pr2.rSquared.toFixed(6),
                    rmse: +pr2.rmse.toFixed(4),
                    created_at: this.formatDate(new Date()),
                    data_points: data.length
                });
            }
        }

        // 三次多项式 (degree=3)
        if (data.length >= 8) {
            const pr3 = this.polyRegression(xArr, yArr, 3);
            if (pr3) {
                models.push({
                    id: 3, model_type: 'poly3',
                    params: pr3.params.map(p => +p.toFixed(6)),
                    r_squared: +pr3.rSquared.toFixed(6),
                    rmse: +pr3.rmse.toFixed(4),
                    created_at: this.formatDate(new Date()),
                    data_points: data.length
                });
            }
        }

        if (models.length === 0) {
            this.showToast('模型训练失败，数据可能存在问题', 'error');
            return;
        }

        this.store.regressionModels = models;
        this.saveStore();
        this.checkAlerts();

        // 同步训练粗精煤泥多因素模型（MLR + PLS），不阻塞单因素密度模型
        let coarseMsg = '';
        try {
            const ok = await this.retrainCoarseModelAsync();
            if (ok) {
                const cm = this.store.coarseModel;
                coarseMsg = `；粗精煤泥多因素模型[${cm.production.toUpperCase()}] R²=${cm[cm.production].metrics.r2.toFixed(3)} 合格率=${cm[cm.production].metrics.passRate.toFixed(0)}%`;
            }
        } catch (e) {
            console.warn('粗精煤泥多因素模型训练失败:', e);
        }

        this.refreshAllPages();
        this.showToast(`模型训练成功，共训练 ${models.length} 个单因素模型（${models.map(m => m.model_type).join('、')}）${coarseMsg}`, 'success');
        } catch (err) {
            console.error('模型训练错误:', err);
            this.showToast('模型训练出错: ' + err.message, 'error');
        }
    },

    // 多项式回归（degree 阶）返回 { params: [a_d, ..., a_1, a_0], rSquared, rmse }
    polyRegression(xArr, yArr, degree) {
        const n = xArr.length;
        if (n <= degree) return null;
        const size = degree + 1;

        // 构建正规方程 (X^T X) β = X^T y
        const matrix = [];
        const rhs = [];
        for (let i = 0; i < size; i++) {
            matrix[i] = [];
            for (let j = 0; j < size; j++) {
                let sum = 0;
                for (let k = 0; k < n; k++) sum += Math.pow(xArr[k], i + j);
                matrix[i][j] = sum;
            }
            let s = 0;
            for (let k = 0; k < n; k++) s += Math.pow(xArr[k], i) * yArr[k];
            rhs[i] = s;
        }

        const coeffs = this.solveLinearSystem(matrix, rhs);
        if (!coeffs) return null;

        // coeffs = [a_0, a_1, ..., a_d]，反转为 [a_d, ..., a_1, a_0] 以匹配 predictAsh 格式
        const params = coeffs.slice().reverse();

        // 计算 R² 和 RMSE
        const yMean = yArr.reduce((s, v) => s + v, 0) / n;
        let ssTot = 0, ssRes = 0;
        for (let k = 0; k < n; k++) {
            let yPred = 0;
            for (let i = 0; i < size; i++) {
                yPred += coeffs[i] * Math.pow(xArr[k], i);
            }
            ssRes += (yArr[k] - yPred) ** 2;
            ssTot += (yArr[k] - yMean) ** 2;
        }
        const rSquared = ssTot > 0 ? 1 - ssRes / ssTot : 0;
        const rmse = Math.sqrt(ssRes / n);

        return { params, rSquared, rmse };
    },

    // 高斯消元法求解线性方程组 Ax = b
    solveLinearSystem(A, b) {
        const n = A.length;
        // 增广矩阵
        const aug = A.map((row, i) => [...row, b[i]]);

        for (let col = 0; col < n; col++) {
            // 列主元选取
            let maxRow = col;
            for (let row = col + 1; row < n; row++) {
                if (Math.abs(aug[row][col]) > Math.abs(aug[maxRow][col])) maxRow = row;
            }
            [aug[col], aug[maxRow]] = [aug[maxRow], aug[col]];
            if (Math.abs(aug[col][col]) < 1e-12) return null;

            // 消元
            for (let row = col + 1; row < n; row++) {
                const factor = aug[row][col] / aug[col][col];
                for (let j = col; j <= n; j++) aug[row][j] -= factor * aug[col][j];
            }
        }

        // 回代
        const x = new Array(n);
        for (let i = n - 1; i >= 0; i--) {
            x[i] = aug[i][n];
            for (let j = i + 1; j < n; j++) x[i] -= aug[i][j] * x[j];
            x[i] /= aug[i][i];
        }
        return x;
    },

    // 工具函数
    formatDate(d) {
        const pad = n => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    },

    formatShortDate(d) {
        const pad = n => String(n).padStart(2, '0');
        return `${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`;
    },

    showToast(msg, type = 'info') {
        const container = document.getElementById('toast-container');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;
        toast.innerHTML = `<span>${msg}</span><button class="toast-close" onclick="this.parentElement.remove()">&times;</button>`;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    },

    openModal(title, bodyHtml, footerHtml) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').innerHTML = bodyHtml;
        document.getElementById('modal-footer').innerHTML = footerHtml || '';
        document.getElementById('modal-overlay').classList.remove('hidden');
    },

    closeModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
    },

    // 简单线性回归
    linearRegression(xArr, yArr) {
        const n = xArr.length;
        let sx = 0, sy = 0, sxy = 0, sxx = 0;
        for (let i = 0; i < n; i++) {
            sx += xArr[i]; sy += yArr[i];
            sxy += xArr[i] * yArr[i]; sxx += xArr[i] * xArr[i];
        }
        const b = (n * sxy - sx * sy) / (n * sxx - sx * sx);
        const a = (sy - b * sx) / n;
        return { slope: b, intercept: a };
    },

    // 总精煤灰分计算
    calcTotalAsh(heavyAsh, heavyAmt, floatAsh, floatAmt, coarseAsh, coarseAmt) {
        const total = heavyAmt + floatAmt + coarseAmt;
        if (total === 0) return 0;
        return (heavyAsh * heavyAmt + floatAsh * floatAmt + coarseAsh * coarseAmt) / total;
    },

    // 根据时间动态查找重介灰分（从 calcLogs 中查找最近的 ash_density 记录）
    // 任务四：就近匹配加时间窗（默认±5分钟）；超窗返回 null 由调用方决定回退（记缺失）
    getAshByTime(timestamp, system, windowMin = 5) {
        const logs = this.store.calcLogs.filter(l => l.calc_type === 'ash_density');
        const targetTime = new Date(timestamp).getTime();
        let best = null;
        let bestDiff = Infinity;

        logs.forEach(l => {
            let ash = null;
            try {
                const input = typeof l.input_json === 'string' ? JSON.parse(l.input_json) : l.input_json;
                ash = input.ash_content;
            } catch (e) {}
            if (ash === null || ash === undefined) return;
            if (system && l.system && l.system !== system) return;

            const diff = Math.abs(new Date(l.timestamp).getTime() - targetTime);
            if (diff < bestDiff) {
                bestDiff = diff;
                best = ash;
            }
        });

        if (best === null) return 8.50;                     // 无任何记录：沿用默认
        return bestDiff <= windowMin * 60000 ? best : null;  // 超窗：记缺失(null)
    },

    // 预测灰分（用 R² 最高的回归模型）
    predictAsh(level) {
        const models = this.store.regressionModels;
        if (models.length === 0) {
            // Fallback: 使用实际灰分的平均值
            const cc = this.store.coarseCoal;
            if (cc.length > 0) {
                const avg = cc.reduce((s, d) => s + d.ash_content, 0) / cc.length;
                return avg;
            }
            return 13.0;
        }
        // #9 优先选 R² 最高的模型
        const model = models.reduce((a, b) => a.r_squared > b.r_squared ? a : b);
        const p = model.params;
        if (model.model_type === 'linear') return p[0] * level + p[1];
        if (model.model_type === 'poly2') return p[0] * level * level + p[1] * level + p[2];
        if (model.model_type === 'poly3') return p[0]*level**3 + p[1]*level**2 + p[2]*level + p[3];
        // Generic polynomial fallback
        let y = 0;
        for (let i = 0; i < p.length; i++) {
            y += p[i] * Math.pow(level, p.length - 1 - i);
        }
        return y;
    },

    // ============================================================
    //  三表数据接口清单（未来一段时间可用数据 = 三张表的全部字段）
    //  field: 接口字段名；sourceTable: 来源表；usedBy: 消费该字段的算法/功能
    //  新增数据或算法时，在此登记，保证接口预留与追溯
    // ============================================================
    DATA_INTERFACES: [
        { field: 'raw_ash',             label: '原煤灰分(%)',        sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'coal_amount',         label: '小时带煤量(t/h)',    sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'sysA',                label: 'A系统开关',          sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'sysB',                label: 'B系统开关',          sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'sys401',              label: '401系统开关',        sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'sys402',              label: '402系统开关',        sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'desliming473',        label: '473脱粉开关',        sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'desliming474',        label: '474脱粉开关',        sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'is_stoppage',         label: '停机/低负荷标志',    sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型'] },
        { field: 'level',               label: '精磁尾液位(%)',      sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型', '单因素模型'] },
        { field: 'ash_content',         label: '315灰分(%)',         sourceTable: '表1 影响因素', usedBy: ['粗精煤泥灰分模型(训练目标)'] },
        { field: 'moisture',            label: '315全水分(%)',       sourceTable: '表1 影响因素', usedBy: ['预留'] },
        { field: 'mining_face',         label: '入洗工作面',         sourceTable: '表1 影响因素', usedBy: ['预留(分类特征)'] },
        { field: 'float_ash',           label: '浮精灰分(%)',        sourceTable: '表2 浮精', usedBy: ['总灰分计算', '预留'] },
        { field: 'float_amount',        label: '浮精煤量(t/h)',      sourceTable: '表2 浮精', usedBy: ['总灰分计算', '预留'] },
        { field: 'filter_press_running', label: '压滤机运行',        sourceTable: '表2 浮精', usedBy: ['预留'] },
        { field: 'belt',                label: '皮带',               sourceTable: '表3 灰分密度', usedBy: ['预留'] },
        { field: 'belt_ash',            label: '皮带灰分(%)',        sourceTable: '表3 灰分密度', usedBy: ['总灰分计算', '密度模型(预留)'] },
        { field: 'density',             label: '密度(g/cm³)',        sourceTable: '表3 灰分密度', usedBy: ['密度推荐', '密度模型(预留)'] },
        { field: 'ash_density_system',  label: '系统(A/B)',          sourceTable: '表3 灰分密度', usedBy: ['内部参考'] },
    ],

    // 从灰分密度日志取最新密度（表3接口）
    getLatestDensity() {
        const logs = this.store.calcLogs.filter(l => l.calc_type === 'ash_density');
        for (let i = logs.length - 1; i >= 0; i--) {
            try {
                const d = JSON.parse(logs[i].input_json || '{}').density;
                if (typeof d === 'number' && !isNaN(d)) return d;
            } catch (e) { /* 忽略坏记录 */ }
        }
        return 1.450;
    },

    // ============================================================
    //  量数据（录入+输入）：解析各量的当前值
    //  manual=人工录入 | auto=数据源自动 | calc=系统计算
    // ============================================================
    resolveAmount(key) {
        const cfg = (this.store.amountInputs && this.store.amountInputs[key]) || {};
        const mode = cfg.mode || 'manual';
        // 先算自动层值（手动有效时也计算，便于检测自动值变化 → 接管更早的手工）
        let auto = null;
        if (key === 'floatAmount' && mode === 'auto') {
            // 数据源：表2 浮精时间上最新一条的 浮精煤量(t/h)
            const last = this._latestByTime(this.store.floatCoal);
            auto = last && typeof last.coal_amount === 'number' ? last.coal_amount : null;
            if (auto != null) this._autoBump(key, auto);
        } else if (key === 'denseAmount' && mode === 'calc') {
            // 计算：重介精煤量 = 总精煤量 − 浮精量 − 粗精煤泥量
            const t = this.resolveAmount('totalAmount');
            const f = this.resolveAmount('floatAmount');
            const c = this.resolveAmount('coarseAmount');
            if (t != null && f != null && c != null) {
                auto = t - f - c;
                auto = auto < 0 ? 0 : +auto.toFixed(1);
                this._autoBump(key, auto);
            }
        } else if (key === 'totalAmount') {
            auto = this.resolveTotalAmount();   // 内部已打点
        } else if (key === 'coarseAmount') {
            auto = this.resolveCoarseAmount();  // 内部已打点
        }
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, key)) return manual;
        if (auto != null) return auto;
        return manual;
    },

    // 更新某量的模式/录入值（录入+输入切换），并联动刷新各页面
    setAmountInput(key, patch) {
        if (!this.store.amountInputs) this.store.amountInputs = {};
        if (!this.store.amountInputs[key]) this.store.amountInputs[key] = { mode: 'manual', manual: null };
        if (this.store.totalAshManualOn && typeof patch.manual === 'number' && isFinite(patch.manual)) {
            this.showToast('"总灰分修改"开启中，公式因素已锁定；关闭开关后可修改', 'warning');
            return;
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
        }
        Object.assign(this.store.amountInputs[key], patch);
        // 人工改数 → 重置防震荡记忆，方向翻转不停自动执行
        this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // 量数据变化后联动刷新相关页面（总览/粗精煤泥/浮精；未初始化的页面访问时会自行读取）
    notifyAmountChange() {
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
        if (typeof CollectPage !== 'undefined' && document.getElementById('collect-tbody')) CollectPage.renderTable();
        if (typeof CoarsePage !== 'undefined' && CoarsePage.trendChart) { CoarsePage.refresh(); CoarsePage.updateCharts(); }
        if (typeof FloatPage !== 'undefined' && FloatPage.linkageChart) { FloatPage.refresh(); FloatPage.updateCharts(); }
    },

    // ============================================================
    //  任务三：总精煤灰分公式 + 反推重介精煤灰分
    //  总精煤灰分：auto=表3最新501/502皮带灰分按皮带秤煤量加权 | manual=化验录入
    //  重介精煤灰分 = (总精煤灰分×总煤量 − 浮精灰分×浮精量 − 粗精煤泥灰分×粗精煤泥量) ÷ 重介精煤量
    // ============================================================
    // 皮带秤默认值（PLC未接入时的仪表模拟值，与数据采集页在线仪表一致）
    SCALE_DEFAULT: { '501': 268.5, '502': 235.2 },
    // 501/502 皮带灰分仪默认值（与数据采集页在线仪表死数据一致）
    ASH_METER_DEFAULT: { '501': 8.52, '502': 10.68 },
    // 在线仪表全部默认值（PLC未接入时的死数据；PLC接入后替换）
    INSTRUMENT_DEFAULT: {
        ash_501: 8.52, ash_502: 10.68, scale_501: 268.5, scale_502: 235.2,
        density: 1.450, level_tail: 55, float_ash: 9.85,
    },

    // ============================================================
    //  手动有效期机制（2026-09 需求）：
    //  规则：最后一次手工优先级最高；当自动层数值发生变化（密度自动执行每步、
    //  新导入/补录数据引起自动值更新）时，早于该时刻的手工值失效，自动值接管。
    //  ============================================================
    // 自动层数值变化时打点（值确实变化才打点，避免每帧刷新都失效手工）
    _autoBump(key, value) {
        if (value == null || (typeof value === 'number' && !isFinite(value))) return;
        const st = this.store.autoState || (this.store.autoState = {});
        const cur = st[key];
        if (!cur || Math.abs(+cur.v - +value) > 1e-9) {
            st[key] = { v: +value, t: Date.now() };
            this.saveStore();
        }
    },
    // 手动值是否仍有效：无自动打点 → 有效；手动时刻 ≥ 自动打点时刻 → 有效
    _manualValid(cfg, key) {
        const m = cfg && cfg.manual;
        if (typeof m !== 'number' || !isFinite(m) || m < 0) return false;
        const at = (cfg && cfg.manualAt) || 0;
        const st = this.store.autoState && this.store.autoState[key];
        return !st || at >= st.t;
    },

    // 外部(操作员/新数据)改数：递增外部纪元——自动执行据此重算目标密度
    _onExternalInput() {
        this._densityAutoEpoch = (this._densityAutoEpoch || 0) + 1;
    },

    // 在线仪表权威值：手动(双击，最后一次优先) > 录入(导入/补录) > 默认(仪表死数据)
    resolveInstrument(id) {
        const cfg = (this.store.instrumentInputs && this.store.instrumentInputs[id]) || {};
        let entry = null;
        let dataBumped = false;   // 灰分行已在分支内用数据部分打点，避免共享打点重复
        if (id === 'scale_501' || id === 'scale_502') {
            entry = this.latestCalcValue('belt_scale', id === 'scale_501' ? '501' : '502');
        } else if (id === 'ash_501' || id === 'ash_502') {
            // 录入：补录灰分仪 或 表3导入的该皮带灰分
            const base = this.latestBeltAsh(id === 'ash_501' ? '501' : '502');
            // 仿真：密度变化→灰分测量变化（基准点线性化，密度↑0.01→灰分↑≈0.33%）
            const rho = this.resolveInstrument('density');
            const d = (rho - this.getSimBaseRho()) / this.DENSITY_GUIDE.simK;
            entry = +((base != null ? base : this.INSTRUMENT_DEFAULT[id]) + d).toFixed(4);
            // 打点只用数据部分：密度仿真变化不算自动动作（只有新数据才接管更早的手工灰分）
            this._autoBump(id, base != null ? base : this.INSTRUMENT_DEFAULT[id]);
            dataBumped = true;
        } else if (id === 'density') {
            // 录入：优先手工补录的密度计值(density_meter，倒序取最近一条有效)，其次表3灰分密度导入最新密度(ash_density)
            const dmLogs = (this.store.calcLogs || []).filter(l => l.calc_type === 'density_meter');
            for (let i = dmLogs.length - 1; i >= 0; i--) {
                try {
                    const v = +JSON.parse(dmLogs[i].input_json || '{}').value;
                    if (isFinite(v) && v >= 1.3 && v <= 1.6) { entry = v; break; }
                } catch (e) { /* 忽略坏记录 */ }
            }
            if (entry == null) {
                const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
                for (let i = logs.length - 1; i >= 0; i--) {
                    try {
                        const v = JSON.parse(logs[i].input_json || '{}');
                        if (typeof v.density === 'number' && isFinite(v.density) && v.density >= 1.3 && v.density <= 1.6) { entry = v.density; break; }
                    } catch (e) { /* 忽略坏记录 */ }
                }
            }
        } else if (id === 'level_tail') {
            // 录入：精磁尾最新一条液位
            const list = this.store.magneticTail || [];
            for (let i = list.length - 1; i >= 0; i--) {
                if (typeof list[i].level === 'number' && list[i].level > 0) { entry = list[i].level; break; }
            }
        }
        // 自动层打点：只对"当前生效的自动值"打点（录入优先，否则默认），值变化才打点
        if (!dataBumped) {
            if (entry != null) { this._autoBump(id, entry); }
            else {
                const dv = this.INSTRUMENT_DEFAULT[id] != null ? this.INSTRUMENT_DEFAULT[id] : null;
                if (dv != null) this._autoBump(id, dv);
            }
        }
        if (this._manualValid(cfg, id)) return cfg.manual;
        if (entry != null) return entry;
        return this.INSTRUMENT_DEFAULT[id] != null ? this.INSTRUMENT_DEFAULT[id] : null;
    },

    // 当前生效层级（来源列显示）
    instrumentLayer(id) {
        const cfg = (this.store.instrumentInputs && this.store.instrumentInputs[id]) || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) {
            if (this._manualValid(cfg, id)) return cfg.autoExec ? '自动执行' : '手动';
            return '自动(已接管)';
        }
        const hasEntry = (() => {
            if (id === 'scale_501' || id === 'scale_502') return this.latestCalcValue('belt_scale', id === 'scale_501' ? '501' : '502') != null;
            if (id === 'ash_501' || id === 'ash_502') return this.latestBeltAsh(id === 'ash_501' ? '501' : '502') != null;
            if (id === 'density') {
                const dmLogs = (this.store.calcLogs || []).filter(l => l.calc_type === 'density_meter');
                for (let i = dmLogs.length - 1; i >= 0; i--) {
                    try { const v = +JSON.parse(dmLogs[i].input_json || '{}').value; if (isFinite(v) && v >= 1.3 && v <= 1.6) return true; } catch (e) {}
                }
                const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
                for (let i = logs.length - 1; i >= 0; i--) {
                    try { const v = JSON.parse(logs[i].input_json || '{}'); if (typeof v.density === 'number' && isFinite(v.density) && v.density >= 1.3 && v.density <= 1.6) return true; } catch (e) {}
                }
                return false;
            }
            if (id === 'level_tail') {
                const list = this.store.magneticTail || [];
                return list.some(m => typeof m.level === 'number' && m.level > 0);
            }
            return false;
        })();
        return hasEntry ? '录入' : '默认(仪表)';
    },

    // 建议密度（在线仪表密度计权威值）
    resolveDensity() { return this.resolveInstrument('density'); },

    setInstrumentInput(id, patch) {
        if (!this.store.instrumentInputs) this.store.instrumentInputs = {};
        if (!this.store.instrumentInputs[id]) this.store.instrumentInputs[id] = { manual: null };
        // 总灰分为"手动"来源时，间接影响总灰分的仪表(皮带秤→总煤量→总灰分；液位→粗精灰分→总灰分)冻结
        if (this.store.totalAshManualOn && ['scale_501', 'scale_502', 'level_tail'].includes(id) && typeof patch.manual === 'number' && isFinite(patch.manual)) {
            this.showToast('总灰分当前为"手动"来源，该间接因素已冻结；切换为"计算"后可修改', 'warning');
            return;
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
            if (!('autoExec' in patch)) patch.autoExec = false;   // 操作员手动写入清除自动执行标记
        }
        Object.assign(this.store.instrumentInputs[id], patch);
        // 外部(操作员)改动输入时重置防震荡记忆：方向翻转若由人工改数引起，不停止自动执行；
        // 自动执行器自己的写入(autoExec)不重置，保证真正的过冲翻转仍能停步
        if (!patch.autoExec) this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // ============================================================
    //  重介精煤灰分 → 介质密度指导（增量式）
    //  ρ建议 = ρ当前 + K×(A目标 − A实际)；灰分偏高(A实际>A目标)→降密度
    //  K：表3历史『灰分~密度』线性回归斜率；死区±0.05%；单次限幅±0.01；范围1.35~1.60
    // ============================================================
    DENSITY_GUIDE: { deadband: 0.05, maxStep: 0.01, rhoMin: 1.35, rhoMax: 1.60, kFallback: 0.03,
                     simBaseRho: 1.49, simK: 0.03 },   // 灰分测量响应仿真：密度↑0.01 → 灰分↑≈0.33%

    // 专家经验调整表（总灰分偏差 → 密度修正量，线性插值/外推）：
    //   |ΔA|≤0.05% → 不调；0.15%→0.01；0.25%→0.02；>0.25% 按末段斜率0.1外推
    EXPERT_ADJUST: { deadband: 0.05, p1: { dA: 0.15, dRho: 0.01 }, p2: { dA: 0.25, dRho: 0.02 }, slope: 0.1 },
    // 专家经验反推的物理增益 K（0.01/0.15≈0.067，0.02/0.25=0.08，取0.075）：
    // 仅用于"调密后重介灰分预测"（ΔA ≈ Δρ/K），不参与调整量计算
    K_PREDICT: 0.075,

    // 专家经验：|总灰分偏差| → 密度修正量
    expertAdjust(dAbs) {
        const t = this.EXPERT_ADJUST;
        if (!(dAbs > t.deadband)) return 0;
        if (dAbs <= t.p1.dA) return (dAbs - t.deadband) / (t.p1.dA - t.deadband) * t.p1.dRho;
        if (dAbs <= t.p2.dA) return t.p1.dRho + (dAbs - t.p1.dA) / (t.p2.dA - t.p1.dA) * (t.p2.dRho - t.p1.dRho);
        return t.p2.dRho + (dAbs - t.p2.dA) * t.slope;
    },

    // 灰分→密度 增益 K（表3 灰分密度历史线性回归），样本不足用默认0.03
    densityGainK() {
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
        const pts = [];
        logs.forEach(l => {
            try {
                const v = JSON.parse(l.input_json || '{}');
                if (isFinite(+v.ash_content) && isFinite(+v.density) && +v.density >= 1.3 && +v.density <= 1.6) pts.push([+v.ash_content, +v.density]);
            } catch (e) { /* 忽略坏记录 */ }
        });
        if (pts.length < 5) return this.DENSITY_GUIDE.kFallback;
        const n = pts.length;
        const sx = pts.reduce((s, p) => s + p[0], 0);
        const sy = pts.reduce((s, p) => s + p[1], 0);
        const sxy = pts.reduce((s, p) => s + p[0] * p[1], 0);
        const sxx = pts.reduce((s, p) => s + p[0] * p[0], 0);
        const denom = n * sxx - sx * sx;
        if (Math.abs(denom) < 1e-9) return this.DENSITY_GUIDE.kFallback;
        const k = (n * sxy - sx * sy) / denom;
        // 物理约束：密度↑→灰分↑，K 必须为正；表3数据存在系统混杂(A/B)可能得到负斜率，
        // 负相关/异常值直接回退默认增益 0.03，保证调整方向不反
        if (!(k > 0.005 && k < 0.2)) return this.DENSITY_GUIDE.kFallback;
        return k;
    },

    // 仿真基准密度：取表3最新有效密度（灰分测量所在工况点），缺省1.49
    getSimBaseRho() {
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
        for (let i = logs.length - 1; i >= 0; i--) {
            try {
                const v = JSON.parse(logs[i].input_json || '{}');
                if (typeof v.density === 'number' && v.density >= 1.3 && v.density <= 1.6) return v.density;
            } catch (e) { /* 忽略坏记录 */ }
        }
        return this.DENSITY_GUIDE.simBaseRho;
    },

    // 密度指导参数（页面可调，持久化）：钳制幅度 maxStep、死区 deadband
    getDensityGuide() {
        const s = this.store.densityGuide || {};
        return {
            deadband: (typeof s.deadband === 'number' && s.deadband > 0) ? s.deadband : this.DENSITY_GUIDE.deadband,
            maxStep: (typeof s.maxStep === 'number' && s.maxStep > 0) ? s.maxStep : this.DENSITY_GUIDE.maxStep,
            rhoMin: this.DENSITY_GUIDE.rhoMin, rhoMax: this.DENSITY_GUIDE.rhoMax,
            kFallback: this.DENSITY_GUIDE.kFallback,
            simBaseRho: this.DENSITY_GUIDE.simBaseRho, simK: this.DENSITY_GUIDE.simK,
        };
    },

    applyDensityGuideSettings() {
        const msEl = document.getElementById('density-maxstep');
        const tolEl = document.getElementById('ash-target-tol');
        const ms = msEl ? parseFloat(msEl.value) : NaN;
        const tol = tolEl ? parseFloat(tolEl.value) : NaN;
        if (!isFinite(ms) || ms <= 0 || ms > 0.1) { this.showToast('钳制幅度需在 0.001~0.1 之间', 'error'); return; }
        if (!isFinite(tol) || tol <= 0 || tol > 3) { this.showToast('达标容差需在 0~3 之间', 'error'); return; }
        this.store.densityGuide = { maxStep: ms, deadband: (this.store.densityGuide && this.store.densityGuide.deadband) || 0.05 };
        this.store.ashTargetTol = tol;
        this.saveStore();
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
        this.showToast(`已应用：钳制±${ms} g/cm³，达标容差±${tol}%`, 'success');
    },

    // 密度指导计算（总灰分偏差驱动）：
    // 实际总灰分 = 在线仪表"总精煤灰分"同源值（手动化验 > 公式计算 > 录入 > 默认）；
    // 偏差 = 实际 − 期望；偏差 ≤ 容差 → 已达标保持；
    // 否则按专家经验表给密度修正量：|ΔA| 0.15%→0.01、0.25%→0.02（线性插值/外推，≤0.05%不调）
    // 两个版本（总览页可切换，store.guideScheme）：
    //   total：总灰分版——偏差=实际总灰分−目标总灰分（现行）
    //   heavy：重介精煤灰分版——目标重介灰分=(A目标×总量−浮−粗)/重介量，偏差=实测重介灰分−目标重介灰分，
    //          换算成等效总灰分偏差（×重介量/总量）后共用同一专家表
    computeDensityGuidance(targetTotalAsh) {
        const g = this.getDensityGuide();
        const scheme = (this.store.guideScheme === 'heavy') ? 'heavy' : 'total';
        const rhoCur = this.resolveDensity();                    // 密度计权威值
        const heavyAsh = this.getHeavyAsh();                     // 静态初始值/采样值，不参与实时反推
        const actualTotal = this.resolveTotalAsh();              // 与卡片/在线仪表同源
        const tol = (this.store.ashTargetTol != null) ? this.store.ashTargetTol : 0.1;
        const r = {
            valid: actualTotal != null && isFinite(actualTotal),
            rhoCur, K: this.K_PREDICT, heavyAsh,
            targetTotal: targetTotalAsh, actualTotal, scheme,
            targetHeavy: null, deltaAHeavy: null,
            deltaA: null, deltaRho: 0, rhoNew: rhoCur, direction: 'stable', reason: '',
            deadband: tol, maxStep: g.maxStep,
        };
        if (!r.valid) { r.reason = '总精煤灰分数据不完整，暂无密度调整建议'; return r; }
        if (scheme === 'heavy') {
            const heavyAmt = this.resolveAmount('denseAmount');
            const floatAmt = this.resolveAmount('floatAmount');
            const coarseAmt = this.resolveAmount('coarseAmount');
            const floatAsh = this.resolveFloatAsh();
            const coarseAsh = this.resolveCoarseAsh();
            const amountsOk = heavyAmt != null && heavyAmt > 0 && floatAmt != null && coarseAmt != null && floatAsh != null && coarseAsh != null;
            if (!amountsOk) { r.valid = false; r.reason = '量/灰分数据不完整，重介精煤灰分版无法换算目标重介灰分'; return r; }
            const totalAmt = heavyAmt + floatAmt + coarseAmt;
            r.heavyAmt = heavyAmt; r.totalAmt = totalAmt;
            r.targetHeavy = +((targetTotalAsh * totalAmt - floatAsh * floatAmt - coarseAsh * coarseAmt) / heavyAmt).toFixed(3);
            r.deltaAHeavy = +(heavyAsh - r.targetHeavy).toFixed(3);
            r.deltaA = +(r.deltaAHeavy * heavyAmt / totalAmt).toFixed(3);   // 等效总灰分偏差（共用专家表）
        } else {
            r.deltaA = +(actualTotal - targetTotalAsh).toFixed(3);
        }
        if (Math.abs(r.deltaA) <= tol) {
            r.reason = scheme === 'heavy'
                ? `重介灰分偏差 ${r.deltaAHeavy >= 0 ? '+' : ''}${r.deltaAHeavy.toFixed(2)}%（实测${heavyAsh.toFixed(2)}% / 目标${r.targetHeavy.toFixed(2)}%，等效总灰分 ${r.deltaA >= 0 ? '+' : ''}${r.deltaA.toFixed(2)}%）≤ ±${tol}% —— 已达标，密度保持`
                : `实际总灰分 ${actualTotal.toFixed(2)}% 与期望 ${targetTotalAsh.toFixed(2)}% 偏差 ${r.deltaA >= 0 ? '+' : ''}${r.deltaA.toFixed(2)}% ≤ ±${tol}% —— 已达标，密度保持`;
            return r;
        }
        // 专家经验修正量（完整修正，人工执行；密度限幅1.35~1.60）
        const dRhoFull = -Math.sign(r.deltaA) * this.expertAdjust(Math.abs(r.deltaA));   // 灰分偏高→降密度
        r.deltaRho = +dRhoFull.toFixed(4);
        r.rhoNew = Math.max(g.rhoMin, Math.min(g.rhoMax, +(rhoCur + dRhoFull).toFixed(3)));
        r.direction = r.deltaA > 0 ? 'down' : 'up';
        r.reason = scheme === 'heavy'
            ? `重介灰分${r.deltaAHeavy > 0 ? '偏高' : '偏低'} ${Math.abs(r.deltaAHeavy).toFixed(2)}%` +
              `（实测${heavyAsh.toFixed(2)}% / 目标${r.targetHeavy.toFixed(2)}%，等效总灰分偏差${r.deltaA >= 0 ? '+' : ''}${r.deltaA.toFixed(2)}%），按专家经验建议` +
              `${r.direction === 'down' ? '下调' : '上调'}密度至 ${r.rhoNew.toFixed(3)} g/cm³（人工执行）`
            : `实际总灰分${r.deltaA > 0 ? '偏高' : '偏低'} ${Math.abs(r.deltaA).toFixed(2)}%` +
              `（${actualTotal.toFixed(2)}% / 期望${targetTotalAsh.toFixed(2)}%），按专家经验建议` +
              `${r.direction === 'down' ? '下调' : '上调'}密度至 ${r.rhoNew.toFixed(3)} g/cm³（人工执行）`;
        return r;
    },

    // 切换密度指导版本：总灰分版 / 重介精煤灰分版（持久化）
    toggleGuideScheme() {
        this.store.guideScheme = (this.store.guideScheme === 'heavy') ? 'total' : 'heavy';
        this.saveStore();
        this._onExternalInput();
        this._syncGuideSchemeBtn();
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
        this.showToast(`已切换为${this.store.guideScheme === 'heavy' ? '重介精煤灰分版' : '总灰分版'}密度指导`, 'info');
    },
    _syncGuideSchemeBtn() {
        const btn = document.getElementById('btn-guide-scheme');
        if (!btn) return;
        const heavy = (this.store.guideScheme === 'heavy');
        btn.textContent = heavy ? '重介精煤灰分版' : '总灰分版';
        btn.title = heavy ? '当前：重介精煤灰分版（偏差=实测重介灰分−目标重介灰分）点击切换总灰分版' : '当前：总灰分版（偏差=实际总灰分−目标总灰分）点击切换重介精煤灰分版';
        btn.classList.toggle('btn-primary', heavy);
    },

    // ============================================================
    //  推测简报：三表数据 1h 对齐 + 逐小时重建在线仪表状态
    //  表头：左侧静态数据(皮带秤/粗精煤泥量/重介灰分/总精煤量)，右侧 建议密度|实测密度 + 预测粗灰|实测粗灰
    // ============================================================
    BRIEF_HEADERS: ['时间',
                    '501皮带秤(t/h)', '502皮带秤(t/h)', '粗精煤泥量(t/h)', '重介精煤灰分(%)', '总精煤量(t/h)',
                    '501皮带灰分仪(%)', '502皮带灰分仪(%)', '精磁尾液位(%)', '浮精灰分(%)', '浮精量(t/h)', '总精煤灰分(%)',
                    '建议密度(g/cm³)', '实测密度(g/cm³)', '预测粗精煤泥灰分(%)', '实测粗精煤泥灰分(%)'],

    buildHourlyBrief() {
        const tol = (this.store.ashTargetTol != null) ? this.store.ashTargetTol : 0.1;
        const target = (this.store.ashTarget != null) ? this.store.ashTarget : 8.50;
        const scheme = (this.store.guideScheme === 'heavy') ? 'heavy' : 'total';
        const lim = this.getDensityGuide();
        const heavyAsh = this.getHeavyAsh();          // 静态初始值/采样值（重介灰分恒值 8.50）
        const COARSE_AMT = 40;                        // 粗精煤泥量恒值（三表无数据源，沿用当前默认）
        const SCALE_501 = 268.5, SCALE_502 = 235.2;   // 皮带秤恒值（三表无数据源）
        const TOTAL_AMT = +(SCALE_501 + SCALE_502).toFixed(1);   // 503.7
        const DEF = { ash501: 8.52, ash502: 10.68, density: 1.450, level: 55, floatAsh: 9.85 };

        const toTs = t => { const d = new Date(t); return isNaN(d) ? null : d.getTime(); };

        // 表1 粗精煤泥多因素（液位 + 实测315灰分 + 模型因子）
        const coarseRecs = (this.store.coarseCoal || [])
            .map(r => ({ t: toTs(r.timestamp), r })).filter(x => x.t != null).sort((a, b) => a.t - b.t);
        // 表2 浮精
        const floatRecs = (this.store.floatCoal || [])
            .map(r => ({ t: toTs(r.timestamp), r })).filter(x => x.t != null).sort((a, b) => a.t - b.t);
        // 表3 灰分密度（按皮带解析 501/502 灰分 + 密度）
        const adRecs = [];
        (this.store.calcLogs || []).forEach(l => {
            if (l.calc_type !== 'ash_density') return;
            const t = toTs(l.timestamp); if (t == null) return;
            try {
                const v = JSON.parse(l.input_json || '{}');
                const density = (isFinite(+v.density) && +v.density >= 1.3 && +v.density <= 1.6) ? +v.density : null;
                adRecs.push({ t, belt: String(v.belt || ''), ash: isFinite(+v.ash_content) ? +v.ash_content : null, density });
            } catch (e) { /* 忽略坏记录 */ }
        });
        adRecs.sort((a, b) => a.t - b.t);

        // 连续 1h 桶：三表最早~最晚，每个整点一行（两次采样之间的整点由递归预测/前向填充补位）
        if (!coarseRecs.length && !floatRecs.length && !adRecs.length) return { headers: this.BRIEF_HEADERS, rows: [] };
        const allTs = [].concat(coarseRecs.map(x => x.t), floatRecs.map(x => x.t), adRecs.map(x => x.t));
        const hMin = Math.floor(Math.min(...allTs) / 3600000);
        const hMax = Math.floor(Math.max(...allTs) / 3600000);
        const hours = [];
        for (let h = hMin; h <= hMax; h++) hours.push(h);

        // 前向填充游标（每表独立）
        let iCoarse = 0, iFloat = 0, iAd = 0;
        let curCoarse = null, curFloat = null;
        let prevCoarse = null;     // 上一小时桶的 表1 记录（判断是否新采样）
        let estCoarse = null;      // 递归软测量值（粗精煤泥灰分预测值）
        let prevForecast = null;   // 上一小时的模型原始预测（用于增量）
        let curAsh501 = null, curAsh502 = null, curDensity = null;

        const pad = n => String(n).padStart(2, '0');
        const fmtHour = hms => { const d = new Date(hms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`; };
        const fmt = (v, n) => (v == null || !isFinite(v)) ? '' : (+v).toFixed(n);

        const rows = [];
        for (const h of hours) {
            const hEnd = (h + 1) * 3600000;
            while (iCoarse < coarseRecs.length && coarseRecs[iCoarse].t < hEnd) { curCoarse = coarseRecs[iCoarse].r; iCoarse++; }
            while (iFloat < floatRecs.length && floatRecs[iFloat].t < hEnd) { curFloat = floatRecs[iFloat].r; iFloat++; }
            const densityBefore = curDensity;
            while (iAd < adRecs.length && adRecs[iAd].t < hEnd) {
                const a = adRecs[iAd];
                if (a.belt === '501' && a.ash != null) curAsh501 = a.ash;
                if (a.belt === '502' && a.ash != null) curAsh502 = a.ash;
                if (a.density != null) curDensity = a.density;
                iAd++;
            }
            // 实测密度：仅本小时有表3新密度测量时有值（稀疏），用于与建议密度对照
            const densityMeasured = (curDensity != null && curDensity !== densityBefore) ? curDensity : null;

            const level = (curCoarse && typeof curCoarse.level === 'number') ? curCoarse.level : DEF.level;
            const floatAsh = (curFloat && typeof curFloat.ash_content === 'number') ? curFloat.ash_content : DEF.floatAsh;
            const floatAmt = (curFloat && typeof curFloat.coal_amount === 'number') ? curFloat.coal_amount : null;
            const ash501 = curAsh501 != null ? curAsh501 : DEF.ash501;
            const ash502 = curAsh502 != null ? curAsh502 : DEF.ash502;
            const density = curDensity != null ? curDensity : DEF.density;

            // 粗精煤泥灰分实测 = 表1 315灰分（人工采样稀疏，仅采样时刻有值）
            const isNewSample = (curCoarse != null && curCoarse !== prevCoarse);
            const coarseMeasured = (isNewSample && curCoarse && typeof curCoarse.ash_content === 'number') ? curCoarse.ash_content : null;

            // 模型原始预测（当前因子）
            let forecast = null;
            if (curCoarse) {
                const p = this.predictCoarseAsh(curCoarse);
                if (p != null && isFinite(p)) forecast = +p.toFixed(2);
            }

            // 递归软测量：基值=第一次采样；有采样→反馈重置；无采样→上一预测值+模型增量
            if (isNewSample && coarseMeasured != null) {
                estCoarse = +coarseMeasured.toFixed(2);                          // 采样反馈：覆盖预测
            } else if (estCoarse != null && forecast != null && prevForecast != null) {
                estCoarse = +(estCoarse + (forecast - prevForecast)).toFixed(2); // 上一预测值 + 模型增量
            }
            const coarseModel = estCoarse;

            const heavyAmt = (floatAmt != null) ? +(TOTAL_AMT - floatAmt - COARSE_AMT).toFixed(1) : null;
            const formulaOk = (heavyAmt != null && heavyAmt > 0 && floatAmt != null && floatAsh != null && coarseModel != null);

            // 总精煤灰分：公式优先（粗灰用递归软测量），否则表3仪表加权
            let totalAsh = null;
            if (formulaOk) {
                totalAsh = +this.calcTotalAsh(heavyAsh, heavyAmt, floatAsh, floatAmt, coarseModel, COARSE_AMT).toFixed(2);
            } else {
                totalAsh = +((ash501 * SCALE_501 + ash502 * SCALE_502) / (SCALE_501 + SCALE_502)).toFixed(2);
            }

            // 建议密度：仅公式完整(有粗灰数据)时计算；缺粗灰数据时留空（避免仪表加权失真顶到1.60）
            let rhoNew = null;
            if (formulaOk) {
                if (scheme === 'heavy') {
                    const totalAmt = heavyAmt + floatAmt + COARSE_AMT;
                    const targetHeavy = +((target * totalAmt - floatAsh * floatAmt - coarseModel * COARSE_AMT) / heavyAmt).toFixed(3);
                    const deltaAHeavy = +(heavyAsh - targetHeavy).toFixed(3);
                    const deltaA = +(deltaAHeavy * heavyAmt / totalAmt).toFixed(3);
                    rhoNew = (Math.abs(deltaA) <= tol)
                        ? density
                        : Math.max(lim.rhoMin, Math.min(lim.rhoMax, +(density - Math.sign(deltaA) * this.expertAdjust(Math.abs(deltaA))).toFixed(3)));
                } else {
                    const deltaA = +(totalAsh - target).toFixed(3);
                    rhoNew = (Math.abs(deltaA) <= tol)
                        ? density
                        : Math.max(lim.rhoMin, Math.min(lim.rhoMax, +(density - Math.sign(deltaA) * this.expertAdjust(Math.abs(deltaA))).toFixed(3)));
                }
            }

            rows.push([
                fmtHour(h * 3600000),
                fmt(SCALE_501, 1), fmt(SCALE_502, 1), fmt(COARSE_AMT, 1), fmt(heavyAsh, 2), fmt(TOTAL_AMT, 1),
                fmt(ash501, 2), fmt(ash502, 2), fmt(level, 1), fmt(floatAsh, 2), fmt(floatAmt, 1), fmt(totalAsh, 2),
                fmt(rhoNew, 3), fmt(densityMeasured, 3),
                fmt(coarseModel, 2), fmt(coarseMeasured, 2),
            ]);
            prevForecast = forecast;
            prevCoarse = curCoarse;
        }

        // 只保留能给出「建议密度」和「粗精煤泥灰分(预测)」的行；两者都无法给出的行不生成
        const briefRows = rows.filter(r => r[12] !== '' && r[14] !== '');
        return { headers: this.BRIEF_HEADERS, rows: briefRows };
    },

    // ============================================================
    //  密度建议自动执行：每1秒执行一次建议（写入密度计），偏差进死区后自动停步
    // ============================================================
    densityAutoTimer: null,

    // 同步按钮文案/样式到当前开关状态
    _syncDensityAutoBtn() {
        const btn = document.getElementById('btn-density-auto');
        if (!btn) return;
        if (this.densityAutoTimer) {
            btn.textContent = '密度自动执行：开';
            btn.classList.add('btn-primary');
        } else {
            btn.textContent = '密度自动执行：关';
            btn.classList.remove('btn-primary');
        }
    },

    startDensityAuto() {
        this.stopDensityAuto();
        this._densityAutoLastEpoch = undefined;   // 首步必重算目标密度
        this._densityTarget = null;
        this._densityPredictAsh = null;           // 调密后重介灰分预测值（仅展示，不参与计算）
        this._densityReachedNotified = false;
        this.densityAutoTimer = setInterval(() => {
            const g = this.computeDensityGuidance((this.store.ashTarget != null) ? this.store.ashTarget : 8.50);
            const lim = this.getDensityGuide();
            const epochNow = (this._densityAutoEpoch || 0);
            const externalChange = (this._densityAutoLastEpoch === undefined) || (epochNow !== this._densityAutoLastEpoch);
            // 采样/改数/新数据后重算目标密度：ρ目标 = ρ当前 + K×(期望总灰分 − 实际总灰分)（完整修正量，限幅1.35~1.60）
            if (externalChange && g.valid) {
                const full = +(g.rhoCur + g.deltaRho).toFixed(3);
                if (full > lim.rhoMax || full < lim.rhoMin) {
                    this.showToast(`建议修正量超出密度限值，目标钳制到 ${g.rhoNew.toFixed(3)} g/cm³`, 'warning');
                }
                this._densityTarget = g.rhoNew;   // 达标时 g.rhoNew = 当前密度 → 保持
                this._densityAutoLastEpoch = epochNow;
                this._densityReachedNotified = false;
                // 预测调密后的重介精煤灰分（仅展示，不参与实时计算；真实值以采样为准）
                if (Math.abs(g.deltaRho) > 1e-9 && Math.abs(g.deltaA) > g.deadband) {
                    this._densityPredictAsh = +(g.heavyAsh + (g.rhoNew - g.rhoCur) / (g.K || 0.03)).toFixed(2);
                } else {
                    this._densityPredictAsh = null;
                }
            }
            // 按钳制逐步走向目标密度（每1秒、每步±maxStep），到位后保持，等下次采样/改数重算
            if (g.valid && this._densityTarget != null) {
                const cur = this.resolveDensity();
                const diff = +(this._densityTarget - cur).toFixed(4);
                if (Math.abs(diff) > 1e-9) {
                    const step = Math.min(lim.maxStep, Math.abs(diff));
                    const rhoNew = +(cur + Math.sign(diff) * step).toFixed(3);
                    this.setInstrumentInput('density', { manual: rhoNew, autoExec: true });
                } else if (!this._densityReachedNotified && this._densityPredictAsh != null) {
                    // 已到目标密度：提示预测重介灰分（请采样验证）
                    this._densityReachedNotified = true;
                    this.showToast(`密度已调至目标 ${this._densityTarget.toFixed(3)} g/cm³：预测重介精煤灰分 ≈ ${this._densityPredictAsh.toFixed(2)}%（仅预测，请采样验证实际值）`, 'success');
                }
                // diff≈0 → 保持（已到目标密度，等待下一次采样）
            }
            if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
        }, 1000);
        this.store.densityAutoOn = true;
        this.saveStore();
        this._syncDensityAutoBtn();
    },

    stopDensityAuto() {
        if (this.densityAutoTimer) { clearInterval(this.densityAutoTimer); this.densityAutoTimer = null; }
    },

    toggleDensityAuto(btn) {
        if (this.densityAutoTimer) {
            this.stopDensityAuto();
            this.store.densityAutoOn = false;
            this.saveStore();
            this._syncDensityAutoBtn();
            this.showToast('已停止密度自动执行', 'info');
        } else {
            this.startDensityAuto();
            this.showToast('已开启：每1秒执行一次密度建议，进入死区自动停步', 'info');
        }
    },

    // 皮带秤当前值：优先手工补录(belt_scale)最新值，否则默认模拟值
    resolveScaleValue(belt) {
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'belt_scale');
        for (let i = logs.length - 1; i >= 0; i--) {
            try {
                const v = JSON.parse(logs[i].input_json || '{}');
                if (String(v.belt || '') === String(belt) && isFinite(+v.value)) return +v.value;
            } catch (e) { /* 忽略坏记录 */ }
        }
        return this.SCALE_DEFAULT[String(belt)] || null;
    },

    // ============================================================
    //  任务四：总精煤量 / 总精煤灰分（在线仪表实时数据区）
    //  取值链：手动(双击修改) > 录入(导入/补录数据) > 默认(在线仪表死数据，将来接PLC实时值)
    // ============================================================
    // 最新一条指定类别/皮带的补录或导入值（calcLogs）
    latestCalcValue(calcType, belt) {
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === calcType);
        for (let i = logs.length - 1; i >= 0; i--) {
            try {
                const v = JSON.parse(logs[i].input_json || '{}');
                if ((belt == null || String(v.belt || '') === String(belt)) && isFinite(+v.value)) return +v.value;
            } catch (e) { /* 忽略坏记录 */ }
        }
        return null;
    },

    // 皮带灰分（灰分仪 501/502 录入层）：优先 ash_meter 补录，其次表3灰分密度导入的皮带灰分
    latestBeltAsh(belt) {
        const v = this.latestCalcValue('ash_meter', belt);
        if (v != null) return v;
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
        for (let i = logs.length - 1; i >= 0; i--) {
            try {
                const d = JSON.parse(logs[i].input_json || '{}');
                if (String(d.belt || '') === String(belt) && isFinite(+d.ash_content)) return +d.ash_content;
            } catch (e) { /* 忽略坏记录 */ }
        }
        return null;
    },

    // 总精煤量：手动 > 录入(导入/补录的501+502皮带秤) > 默认(仪表501+502之和)
    resolveTotalAmount() {
        const cfg = (this.store.amountInputs && this.store.amountInputs.totalAmount) || {};
        const entry = this.totalAmountEntry();
        // 只对当前生效的自动值打点（录入优先，否则默认）
        if (entry != null) { this._autoBump('totalAmount', entry); }
        else {
            const dv = +(this.resolveInstrument('scale_501') + this.resolveInstrument('scale_502')).toFixed(1);
            this._autoBump('totalAmount', dv);
        }
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'totalAmount')) return manual;
        if (entry != null) return entry;
        return +(this.resolveInstrument('scale_501') + this.resolveInstrument('scale_502')).toFixed(1);
    },

    totalAmountEntry() {
        const cfg = (this.store.amountInputs && this.store.amountInputs.totalAmount) || {};
        if (typeof cfg.entry === 'number' && isFinite(cfg.entry) && cfg.entry >= 0) return cfg.entry;
        const e501 = this.latestCalcValue('belt_scale', '501');
        const e502 = this.latestCalcValue('belt_scale', '502');
        return (e501 != null && e502 != null) ? +(e501 + e502).toFixed(1) : null;
    },

    // 公式计算总精煤灰分：加权(重介灰分静态值, 浮精, 粗精)；数据不完整返回 null
    formulaTotalAsh() {
        const heavyAmt = this.resolveAmount('denseAmount');
        const floatAmt = this.resolveAmount('floatAmount');
        const coarseAmt = this.resolveAmount('coarseAmount');
        const floatAsh = this.resolveFloatAsh();
        const coarseAsh = this.resolveCoarseAsh();
        if (heavyAmt == null || heavyAmt <= 0 || floatAmt == null || coarseAmt == null || floatAsh == null || coarseAsh == null) return null;
        return +this.calcTotalAsh(this.getHeavyAsh(), heavyAmt, floatAsh, floatAmt, coarseAsh, coarseAmt).toFixed(4);
    },

    // 总精煤灰分：手动(化验) > 公式计算(给定重介/浮/粗后推出) > 录入(导入/补录) > 默认(仪表加权)
    resolveTotalAsh() {
        const cfg = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'totalAsh')) return manual;
        const formula = this.formulaTotalAsh();
        if (formula != null) { this._autoBump('totalAsh', formula); return formula; }
        const entry = this.totalAshEntry();
        if (entry != null) { this._autoBump('totalAsh', entry); return entry; }
        const w501 = this.resolveInstrument('scale_501'), w502 = this.resolveInstrument('scale_502');
        const dv = +((this.resolveInstrument('ash_501') * w501 + this.resolveInstrument('ash_502') * w502) / (w501 + w502)).toFixed(4);
        this._autoBump('totalAsh', dv);
        return dv;
    },

    totalAshEntry() {
        const cfg = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        if (typeof cfg.entry === 'number' && isFinite(cfg.entry) && cfg.entry >= 0) return cfg.entry;
        const a501 = this.latestBeltAsh('501');
        const a502 = this.latestBeltAsh('502');
        if (a501 == null && a502 == null) return null;
        // 仿真：密度变化→灰分测量变化
        const rho = this.resolveInstrument('density');
        const d = (rho - this.getSimBaseRho()) / this.DENSITY_GUIDE.simK;
        const w501 = this.resolveInstrument('scale_501'), w502 = this.resolveInstrument('scale_502');
        if (a501 == null) return +(a502 + d).toFixed(4);        // 只有502皮带有数据时直接取
        if (a502 == null) return +(a501 + d).toFixed(4);        // 只有501皮带有数据时直接取
        return +(((a501 + d) * w501 + (a502 + d) * w502) / (w501 + w502)).toFixed(4);
    },

    // 当前生效层级（在线仪表表来源列显示：手动/公式计算/录入/默认(仪表)）
    totalInputLayer(kind) {
        const cfg = kind === 'totalAmount'
            ? ((this.store.amountInputs && this.store.amountInputs.totalAmount) || {})
            : ((this.store.ashInputs && this.store.ashInputs.totalAsh) || {});
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) {
            return this._manualValid(cfg, kind === 'totalAmount' ? 'totalAmount' : 'totalAsh') ? '手动' : '自动(已接管)';
        }
        if (kind === 'totalAsh' && this.formulaTotalAsh() != null) return '公式计算';
        const entry = kind === 'totalAmount' ? this.totalAmountEntry() : this.totalAshEntry();
        return entry != null ? '录入' : '默认(仪表)';
    },

    // ============================================================
    //  在线仪表可修改项（手动 > 默认）
    // ============================================================
    // 时间上最新的一条记录（存储顺序不一定按时间，导入顺序会影响数组末尾）
    _latestByTime(list) {
        let best = null, bestT = -Infinity;
        (list || []).forEach(d => {
            const t = new Date(d.timestamp).getTime();
            const v = isFinite(t) ? t : 0;
            if (v >= bestT) { bestT = v; best = d; }
        });
        return best;
    },

    // 粗精煤泥灰分：手动 > 默认(模型前馈预测，EMA平滑压单点噪声，缺失用实测)
    resolveCoarseAsh() {
        const cfg = this.store.coarseAshInput || {};
        const last = this._latestByTime(this.store.coarseCoal);
        let auto = null;
        if (last) {
            const raw = this.predictCoarseAsh(last) || (typeof last.ash_content === 'number' ? last.ash_content : null);
            if (typeof raw === 'number' && isFinite(raw)) {
                auto = +this._emaCoarseAsh(raw, String(last.timestamp || '')).toFixed(2);
                this._autoBump('coarseAsh', auto);
            }
        }
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'coarseAsh')) return manual;
        return auto;
    },

    // 预测值指数平滑（α=0.3）：压单点噪声；跨停>6小时或换序列时重置为原始值
    _emaCoarseAsh(raw, ts) {
        const st = this.store.coarseAshEma || (this.store.coarseAshEma = { v: null, t: null });
        let v = raw;
        if (st.v != null && st.t != null && ts) {
            const gap = Math.abs(new Date(ts).getTime() - new Date(st.t).getTime());
            if (isFinite(gap) && gap <= 6 * 3600000) v = 0.3 * raw + 0.7 * st.v;
        }
        if (st.v !== v || st.t !== ts) { st.v = v; st.t = ts; this.saveStore(); }
        return v;
    },

    coarseAshLayer() {
        const cfg = this.store.coarseAshInput || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m)) {
            return this._manualValid(cfg, 'coarseAsh') ? '手动' : '自动(已接管)';
        }
        return '默认(模型)';
    },

    setCoarseAshInput(patch) {
        if (!this.store.coarseAshInput) this.store.coarseAshInput = { manual: null };
        if (this.store.totalAshManualOn && typeof patch.manual === 'number' && isFinite(patch.manual)) {
            this.showToast('"总灰分修改"开启中，公式因素已锁定；关闭开关后可修改', 'warning');
            return;
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
        }
        Object.assign(this.store.coarseAshInput, patch);
        // 注：手动接管不再重置EMA状态——重置会使自动值瞬时变化误触发"自动接管"，让刚设的手工立即失效
        // 人工改数 → 重置防震荡记忆，方向翻转不停自动执行
        this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // 粗精煤泥量：手动 > 计算(315筛子量−筛下水量，后续导入表提供)
    resolveCoarseAmount() {
        const cfg = (this.store.amountInputs && this.store.amountInputs.coarseAmount) || {};
        const cc = this.store.coarseCalc || {};
        let calc = null;
        if (typeof cc.screen315 === 'number' && typeof cc.waterUnder === 'number' && cc.screen315 >= cc.waterUnder) {
            calc = +(cc.screen315 - cc.waterUnder).toFixed(1);
            this._autoBump('coarseAmount', calc);
        }
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'coarseAmount')) return manual;
        if (calc != null) return calc;
        return manual;
    },

    coarseAmountLayer() {
        const cfg = (this.store.amountInputs && this.store.amountInputs.coarseAmount) || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) {
            return this._manualValid(cfg, 'coarseAmount') ? '手动' : '自动(已接管)';
        }
        const cc = this.store.coarseCalc || {};
        return (typeof cc.screen315 === 'number' && typeof cc.waterUnder === 'number') ? '计算' : '—';
    },

    // 浮精灰分：手动 > 录入(表2最新) > 默认(仪表9.85，由界面显示)
    resolveFloatAsh() {
        const cfg = this.store.floatAshInput || {};
        const last = this._latestByTime(this.store.floatCoal);
        let auto = null;
        if (last && typeof last.ash_content === 'number') {
            auto = last.ash_content;
            this._autoBump('floatAsh', auto);
        } else {
            auto = this.INSTRUMENT_DEFAULT.float_ash;   // 无表2数据回退仪表默认
            this._autoBump('floatAsh', auto);
        }
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'floatAsh')) return manual;
        return auto;
    },

    floatAshLayer() {
        const cfg = this.store.floatAshInput || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) {
            return this._manualValid(cfg, 'floatAsh') ? '手动' : '自动(已接管)';
        }
        return (this.store.floatCoal || []).length > 0 ? '录入' : '默认(仪表)';
    },

    setFloatAshInput(patch) {
        if (!this.store.floatAshInput) this.store.floatAshInput = { manual: null };
        if (this.store.totalAshManualOn && typeof patch.manual === 'number' && isFinite(patch.manual)) {
            this.showToast('"总灰分修改"开启中，公式因素已锁定；关闭开关后可修改', 'warning');
            return;
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
        }
        Object.assign(this.store.floatAshInput, patch);
        // 人工改数 → 重置防震荡记忆，方向翻转不停自动执行
        this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // 更新灰分数据（总精煤灰分来源切换/化验录入）
    setAshInput(key, patch) {
        if (!this.store.ashInputs) this.store.ashInputs = {};
        if (!this.store.ashInputs[key]) this.store.ashInputs[key] = { mode: 'auto', manual: null };
        // 总灰分来源自动同步：手动值输入→切"手动"来源（公式因素冻结）；清空→切"计算"来源（解冻）
        if (key === 'totalAsh') {
            if (typeof patch.manual === 'number' && isFinite(patch.manual) && patch.manual >= 0) {
                this.store.totalAshManualOn = true;
            } else if (patch.manual === null) {
                this.store.totalAshManualOn = false;
            }
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
        }
        Object.assign(this.store.ashInputs[key], patch);
        // 人工改数 → 递增外部纪元（自动执行重算目标密度）
        this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // 总灰分修改开关：默认关（公式计算级别）；开=手动化验值优先且公式因素锁定；关→清空手动恢复公式计算
    toggleTotalAshManual() {
        if (this.store.totalAshManualOn) {
            this.store.totalAshManualOn = false;
            this.saveStore();
            this.setAshInput('totalAsh', { manual: null });   // 恢复公式计算级别
            this.showToast('已关闭"总灰分修改"：总精煤灰分恢复公式计算级别', 'info');
        } else {
            this.store.totalAshManualOn = true;
            this.saveStore();
            this.showToast('已开启"总灰分修改"：手动化验值优先，公式因素（重介/浮精/粗精）已锁定', 'warning');
        }
        this._syncTotalAshBtn();
        if (typeof OverviewPage !== 'undefined' && OverviewPage.chart) OverviewPage.refresh();
    },
    _syncTotalAshBtn() {
        const btn = document.getElementById('btn-totalash-manual');
        if (!btn) return;
        const on = !!this.store.totalAshManualOn;
        btn.textContent = on ? '总灰分修改：开' : '总灰分修改：关';
        btn.classList.toggle('btn-primary', on);
        btn.title = on ? '手动总精煤灰分优先；公式因素已锁定。点击关闭恢复公式计算' : '总精煤灰分为公式计算级别（不可修改）。点击开启手动修改';
    },

    // 反推重介精煤灰分（任务三核心）
    // 保护：限幅5%~13%；相对上次反推值单次变化不超过±2%；重介精煤量<=0不反推
    backCalcHeavyAsh() {
        const totalAsh = this.resolveTotalAsh();
        if (totalAsh == null) return this.store.heavyAshBackcalc;
        const heavyAmt = this.resolveAmount('denseAmount');
        const floatAmt = this.resolveAmount('floatAmount');
        const coarseAmt = this.resolveAmount('coarseAmount');
        if (heavyAmt == null || heavyAmt <= 0 || floatAmt == null || coarseAmt == null) return this.store.heavyAshBackcalc;
        const totalAmt = heavyAmt + floatAmt + coarseAmt;
        // 浮精灰分/粗精煤泥灰分：统一解析（手动>默认），与在线仪表表一致
        const floatAsh = this.resolveFloatAsh();
        const coarseAsh = this.resolveCoarseAsh();
        if (floatAsh == null || coarseAsh == null) return this.store.heavyAshBackcalc;
        let ash = (totalAsh * totalAmt - floatAsh * floatAmt - coarseAsh * coarseAmt) / heavyAmt;
        if (!isFinite(ash)) return this.store.heavyAshBackcalc;
        // 限幅 5%~13%
        ash = Math.max(5.0, Math.min(13.0, ash));
        // 总精煤灰分为手动值时视为权威输入，跳过±2%单步钳制（一步到位）；
        // 自动/录入来源保留钳制，防止仪表/导入坏值突变
        const totalCfg = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        const isManualTotal = (typeof totalCfg.manual === 'number' && isFinite(totalCfg.manual) && totalCfg.manual >= 0);
        const prev = this.store.heavyAshBackcalc;
        if (!isManualTotal && typeof prev === 'number' && prev > 0) {
            const cap = Math.max(0.1, prev * 0.02);
            if (ash > prev + cap) ash = +(prev + cap).toFixed(3);
            if (ash < prev - cap) ash = +(prev - cap).toFixed(3);
        }
        ash = +ash.toFixed(3);
        if (this.store.heavyAshBackcalc !== ash) {
            this.store.heavyAshBackcalc = ash;
            this.saveStore();
        }
        return ash;
    },

    // 取当前重介精煤灰分：静态初始值——手动(采样/手写) > 默认8.50（不参与实时反推演算；反推值仅展示）
    getHeavyAsh() {
        const cfg = (this.store.heavyAshInput && this.store.heavyAshInput) || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0 && this._manualValid(cfg, 'heavyAsh')) return m;
        return 8.50;
    },

    // 重介精煤灰分（在线仪表行）：手动 > 默认8.50
    resolveHeavyAsh() { return this.getHeavyAsh(); },

    heavyAshLayer() {
        const cfg = (this.store.heavyAshInput && this.store.heavyAshInput) || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) {
            return this._manualValid(cfg, 'heavyAsh') ? '手动(采样)' : '默认8.50';
        }
        return '默认8.50';
    },

    setHeavyAshInput(patch) {
        if (!this.store.heavyAshInput) this.store.heavyAshInput = { manual: null };
        if (this.store.totalAshManualOn && typeof patch.manual === 'number' && isFinite(patch.manual)) {
            this.showToast('"总灰分修改"开启中，公式因素已锁定；关闭开关后可修改', 'warning');
            return;
        }
        if ('manual' in patch) {
            patch = Object.assign({}, patch);
            patch.manualAt = (typeof patch.manual === 'number' && isFinite(patch.manual)) ? Date.now() : null;
        }
        Object.assign(this.store.heavyAshInput, patch);
        // 人工改数 → 递增外部纪元（自动执行重算目标密度）
        this._onExternalInput();
        this.saveStore();
        this.notifyAmountChange();
    },

    // ============================================================
    //  多因素回归引擎（MLR 多元线性回归 + PLS 偏最小二乘）
    //  目标：粗精煤泥灰分(%)；因子见 MLR_FEATURES
    // ============================================================

    // 因子顺序固定，coefs/means/stds/stdCoef 均按此顺序对齐
    MLR_FEATURES: ['raw_ash', 'coal_amount', 'sysA', 'sysB', 'sys401', 'sys402',
                   'desliming473', 'desliming474', 'is_stoppage', 'level'],

    FEATURE_LABELS: {
        raw_ash: '原煤灰分(%)', coal_amount: '小时带煤量(t/h)',
        sysA: 'A系统开启', sysB: 'B系统开启', sys401: '401系统开启', sys402: '402系统开启',
        desliming473: '473脱粉开启', desliming474: '474脱粉开启',
        is_stoppage: '停机/低负荷', level: '精磁尾液位(%)'
    },

    // 出厂默认模型：基于"粗精煤泥灰分影响因素6.16-7.14.xlsx"(113条)训练的多因素回归
    // MLR 标准化正规方程 + PLS(NIPALS)，按留一交叉验证 Q² 选优生产模型；缺失因子均值补全
    // 注：Excel 无"停机"列，is_stoppage 系数置0（导入数据时按 带煤量<=10 自动推断）
    DEFAULT_COARSE_MODEL: {
        type: 'pls', n: 113, trainPeriod: '2026-06-16 ~ 2026-07-14',
        intercept: 26.551004,
        coefs: [0.013260, -0.001465, -2.254154, 0.609851, -0.928724, 1.236704, 0.795503, -0.995461, 0.0, -0.181664],
        means:  [35.970708, 846.307080, 0.964602, 0.584071, 0.672566, 0.823009, 0.902655, 0.929204, 0.0, 55.658407],
        stds:   [17.671356, 256.937020, 0.185607, 0.495077, 0.471367, 0.383361, 0.297748, 0.257627, 1.0, 12.471265],
        stdCoef:[0.067562, -0.108522, -0.120631, 0.087052, -0.126220, 0.136696, 0.068292, -0.073943, 0.0, -0.653220],
        metrics: { r2: 0.522635, adjR2: 0.480924, rmse: 2.385691, mae: 1.767178, passRate: 32.743363, q2: 0.421805 }
    },

    // 备选 MLR 模型（同一数据集），供"对比查看"与出厂回退
    DEFAULT_COARSE_MODEL_MLR: {
        type: 'mlr', n: 113, trainPeriod: '2026-06-16 ~ 2026-07-14',
        intercept: 26.782596,
        coefs: [0.014085, -0.002248, -5.014370, 1.597108, 0.068595, 2.378673, 1.086430, -0.668272, 0.0, -0.176086],
        means:  [35.970708, 846.307080, 0.964602, 0.584071, 0.672566, 0.823009, 0.902655, 0.929204, 0.0, 55.658407],
        stds:   [17.671356, 256.937020, 0.185607, 0.495077, 0.471367, 0.383361, 0.297748, 0.257627, 1.0, 12.471265],
        stdCoef:[0.071763, -0.166564, -0.268344, 0.227975, 0.009323, 0.262920, 0.093268, -0.049639, 0.0, -0.633163],
        metrics: { r2: 0.534243, adjR2: 0.493545, rmse: 2.356507, mae: 1.759189, passRate: 31.858407, q2: 0.402075 }
    },

    // 取当前生效的粗精煤泥模型（优先生产模型，缺则回退出厂默认）
    getCoarseModel(which) {
        which = which || (this.store.coarseModel ? this.store.coarseModel.production : 'pls');
        if (this.store.coarseModel && this.store.coarseModel[which]) return this.store.coarseModel[which];
        if (which === 'mlr') return this.DEFAULT_COARSE_MODEL_MLR;
        return this.DEFAULT_COARSE_MODEL;
    },

    // 用指定模型预测一条记录的灰分；缺失因子用训练集均值补全
    predictCoarseAsh(rec, which) {
        if (!rec) return null;
        const feats = this.MLR_FEATURES;
        const model = this.getCoarseModel(which);
        const means = model.imputeMeans || model.means || feats.map(() => 0);
        let y = model.intercept;
        for (let j = 0; j < feats.length; j++) {
            let v = rec[feats[j]];
            if (typeof v !== 'number' || isNaN(v)) v = means[j];
            y += model.coefs[j] * v;
        }
        return y;
    },

    // 训练范围过滤：jun_jul=仅6-7月(出厂口径) | 30d=近30天 | all=全部
    filterTrainRows(range) {
        let rows = this.store.coarseCoal.filter(d =>
            typeof d.ash_content === 'number' && !isNaN(d.ash_content) && d.ash_content > 0);
        const r = range || this.store.coarseTrainRange || 'jun_jul';
        if (r === 'jun_jul') {
            rows = rows.filter(d => {
                const t = String(d.timestamp || '');
                return t.startsWith('2026-06') || t.startsWith('2026-07');
            });
        } else if (r === '30d') {
            const times = rows.map(d => new Date(d.timestamp).getTime()).filter(t => isFinite(t));
            if (times.length > 0) {
                const maxT = Math.max(...times);
                rows = rows.filter(d => {
                    const t = new Date(d.timestamp).getTime();
                    return isFinite(t) && (maxT - t) <= 30 * 86400000;
                });
            }
        }
        return rows;
    },

    // 从 coarseCoal 构造训练集 X/y（按训练范围过滤，缺失因子列均值补全）
    _buildCoarseXY(range) {
        const feats = this.MLR_FEATURES;
        const rows = this.filterTrainRows(range);
        // 按时间排序：时间序列交叉验证与"预测下一小时"语义依赖时间先后
        rows.sort((a, b) => { const ta = String(a.timestamp || ''), tb = String(b.timestamp || ''); return ta < tb ? -1 : ta > tb ? 1 : 0; });
        if (rows.length < feats.length + 2) return null;
        const means = feats.map(f => {
            const vals = rows.map(r => r[f]).filter(v => typeof v === 'number' && !isNaN(v));
            return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
        });
        const X = rows.map(r => feats.map((f, i) => {
            const v = r[f];
            return (typeof v === 'number' && !isNaN(v)) ? v : means[i];
        }));
        const y = rows.map(r => r.ash_content);
        return { X, y, means, rows };
    },

    // 训练 MLR + PLS，按时间序列交叉验证 Q² 选生产模型（回退留一 Q²），回填每条记录 predicted_ash
    trainCoarseModel(range) {
        const useRange = range || this.store.coarseTrainRange || 'jun_jul';
        const d = this._buildCoarseXY(useRange);
        if (!d) return false;
        const mlr = this.trainMlr(d.X, d.y);
        const pls = this.trainPls(d.X, d.y, this.MLR_FEATURES.length);
        if (!mlr || !pls) return false;

        // 记录训练时的缺失因子填补均值，保证预测时与训练一致（否则会用标准化均值，引入偏差）
        mlr.imputeMeans = d.means; pls.imputeMeans = d.means;

        // 时间序列交叉验证 Q²：更贴近"预测下一小时"的真实能力，生产模型优先按它选
        const mlrQ2t = this._timeCvQ2(d.X, d.y, 'mlr');
        const plsQ2t = this._timeCvQ2(d.X, d.y, 'pls', pls.A);
        mlr.metrics.q2Time = (mlrQ2t == null) ? null : +mlrQ2t.toFixed(4);
        pls.metrics.q2Time = (plsQ2t == null) ? null : +plsQ2t.toFixed(4);
        let production;
        if (mlrQ2t != null && plsQ2t != null) production = (plsQ2t >= mlrQ2t) ? 'pls' : 'mlr';
        else production = (pls.metrics.q2 >= mlr.metrics.q2) ? 'pls' : 'mlr';

        const tol = (this.store.coarseTolerance !== undefined) ? this.store.coarseTolerance : 0.8;
        if (!this.store.coarseModelHistory) this.store.coarseModelHistory = [];
        this.store.coarseModelHistory.push({
            trainedAt: this.formatDate(new Date()), n: d.rows.length, tolerance: tol,
            range: useRange, production,
            mlr: { r2: +mlr.metrics.r2.toFixed(4), passRate: +mlr.metrics.passRate.toFixed(1),
                   q2: +mlr.metrics.q2.toFixed(4), q2Time: (mlrQ2t == null) ? null : +mlrQ2t.toFixed(4),
                   rmse: +mlr.metrics.rmse.toFixed(3), mae: +mlr.metrics.mae.toFixed(3) },
            pls: { r2: +pls.metrics.r2.toFixed(4), passRate: +pls.metrics.passRate.toFixed(1),
                   q2: +pls.metrics.q2.toFixed(4), q2Time: (plsQ2t == null) ? null : +plsQ2t.toFixed(4),
                   rmse: +pls.metrics.rmse.toFixed(3), mae: +pls.metrics.mae.toFixed(3),
                   A: pls.A }
        });
        if (this.store.coarseModelHistory.length > 30) this.store.coarseModelHistory.shift();

        // 先写入 coarseModel，再用生产模型回填每条记录预测值（缺失因子自动均值补全）
        this.store.coarseModel = {
            mlr, pls, production, trainedAt: this.formatDate(new Date()),
            n: d.rows.length, tolerance: tol, range: useRange
        };
        this.store.coarseCoal.forEach(r => {
            r.predicted_ash = +this.predictCoarseAsh(r, production).toFixed(4);
        });
        this.saveStore();
        return true;
    },

    // 仅重算最新预测值（5分钟滚动用，不重训练）
    refreshCoarsePredictions() {
        const which = this.store.coarseModel ? this.store.coarseModel.production : 'mlr';
        this.store.coarseCoal.forEach(r => {
            r.predicted_ash = +this.predictCoarseAsh(r, which).toFixed(4);
        });
    },

    // 训练 MLR+PLS 粗灰模型：http 下走后端 sklearn 训练接口；file:// 或后端不可用回退本地训练。
    // 返回 Promise<boolean>；成功时已更新 store.coarseModel / coarseModelHistory / predicted_ash 并 saveStore。
    async retrainCoarseModelAsync(range) {
        range = range || this.store.coarseTrainRange || 'jun_jul';
        if (window.Api && window.location.protocol.startsWith('http')) {
            try {
                const resp = await window.Api.retrainCoarseModel(range);
                if (resp && resp.ok && resp.coarseModel) {
                    this.store.coarseModel = resp.coarseModel;
                    if (!this.store.coarseModelHistory) this.store.coarseModelHistory = [];
                    if (resp.history) {
                        this.store.coarseModelHistory.push(resp.history);
                        if (this.store.coarseModelHistory.length > 30) this.store.coarseModelHistory.shift();
                    }
                    this.refreshCoarsePredictions();
                    this.saveStore();
                    return true;
                }
            } catch (e) {
                console.warn('远程训练失败，回退本地训练:', e);
            }
        }
        return this.trainCoarseModel(range);
    },

    // ---------- 线性代数小工具 ----------
    _mean(a) { return a.reduce((s, v) => s + v, 0) / (a.length || 1); },
    _std(a) { const m = this._mean(a); const s = a.reduce((acc, v) => acc + (v - m) ** 2, 0); return a.length > 1 ? Math.sqrt(s / (a.length - 1)) : 0; },
    _sst(a) { const m = this._mean(a); return a.reduce((acc, v) => acc + (v - m) ** 2, 0); },
    _colMeansStd(X) {
        const n = X.length, k = X[0].length;
        const means = new Array(k).fill(0), stds = new Array(k).fill(0);
        for (let j = 0; j < k; j++) { let s = 0; for (let i = 0; i < n; i++) s += X[i][j]; means[j] = s / n; }
        for (let j = 0; j < k; j++) {
            let s = 0; for (let i = 0; i < n; i++) { const dd = X[i][j] - means[j]; s += dd * dd; }
            stds[j] = n > 1 ? Math.sqrt(s / (n - 1)) : 0;
            if (stds[j] < 1e-9) stds[j] = 1;
        }
        return { means, stds };
    },
    _standardize(X, means, stds) { return X.map(r => r.map((v, j) => (v - means[j]) / stds[j])); },
    _matInv(A) {
        const n = A.length;
        const cols = [];
        for (let c = 0; c < n; c++) {
            const e = new Array(n).fill(0); e[c] = 1;
            const sol = this.solveLinearSystem(A, e);   // solveLinearSystem 不修改入参 A
            if (!sol) return null;
            cols.push(sol);
        }
        const res = [];
        for (let i = 0; i < n; i++) { res[i] = []; for (let j = 0; j < n; j++) res[i][j] = cols[j][i]; }
        return res;
    },
    _metrics(y, yhat, k) {
        const n = y.length, ym = this._mean(y);
        let ssRes = 0, ssTot = 0, mae = 0;
        for (let i = 0; i < n; i++) { const e = y[i] - yhat[i]; ssRes += e * e; ssTot += (y[i] - ym) ** 2; mae += Math.abs(e); }
        const r2 = ssTot > 0 ? 1 - ssRes / ssTot : 0;
        const adjR2 = 1 - (1 - r2) * (n - 1) / Math.max(1, n - k - 1);
        const tol = (this.store.coarseTolerance !== undefined) ? this.store.coarseTolerance : 0.8;
        const passRate = y.filter((v, i) => Math.abs(v - yhat[i]) <= tol).length / n * 100;
        // 三档容差合格率：±0.8 / ±1.0 / ±1.5（供工艺确认可接受偏差口径）
        const passRate1 = y.filter((v, i) => Math.abs(v - yhat[i]) <= 1.0).length / n * 100;
        const passRate15 = y.filter((v, i) => Math.abs(v - yhat[i]) <= 1.5).length / n * 100;
        return { r2, adjR2, rmse: Math.sqrt(ssRes / n), mae: mae / n, passRate, passRate1, passRate15 };
    },

    // ---------- MLR（标准化后正规方程，含 LOOCV Q²） ----------
    trainMlr(X, y) {
        const drop = this._constCols(X);
        const Xd = drop.length ? X.map(r => r.filter((_, j) => !drop.includes(j))) : X;
        const n = Xd.length, k = Xd[0].length;
        const { means, stds } = this._colMeansStd(Xd);
        const Z = this._standardize(Xd, means, stds);
        const K = k + 1;                       // 设计矩阵 [1, Z]
        const A = [], b = new Array(K).fill(0);
        for (let p = 0; p < K; p++) A[p] = new Array(K).fill(0);
        for (let i = 0; i < n; i++) {
            for (let p = 0; p < K; p++) {
                const mp = (p === 0) ? 1 : Z[i][p - 1];
                b[p] += mp * y[i];
                for (let q = p; q < K; q++) { const mq = (q === 0) ? 1 : Z[i][q - 1]; A[p][q] += mp * mq; }
            }
        }
        for (let p = 0; p < K; p++) for (let q = 0; q < p; q++) A[p][q] = A[q][p];
        // 岭回归：λ 在网格上按留一交叉误差选优（λ=0 即普通最小二乘；截距列不惩罚）
        let diagMean = 0;
        for (let p = 1; p < K; p++) diagMean += A[p][p];
        diagMean /= (K - 1);
        const lamGrid = [0, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1].map(f => f * diagMean);
        let best = null;
        for (const lam of lamGrid) {
            const Ar = lam > 0 ? A.map((row, i) => row.map((v, j) => (i === j && j > 0) ? v + lam : v)) : A;
            const beta = this.solveLinearSystem(Ar, b);
            if (!beta) continue;
            const Minv = this._matInv(Ar);
            if (!Minv) continue;
            let sse = 0;
            for (let i = 0; i < n; i++) {
                let yh = beta[0];
                for (let p = 1; p < K; p++) yh += beta[p] * Z[i][p - 1];
                let h = 0;
                for (let p = 0; p < K; p++) {
                    const mp = (p === 0) ? 1 : Z[i][p - 1];
                    let inner = 0;
                    for (let q = 0; q < K; q++) { const mq = (q === 0) ? 1 : Z[i][q - 1]; inner += Minv[p][q] * mq; }
                    h += mp * inner;
                }
                const loo = (h < 1 - 1e-9) ? (y[i] - yh) / (1 - h) : (y[i] - yh);
                sse += loo * loo;
            }
            if (!best || sse < best.sse) best = { sse, lam, beta };
        }
        if (!best) return null;
        const betaStd = best.beta;

        const coefs = new Array(k);
        let intercept = betaStd[0];
        for (let j = 0; j < k; j++) { coefs[j] = betaStd[1 + j] / stds[j]; intercept -= coefs[j] * means[j]; }
        const yhat = Xd.map(r => { let s = intercept; for (let j = 0; j < k; j++) s += coefs[j] * r[j]; return s; });
        const m = this._metrics(y, yhat, k);
        const stdY = this._std(y);
        const stdCoef = coefs.map((c, j) => stdY > 0 ? c * stds[j] / stdY : 0);

        // LOOCV Q² 直接复用 λ 选优时的留一误差
        const sst = this._sst(y);
        m.q2 = sst > 0 ? 1 - best.sse / sst : 0;
        m.lambda = best.lam;

        // 将被剔除的常数特征列补回（系数0，均值0，std1）
        const full = this._unDrop({ coefs, means, stds, stdCoef }, drop, X[0].length);
        return { type: 'mlr', intercept, coefs: full.coefs, means: full.means, stds: full.stds, stdCoef: full.stdCoef, yhat, metrics: m, n, k, drop, lambda: best.lam };
    },

    // ---------- PLS1 NIPALS（A 由 LOOCV 选优） ----------
    // Z: 标准化 X(n×k), yc: 中心化 y(n), A: 分量数 → 返回 {W,P,C,Bstd}
    _plsCore(Z, yc, A) {
        const n = Z.length, k = Z[0].length;
        const X0 = Z.map(r => r.slice()), y0 = yc.slice();
        const W = [], P = [], C = [];
        for (let a = 0; a < A; a++) {
            let w = new Array(k).fill(0);
            for (let j = 0; j < k; j++) for (let i = 0; i < n; i++) w[j] += X0[i][j] * y0[i];
            const wnorm = Math.sqrt(w.reduce((s, v) => s + v * v, 0)) || 1;
            w = w.map(v => v / wnorm);
            const t = new Array(n).fill(0);
            for (let i = 0; i < n; i++) for (let j = 0; j < k; j++) t[i] += X0[i][j] * w[j];
            let tt = 0, ty = 0;
            for (let i = 0; i < n; i++) { tt += t[i] * t[i]; ty += t[i] * y0[i]; }
            const c = tt ? ty / tt : 0;
            const p = new Array(k).fill(0);
            for (let j = 0; j < k; j++) { for (let i = 0; i < n; i++) p[j] += X0[i][j] * t[i]; p[j] /= (tt || 1); }
            for (let i = 0; i < n; i++) { for (let j = 0; j < k; j++) X0[i][j] -= t[i] * p[j]; y0[i] -= t[i] * c; }
            W.push(w); P.push(p); C.push(c);
        }
        // Bstd = W (PᵀW)⁻¹ C
        const M = [];
        for (let a = 0; a < A; a++) { M[a] = []; for (let bb = 0; bb < A; bb++) { let s = 0; for (let j = 0; j < k; j++) s += P[a][j] * W[bb][j]; M[a][bb] = s; } }
        const tmp = this.solveLinearSystem(M, C.slice());
        const Bstd = new Array(k).fill(0);
        for (let j = 0; j < k; j++) for (let a = 0; a < A; a++) Bstd[j] += (tmp ? W[a][j] * tmp[a] : 0);
        return { W, P, C, Bstd };
    },

    trainPls(X, y, Amax) {
        const drop = this._constCols(X);
        const Xd = drop.length ? X.map(r => r.filter((_, j) => !drop.includes(j))) : X;
        const n = Xd.length, k = Xd[0].length;
        const { means, stds } = this._colMeansStd(Xd);
        const Z = this._standardize(Xd, means, stds);
        const yMean = this._mean(y), stdY = this._std(y);
        const yc = y.map(v => v - yMean);
        Amax = Math.min(Amax || k, k);

        // LOOCV 选 A（全局预处理，近似但稳定）
        let bestA = 1, bestQ2 = -Infinity;
        const sst = this._sst(y);
        if (n >= 10) {
            const sseByA = new Array(Amax + 1).fill(0);
            for (let i = 0; i < n; i++) {
                const Zt = [], yct = [];
                for (let r = 0; r < n; r++) if (r !== i) { Zt.push(Z[r]); yct.push(yc[r]); }
                for (let A = 1; A <= Amax; A++) {
                    const core = this._plsCore(Zt, yct, A);
                    let pred = yMean;
                    for (let j = 0; j < k; j++) pred += core.Bstd[j] * Z[i][j];
                    const e = y[i] - pred; sseByA[A] += e * e;
                }
            }
            for (let A = 1; A <= Amax; A++) { const q = sst > 0 ? 1 - sseByA[A] / sst : 0; if (q > bestQ2) { bestQ2 = q; bestA = A; } }
        }

        const core = this._plsCore(Z, yc, bestA);
        const coefs = new Array(k);
        let intercept = yMean;
        for (let j = 0; j < k; j++) { coefs[j] = core.Bstd[j] / stds[j]; intercept -= coefs[j] * means[j]; }
        const yhat = Xd.map(r => { let s = intercept; for (let j = 0; j < k; j++) s += coefs[j] * r[j]; return s; });
        const m = this._metrics(y, yhat, k);
        m.q2 = bestQ2;
        const stdCoef = coefs.map((c, j) => stdY > 0 ? c * stds[j] / stdY : 0);

        // 将被剔除的常数特征列补回（系数0，均值0，std1）
        const full = this._unDrop({ coefs, means, stds, stdCoef }, drop, X[0].length);
        return { type: 'pls', intercept, coefs: full.coefs, means: full.means, stds: full.stds, stdCoef: full.stdCoef, A: bestA, yhat, metrics: m, n, k, drop };
    },

    // 用训练好的模型对一条完整特征向量预测（缺失值回退训练均值）
    _predictFromModelVec(xrow, mdl) {
        let s = mdl.intercept;
        const nf = mdl.coefs.length;
        for (let j = 0; j < nf; j++) {
            let v = xrow[j];
            if (typeof v !== 'number' || isNaN(v)) {
                v = (mdl.imputeMeans && typeof mdl.imputeMeans[j] === 'number') ? mdl.imputeMeans[j] : (mdl.means[j] || 0);
            }
            s += mdl.coefs[j] * v;
        }
        return s;
    },

    // 时间序列交叉验证（walk-forward）：按时间先后滚动，前段训练、后段测试，池化测试误差 → Q²
    // 更贴近"用过去数据预测下一小时"的真实能力（留一交叉会偷看未来，高估模型）
    _timeCvQ2(X, y, kind, Amax) {
        const n = X.length;
        if (n < 30) return null;
        const b1 = Math.max(12, Math.round(n * 0.7));
        const b2 = Math.max(b1 + 8, Math.round(n * 0.85));
        const blocks = [[b1, b2], [b2, n]];
        let sse = 0, sst = 0, cnt = 0;
        for (const [s, e] of blocks) {
            if (e - s < 5) continue;
            const Xtr = X.slice(0, s), ytr = y.slice(0, s);
            const Xte = X.slice(s, e), yte = y.slice(s, e);
            let mdl = null;
            if (kind === 'mlr') mdl = this.trainMlr(Xtr, ytr);
            else mdl = this.trainPls(Xtr, ytr, Amax || Math.min(8, Xtr[0].length));
            if (!mdl) continue;
            const ym = this._mean(yte);
            for (let i = 0; i < yte.length; i++) {
                const e = yte[i] - this._predictFromModelVec(Xte[i], mdl);
                sse += e * e; sst += (yte[i] - ym) ** 2; cnt++;
            }
        }
        if (cnt < 10) return null;
        return sst > 0 ? 1 - sse / sst : 0;
    },

    // 找出零方差（常数）列：训练时剔除，避免设计矩阵奇异
    _constCols(X) {
        const k = X[0].length, n = X.length;
        const cols = [];
        for (let j = 0; j < k; j++) {
            const first = X[0][j];
            let constant = true;
            for (let i = 1; i < n; i++) { if (X[i][j] !== first) { constant = false; break; } }
            if (constant) { cols.push(j); continue; }
            // 与前面已保留列完全重复（共线）也剔除，避免正规方程奇异
            for (let p = 0; p < j; p++) {
                if (cols.includes(p)) continue;
                let dup = true;
                for (let i = 0; i < n; i++) { if (X[i][j] !== X[i][p]) { dup = false; break; } }
                if (dup) { cols.push(j); break; }
            }
        }
        return cols;
    },

    // 将被剔除的常数列补回模型数组（占位系数0、均值0、std1、标准化系数0）
    _unDrop(model, drop, fullK) {
        if (!drop || drop.length === 0) return model;
        const out = { coefs: new Array(fullK).fill(0), means: new Array(fullK).fill(0),
                      stds: new Array(fullK).fill(1), stdCoef: new Array(fullK).fill(0) };
        let idx = 0;
        for (let j = 0; j < fullK; j++) {
            if (drop.includes(j)) continue;
            out.coefs[j] = model.coefs[idx]; out.means[j] = model.means[idx];
            out.stds[j] = model.stds[idx]; out.stdCoef[j] = model.stdCoef[idx];
            idx++;
        }
        return out;
    }
};

// 页面加载完成后初始化
document.addEventListener('DOMContentLoaded', () => App.init());
