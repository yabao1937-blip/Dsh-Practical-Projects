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
        // 重介精煤灰分来源：true=手动值优先；false=计算（502在线 + 密度仿真）。
        // 2026-09-12 新增：此前只要填过手动值就**永久**压住计算值 ——
        // 于是"把密度计调到建议密度 → 重介灰分随之变化 → 总灰分达标"这条链走不通。
        heavyAshManualOn: false,
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
            // 实测密度计（2026-09 新增）：操作员/化验室实测值，作为比在线密度计更权威的参考。
            // **仅展示**：不参与建议密度/灰分公式/模型（密度指导只对"调整量"敏感，对密度计读数不敏感），
            // 目前只用于：① 两个界面展示；② 与在线密度计的偏差；③ 记入密度决策日志 ctx 供日后标定。
            density_actual: { manual: null },
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
        // —— 以下启动期默认值补齐/迁移仅写本地(saveStore(true)),不镜像到服务器:
        //    旧浏览器/全新 profile 打开页面时,不得用本地旧数据覆盖服务器新数据 ——
        // 老数据无 amountInputs 时补默认（录入+输入）
        if (!this.store.amountInputs) {
            this.store.amountInputs = {
                totalAmount:  { mode: 'manual', manual: 450 },  // 总精煤量 501+502 皮带 t/h
                floatAmount:  { mode: 'auto',   manual: null }, // 浮精量，自动取表2最新值
                coarseAmount: { mode: 'manual', manual: 40 },   // 粗精煤泥量 t/h
                denseAmount:  { mode: 'calc',   manual: null }, // 重介精煤量 = 总 − 浮 − 粗
            };
            this.saveStore(true);
        }
        // 老数据无 ashInputs 时补默认（任务三）
        if (!this.store.ashInputs) {
            this.store.ashInputs = { totalAsh: { mode: 'auto', manual: null } };
            this.saveStore(true);
        }
        // 任务四：录入层字段补齐（手动>录入>默认）
        if (this.store.amountInputs && this.store.amountInputs.totalAmount && this.store.amountInputs.totalAmount.entry === undefined) {
            this.store.amountInputs.totalAmount.entry = null;
            this.saveStore(true);
        }
        if (this.store.ashInputs && this.store.ashInputs.totalAsh && this.store.ashInputs.totalAsh.entry === undefined) {
            this.store.ashInputs.totalAsh.entry = null;
            this.saveStore(true);
        }
        // 在线仪表可修改项补齐
        if (!this.store.coarseAshInput) { this.store.coarseAshInput = { manual: null }; this.saveStore(true); }
        if (!this.store.heavyAshInput) { this.store.heavyAshInput = { manual: null }; this.saveStore(true); }
        if (this.store.heavyAshManualOn == null) {
            // 迁移：升级前"有手动值就是手动优先"。为不悄悄改变现场行为，按这个事实初始化一次；
            // 之后由在线仪表行的 手动/计算 下拉决定。
            const hv = this.store.heavyAshInput && this.store.heavyAshInput.manual;
            this.store.heavyAshManualOn = (typeof hv === 'number' && isFinite(hv));
            this.saveStore(true);
        }
        if (this.store.ashTarget == null) { this.store.ashTarget = 8.50; this.saveStore(true); }
        if (!this.store.coarseTrainRange) { this.store.coarseTrainRange = 'jun_jul'; this.saveStore(true); }
        // 出厂烘焙模型无 range 字段时回填当前训练范围
        if (this.store.coarseModel && !this.store.coarseModel.range) {
            this.store.coarseModel.range = this.store.coarseTrainRange || 'jun_jul';
            this.saveStore(true);
        }
        // 手动有效期机制状态补齐
        if (!this.store.autoState) { this.store.autoState = {}; this.saveStore(true); }
        // 采样记录与达标容差补齐
        if (!this.store.heavySamples) { this.store.heavySamples = []; this.saveStore(true); }
        if (this.store.ashTargetTol == null) { this.store.ashTargetTol = 0.1; this.saveStore(true); }
        // 旧默认0.3迁移为专家经验版默认0.1（专家调整表在0.15%~0.25%区间内）
        if (this.store.ashTargetTol === 0.3) { this.store.ashTargetTol = 0.1; this.saveStore(true); }
        // 密度计：纯手动录入（每次采样时录入实际密度计数值），系统只给"建议密度"展示，不自动写入密度计
        if (this.store.densityAutoOn == null) { this.store.densityAutoOn = false; this.saveStore(true); }
        if (!this.store.guideScheme) { this.store.guideScheme = 'total'; this.saveStore(true); }
        this._syncGuideSchemeBtn();
        // 总灰分修改开关：默认关=公式计算级别；关闭状态下清掉遗留的手动总灰分
        if (this.store.totalAshManualOn == null) { this.store.totalAshManualOn = false; this.saveStore(true); }
        if (!this.store.totalAshManualOn) this.setAshInput('totalAsh', { manual: null });
        this._syncTotalAshBtn();
        if (!this.store.floatAshInput) { this.store.floatAshInput = { manual: null }; this.saveStore(true); }
        if (!this.store.coarseCalc) { this.store.coarseCalc = { screen315: null, waterUnder: null }; this.saveStore(true); }
        if (!this.store.instrumentInputs) {
            this.store.instrumentInputs = {
                ash_501: { manual: null }, ash_502: { manual: null },
                density_actual: { manual: null },
                scale_501: { manual: null }, scale_502: { manual: null },
                density: { manual: null }, level_tail: { manual: null },
            };
            this.saveStore(true);
        }
        this.bindNav();        this.startClock();

        // 「从服务器恢复数据」按钮:仅后端托管(http://)模式显示
        const pullBtn = document.getElementById('btn-pull-server');
        if (pullBtn && window.location.protocol.startsWith('http') && window.Api) {
            pullBtn.style.display = '';
        }
        // 镜像合并的收尾：页面隐藏/卸载前把待发的镜像立刻冲出去，
        // 否则用户"改完就关页面"会丢掉最后那次镜像（本地 localStorage 仍有，但服务器没有）。
        const flushMirror = () => this._flushMirror(true);
        window.addEventListener('pagehide', flushMirror);
        window.addEventListener('beforeunload', flushMirror);
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') flushMirror();
            else this.retryMirrorIfPending();          // 切回前台：补发之前失败的镜像
        });
        window.addEventListener('online', () => this.retryMirrorIfPending());

        // http 模式:服务器数据更多时自动反向同步(旧浏览器自愈,防止镜像覆盖服务器新数据)
        this.autoPullIfStale();
        // 补发上次遗留的待发镜像（含"关页面时那次请求被中断"的情况——发送前就写了副本）。
        // 放在 autoPullIfStale **之后**：若刚刚做了反向同步，服务器数据已成为权威，
        // 旧快照已过时（_applyServerState 会清槽），不应再推回去。
        this.retryMirrorIfPending();
        // 补记历史密度决策的灰分响应(页面打开时兜底一次)
        this._completeDensityDecisionResponses();
        // 存量 influence_value 一次性重算（旧口径写死 8.50 → 当前口径），__fixes.influence 标记只跑一次
        this.recomputeInfluenceValues();

        this.checkAlerts();
        // 只初始化首页（当前可见页）
        this.initPage('page-overview');
    },

    // 用服务器 state 覆盖本地数据集合(纯本地键保留),供手动/自动两条路径复用
    // __merged / __fixes 必须保留：它们是「本地已执行过的一次性动作」标记，
    // 被服务器数据清掉会导致种子重灌或历史清理重复执行。
    _applyServerState(st) {
        const localOnly = ['manualEntries', 'importLogs', 'alerts', 'regressionModels',
                           'coarseModelHistory', 'heavySamples', 'rawSlime', '__merged', '__fixes'];
        const keep = {};
        localOnly.forEach(k => { if (this.store[k] !== undefined) keep[k] = this.store[k]; });
        this.store = Object.assign({}, this.store, st, keep);
        // 刚从服务器拉了权威数据 → 之前没送达的旧快照已过时（它的内容已被本次覆盖吸收），
        // 留着只会在下次启动被推回去、把刚同步下来的服务器数据洗回旧状态。
        this._clearPendingSlot(null);
        this.saveStore();
        this._onExternalInput();
        this.refreshAllPages();
    },

    // 「测量记录向量」：与后端防回退守卫的判据同口径（migrate._record_vector）。
    // 只数三类测量记录（coal_records 的三个 category），不含 manualEntries/importLogs/alerts
    // —— 那些是用户可清空的日志类数据，纳入比较会把正常清空误判成"服务器更新"。
    // store.calcLogs 同时装 ash_density 与其它补录，这里只取 ash_density 与后端对齐。
    _measureVector(store) {
        const s = store || this.store || {};
        return {
            coarse: (s.coarseCoal || []).length,
            float: (s.floatCoal || []).length,
            ash_density: (s.calcLogs || []).filter(l => l && l.calc_type === 'ash_density').length,
        };
    },

    // http 模式加载：服务器在**每一类**测量记录上都不少于本地、且至少一类更多 → 自动拉取。
    // 旧判据是「三数组总数多者胜」，与守卫（按记录数逐表比较）不同口径，可能出现
    // 「自动拉取判定服务器更新并覆盖本地」而「镜像被守卫拒绝」的静默不一致；
    // 且总数相等也可能已经回退（一类多、另一类少）。
    // 任一类本地更多 → 不动本地（本地为最新）。
    async autoPullIfStale() {
        if (!window.location.protocol.startsWith('http') || !window.Api) return;
        try {
            const st = await window.Api.getState();
            if (!st || !Array.isArray(st.coarseCoal)) return;
            const sv = this._measureVector(st), lv = this._measureVector(this.store);
            const keys = Object.keys(lv);
            const serverWinsAll = keys.every(k => sv[k] >= lv[k]);
            const serverMore = keys.some(k => sv[k] > lv[k]);
            const localMore = keys.some(k => lv[k] > sv[k]);
            if (serverWinsAll && serverMore) {
                const fmt = v => keys.map(k => `${k} ${v[k]}`).join(' / ');
                this._applyServerState(st);
                this.showToast(`检测到服务器有较新数据（${fmt(sv)} > 本地 ${fmt(lv)}），已自动同步`, 'info');
            } else if (serverMore && localMore) {
                // 两侧各有更全的部分：不自动覆盖，避免把本地较新的那类记录洗掉
                console.warn('服务器与本地各有更新的部分，已跳过自动同步', { server: sv, local: lv });
            }
        } catch (e) { /* 后端未启动时静默 */ }
    },

    // 从服务器整库恢复（仅 http:// 后端托管模式,手动入口）：
    // 双轨架构中 localStorage 为主存储、saveStore 单向镜像 PUT /state；服务器侧产生的
    // 新数据（自动化导入脚本、API 重训练）本地看不到，此入口提供反向同步。
    async pullFromServer() {
        if (!window.location.protocol.startsWith('http') || !window.Api) {
            this.showToast('仅在后端托管(http://)模式下可用', 'warning');
            return;
        }
        if (!window.confirm('将用服务器数据覆盖本地浏览器数据（本地未同步到服务器的改动会丢失），是否继续？')) return;
        try {
            const st = await window.Api.getState();
            if (!st || !Array.isArray(st.coarseCoal)) throw new Error('服务器 state 结构异常');
            this._applyServerState(st);
            this.showToast(`已从服务器恢复：粗精煤泥 ${this.store.coarseCoal.length} 条 / 浮精 ${this.store.floatCoal.length} 条 / 灰分密度 ${(this.store.calcLogs || []).length} 条`, 'success');
        } catch (e) {
            console.warn('pullFromServer 失败:', e);
            this.showToast('从服务器恢复失败: ' + e.message, 'error');
        }
    },

    // 持久化：保存到 localStorage（http 访问时额外镜像到后端 /state）
    // skipMirror=true 仅写本地：用于启动期默认值补齐/种子灌入——旧本地数据
    // 不能借启动流程镜像覆盖服务器上的新数据（服务器侧导入/训练成果）。
    saveStore(skipMirror) {
        let body = null;
        try {
            body = JSON.stringify(this.store);      // 一次序列化，本地写与镜像复用
            localStorage.setItem('dmcs_store', body);
        } catch (e) {
            console.warn('localStorage 保存失败:', e);
        }
        if (skipMirror || body === null) return;
        if (!window.Api || !window.location.protocol.startsWith('http')) return;
        this._scheduleMirror(body);                 // 镜像合并后发送（见 _scheduleMirror）
    },

    // ============================================================
    //  整库镜像的合并（2026-09）
    //  背景：saveStore 有 60+ 调用点，其中自动执行器与粗灰 EMA 都是 1 秒定时器。
    //  原实现每次 saveStore 都整库 PUT /state —— 单次快照实测 130.3 KB，
    //  即"自动执行期间每秒往服务器推 130 KB"，且并发下还会与 GET 抢 SQLite 写锁。
    //  改法：
    //    · localStorage 仍然**立即**写（它是主存储，不能延后）；
    //    · 镜像合并到 MIRROR_DEBOUNCE_MS 一次，只发最后一次内容（每次都是全量快照，
    //      所以"丢掉中间态"不丢数据）；
    //    · 内容与上次已发送的完全相同则跳过；
    //    · 页面隐藏/卸载前立刻冲一次（keepalive），避免"刚改完就关页面"服务器没收到。
    //  安全前提：镜像本身是尽力而为的副本，真正的权威数据在 localStorage 与服务端库。
    //  失败处置（2026-09 二次修订）：发送失败**不再静默**——留下持久化待发副本
    //  （localStorage: dmcs_mirror_pending，单槽，因为每次都是全量快照）、
    //  提示一次、并在 断网恢复 / 切回前台 / 下次保存 时重试；成功后清除。
    //  历史教训：这里原来是 `.catch(() => {})`，keepalive 超限（Chrome 64 KiB 上限）
    //  与 stale 守卫拒绝都被吞掉，界面一切正常而服务器始终收不到数据。
    // ============================================================
    MIRROR_DEBOUNCE_MS: 2000,
    // Chrome/Edge 对 keepalive 请求体的硬上限是 64 KiB —— 注意是**字节**，
    // 而整库快照以中文为主（JSON.stringify 不转义非 ASCII，1 个中文字符 = 3 字节 UTF-8）。
    // 用 body.length（字符数）去卡会得出"以为装得下、实际 3 倍超限"的结论，
    // 于是又变成 promise 拒绝 → 之前被 .catch 静默吞掉，表现为"冲了但服务器没收到"。
    // 这里按**字节**判断（Blob.size），阈值留到 48 KB。
    MIRROR_KEEPALIVE_MAX: 48000,
    _mirrorTimer: null,
    _mirrorPending: null,
    _mirrorSent: null,
    _mirrorStats: { scheduled: 0, sent: 0, skippedSame: 0, keepalive: 0, noKeepalive: 0 },

    // 待发镜像的持久化副本（单槽）。每次镜像都是**全量快照**，所以只需留最新一份。
    // 存 localStorage 而不是 store 里，避免它自己又被塞进镜像载荷里。
    // 槽内结构 { wall, stamp, body }，多出来的两个字段是为了多标签页安全：
    //   · wall  —— 写入时刻，避免旧标签页把新标签页刚写的快照覆盖掉（旧覆盖新）；
    //   · stamp —— 本次写入的唯一标记，只有写下它的那次请求才允许清除它：
    //              否则"B 标签页发送成功"会把 A 标签页尚未送达的唯一副本删掉（丢数据）。
    MIRROR_PENDING_KEY: 'dmcs_mirror_pending',
    mirrorStatus: { pending: false, lastError: null, lastOkAt: null, failures: 0, rejected: 0 },
    _mirrorInFlight: false,

    _readPendingSlot() {
        try {
            const raw = localStorage.getItem(this.MIRROR_PENDING_KEY);
            if (!raw) return null;
            const s = JSON.parse(raw);
            return (s && typeof s.body === 'string') ? s : null;
        } catch (e) { return null; }     // 旧格式(裸 body)或损坏：当作没有
    },
    _writePendingSlot(body, wall, stamp) {
        try {
            const cur = this._readPendingSlot();
            if (cur && cur.wall > wall) return false;    // 已有更新的快照，不覆盖
            localStorage.setItem(this.MIRROR_PENDING_KEY,
                JSON.stringify({ wall: wall, stamp: stamp, body: body }));
            return true;
        } catch (e) { return false; }    // 配额不足等：忽略，内存里仍有一份
    },
    _clearPendingSlot(stamp) {
        try {
            const cur = this._readPendingSlot();
            if (!cur) return;
            if (stamp && cur.stamp !== stamp) return;    // 不是自己写的那份：不动别人的
            localStorage.removeItem(this.MIRROR_PENDING_KEY);
        } catch (e) { /* 忽略 */ }
    },
    // keepalive 的判据必须是**字节**（见 MIRROR_KEEPALIVE_MAX 注释）
    _mirrorBytes(body) {
        try { return new Blob([body]).size; } catch (e) { return body.length; }
    },

    _scheduleMirror(body) {
        this._mirrorPending = body;
        this._mirrorStats.scheduled++;
        if (this._mirrorTimer) return;
        this._mirrorTimer = setTimeout(() => {
            this._mirrorTimer = null;
            this._flushMirror(false);
        }, this.MIRROR_DEBOUNCE_MS);
    },

    // immediate=true 表示走"页面隐藏/卸载"路径：能带 keepalive 就带（否则卸载会中断请求）
    _flushMirror(immediate) {
        if (this._mirrorTimer) { clearTimeout(this._mirrorTimer); this._mirrorTimer = null; }
        // 取本次挂起的，或上次没送达留下来的（都是全量快照；有挂起的就用挂起的）
        const slot = this._readPendingSlot();
        const body = this._mirrorPending || (slot && slot.body);
        if (!body) return;
        // 内容与"上次已处理过的那份"（成功送达，或已被守卫明确拒绝）逐字节相同 → 不必再发。
        // 判据必须放在**看来源之前**：saveStore 每次都会置 _mirrorPending，
        // 若只在"来自槽"时才判重，正常保存路径上的重复内容永远会重发
        // （被守卫拒绝的整库会被反复重发，每次 175 KB）。
        if (body === this._mirrorSent) {
            this._mirrorPending = null; this._mirrorStats.skippedSame++; return;
        }
        this._mirrorPending = null;
        this._mirrorStats.sent++;
        const bytes = this._mirrorBytes(body);
        const canKeepalive = !!immediate && bytes <= this.MIRROR_KEEPALIVE_MAX;
        if (canKeepalive) this._mirrorStats.keepalive++; else this._mirrorStats.noKeepalive++;
        // **发送前**先落待发副本（关键）：页面关闭/切后台时这次请求会被浏览器中断，
        // promise 永远不会回调 —— 靠回调里再补救根本来不及，那就是"关页面丢最后一笔"。
        // 写在这里的语义是"本地已保存、服务器尚未确认"，成功或被守卫拒绝后再清掉。
        const wall = Date.now();
        const stamp = wall + '-' + Math.random().toString(36).slice(2, 8);
        const hadSlot = !!slot;
        const wrote = this._writePendingSlot(body, wall, stamp);
        if (wrote || hadSlot) this.mirrorStatus.pending = true;
        this._mirrorInFlight = true;
        let res;
        try {
            res = window.Api.putStateBody(body, { keepalive: canKeepalive });
        } catch (e) {
            res = Promise.resolve({ ok: false, error: String((e && e.message) || e) });
        }
        Promise.resolve(res).then(r => {
            this._mirrorInFlight = false;
            if (r && r.ok) {
                this._mirrorSent = body;
                this._clearPendingSlot(stamp);          // 只清自己写的那一份
                this.mirrorStatus.pending = !!this._readPendingSlot();
                this.mirrorStatus.lastOkAt = Date.now();
                if (this.mirrorStatus.failures) {
                    this.showToast('本地数据已同步到服务器', 'success');
                    this.mirrorStatus.failures = 0;
                }
                return;
            }
            // 失败**不再静默**：记状态、提示一次（避免刷屏）。分两类（2026-09）：
            //  · rejected = 服务器**明确拒绝**（守卫说服务器记录更多）。本地快照本就不该覆盖它，
            //    重试结果必然相同 → 清掉自己那份待发副本（不清的话每次 online/切前台都会
            //    重发一次注定被拒的 175 KB），并记入 _mirrorSent 以免内容不变时反复重发。
            //    随后**真的去反向同步一次**（autoPullIfStale 原本只在启动跑一次，
            //    所以旧提示语"正在自动反向同步"是假的，这里让它变成真的）。
            //  · 其它 = 传输失败 / 服务器瞬时异常（后端未启动、keepalive 超限、断网、SQLite 写锁
            //    超时）：待发副本已在发送前写好，留着重试。
            this.mirrorStatus.lastError = (r && (r.reason || r.error)) || 'unknown';
            this.mirrorStatus.failures++;
            if (r && r.denied) {
                // 权限问题（写接口要 token）：留待发副本（token 修正后能补上），
                // 但提示文案必须说清是权限，而不是"服务器数据更新"。
                this.mirrorStatus.pending = true;
                console.warn('写接口拒绝（可能需要 X-DMCS-Token）:', this.mirrorStatus.lastError);
                if (this.mirrorStatus.failures === 1) {
                    this.showToast('服务器拒绝写入（写接口需要访问令牌）：请确认页面来自服务器地址 '
                        + 'http://<服务器IP>:8000/ 打开；本地副本已保留，恢复权限后会自动补发', 'error');
                }
                return;
            }
            if (r && r.rejected) {
                this._mirrorSent = body;
                this._clearPendingSlot(stamp);
                this.mirrorStatus.pending = !!this._readPendingSlot();
                this.mirrorStatus.rejected++;
                console.warn('服务器拒绝了本次整库镜像(本地未覆盖服务器):', this.mirrorStatus.lastError);
                if (this.mirrorStatus.failures === 1) {
                    this.showToast('服务器数据比本地更新，本次快照未覆盖服务器；正在尝试从服务器同步', 'info');
                    // 让上面这句话变成真的：autoPullIfStale 原本只在启动时跑一次。
                    // 只在一次失败 streak 的第一次触发，避免持续被拒时反复拉取。
                    this.autoPullIfStale();
                }
                return;
            }
            this.mirrorStatus.pending = true;
            console.warn('镜像未送达服务器:', this.mirrorStatus.lastError);
            if (this.mirrorStatus.failures === 1) {
                this.showToast('本地数据未能同步到服务器，将在网络恢复或切回页面时自动重试', 'warning');
            }
        }).catch(() => { this._mirrorInFlight = false; });
    },

    // 失败后的重试入口（断网恢复 / 切回前台 / 下次保存时都会调用）。
    // 发送中的请求不算"待重试"，否则 online 事件会和正在飞的请求重复发一次整库。
    retryMirrorIfPending() {
        if (this._mirrorInFlight) return;
        const slot = this._readPendingSlot();
        if (slot || this._mirrorPending) this._flushMirror(false);
    },

    // 一次性数据迁移：清空旧数据并载入合并系统种子数据（__merged 标记版本）
    applySeedStore() {
        const seed = window.DMCS_SEED_STORE;
        if (seed) {
            this.store = JSON.parse(JSON.stringify(seed));
            this.saveStore(true);   // 种子灌入不镜像:首次运行时服务器可能有真实数据
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
        // 简报页已生成过才重渲染（没生成过就别在每次数据变动时白算一遍 263 行）
        if (typeof BriefPage !== 'undefined' && BriefPage.result) {
            BriefPage.render();
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
                this.goToPage(item.dataset.page);
            });
        });
    },

    // 以代码方式切页（弹窗里的「在简报页查看全部」等入口用），与点击导航行为一致
    goToPage(pageId) {
        const item = document.querySelector(`.nav-item[data-page="${pageId}"]`);
        const sec = pageId ? document.getElementById(pageId) : null;
        if (!item || !sec) return;
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        sec.classList.add('active');
        // 延迟初始化或刷新图表
        setTimeout(() => this.initPage(pageId), 50);
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
                case 'page-brief':
                    if (typeof BriefPage !== 'undefined') BriefPage.init();
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
                case 'page-brief':
                    // 切回简报页时重渲染：既刷新数据，也重新执行"滚到最新一行"。
                    // （页面隐藏时 scrollHeight/clientHeight 为 0，渲染阶段的自动滚动会失效，
                    //   所以必须在页面已可见之后再滚一次）
                    if (typeof BriefPage !== 'undefined' && BriefPage.result) {
                        BriefPage.render();
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
            heavyAshManualOn: false,
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
                density_actual: { manual: null },
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
            // 任务三：重介精煤灰分用反推值（手动采样 > 502在线 > 默认7.9 兜底），三量走量数据面板
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

    // 「影响值」唯一口径（浮精页图表/表格/详情 + 存量重算都走这里）。
    // 重介灰分按该点时刻动态取（getAshByTime），超窗才回退 8.50；
    // 旧实现有两套：导入时按写死的 8.50 存进 influence_value，渲染时又按时刻重算，
    // 同一个数在表格与图表上不一致，重介灰分默认口径改成「502在线/7.9」后差距更大。
    INFLUENCE_HEAVY_AMT: 250,
    influenceOfFloat(rec) {
        const amt = this.INFLUENCE_HEAVY_AMT;
        const hAsh = this.getAshByTime(rec.timestamp) ?? 8.50;   // 超窗记缺失回退默认
        const base = this.calcTotalAsh(hAsh, amt, 0, 0, 0, 0);
        const withF = this.calcTotalAsh(hAsh, amt, rec.ash_content, rec.coal_amount, 0, 0);
        return +(withF - base).toFixed(3);
    },
    // 粗精煤泥记录的影响值：同一基准下把粗精煤泥作为第三组分计入
    influenceOfCoarse(rec) {
        const amt = this.INFLUENCE_HEAVY_AMT;
        const hAsh = this.getAshByTime(rec.timestamp) ?? 8.50;
        const base = this.calcTotalAsh(hAsh, amt, 0, 0, 0, 0);
        const withC = this.calcTotalAsh(hAsh, amt, 0, 0, rec.ash_content, rec.coal_amount);
        return +(withC - base).toFixed(3);
    },

    // 一次性迁移：把存量 influence_value 从旧口径（写死 8.50）重算到当前口径。
    // 显示层已改为实时重算、不再读它，但该字段仍随整库镜像对外提供，留着旧值会持续误导下游。
    recomputeInfluenceValues() {
        if (this.store.__fixes && this.store.__fixes.influence) return 0;   // 已执行过
        let n = 0;
        (this.store.floatCoal || []).forEach(r => {
            if (typeof r.ash_content !== 'number' || typeof r.coal_amount !== 'number') return;
            const v = this.influenceOfFloat(r);
            if (r.influence_value !== v) { r.influence_value = v; n++; }
        });
        (this.store.coarseCoal || []).forEach(r => {
            if (typeof r.ash_content !== 'number' || typeof r.coal_amount !== 'number') return;
            const v = this.influenceOfCoarse(r);
            if (r.influence_value !== v) { r.influence_value = v; n++; }
        });
        this.store.__fixes = Object.assign({}, this.store.__fixes, { influence: 1 });
        this.saveStore();
        if (n) console.info(`[一次性修复] 重算 influence_value（旧口径 8.50 → 当前口径）${n} 条`);
        return n;
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
    // 501/502 皮带灰分仪默认值。2026-09 工艺确认:501=总混配皮带(重介+浮精+粗,
    // 灰分应略高于502);502=仅重介精煤(在线重介灰分,实测均值≈7.85)。
    // 旧默认 8.52/10.68 方向颠倒,已校正。
    ASH_METER_DEFAULT: { '501': 8.8, '502': 7.9 },
    // 在线仪表全部默认值（PLC未接入时的死数据；PLC接入后替换）
    INSTRUMENT_DEFAULT: {
        ash_501: 8.8, ash_502: 7.9, scale_501: 268.5, scale_502: 235.2,
        density: 1.450, level_tail: 55, float_ash: 9.85,
        // density_actual **刻意没有默认值**：实测值只应来自人工录入，
        // 给它编一个默认（例如取在线值）会让人分不清哪条是真实测。
    },

    // ---- P0 安全守卫（2026-09-12，三模型会诊结论）----
    // 逐步建议：发布的建议值不超过 maxStep（钳制），并给出完整目标与步数。
    DENSITY_STEPWISE: true,
    // 最短驻留：密度变动后这段时间内不再出新建议（过程到位 + 化验周期）
    DENSITY_DWELL_MS: 30 * 60000,
    // 占位值判定：手动灰分等于目标值且超过这段时间未更新 → 视为占位值，不给建议
    PLACEHOLDER_STALE_MS: 24 * 3600 * 1000,

    // 「驱动数据键」：当前偏差是由哪一份数据算出来的。键不变 ⇒ 建议不该重复给。
    // 组成：方案 + 实际值/重介值 + 该值的来源时间戳（手动=manualAt；录入=最新记录时间）。
    _densityDriveKey(actualTotal, heavyAsh, scheme) {
        const cfg = (this.store.instrumentInputs && this.store.instrumentInputs) || {};
        const totalManual = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        const heavyManual = this.store.heavyAshInput || {};
        let stamp = 0, src = 'calc';
        if (scheme === 'heavy') {
            if (this.heavyAshSource() === 'manual') { src = 'heavyManual'; stamp = heavyManual.manualAt || 0; }
        } else if (this.store.totalAshManualOn && typeof totalManual.manual === 'number') {
            src = 'totalManual'; stamp = totalManual.manualAt || 0;
        }
        if (!stamp) {
            // 非手动来源：用最新数据时间戳（粗精煤泥记录/补录），没有则用值本身
            let ts = '';
            (this.store.coarseCoal || []).forEach(r => { if (r && r.timestamp && r.timestamp > ts) ts = r.timestamp; });
            stamp = ts ? new Date(String(ts).replace(' ', 'T')).getTime() : 0;
        }
        const v = (scheme === 'heavy' ? heavyAsh : actualTotal);
        return `${scheme}|${src}|${stamp || 0}|${v == null ? 'na' : (+v).toFixed(3)}`;
    },

    // 该记录一份"已按某份数据动作过"的闩锁（在操作员/自动执行真正改了密度时调用）
    _latchDensityAction() {
        const st = this.store;
        if (!st.densityActionLatch) st.densityActionLatch = null;
        if (this._lastDriveKey) {
            st.densityActionLatch = { key: this._lastDriveKey, at: Date.now(), rho: this.resolveDensity() };
            this.saveStore();
        }
    },
    DENSITY_ACTUAL: {
        hardMin: 1.30, hardMax: 1.65,     // 之外**拒收**（手滑多打一位）
        softMin: 1.35, softMax: 1.60,     // 之外但在硬限内 → 质量状态标黄"偏离"
        devWarn: 0.02,                    // |实测−在线| 超过它 → 卡片上标黄（"已知密度计存在误差"看的就是这个）
    },

    // 实测密度 vs 在线密度计的偏差（两者都有效才算）。**只用于展示**。
    densityActualDeviation() {
        const n = v => (typeof v === 'number' && isFinite(v)) ? v : null;
        let actual = null, online = null;
        try { actual = n(this.resolveInstrument('density_actual')); } catch (e) { /* 未录入 */ }
        try { online = n(this.resolveInstrument('density')); } catch (e) { /* 仪表未接 */ }
        if (actual == null || online == null) return { actual: actual, online: online, dev: null };
        return { actual: actual, online: online, dev: +(actual - online).toFixed(3) };
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
            // 仿真：密度变化→灰分测量变化（基准点线性化）
            // P0 门控（2026-09-12）：只在密度**有真实来源**时才叠加仿真增量。
            // 否则默认密度 1.45（距基准 1.49 有 0.04）会凭空把 502 的 7.9% 变成 6.57% ——
            // 那不是测量，是编造出来的偏差，会直接进总灰分与建议密度。
            // 同时限幅 |Δ灰分| ≤ 0.5，避免远离工作点时仿真线性外推给出荒唐值。
            const rho = this.resolveInstrument('density');
            const rhoIsReal = this.instrumentLayer('density') !== '默认(仪表)';
            let d = rhoIsReal ? (rho - this.getSimBaseRho()) / this.DENSITY_GUIDE.simK : 0;
            if (d > 0.5) d = 0.5; else if (d < -0.5) d = -0.5;
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
        } else if (id === 'density_actual') {
            // 录入：手工补录的"实测密度计"最新一条（calc_type=density_actual）。
            // 注意与在线密度计完全分开：它读 density_meter / ash_density，本行只读 density_actual，
            // 两边不会互相污染。
            const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'density_actual');
            const R = this.DENSITY_ACTUAL;
            for (let i = logs.length - 1; i >= 0; i--) {
                try {
                    const v = +JSON.parse(logs[i].input_json || '{}').value;
                    if (isFinite(v) && v >= R.hardMin && v <= R.hardMax) { entry = v; break; }
                } catch (e) { /* 忽略坏记录 */ }
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
            if (id === 'density_actual') {
                const R = this.DENSITY_ACTUAL;
                return (this.store.calcLogs || []).some(l => {
                    if (l.calc_type !== 'density_actual') return false;
                    try {
                        const v = +JSON.parse(l.input_json || '{}').value;
                        return isFinite(v) && v >= R.hardMin && v <= R.hardMax;
                    } catch (e) { return false; }
                });
            }
            if (id === 'level_tail') {
                const list = this.store.magneticTail || [];
                return list.some(m => typeof m.level === 'number' && m.level > 0);
            }
            return false;
        })();
        // 实测密度计没有"仪表默认值"这一层，所以叫"未录入"，不要写成"默认(仪表)"
        if (id === 'density_actual') return hasEntry ? '录入' : '未录入';
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
            // P0②：密度真的被改了 → 记闩锁（同一份驱动数据不再重复给建议），并记下变动时刻
            if (id === 'density' && typeof patch.manual === 'number' && isFinite(patch.manual)) {
                this.store.densityLastMoveAt = Date.now();
                this._latchDensityAction();
            }
        }
        // 决策日志:操作员手动设定密度(有效范围内)——记录决策上下文,45分钟后自动补记灰分响应
        if (id === 'density' && !patch.autoExec && typeof patch.manual === 'number'
            && isFinite(patch.manual) && patch.manual >= 1.3 && patch.manual <= 1.6) {
            const prev = this.resolveDensity();
            this._logDensityDecision('density_set', {
                scheme: (this.store.guideScheme === 'heavy') ? 'heavy' : 'total',
                targetTotal: (this.store.ashTarget != null) ? this.store.ashTarget : 8.50,
                deadband: (this.store.ashTargetTol != null) ? this.store.ashTargetTol : 0.1,
                rhoCur: prev, rhoNew: patch.manual, deltaRho: +(patch.manual - prev).toFixed(4),
                deltaA: null, K: this.K_PREDICT, kSource: 'default',
                heavyAsh: this.getHeavyAsh(), actualTotal: this.resolveTotalAsh(),
            });
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
    // 仅用于"调密后重介灰分预测"（ΔA ≈ Δρ/K），不参与调整量计算。
    // 表3 配对足够时由 densityGainK() 数据驱动估计（分系统 OLS），此常数为回退值。
    K_PREDICT: 0.075,

    // 专家经验：|总灰分偏差| → 密度修正量
    expertAdjust(dAbs) {
        const t = this.EXPERT_ADJUST;
        if (!(dAbs > t.deadband)) return 0;
        if (dAbs <= t.p1.dA) return (dAbs - t.deadband) / (t.p1.dA - t.deadband) * t.p1.dRho;
        if (dAbs <= t.p2.dA) return t.p1.dRho + (dAbs - t.p1.dA) / (t.p2.dA - t.p1.dA) * (t.p2.dRho - t.p1.dRho);
        return t.p2.dRho + (dAbs - t.p2.dA) * t.slope;
    },

    // 灰分→密度 增益 K（表3 灰分密度配对，分系统 OLS 加权平均；与后端 density_model.py 逐值一致）
    // A/B 为并联两套重介系统、同灰分下密度设定不同，直接合并回归会把系统间设定差混进斜率，
    // 故分系统拟合后按样本数加权；单系统 <5 点、|分母|<1e-9、斜率非正或越出 (0.005,0.2) 丢弃；
    // 分系统全失效回退 pooled，仍失效 valid=false（调用方回退 K_PREDICT 展示常数）。
    // 返回 {valid, k, n, source, totalPoints, systems:{名:{k,n,r2,used}}}
    densityGainK() {
        const logs = (this.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
        const pts = [];
        logs.forEach(l => {
            try {
                const v = JSON.parse(l.input_json || '{}');
                if (isFinite(+v.ash_content) && isFinite(+v.density) && +v.density >= 1.3 && +v.density <= 1.6) {
                    pts.push({ system: String(v.system || '').trim(), a: +v.ash_content, r: +v.density });
                }
            } catch (e) { /* 忽略坏记录 */ }
        });
        const ols = pairs => {
            const n = pairs.length;
            if (n < 5) return null;
            let sx = 0, sy = 0, sxy = 0, sxx = 0;
            pairs.forEach(p => { sx += p[0]; sy += p[1]; sxy += p[0] * p[1]; sxx += p[0] * p[0]; });
            const denom = n * sxx - sx * sx;
            if (Math.abs(denom) < 1e-9) return null;
            const k = (n * sxy - sx * sy) / denom;
            const ym = sy / n;
            let ssRes = 0, ssTot = 0;
            pairs.forEach(p => {
                const yh = ym + k * (p[0] - sx / n);
                ssRes += (p[1] - yh) ** 2; ssTot += (p[1] - ym) ** 2;
            });
            return { k, n, r2: ssTot > 0 ? 1 - ssRes / ssTot : 0 };
        };
        // 物理约束：密度↑→灰分↑，K 必须为正且有界；表3 系统混杂可能得到负斜率，直接丢弃保证方向不反
        const kOk = k => (k > 0.005 && k < 0.2 && isFinite(k));
        const bySys = {};
        pts.forEach(p => { (bySys[p.system] = bySys[p.system] || []).push([p.a, p.r]); });
        const systems = {}; const used = [];
        Object.keys(bySys).sort().forEach(name => {
            const fit = ols(bySys[name]);
            if (!fit) return;
            const entry = { k: fit.k, n: fit.n, r2: fit.r2, used: kOk(fit.k) };
            if (entry.used) used.push(entry);
            systems[name] = entry;
        });
        if (used.length) {
            let ks = 0, ws = 0;
            used.forEach(e => { ks += e.k * e.n; ws += e.n; });
            return { valid: true, k: ks / ws, n: ws, source: 'per_system', systems, totalPoints: pts.length };
        }
        const pooled = ols(pts.map(p => [p.a, p.r]));
        if (pooled && kOk(pooled.k)) return { valid: true, k: pooled.k, n: pooled.n, source: 'pooled', systems, totalPoints: pts.length };
        return { valid: false, k: null, n: pts.length, source: 'none', systems, totalPoints: pts.length };
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
    // P0 状态位：占位值 / 是否保持在闩锁与驻留窗口内（前后端同口径）
    densityGuardState(state) {
        const s = state || {};
        const now = Date.now();
        const manualTotal = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        const heavyManual = this.store.heavyAshInput || {};
        const target = this.store.ashTarget;
        const isStale = at => !at || (now - at) > this.PLACEHOLDER_STALE_MS;
        let placeholder = false;
        if (s.scheme !== 'heavy' && this.store.totalAshManualOn
            && typeof manualTotal.manual === 'number' && Math.abs(manualTotal.manual - target) < 1e-9) {
            placeholder = isStale(manualTotal.manualAt);
        }
        if (s.scheme === 'heavy' && this.heavyAshSource() === 'manual'
            && typeof heavyManual.manual === 'number' && Math.abs(heavyManual.manual - target) < 1e-9) {
            placeholder = isStale(heavyManual.manualAt);
        }
        const latch = this.store.densityActionLatch;
        const key = this._densityDriveKey(s.actualTotal, s.heavyAsh, s.scheme);
        const lastMove = this.store.densityLastMoveAt || 0;
        let hold = false, holdReason = '';
        if (latch && latch.key === key) {
            hold = true;
            holdReason = '已按当前这份数据调整过密度（同一份化验只动作一次）；'
                + '请等新的化验/在线数据后再看下一步建议';
        } else if (lastMove && (now - lastMove) < this.DENSITY_DWELL_MS) {
            hold = true;
            holdReason = `刚调整过密度（${Math.round((now - lastMove) / 60000)} 分钟前），`
                + `过程到位与化验需要时间，${Math.round(this.DENSITY_DWELL_MS / 60000)} 分钟内不再给新建议；`
                + '如已拿到新化验，录入后会自动重新计算';
        }
        return { placeholder, hold, holdReason, driveKey: key };
    },

    computeDensityGuidance(targetTotalAshOverride) {
        const g = this.getDensityGuide();
        const scheme = (this.store.guideScheme === 'heavy') ? 'heavy' : 'total';
        const rhoCur = this.resolveDensity();                    // 密度计权威值
        const heavyAsh = this.getHeavyAsh();                     // 静态初始值/采样值，不参与实时反推
        const actualTotal = this.resolveTotalAsh();              // 与卡片/在线仪表同源
        const tol = (this.store.ashTargetTol != null) ? this.store.ashTargetTol : 0.1;
        // 数据驱动 K（表3 配对分系统拟合）优先，无有效估计回退展示常数 K_PREDICT
        const kData = this.densityGainK();
        const kUsed = (kData && kData.valid && isFinite(kData.k) && kData.k > 0) ? kData.k : this.K_PREDICT;
        // K 三态：data=数据可辨识 / unidentifiable=样本够但斜率被物理约束拒掉 / insufficient=样本不足。
        // 区分这两者是关键：实测两系统斜率均为负(约 -0.022/-0.020, R²≈0.05)，
        // 属"闭环数据不可辨识"，不是"样本不够"——现场看到负斜率才会理解为什么要做阶跃实验。
        const kState = (kData && kData.valid) ? 'data'
                     : ((kData && kData.totalPoints >= 5) ? 'unidentifiable' : 'insufficient');
        // 常量灰分不得驱动控制建议：501 未接入时总灰分是默认常量，
        // 照它算 deltaA（如 8.8−8.5=0.3 超容差）会推出"下调密度"——用编造的灰分指挥现场操作。
        const totalIsConstant = this.totalAshIsConstant();
        const r = {
            valid: actualTotal != null && isFinite(actualTotal) && !totalIsConstant,
            rhoCur, K: kUsed, kSource: (kUsed === this.K_PREDICT) ? 'default' : 'data',
            kInfo: {
                state: kState,
                n: (kData && kData.valid) ? kData.n : ((kData && kData.totalPoints) || 0),
                source: (kData && kData.valid) ? kData.source : 'none',
                systems: (kData && kData.systems) || {},
            },
            heavyAsh,
            targetTotal: targetTotalAsh, actualTotal, scheme,
            targetHeavy: null, deltaAHeavy: null,
            deltaA: null, deltaRho: 0, rhoNew: rhoCur, direction: 'stable', reason: '',
            deadband: tol, maxStep: g.maxStep,
        };
        // P0 状态位（占位值 / 闩锁与驻留内保持）—— 与后端同口径，后端由 state 传入相同三值
        const gs = this.densityGuardState({ scheme: scheme, actualTotal: actualTotal, heavyAsh: heavyAsh });
        r.placeholderManual = gs.placeholder;
        r.hold = gs.hold;
        r.holdReason = gs.holdReason;
        r.driveKey = gs.driveKey;
        // 常量灰分不得驱动控制建议（与后端 compute_density_guidance 同序同文案）：
        // 501 未接入时总灰分是默认常量，照它算 deltaA（如 8.8−8.5=0.3 超容差）会推出"下调密度"。
        // 必须排在「数据不完整」通用判定之前，否则与后端给出的 reason 不一致。
        if (totalIsConstant) {
            r.valid = false;
            r.reason = '501 皮带灰分仪尚未接入（无在线总灰分数据），当前总灰分取默认常量、非实测 —— '
                     + '不做密度调整建议；请录入总灰分实测值，或等 501 数据接入';
            return r;
        }
        if (!r.valid) { r.reason = '总精煤灰分数据不完整，暂无密度调整建议'; return r; }
        // P0④ 占位值守卫：手动灰分等于目标值且长期未更新 → 视为占位值，宁可不给建议
        if (r.placeholderManual) {
            r.valid = false;
            r.reason = '当前灰分取的是「手动值」，且该值等于目标灰分并已超过 24 小时未更新 —— '
                     + '疑似占位值（不是新化验结果）。请录入新的化验值后再看密度建议。';
            return r;
        }
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
        // P0② 动作闩锁 / P0③ 最短驻留：同一份驱动数据只允许一次动作
        this._lastDriveKey = this._densityDriveKey(actualTotal, heavyAsh, scheme);
        if (r.hold) {
            r.direction = 'stable';
            r.rhoNew = rhoCur;
            r.deltaRho = 0;
            r.reason = r.holdReason || '已按当前这份数据调整过密度，等新的化验/在线数据后再给下一步建议';
            return r;
        }
        // 专家经验修正量（完整修正量）
        const dRhoFull = -Math.sign(r.deltaA) * this.expertAdjust(Math.abs(r.deltaA));   // 灰分偏高→降密度
        // P0① 逐步建议：发布的建议不超过 maxStep，避免"整步修正"依赖未验证前提而在闭环里震荡。
        const stepLimit = (this.DENSITY_STEPWISE && g.maxStep > 0) ? g.maxStep : Math.abs(dRhoFull);
        const dRhoStep = Math.max(-stepLimit, Math.min(stepLimit, dRhoFull));
        r.deltaRhoFull = +dRhoFull.toFixed(4);
        r.rhoTargetFull = Math.max(g.rhoMin, Math.min(g.rhoMax, +(rhoCur + dRhoFull).toFixed(3)));
        r.steps = Math.max(1, Math.ceil(Math.abs(dRhoFull) / (stepLimit || 1) - 1e-9));
        r.stepwise = this.DENSITY_STEPWISE && r.steps > 1;
        r.deltaRho = +dRhoStep.toFixed(4);
        r.rhoNew = Math.max(g.rhoMin, Math.min(g.rhoMax, +(rhoCur + dRhoStep).toFixed(3)));
        r.direction = r.deltaA > 0 ? 'down' : 'up';
        r.reason = scheme === 'heavy'
            ? `重介灰分${r.deltaAHeavy > 0 ? '偏高' : '偏低'} ${Math.abs(r.deltaAHeavy).toFixed(2)}%` +
              `（实测${heavyAsh.toFixed(2)}% / 目标${r.targetHeavy.toFixed(2)}%，等效总灰分偏差${r.deltaA >= 0 ? '+' : ''}${r.deltaA.toFixed(2)}%），按专家经验建议` +
              `${r.direction === 'down' ? '下调' : '上调'}密度至 ${r.rhoNew.toFixed(3)} g/cm³（人工执行）`
            : `实际总灰分${r.deltaA > 0 ? '偏高' : '偏低'} ${Math.abs(r.deltaA).toFixed(2)}%` +
              `（${actualTotal.toFixed(2)}% / 期望${targetTotalAsh.toFixed(2)}%），按专家经验建议` +
              `${r.direction === 'down' ? '下调' : '上调'}密度至 ${r.rhoNew.toFixed(3)} g/cm³（人工执行）`
              + (r.stepwise
                  ? `；完整修正目标 ${r.rhoTargetFull.toFixed(3)}（本步只走 ${Math.abs(r.deltaRho).toFixed(3)}，`
                    + `共约 ${r.steps} 步，每步之后等新的化验/在线数据再走下一步）`
                  : '');
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
        const heavyAsh = this.getHeavyAsh();          // 手动采样 > 502在线 > 默认7.9（不再恒值 8.50）
        const COARSE_AMT = 40;                        // 粗精煤泥量恒值（三表无数据源，沿用当前默认）
        const SCALE_501 = 268.5, SCALE_502 = 235.2;   // 皮带秤恒值（三表无数据源）
        const TOTAL_AMT = +(SCALE_501 + SCALE_502).toFixed(1);   // 503.7
        // 2026-09 工艺确认:501=总混配(在线总灰分),502=仅重介(在线重介灰分);
        // 默认值随之校正(旧 8.52/10.68 方向颠倒)。heavyAsh 走 getHeavyAsh(502 在线链)。
        // 注意：ash501 **没有**默认值 —— 501 未接入时该列留空，不用常量冒充实测
        // （与后端 brief.DEF 一致：真实库 360 条 ash_density 全部 belt=502、零条 501）
        const DEF = { ash502: 7.9, density: 1.450, level: 55, floatAsh: 9.85 };

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

        // 前向填充游标(每表独立);各表当前记录的时间戳用于 24h 新鲜度判定
        const FRESH_MS = 24 * 3600000;   // 三表续传窗口:超过视为该表在本小时无数据
        let iCoarse = 0, iFloat = 0, iAd = 0;
        let curCoarse = null, curFloat = null;
        let curCoarseT = -Infinity, curFloatT = -Infinity, curAdT = -Infinity;
        let prevCoarse = null;     // 上一成行的 表1 记录（判断是否新采样）
        let estCoarse = null;      // 递归软测量值（粗精煤泥灰分预测值）
        let prevForecast = null;   // 上一成行的模型原始预测（用于增量）
        let curAsh501 = null, curAsh502 = null, curDensity = null;

        const pad = n => String(n).padStart(2, '0');
        const fmtHour = hms => { const d = new Date(hms); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:00`; };
        const fmt = (v, n) => (v == null || !isFinite(v)) ? '' : (+v).toFixed(n);

        const rows = [];
        for (const h of hours) {
            const hEnd = (h + 1) * 3600000;
            while (iCoarse < coarseRecs.length && coarseRecs[iCoarse].t < hEnd) { curCoarse = coarseRecs[iCoarse].r; curCoarseT = coarseRecs[iCoarse].t; iCoarse++; }
            while (iFloat < floatRecs.length && floatRecs[iFloat].t < hEnd) { curFloat = floatRecs[iFloat].r; curFloatT = floatRecs[iFloat].t; iFloat++; }
            const densityBefore = curDensity;
            while (iAd < adRecs.length && adRecs[iAd].t < hEnd) {
                const a = adRecs[iAd];
                if (a.belt === '501' && a.ash != null) curAsh501 = a.ash;
                if (a.belt === '502' && a.ash != null) curAsh502 = a.ash;
                if (a.density != null) curDensity = a.density;
                curAdT = a.t;
                iAd++;
            }

            // 行存在规则(2026-09 用户确认:三表齐全才成行):粗精煤泥/浮精/灰分密度
            // 各自最近一条记录距本小时结束 ≤ 24h 才生成该行,否则跳过——
            // 三表时间窗不重叠的时段(如 6-7月只有表1)不再用陈旧续传凑行。
            // 跳过时递归状态(prevForecast/prevCoarse/estCoarse)不推进,空窗后新采样自然重新锚定。
            const threeOk = (curCoarse != null && hEnd - curCoarseT <= FRESH_MS
                          && curFloat != null && hEnd - curFloatT <= FRESH_MS
                          && hEnd - curAdT <= FRESH_MS);
            if (!threeOk) continue;

            // 实测密度：仅本小时有表3新密度测量时有值（稀疏），用于与建议密度对照
            const densityMeasured = (curDensity != null && curDensity !== densityBefore) ? curDensity : null;

            const level = (curCoarse && typeof curCoarse.level === 'number') ? curCoarse.level : DEF.level;
            const floatAsh = (curFloat && typeof curFloat.ash_content === 'number') ? curFloat.ash_content : DEF.floatAsh;
            const floatAmt = (curFloat && typeof curFloat.coal_amount === 'number') ? curFloat.coal_amount : null;
            const ash501 = curAsh501;   // 无该小时的 501 记录 → null → 该列留空（不打印常量冒充实测）
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

            // 总精煤灰分：公式优先（粗灰用递归软测量）；否则 501 直读（501=总混配皮带）
            let totalAsh = null;
            if (formulaOk) {
                totalAsh = +this.calcTotalAsh(heavyAsh, heavyAmt, floatAsh, floatAmt, coarseModel, COARSE_AMT).toFixed(2);
            } else if (ash501 != null) {
                totalAsh = +ash501.toFixed(2);
            } else {
                totalAsh = null;   // 公式不完整且无在线总灰分 → 留空（常量不得作为实测值出现在导出表里）
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

        // 行存在性由"三表齐全(24h内)"规则决定,不再事后过滤;数据对齐但公式缺项的行
        // 保留(数据列有意义,建议密度列留空)
        return { headers: this.BRIEF_HEADERS, rows };
    },

    // ============================================================
    //  密度建议自动执行：每1秒执行一次建议（写入密度计），偏差进死区后自动停步
    // ============================================================
    densityAutoTimer: null,

    // ============================================================
    //  密度决策日志(工况条件化 Stage 0 观测):
    //  记录每次真实密度决策(自动重定目标/操作员设定)时的工况上下文(带煤量/原煤灰分/
    //  脱粉/系统组合/工作面/精磁尾液位),并在决策满45分钟、期间无新决策时,
    //  用「决策后第一条人工化验」补记灰分响应——
    //  每次真实调整 = 一个 mini 阶跃实验点(Δρ_actual, ΔA_actual),供 K(工况) 标定。
    //  注意:闭环常规数据不可辨识 K(见 densityGainK 恒 valid=false),响应量必须取人工采样;
    //  502 在线值是被控量本身,回路会把灰分拉回目标,用它算 ΔA 恒接近 0。
    //  存储:store.densityDecisionLog(auto_state 持久化,上限 200 条)。
    //  查看:GET /api/v1/state → densityDecisionLog。
    // ============================================================
    DECISION_LOG_MAX: 200,
    DECISION_RESPONSE_MIN_AGE_MS: 45 * 60000,   // 决策后等过程到位再找人工化验补记响应

    _decisionCtx() {
        let last = null, lastT = '';
        (this.store.coarseCoal || []).forEach(r => {
            if (r && r.timestamp && r.timestamp >= lastT) { lastT = r.timestamp; last = r; }
        });
        const n = (v) => (typeof v === 'number' && isFinite(v)) ? v : null;
        return {
            coalAmount: last ? n(last.coal_amount) : null,
            rawAsh: last ? n(last.raw_ash) : null,
            desl473: last ? (last.desliming473 || 0) : null,
            desl474: last ? (last.desliming474 || 0) : null,
            sysA: last ? (last.sysA || 0) : null, sysB: last ? (last.sysB || 0) : null,
            sys401: last ? (last.sys401 || 0) : null, sys402: last ? (last.sys402 || 0) : null,
            miningFace: last ? (last.mining_face || '') : '',
            levelTail: (() => { try { return this.resolveInstrument('level_tail'); } catch (e) { return null; } })(),
            // 实测密度计（2026-09）：只记录、不参与决策。有了它，以后可以拿决策日志
            // 标定"在线密度计 vs 实测"的偏差随时间/工况的变化。
            densityActual: (() => { try { return this.resolveInstrument('density_actual'); } catch (e) { return null; } })(),
            // 重介灰分这一笔是"人工值"还是"502在线+密度仿真"算出来的 —— 影响日后标定时对数据的信任判断
            heavyAshSource: this.heavyAshSource(),
        };
    },

    _logDensityDecision(trigger, g) {
        try {
            this._completeDensityDecisionResponses();
            if (!this.store.densityDecisionLog) this.store.densityDecisionLog = [];
            const log = this.store.densityDecisionLog;
            const now = Date.now();
            // 节流:同类触发 10 分钟内不重复记录(页面反复刷新不刷日志)
            for (let i = log.length - 1; i >= 0; i--) {
                if (log[i].trigger !== trigger) continue;
                const t0 = new Date(String(log[i].ts).replace(' ', 'T')).getTime();
                if (isFinite(t0) && now - t0 < 10 * 60000) return;
                break;
            }
            log.push({
                ts: this.formatDate(new Date()),
                trigger,                                    // retarget | density_set
                scheme: g.scheme, target: g.targetTotal, tol: g.deadband,
                rhoCur: g.rhoCur, rhoNew: g.rhoNew, deltaRho: g.deltaRho, deltaA: g.deltaA,
                kUsed: g.K, kSource: g.kSource || 'default',
                heavyAsh: g.heavyAsh, totalAsh: g.actualTotal,
                ctx: this._decisionCtx(),
                response: null,
            });
            if (log.length > this.DECISION_LOG_MAX) this.store.densityDecisionLog = log.slice(-this.DECISION_LOG_MAX);
            this.saveStore();
        } catch (e) { console.warn('决策日志写入失败:', e); }
    },

    // 决策时间戳(本地时间字符串) → 毫秒
    _decisionMs(e) {
        const t = new Date(String(e && e.ts || '').replace(' ', 'T')).getTime();
        return isFinite(t) ? t : null;
    },

    // 决策日志导出（Excel）：每行一条决策 + 其响应，直接作为 K(工况) 标定的输入表。
    // 不做导出的话日志只能通过 GET /api/v1/state 看 JSON，现场拿不到、进不了标定流程。
    exportDensityDecisionLog() {
        const log = this.store.densityDecisionLog || [];
        if (!log.length) { this.showToast('暂无密度决策记录（真实调整密度后才会生成）', 'warning'); return; }
        const SRC = { heavySamples: '采样记录', heavyAshInput: '在线仪表手动录入' };
        const head = ['决策时间', '触发', '方案', '目标灰分', '容差',
                      'ρ旧', 'ρ新', 'Δρ建议', 'ΔA等效', 'K', 'K来源', '重介灰分', '重介灰分来源', '总灰分',
                      '带煤量', '原煤灰分', '脱粉473', '脱粉474', '系统A', '系统B', '系统401', '系统402',
                      '工作面', '精磁尾液位', '实测密度',
                      '响应状态', '响应时间', '滞后(分)', '响应来源', 'ρ实测', '重介灰分实测', 'Δρ实测', 'ΔA实测'];
        const rows = log.map(e => {
            const c = e.ctx || {};
            const r = e.response;
            const state = !r ? '待补记(未等到人工化验)'
                        : r.invalidated ? `作废(${r.reason || ''})` : '已闭环';
            return [
                e.ts, e.trigger, e.scheme === 'heavy' ? '重介版' : '总灰分版', e.target, e.tol,
                e.rhoCur, e.rhoNew, e.deltaRho, e.deltaA, e.kUsed, e.kSource, e.heavyAsh,
                (c.heavyAshSource === 'manual' ? '手动(采样)' : '计算(502在线+密度)'), e.totalAsh,
                c.coalAmount ?? '', c.rawAsh ?? '', c.desl473 ?? '', c.desl474 ?? '',
                c.sysA ?? '', c.sysB ?? '', c.sys401 ?? '', c.sys402 ?? '', c.miningFace ?? '', c.levelTail ?? '',
                c.densityActual ?? '',
                state, r ? (r.ts || '') : '', r ? (r.lagMin ?? '') : '', r ? (SRC[r.source] || r.source || '') : '',
                r ? (r.rhoNow ?? '') : '', r ? (r.heavyAshNow ?? '') : '',
                r ? (r.dRhoActual ?? '') : '', r ? (r.dAActual ?? '') : '',
            ];
        });
        const ws = XLSX.utils.aoa_to_sheet([head, ...rows]);
        ws['!cols'] = head.map(h => ({ wch: Math.max(10, String(h).length * 2) }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '密度决策日志');
        // 附一张口径说明，避免拿到表的人误读 invalidated 行
        const notes = [
            ['字段', '说明'],
            ['触发', 'retarget=系统按目标灰分重定密度；density_set=操作员手动设定密度'],
            ['Δρ建议 / ΔA等效', '决策当时算出的建议修正量与等效总灰分偏差'],
            ['K / K来源', 'K来源=default 表示数据不可辨识（闭环斜率非正）而回退经验值 0.075'],
            ['响应状态', '待补记=决策后尚未出现人工化验；作废=窗口内又发生了新的密度决策，累积量无法归属'],
            ['响应来源', '只取人工化验（采样记录 / 在线仪表手动录入）；不用在线仪表自动值——那是被控量，闭环下 ΔA≈0'],
            ['滞后(分)', '从决策到取样化验的分钟数，用于判断过程是否已到位'],
            ['重介灰分来源', '手动(采样)=操作员填的化验值优先；计算(502在线+密度)=由 502 在线值加密度仿真得出。'
                          + '后者随密度计变化，因此"调密度→灰分变化→总灰分达标"这条链只在计算档下成立'],
            ['实测密度', '决策时的实测密度计读数（人工录入）。**仅记录、不参与建议密度的计算**；'
                       + '与 ρ旧(在线密度计) 相减即为两台密度计的偏差，可用于标定在线密度计'],
        ];
        XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(notes), '口径说明');
        XLSX.writeFile(wb, `密度决策日志_${rows.length}条.xlsx`);
        this.showToast(`密度决策日志已导出（${rows.length} 条）`, 'success');
    },

    // 决策之后的第一条「人工化验」重介灰分采样(用于 K 标定的响应量)。
    // 为什么不用在线仪表：502 在线值就是本回路的被控量，闭环下回路会把灰分拉回目标，
    // ΔA 恒接近 0（斜率估计系统性偏小甚至为负）——这正是 densityGainK() 在真实数据上
    // 恒为 valid=false 的原因。只有人工采样(t≈开环阶跃响应)才带得出真实 ΔA。
    _heavySampleAfter(t0) {
        // ① 采样记录表(heavySamples)：取决策之后最早的一条
        let best = null;
        (this.store.heavySamples || []).forEach(s => {
            const at = new Date(String(s && s.timestamp || '').replace(' ', 'T')).getTime();
            if (!isFinite(at) || at <= t0) return;
            if (typeof s.ash_content !== 'number' || !isFinite(s.ash_content)) return;
            if (!best || at < best.at) {
                const rho = (typeof s.rho === 'number' && isFinite(s.rho)) ? s.rho : null;
                best = { at, ash: s.ash_content, rho, source: 'heavySamples' };
            }
        });
        if (best) return best;
        // ② 在线仪表行的手动重介灰分(采样/手写录入)：manualAt 晚于决策即视为决策后化验
        const cfg = this.store.heavyAshInput || {};
        const at = cfg.manualAt || 0;
        if (at > t0 && typeof cfg.manual === 'number' && isFinite(cfg.manual)) {
            return { at, ash: cfg.manual, rho: this.resolveDensity(), source: 'heavyAshInput' };
        }
        return null;
    },

    // 补记响应:决策满45分钟、且期间没有新的密度决策、且拿到了决策后的人工化验值时,
    // 记录 实际Δρ 与 重介灰分ΔA —— K 标定的原料。
    // 三种"不可用"情况都显式标记，避免把被污染的数据当成有效阶跃点：
    //   · 窗口内又发生了新的密度决策 → 累积量会算到本条上(invalidated)
    //   · 一直等不到人工化验 → 保持未闭环(response=null)，等下次页面打开再试
    _completeDensityDecisionResponses() {
        try {
            const log = this.store.densityDecisionLog;
            if (!log || !log.length) return;
            const now = Date.now();
            let lastDecisionMs = -Infinity;
            log.forEach(e => { const t = this._decisionMs(e); if (t !== null) lastDecisionMs = Math.max(lastDecisionMs, t); });
            let changed = false;
            log.forEach(e => {
                if (e.response) return;
                const t0 = this._decisionMs(e);
                if (t0 === null) {
                    e.response = { invalidated: true, reason: '决策时间戳无法解析' };
                    changed = true;
                    return;
                }
                // 本条之后又出现了新的密度决策：密度已被再次改动，响应不再属于本条
                if (t0 < lastDecisionMs) {
                    e.response = { invalidated: true, reason: '窗口内出现后续密度决策，累积量无法归属' };
                    changed = true;
                    return;
                }
                if (now - t0 < this.DECISION_RESPONSE_MIN_AGE_MS) return;   // 过程尚未到位
                const sample = this._heavySampleAfter(t0);
                if (!sample) return;                                        // 等人工化验，保持未闭环
                const rhoSample = (sample.rho != null) ? sample.rho : this.resolveDensity();
                e.response = {
                    ts: this.formatDate(new Date(sample.at)),
                    lagMin: Math.round((sample.at - t0) / 60000),
                    source: sample.source,              // heavySamples | heavyAshInput
                    rhoNow: rhoSample, heavyAshNow: sample.ash,
                    dRhoActual: +(rhoSample - e.rhoCur).toFixed(4),
                    dAActual: +(sample.ash - e.heavyAsh).toFixed(3),
                };
                changed = true;
            });
            if (changed) this.saveStore();
        } catch (e) { /* 日志失败不影响主流程 */ }
    },

    // K 的一句话结论（给操作员看的，不是统计量堆砌）
    kStateText(kInfo) {
        const st = (kInfo && kInfo.state) || 'insufficient';
        if (st === 'data') {
            const how = (kInfo.source === 'per_system') ? '分系统加权' : '合并回归';
            return `表3配对数据驱动（n=${kInfo.n}，${how}）`;
        }
        if (st === 'unidentifiable') {
            return `数据不可辨识 → 回退经验值 ${this.K_PREDICT}：表3配对的回归斜率越出物理约束`
                 + `（密度↑→灰分↑）已被拒，属闭环数据固有性质，需按《密度阶跃实验》开环标定`;
        }
        return `样本不足（表3有效配对 ${(kInfo && kInfo.n) || 0} 点 < 5）→ 回退经验值 ${this.K_PREDICT}`;
    },

    // K 的分系统明细（悬停查看：每套系统的斜率、样本数、R²、是否被采用）
    kSystemsText(kInfo) {
        const sys = (kInfo && kInfo.systems) || {};
        const keys = Object.keys(sys).sort();
        if (!keys.length) return '无分系统拟合结果（表3 有效配对数不足）';
        return '分系统拟合明细：' + keys.map(k => {
            const e = sys[k] || {};
            const kk = (typeof e.k === 'number' && isFinite(e.k)) ? e.k.toFixed(4) : '—';
            const r2 = (typeof e.r2 === 'number' && isFinite(e.r2)) ? e.r2.toFixed(3) : '—';
            return `系统${k || '(空)'} k=${kk} n=${e.n} R²=${r2}${e.used ? '' : '（斜率不合物理约束，已拒）'}`;
        }).join('；');
    },

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
                if (Math.abs(g.deltaRho) > 1e-9) this._logDensityDecision('retarget', g);   // 决策日志:真实重定目标
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

    // 501 灰分的来源层级：'manual' | 'online' | 'none'（与后端 resolvers.ash501_layer 一致）。
    // 'none' = 三层都没有真实数据源，回落到 INSTRUMENT_DEFAULT 常量 8.8。
    // 实测依据：真实库 360 条 ash_density 记录全部 belt=502、零条 501（PLC 未接入）。
    ash501Layer() {
        const cfg = (this.store.instrumentInputs && this.store.instrumentInputs.ash_501) || {};
        const m = cfg.manual;
        if (typeof m === 'number' && isFinite(m) && m >= 0) return 'manual';
        if (this.latestCalcValue('ash_meter', '501') != null) return 'online';
        if (this.latestBeltAsh('501') != null) return 'online';
        return 'none';
    },

    // 总精煤灰分 + 来源层级（'manual'|'formula'|'entry'|'ash501'|'none'）。
    // 与后端 resolvers.resolve_total_ash_ex 逐值一致；来源用于判断这个数是不是常量。
    resolveTotalAshEx() {
        const cfg = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        const manual = (typeof cfg.manual === 'number' && isFinite(cfg.manual) && cfg.manual >= 0) ? cfg.manual : null;
        if (manual != null && this._manualValid(cfg, 'totalAsh')) return { value: manual, source: 'manual' };
        const formula = this.formulaTotalAsh();
        if (formula != null) { this._autoBump('totalAsh', formula); return { value: formula, source: 'formula' }; }
        const entry = this.totalAshEntry();
        if (entry != null) { this._autoBump('totalAsh', entry); return { value: entry, source: 'entry' }; }
        // 2026-09 工艺确认:501 承载的就是总精煤混配(重介+浮精+粗),其灰分仪读数
        // 即在线总灰分直读;502(重介组分)不再混入平均(否则重介灰分被重复计入)。
        const dv = +this.resolveInstrument('ash_501').toFixed(4);
        this._autoBump('totalAsh', dv);
        return { value: dv, source: 'ash501' };
    },

    // 总精煤灰分：手动(化验) > 公式计算(给定重介/浮/粗后推出) > 录入(导入/补录) > 501直读
    resolveTotalAsh() {
        return this.resolveTotalAshEx().value;
    },

    // 当前总灰分是否为"常量"（501 无任何真实数据源却落到了 501 兜底）
    totalAshIsConstant() {
        return this.resolveTotalAshEx().source === 'ash501' && this.ash501Layer() === 'none';
    },

    totalAshEntry() {
        const cfg = (this.store.ashInputs && this.store.ashInputs.totalAsh) || {};
        if (typeof cfg.entry === 'number' && isFinite(cfg.entry) && cfg.entry >= 0) return cfg.entry;
        // 2026-09 工艺确认:501=总混配皮带,其灰分即总灰分;502(重介组分)不混入平均。
        // 仿真:密度变化→灰分测量响应(simK)仍作用于 501 读数上。
        const a501 = this.latestBeltAsh('501');
        if (a501 == null) return null;
        const rho = this.resolveInstrument('density');
        const d = (rho - this.getSimBaseRho()) / this.DENSITY_GUIDE.simK;
        return +(a501 + d).toFixed(4);
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
        if (entry != null) return '录入';
        // 标成"默认(仪表)"会让人以为有仪表在读：501 无数据源时它其实是硬编码常量
        if (kind === 'totalAsh' && this.ash501Layer() === 'none') return '默认常量(501未接入)';
        return '默认(仪表)';
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

    // 取当前重介精煤灰分：手动(采样/手写) > 502皮带灰分仪在线值(502只承载重介精煤)
    // > 默认7.9(502实测均值)。不参与实时反推演算;反推值仅展示。
    getHeavyAsh() {
        const cfg = (this.store.heavyAshInput && this.store.heavyAshInput) || {};
        const m = cfg.manual;
        // 来源=手动 且 有有效值 → 用手动；否则走计算（502在线，已含密度仿真增量）
        if (this.store.heavyAshManualOn !== false
            && typeof m === 'number' && isFinite(m) && m >= 0
            && this._manualValid(cfg, 'heavyAsh')) {
            return m;
        }
        return this.resolveInstrument('ash_502');
    },

    // 当前重介精煤灰分来源（供界面与决策日志标注）：'manual' | 'calc'
    heavyAshSource() {
        const cfg = (this.store.heavyAshInput && this.store.heavyAshInput) || {};
        const m = cfg.manual;
        const usable = typeof m === 'number' && isFinite(m) && m >= 0 && this._manualValid(cfg, 'heavyAsh');
        return (this.store.heavyAshManualOn !== false && usable) ? 'manual' : 'calc';
    },

    // 重介精煤灰分（在线仪表行）：手动 > 502在线 > 默认
    resolveHeavyAsh() { return this.getHeavyAsh(); },

    heavyAshLayer() {
        const cfg = (this.store.heavyAshInput && this.store.heavyAshInput) || {};
        const m = cfg.manual;
        if (this.heavyAshSource() === 'manual') return '手动(采样)';
        // 计算档：说明它由 502 在线值 + 密度仿真算出（密度计因此在起作用）
        return '计算(' + this._heavyAutoLayer() + '+密度)';
    },

    // 重介灰分自动层文案:502 在线值 > 默认
    _heavyAutoLayer() {
        return this.latestBeltAsh('502') != null ? '502在线' : '默认7.9';
    },

    // 计算档下的重介灰分值（不含手动覆盖）—— 用于界面并列显示"计算值"
    heavyAshComputed() {
        try { return this.resolveInstrument('ash_502'); } catch (e) { return null; }
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
            // 双击填了值 = 明确想用手动值 → 自动切到手动档。
            // （否则会出现"填了却不生效"的最糟意外；清空值时不动档位，计算值自然接管。）
            if (typeof patch.manual === 'number' && isFinite(patch.manual)) {
                patch.heavyAshManualOn = true;
            }
        }
        if ('heavyAshManualOn' in patch) {
            this.store.heavyAshManualOn = !!patch.heavyAshManualOn;
            delete patch.heavyAshManualOn;
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
