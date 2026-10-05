/* Read-only decision map. Never switch/train models, save App.store, or bind telemetry. */
const DecisionOverviewPage = {
    initialized: false, view: 'gpt', selected: 'density', zoom: null, snapshot: null,
    pointChoices: {}, nodes: [], timer: null,
    sourceInteractionUntil: 0,
    inspectorFrame: null,
    motionEnabled: true, motionReduced: false,
    escape(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    number(value, digits = 2, unit = '') { return Number.isFinite(value) ? value.toFixed(digits) + (unit ? ' ' + unit : '') : '—'; },
    time(value) {
        if (!value) return '未记录';
        const date = new Date(value);
        return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12:false}) + (date.getTime() > Date.now() + 60000 ? '（未来时间，需核对）' : '') : '未记录';
    },
    active() { return !!document.getElementById('page-decision')?.classList.contains('active'); },

    // Existing resolvers write autoState/EMA. Run them on a private copy instead.
    readFacade() {
        const reader = Object.create(App);
        reader.store = JSON.parse(JSON.stringify(App.store));
        reader.saveStore = () => {};
        reader._emaCoarseAsh = function(raw, timestamp) {
            const state = this.store.coarseAshEma;
            if (state && state.t === timestamp && Number.isFinite(state.v)) return state.v;
            return App._emaCoarseAsh.call(this, raw, timestamp);
        };
        return reader;
    },

    buildSnapshot(view = this.view) {
        const reader = this.readFacade(), store = reader.store;
        const guide = reader.computeDensityGuidance();
        const total = reader.resolveTotalAshEx();
        const productionEngine = store.coarseModel ? reader.coarseEngine() : 'default';
        const engineLabel = {gpt:'GPT',ds:'DS',default:'出厂默认'}[productionEngine];
        const modelGroup = view === 'production' ? store.coarseModel :
            (productionEngine === 'gpt' ? store.coarseModel : store.coarseModelVariants?.gpt);
        const model = modelGroup && (view === 'production' || reader.coarseEngine(modelGroup) === 'gpt') ?
            modelGroup[modelGroup.production] : (view === 'production' ? reader.getCoarseModel() : null);
        const coarse = reader._latestByTime(store.coarseCoal), floating = reader._latestByTime(store.floatCoal);
        const amounts = {heavy:reader.resolveAmount('denseAmount'),float:reader.resolveAmount('floatAmount'),coarse:reader.resolveAmount('coarseAmount')};
        const ashes = {heavy:reader.getHeavyAsh(),float:reader.resolveFloatAsh(),coarse:reader.resolveCoarseAsh()};
        const sum = Object.values(amounts).every(Number.isFinite) ? amounts.heavy + amounts.float + amounts.coarse : null;
        const formula = reader.formulaTotalAsh();
        const componentFeeds = guide.scheme === 'heavy' || total.source === 'formula';
        const ashOk = guide.valid && Number.isFinite(guide.deltaA) && Math.abs(guide.deltaA) <= guide.deadband;
        const showAdvice = guide.valid && !(guide.hold && !ashOk);
        const prediction = model && coarse ? reader._coarsePredict(coarse, model) : null;
        const inputs = reader.MLR_FEATURES.map((key, index) => {
            const raw = coarse?.[key], missing = !Number.isFinite(raw);
            const means = model && (model.imputeMeans || model.means);
            const base = missing ? means?.[index] : raw;
            const used = Number.isFinite(base) && model ? reader._coarseBoundValue(base, index, model) : null;
            return {key,raw:Number.isFinite(raw) ? raw : null,used,missing,clipped:Number.isFinite(raw) && Number.isFinite(used) && raw !== used};
        });
        return {reader,store,guide,total,productionEngine,engineLabel,modelGroup,model,coarse,floating,amounts,ashes,sum,formula,
            componentFeeds,showAdvice,ashOk,prediction,inputs,previewOnly:view !== 'production' && productionEngine !== 'gpt'};
    },

    init() {
        if (this.initialized) { this.refresh(); return; }
        this.initialized = true;
        document.getElementById('decision-refresh').addEventListener('click', () => {
            this.refresh();
            if (window.Operations) window.Operations.refresh().then(() => this.renderInspector());
        });
        document.getElementById('decision-theme').addEventListener('click', event => {
            const light = document.getElementById('decision-workspace').classList.toggle('is-light');
            event.currentTarget.textContent = light ? '深色视图' : '浅色视图';
        });
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        this.motionReduced = reducedMotion.matches;
        reducedMotion.addEventListener('change', event => { this.motionReduced = event.matches; this.syncMotion(); });
        document.getElementById('decision-motion').addEventListener('click', () => {
            this.motionEnabled = !this.motionEnabled; this.syncMotion();
        });
        document.addEventListener('visibilitychange', () => this.syncMotion());
        document.querySelectorAll('[data-decision-model]').forEach(button => button.addEventListener('click', () => {
            this.view = button.dataset.decisionModel;
            document.querySelectorAll('[data-decision-model]').forEach(b => {
                const selected = b.dataset.decisionModel === this.view;
                b.classList.toggle('is-active', selected); b.setAttribute('aria-pressed', String(selected));
            });
            this.refresh();
        }));
        document.getElementById('decision-fit').addEventListener('click', () => { this.zoom = null; this.fit(); });
        document.getElementById('decision-zoom-in').addEventListener('click', () => this.changeZoom(.15));
        document.getElementById('decision-zoom-out').addEventListener('click', () => this.changeZoom(-.15));
        document.getElementById('decision-canvas-scroll').addEventListener('click', event => {
            const node = event.target.closest('[data-decision-node]');
            if (node) { this.select(node.dataset.decisionNode); return; }
            if (!event.target.closest('.decision-formula,button,a,input,select,textarea,label,[contenteditable="true"]')) this.closeInspector(false);
        });
        document.getElementById('decision-inspector').addEventListener('change', event => {
            if (event.target.id === 'decision-source-point') {
                this.pointChoices[this.selected] = event.target.value;
                this.renderInspector();
                document.getElementById('decision-source-point').focus({preventScroll:true});
            }
        });
        const pauseForPointSelection = event => {
            if (event.target.id === 'decision-source-point') this.sourceInteractionUntil = Date.now() + 15000;
        };
        document.getElementById('decision-inspector').addEventListener('pointerdown', pauseForPointSelection);
        document.getElementById('decision-inspector').addEventListener('keydown', pauseForPointSelection);
        document.getElementById('decision-inspector').addEventListener('click', event => {
            if (event.target.closest('[data-decision-close]')) { this.closeInspector(); return; }
            const link = event.target.closest('[data-decision-page]');
            if (link) { this.closeInspector(false); App.goToPage(link.dataset.decisionPage); this.syncMotion(); }
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && this.active()) this.closeInspector();
        });
        for (const container of [document.querySelector('.main-content'),document.getElementById('decision-canvas-scroll')]) {
            container.addEventListener('scroll', () => this.queueInspectorPosition(), {passive:true});
        }
        document.querySelector('.main-nav').addEventListener('click', event => {
            const page = event.target.closest('[data-page]')?.dataset.page;
            if (page && page !== 'page-decision') this.closeInspector(false);
            this.syncMotion();
        });
        window.addEventListener('resize', () => { if (this.active()) this.fit(); });
        // Reuse Operations' existing polling snapshot; never start a second API poll.
        this.timer = setInterval(() => {
            if (this.active() && !document.hidden && Date.now() > this.sourceInteractionUntil) this.refresh();
        }, 5000);
        this.refresh();
    },
    refresh() {
        if (!this.initialized) return;
        this.syncMotion();
        if (!this.active()) { this.closeInspector(false); return; }
        const focused = document.activeElement?.dataset?.decisionNode;
        const sourceFocused = document.activeElement?.id === 'decision-source-point';
        const closeFocused = document.activeElement?.hasAttribute('data-decision-close');
        const detailPageFocused = document.activeElement?.dataset?.decisionPage;
        this.snapshot = this.buildSnapshot();
        this.nodes = this.buildNodes(this.snapshot);
        this.renderGraph(); this.renderContext(); this.renderInspector(); this.fit();
        if (focused) document.querySelector(`[data-decision-node="${focused}"]`)?.focus({preventScroll:true});
        if (sourceFocused) document.getElementById('decision-source-point')?.focus({preventScroll:true});
        if (closeFocused) document.querySelector('[data-decision-close]')?.focus({preventScroll:true});
        if (detailPageFocused) document.querySelector(`[data-decision-page="${detailPageFocused}"]`)?.focus({preventScroll:true});
    },
    select(id) {
        this.selected = id; this.renderGraph(); this.renderInspector();
        document.getElementById('decision-inspector').classList.add('is-open');
        this.positionInspector(true);
        document.querySelector('[data-decision-close]')?.focus({preventScroll:true});
    },
    closeInspector(restoreFocus = true) {
        const inspector = document.getElementById('decision-inspector');
        if (!inspector?.classList?.contains('is-open')) return;
        inspector.classList.remove('is-open','is-below','is-inline');
        if (this.inspectorFrame) { cancelAnimationFrame(this.inspectorFrame); this.inspectorFrame = null; }
        if (restoreFocus) document.querySelector(`[data-decision-node="${this.selected}"]`)?.focus({preventScroll:true});
    },
    queueInspectorPosition(reconsiderInline = false) {
        const inspector = document.getElementById('decision-inspector');
        if (!inspector?.classList?.contains('is-open')) return;
        if (this.inspectorFrame) {
            if (!reconsiderInline) return;
            cancelAnimationFrame(this.inspectorFrame);
        }
        this.inspectorFrame = requestAnimationFrame(() => { this.inspectorFrame = null; this.positionInspector(false,reconsiderInline); });
    },
    positionInspector(ensureRoom = false, reconsiderInline = false) {
        const inspector = document.getElementById('decision-inspector');
        if (!inspector?.classList?.contains('is-open')) return;
        const inline = inspector.classList.contains('is-inline');
        if (inline && !ensureRoom && !reconsiderInline) return;
        const card = document.querySelector(`[data-decision-node="${this.selected}"]`);
        const node = this.nodes.find(n => n.id === this.selected), root = this.nodes.find(n => n.id === 'density');
        if (!card || !node || !root || !this.active()) { this.closeInspector(false); return; }
        let rect = card.getBoundingClientRect();
        const canvas = document.getElementById('decision-canvas-scroll').getBoundingClientRect();
        const topSafe = (document.querySelector('.top-bar')?.getBoundingClientRect().bottom || 56)+12;
        // Dismiss when the selected card leaves the visible canvas, without moving focus/scroll back.
        if (rect.bottom <= Math.max(topSafe,canvas.top) || rect.top >= Math.min(innerHeight,canvas.bottom) || rect.right <= canvas.left || rect.left >= Math.min(innerWidth,canvas.right)) {
            if (inline && !ensureRoom) return;
            this.closeInspector(false); return;
        }
        const margin = innerWidth < 800 ? 12 : 24, gap = 16;
        const room = {left:rect.left-margin-gap,right:innerWidth-margin-rect.right-gap};
        let placement = node.id === 'density' ? 'bottom' : node.x+node.w/2 < root.x+root.w/2 ? 'right' : 'left';
        const minimumWidth = Math.min(280,innerWidth-2*margin);
        if (placement !== 'bottom' && room[placement] < minimumWidth) {
            const other = placement === 'left' ? 'right' : 'left';
            placement = room[other] >= minimumWidth ? other : 'bottom';
        }
        inspector.classList.remove('is-inline');
        inspector.classList.toggle('is-below',placement === 'bottom');
        let width, left, top;
        if (placement === 'bottom') {
            width = Math.min(960,innerWidth-2*margin);
            if (ensureRoom) {
                const main = document.querySelector('.main-content');
                const minimumHeight = Math.max(100,Math.min(260,innerHeight-topSafe-rect.height-gap-margin));
                const shortage = Math.max(0,rect.bottom+gap+minimumHeight+margin-innerHeight);
                const remaining = Math.max(0,main.scrollHeight-main.clientHeight-main.scrollTop);
                main.scrollTop += Math.min(shortage,remaining,Math.max(0,rect.top-topSafe));
                rect = card.getBoundingClientRect();
            }
            top = Math.max(topSafe,rect.bottom+gap);
            left = Math.max(margin,Math.min((rect.left+rect.right-width)/2,innerWidth-margin-width));
        } else {
            width = Math.min(500,room[placement]);
            left = placement === 'left' ? margin : innerWidth-margin-width;
            top = Math.max(topSafe,Math.min(rect.top,130));
        }
        if (innerHeight-margin-top < 100) {
            // Very short windows use the existing detail area below the canvas rather than cover a card.
            inspector.classList.add('is-inline','is-below');
            inspector.dataset.placement = 'inline';
            if (ensureRoom) inspector.scrollIntoView({block:'nearest'});
            return;
        }
        inspector.dataset.placement = placement;
        inspector.style.setProperty('--di-left',`${left}px`);
        inspector.style.setProperty('--di-top',`${top}px`);
        inspector.style.setProperty('--di-width',`${width}px`);
        inspector.style.setProperty('--di-height',`${innerHeight-margin-top}px`);
    },
    fit() {
        const viewport = document.getElementById('decision-canvas-scroll');
        const scale = this.zoom ?? Math.min(1, Math.max(.65, viewport.clientWidth / 1540));
        this.actualZoom = scale;
        document.getElementById('decision-board').style.transform = `scale(${scale})`;
        const space = document.getElementById('decision-canvas-space');
        space.style.width = `${1540 * scale}px`; space.style.height = `${970 * scale}px`;
        document.getElementById('decision-fit').textContent = `适应画布 · ${Math.round(scale * 100)}%`;
        this.queueInspectorPosition(true);
    },
    changeZoom(delta) { this.zoom = Math.max(.65, Math.min(1.5, (this.actualZoom || 1) + delta)); this.fit(); },
    syncMotion() {
        const button = document.getElementById('decision-motion');
        if (!button) return;
        document.getElementById('decision-workspace').classList.toggle('is-motion-paused', !this.motionEnabled || this.motionReduced || document.hidden || !this.active());
        button.disabled = this.motionReduced;
        button.textContent = this.motionReduced ? '静态视图' : this.motionEnabled ? '暂停动效' : '开启动效';
        button.setAttribute('aria-pressed',String(this.motionEnabled && !this.motionReduced));
        button.title = this.motionReduced ? '已按系统的减少动态效果设置显示静态视图' : '动效示意计算方向，不代表实时采集正在更新';
    },

    buildNodes(s) {
        const n = (id,title,value,source,description,extra = {}) => ({id,title,value,source,description,unit:'',sampleAt:null,enteredAt:null,...extra});
        const family = (group, x, y, extra = {}) => ({family:group,x,y,w:255,h:104,...extra});
        const recordSource = record => record ? '历史业务记录 · 导入 / 补录' : '暂无业务记录';
        const manualInfo = cfg => ({sampleAt:cfg?.sampleAt || null,enteredAt:cfg?.manualAt || null});
        const amountInfo = (key,group) => {
            const cfg = s.store.amountInputs?.[key] || {};
            if (s.reader._manualValid(cfg,key)) return {source:'人工输入',...manualInfo(cfg)};
            if (key === 'floatAmount' && cfg.mode === 'auto' && Number.isFinite(s.floating?.coal_amount)) {
                return {source:recordSource(s.floating),sampleAt:s.floating.timestamp};
            }
            if (key === 'denseAmount' && cfg.mode === 'calc' && Number.isFinite(s.amounts.heavy)) {
                return {source:'总精煤量 − 浮精量 − 粗泥量'};
            }
            const cc = s.store.coarseCalc || {};
            if (key === 'coarseAmount' && Number.isFinite(cc.screen315) && Number.isFinite(cc.waterUnder) && cc.screen315 >= cc.waterUnder) {
                return {source:'315 筛量 − 筛下水量'};
            }
            return Number.isFinite(s.amounts[group]) ? {source:'人工留存值 · 自动量缺失',...manualInfo(cfg)} : {source:'缺少煤量数据'};
        };
        const densityInfo = (() => {
            const cfg = s.store.instrumentInputs?.density || {};
            if (s.reader._manualValid(cfg,'density')) return {source:cfg.autoExec ? '软件侧自动执行值' : '人工输入',...manualInfo(cfg)};
            for (const type of ['density_meter','ash_density']) {
                const logs = (s.store.calcLogs || []).filter(log => log.calc_type === type).sort((a,b) => String(b.timestamp || '').localeCompare(String(a.timestamp || '')));
                for (const log of logs) {
                    try {
                        const input = JSON.parse(log.input_json || '{}');
                        const value = type === 'density_meter' ? +input.value : input.density;
                        if (Number.isFinite(value) && value >= 1.3 && value <= (type === 'density_meter' ? 1.65 : 1.6)) {
                            return {source:type === 'density_meter' ? '密度计历史补录' : '灰分密度历史导入',sampleAt:log.timestamp};
                        }
                    } catch (_) { /* Same valid-record priority as the existing resolver. */ }
                }
            }
            return {source:'默认值 · 非实时测量'};
        })();
        const heavyMeasurement = s.reader.ashMeasurementInfo('heavy');
        const floatManual = s.reader.floatAshLayer() === '手动';
        const floatMeasured = Number.isFinite(s.floating?.ash_content);
        const coarseManual = s.reader.coarseAshLayer() === '手动';
        const coarseSource = coarseManual ? '人工覆盖' : s.coarse ? `${s.engineLabel} 模型 / 化验回退` : '缺少记录';
        const rate = v => this.number(v,1,'t/h');
        const ash = v => this.number(v,2,'%');
        const feature = (id,title,keys,y,format) => {
            const fields = keys.map(key => s.inputs.find(input => input.key === key));
            const value = format ? format(fields) : this.number(fields[0].raw, fields[0].key === 'coal_amount' ? 1 : 2, fields[0].key === 'coal_amount' ? 't/h' : '%');
            const details = fields.map(field => {
                const name = s.reader.FEATURE_LABELS[field.key];
                return `${name}：记录 ${this.number(field.raw)}；模型采用 ${this.number(field.used)}${!s.model ? '（查看模型尚未训练）' : field.missing && Number.isFinite(field.used) ? '（缺失，按训练均值补齐）' : field.clipped ? '（超训练范围，已限幅）' : ''}`;
            }).join('；');
            return n(id,title,value,recordSource(s.coarse),`所查看的${this.view === 'gpt' ? ' GPT' : '当前生产'}模型输入。${details}。采集点位尚未绑定业务字段。`,
                {...family('coarse',1260,y),parent:'coarse',sampleAt:s.coarse?.timestamp,reference:s.previewOnly || !s.model || coarseManual,fields,unit:keys.length === 1 ? (keys[0] === 'coal_amount' ? 't/h' : '%') : '状态'});
        };
        const flags = fields => fields.map(field => {
            const short = field.key.replace('sys','').replace('desliming','');
            return `${short} ${field.raw == null ? '—' : field.raw ? '开' : '关'}`;
        }).join(' · ');
        return [
            n('density','建议密度',s.showAdvice ? this.number(s.guide.rhoNew,3) : '—',s.showAdvice ? (s.ashOk ? '达标保持' : s.guide.direction === 'down' ? '本步建议 · 下调' : '本步建议 · 上调') : s.guide.hold ? '等待新化验 / 新数据' : '暂无有效建议',
                s.guide.reason,{x:670,y:343,w:245,h:207,type:'root',unit:'g/cm³',sampleAt:s.reader.ashMeasurementInfo(s.guide.scheme).sampleAt}),
            n('heavy','重介精煤',ash(s.ashes.heavy),`煤量 ${rate(s.amounts.heavy)}`,`重介灰分与煤量共同进入三组分质量平衡。当前灰分来源：${heavyMeasurement.source}。`,
                {...family('heavy',380,170,{w:220,h:148}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-overview',sampleAt:heavyMeasurement.sampleAt,enteredAt:heavyMeasurement.enteredAt}),
            n('float','浮精',ash(s.ashes.float),`煤量 ${rate(s.amounts.float)}`,`浮精灰分与煤量共同进入三组分质量平衡；压滤机状态为工况参考。`,
                {...family('float',380,612,{w:220,h:148}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-float',sampleAt:s.floating?.timestamp}),
            n('coarse','粗精煤泥',ash(s.ashes.coarse),`煤量 ${rate(s.amounts.coarse)} · 生产 ${s.engineLabel}`,`${coarseSource}。当前生产采用值与下面的 GPT 原始预测分开显示；共享 EMA 不作为 GPT 预测值。`,
                {...family('coarse',965,358.5,{w:220,h:176}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-coarse',sampleAt:coarseManual ? null : s.coarse?.timestamp,enteredAt:coarseManual ? s.store.coarseAshInput?.manualAt : null}),
            n('heavy-ash','重介精煤灰分',ash(s.ashes.heavy),heavyMeasurement.source,'优先有效人工采样值，其次 502 灰分补录 / 导入记录；没有测量记录时使用默认值，不能视作现场实时读数。',
                {...family('heavy',35,45),parent:'heavy',unit:'%',...heavyMeasurement}),
            n('heavy-amount','重介精煤量',rate(s.amounts.heavy),'人工输入 / 总量减浮精减粗泥','有效人工值优先；计算层：总精煤量 − 浮精量 − 粗精煤泥量。缺少组成量时无法计算；总量可能包含默认皮带秤值，需核对来源。',
                {...family('heavy',35,183),parent:'heavy',unit:'t/h',...amountInfo('denseAmount','heavy')}),
            n('density-current','当前分选密度',this.number(s.guide.rhoCur,3,'g/cm³'),'人工输入 / 灰分密度记录 / 默认','建议是在当前密度上按灰分偏差修正，受每步钳制和物理范围约束。这里只显示现有业务取值，不向 PLC 写入。',
                {...family('heavy',380,394.5,{w:220}),parent:'density',unit:'g/cm³',...densityInfo}),
            n('float-ash','浮精灰分',ash(s.ashes.float),floatManual ? '人工覆盖' : floatMeasured ? recordSource(s.floating) : '默认值 · 非实时测量','有效人工覆盖优先，其次时间最新的浮精记录；无数据时原计算链使用默认值。历史化验未记录出报告时刻，不能据采样时刻推断当时已知结果。',
                {...family('float',35,463),parent:'float',unit:'%',...(floatManual ? manualInfo(s.store.floatAshInput) : {sampleAt:floatMeasured ? s.floating.timestamp : null})}),
            n('float-amount','浮精量',rate(s.amounts.float),'人工输入 / 最新浮精记录','有效人工值优先，其次最新浮精记录的 coal_amount。压滤机状态不会自动推导产量。',
                {...family('float',35,581),parent:'float',unit:'t/h',...amountInfo('floatAmount','float')}),
            n('float-press','浮精压滤机运行',s.floating?.filter_press_running == null ? '—' : s.floating.filter_press_running ? '运行' : '停止',recordSource(s.floating),'工况参考：现有密度计算没有使用压滤机运行状态直接预测浮精量。',
                {...family('float',35,699),parent:'float',reference:true,sampleAt:s.floating?.timestamp}),
            n('float-unmodeled','药剂量 / 原煤煤泥含量','待补充','工艺因素 · 未建字段与模型','原图中的工艺因素，目前没有可追溯的输入字段及预测关系。本页保留这一因素，待现场定义测量方式和模型后接入；不填造数值。',
                {...family('float',35,817),parent:'float',reference:true}),
            feature('raw-ash','原煤灰分',['raw_ash'],25),
            feature('coal-rate','入洗原煤量',['coal_amount'],140),
            feature('system-switches','系统运行状态',['sysA','sysB','sys401','sys402'],255,flags),
            feature('desliming','脱粉系统状态',['desliming473','desliming474'],370,flags),
            feature('tail-level','精磁尾液位',['level'],485),
            feature('stoppage','停机 / 低负荷',['is_stoppage'],600,fields => fields[0].raw == null ? '—' : fields[0].raw ? '是' : '否'),
            n('coarse-amount','粗精煤泥量',rate(s.amounts.coarse),s.reader.coarseAmountLayer() === '计算' ? '315 筛量 − 筛下水量' : s.reader.coarseAmountLayer(),
                `量的计算：315 筛量 ${rate(s.store.coarseCalc?.screen315)} − 筛下水量 ${rate(s.store.coarseCalc?.waterUnder)}。这是组分产量，与“入洗原煤量”模型因素不同。`,
                {...family('coarse',1260,715),parent:'coarse',unit:'t/h',...amountInfo('coarseAmount','coarse')}),
            n('coarse-slime','原煤煤泥含量','待补充','工艺因素 · 尚未进入模型','目前的粗精模型使用 10 个输入因素；原煤煤泥含量尚无独立输入字段，也未进入模型。后续需要人工采样或可验证的测量来源。',
                {...family('coarse',1260,830),parent:'coarse',reference:true}),
        ];
    },

    renderContext() {
        const s = this.snapshot, sources = {manual:'人工总灰分',formula:'三组分公式',entry:'501 导入 / 录入',ash501:'501 灰分取值'};
        const message = s.componentFeeds ? '三组分参与当前建议计算' : '三组分显示公式核算对照，建议由所选总灰分驱动';
        document.getElementById('decision-context').innerHTML = `<span>指导口径 <strong>${s.guide.scheme === 'heavy' ? '重介精煤版' : '总灰分版'}</strong></span><span>生产模型 <strong>${this.escape(s.engineLabel)}</strong></span><span>总灰分来源 <strong>${this.escape(sources[s.total.source])}</strong></span><span>${message}</span><span>实时采集 <strong>尚未绑定业务计算</strong></span>`;
    },

    // Short right-angle routes with small rounded corners; aligned nodes stay straight.
    connectorPath(x1,y1,x2,y2,lane) {
        if (Math.abs(y2-y1) < .5) return `M ${x1} ${y1} H ${x2}`;
        const dx = Math.sign(x2-x1), dy = Math.sign(y2-y1);
        const r = Math.min(10,Math.abs(lane-x1)/2,Math.abs(x2-lane)/2,Math.abs(y2-y1)/2);
        return `M ${x1} ${y1} H ${lane-dx*r} Q ${lane} ${y1} ${lane} ${y1+dy*r} V ${y2-dy*r} Q ${lane} ${y2} ${lane+dx*r} ${y2} H ${x2}`;
    },
    routeConnections() {
        return this.nodes.filter(node => node.parent).map(node => {
            const parent = this.nodes.find(n => n.id === node.parent), left = node.x < parent.x;
            const x1 = left ? node.x+node.w : node.x, x2 = left ? parent.x : parent.x+parent.w;
            const y1 = node.y+node.h/2;
            let y2, lane = (x1+x2)/2;
            if (parent.id === 'density') {
                // Keep three left entrances separate; coarse enters from the right.
                y2 = node.id === 'heavy' ? parent.y+42 : node.id === 'float' ? parent.y+162 : parent.y+parent.h/2;
            } else {
                const siblings = this.nodes.filter(n => n.parent === parent.id).sort((a,b) => a.y-b.y);
                const targets = siblings.map((n,i) => ({id:n.id,sourceY:n.y+n.h/2,targetY:parent.y+parent.h*(i+1)/(siblings.length+1)}));
                const target = targets.find(n => n.id === node.id);
                y2 = target.targetY;
                // Nested lanes preserve source/port ordering and avoid crossing fan-in lines.
                const upper = y1 <= y2;
                const peers = targets.filter(n => (n.sourceY <= n.targetY) === upper);
                const rank = peers.findIndex(n => n.id === node.id);
                const gap = Math.abs(x2-x1), inset = Math.min(18,gap/3);
                const step = peers.length > 1 ? Math.min(18,(gap-2*inset)/(peers.length-1)) : 0;
                const distance = inset+(upper ? rank : peers.length-1-rank)*step;
                lane = x2-Math.sign(x2-x1)*distance;
            }
            return {node,parent,path:this.connectorPath(x1,y1,x2,y2,lane)};
        });
    },

    renderGraph() {
        const s = this.snapshot, selected = this.nodes.find(n => n.id === this.selected);
        const colors = {heavy:'var(--dm-blue)',float:'var(--dm-orange)',coarse:'var(--dm-green)'};
        const ancestorIds = new Set([this.selected]);
        let current = selected;
        while (current?.parent) { ancestorIds.add(current.parent); current = this.nodes.find(n => n.id === current.parent); }
        const flowingIds = new Set(), now = Date.now()/1000;
        const paths = this.routeConnections().map(({node,parent,path},index) => {
            const highlight = ancestorIds.has(node.id) && ancestorIds.has(parent.id);
            const flows = !node.reference && node.value !== '—' && parent.value !== '—';
            if (flows) { flowingIds.add(node.id); flowingIds.add(parent.id); }
            // Negative phase keeps motion continuous when the five-second snapshot rebuilds the graph.
            const phase = -((now+index*.37)%4).toFixed(3);
            const trace = flows ? `<path class="decision-flow${highlight ? ' is-highlight' : ''}" d="${path}" pathLength="100" style="--dm-flow-color:${colors[node.family]};animation-delay:${phase}s"/>` : '';
            return `<path class="decision-edge${node.reference ? ' is-reference' : ''}${highlight ? ' is-highlight' : ''}" data-decision-from="${node.id}" data-decision-to="${parent.id}" d="${path}" stroke="${colors[node.family]}" marker-end="url(#decision-arrow-${node.family})"/>${trace}`;
        }).join('');
        const markers = Object.entries(colors).map(([key,color]) => `<marker id="decision-arrow-${key}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8" fill="${color}"/></marker>`).join('');
        const cards = this.nodes.map((node,index) => {
            const active = node.id === this.selected;
            const preview = node.id === 'coarse' ? `<span>查看${this.view === 'gpt' ? ' GPT' : '生产'}预测 ${this.number(s.prediction,2,'%')}${!s.model ? ' · 未训练' : s.previewOnly ? ' · 对照' : ' · 未平滑'}</span>` : '';
            const unit = node.id === 'density' ? '<span class="decision-root-unit">g/cm³</span>' : '';
            const sub = node.id === 'density' ? `<span>当前 ${this.number(s.guide.rhoCur,3)} · 目标灰分 ${this.number(s.guide.targetTotal,2,'%')}</span>` : '';
            const phase = -((now+index*.41)%5).toFixed(3);
            return `<button type="button" class="decision-node${active ? ' is-selected' : ''}${flowingIds.has(node.id) ? ' has-flow' : ''}${node.type === 'root' ? ' decision-root' : node.type === 'component' ? ' decision-component' : ''}" data-decision-node="${node.id}" data-family="${node.family || 'root'}" aria-pressed="${active}" style="left:${node.x}px;top:${node.y}px;width:${node.w}px;min-height:${node.h}px;--dm-pulse-delay:${phase}s"><span class="decision-node-heading">${this.escape(node.title)}${node.reference ? '<span class="decision-badge">对照</span>' : ''}</span><span class="decision-node-value">${this.escape(node.value)}${unit}</span><span class="decision-node-source"><span>${this.escape(node.source)}</span>${sub}${preview}</span></button>`;
        }).join('');
        document.getElementById('decision-board').innerHTML = `<svg class="decision-edges" aria-hidden="true" viewBox="0 0 1540 970"><defs>${markers}</defs>${paths}</svg>${cards}<div class="decision-formula"><strong>三组分质量平衡</strong>总灰分 = Σ（组分灰分 × 组分煤量）/ Σ 组分煤量<br>公式核算 ${this.number(s.formula,2,'%')}<br>当前采用 ${this.number(s.total.value,2,'%')}</div>`;
    },

    telemetryState(item, failed = window.Operations?.telemetryFailed) {
        const labels = {good:'可用',no_data:'无数据',bad_quality:'坏质量',uncertain_quality:'质量不确定',stale:'接收超时',source_stale:'源时间过期',collector_offline:'采集离线',out_of_range:'超出量程',invalid_value_type:'类型不匹配',non_finite_value:'数值无效',future_source_time:'源时钟异常'};
        return failed ? '查询中断 · 最后读数不可用' : `${item.sample?.quality || '无质量信息'} · ${labels[item.status] || '不可用'}`;
    },
    pointIdentity(item) { return JSON.stringify([item.source_id,item.key]); },
    renderInspector() {
        if (!this.snapshot) return;
        const node = this.nodes.find(n => n.id === this.selected) || this.nodes[0], s = this.snapshot;
        const pairs = [['当前业务值',node.value + (node.id === 'density' ? ' g/cm³' : '')],['当前来源',node.source],['采样 / 记录时间',this.time(node.sampleAt)],['人工录入时间',this.time(node.enteredAt)],['化验结果可用时间','未记录（不由采样时刻推断）'],['实时业务绑定','尚未接入']];
        if (node.family && s.sum > 0 && node.type === 'component') {
            const contribution = Number.isFinite(s.ashes[node.family]) ? s.ashes[node.family] * s.amounts[node.family] / s.sum : null;
            pairs.push(['组分质量占比',this.number(s.amounts[node.family] / s.sum * 100,1,'%')],['加权灰分贡献',this.number(contribution,3,'百分点')]);
        }
        if (node.id === 'coarse') {
            pairs.push(['查看模型',s.model ? `${this.view === 'gpt' ? 'GPT' : s.engineLabel} / ${s.modelGroup?.production || s.model.type || '未知'}` : 'GPT 未训练'],['原始模型预测',this.number(s.prediction,2,'%')],['化验灰分',this.number(s.coarse?.ash_content,2,'%')],['模型训练时间',this.time(s.modelGroup?.trainedAt)]);
        }
        if (node.id === 'density') {
            pairs.push(['当前密度',this.number(s.guide.rhoCur,3,'g/cm³')],['灰分容差',this.number(s.guide.deadband,2,'%')],['每步钳制',this.number(s.guide.maxStep,3,'g/cm³')]);
        }
        const facts = pairs.map(([key,value]) => `<dt>${this.escape(key)}</dt><dd>${this.escape(value)}</dd>`).join('');
        const items = window.Operations?.telemetry?.items || [];
        const choice = this.pointChoices[node.id] || '';
        const item = items.find(p => this.pointIdentity(p) === choice);
        const options = items.map(p => `<option value="${this.escape(this.pointIdentity(p))}"${this.pointIdentity(p) === choice ? ' selected' : ''}>${this.escape(`${p.name} · ${p.source_id}/${p.key}`)}</option>`).join('');
        const sample = item?.sample || {};
        const value = typeof sample.value === 'boolean' ? (sample.value ? '开' : '关') : sample.value ?? '—';
        const available = item && item.available && !window.Operations?.telemetryFailed;
        const live = item ? `<dl class="decision-facts"><dt>最后读数</dt><dd class="${available ? '' : 'decision-unavailable'}">${this.escape(value)} ${this.escape(item.unit || '')}${available ? '' : '（不可用）'}</dd><dt>质量 / 时效</dt><dd>${this.escape(this.telemetryState(item))}</dd><dt>数据来源</dt><dd>${this.escape(item.source_id + '/' + item.key)}</dd><dt>源时间</dt><dd>${this.escape(this.time(sample.source_at))}</dd><dt>服务器时间</dt><dd>${this.escape(this.time(sample.server_at))}</dd><dt>接收时间</dt><dd>${this.escape(this.time(sample.received_at))}</dd></dl>` : '<p>尚未选择参考点位。这里可以核对采集读数，选择点位不会绑定业务字段或改变计算。</p>';
        const collector = window.Operations?.telemetryFailed ? '采集查询中断，最后读数不可用' : !items.length ? (window.Operations?.serverState === 'local' ? '单机模式 · 通过服务器地址查看采集' : '暂无点位 · 等待现场配置与联调') : `${items.filter(p => p.available).length} / ${items.length} 个点位可用`;
        document.getElementById('decision-inspector').innerHTML = `<div class="decision-inspector-header"><span>因素与来源详情</span><button type="button" class="decision-inspector-close" data-decision-close>收起 ×</button></div><div class="decision-detail"><div class="decision-detail-heading"><h3>${this.escape(node.title)}</h3><span class="decision-badge">${node.reference ? '对照 / 预留' : '业务取值'}</span></div><p>${this.escape(node.description)}</p><dl class="decision-facts">${facts}</dl>${node.page ? `<button class="decision-detail-link" type="button" data-decision-page="${node.page}">查看对应业务页面 →</button>` : '<button class="decision-detail-link" type="button" data-decision-page="page-collect">查看数据采集与补录 →</button>'}</div><div class="decision-detail decision-source-panel"><h4>实时数据 → 采集来源</h4><p class="decision-live-state">${this.escape(collector)}。实时读数目前仅作来源核对，尚未参与本页计算。</p><label for="decision-source-point">查看采集点（仅核对）</label><select id="decision-source-point"><option value="">选择参考点位</option>${options}</select>${live}</div>`;
        this.queueInspectorPosition();
    },
};
