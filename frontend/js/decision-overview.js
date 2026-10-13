/* GPT live decision map; snapshots never mutate App.store, saved models or PLC values. */
const DecisionOverviewPage = {
    initialized: false, view: 'gpt', selected: 'density', zoom: null, snapshot: null,
    pointChoices: {}, nodes: [], timer: null, aggregationMode:'instant',
    summaryChildren: new Map([['heavy',['heavy-ash','heavy-amount']],['float',['float-ash','float-amount']]]),
    sourceInteractionUntil: 0,
    inspectorFrame: null,
    motionReduced: false, motionPrinting: false,
    revealFrame: null,
    background: null,
    choicesKey: 'dmcs.decision-point-choices.v1',
    retainedKey: 'dmcs.decision-retained-values.v1', retainedValues: {},
    escape(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    number(value, digits = 2, unit = '') { return Number.isFinite(value) ? value.toFixed(digits) + (unit ? ' ' + unit : '') : '—'; },
    time(value) {
        if (!value) return '未记录';
        const date = new Date(value);
        return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12:false}) + (date.getTime() > Date.now() + 60000 ? '（未来时间，需核对）' : '') : '未记录';
    },
    active() { return !!document.getElementById('page-decision')?.classList.contains('active'); },
    liveMode() { return typeof DecisionRealtime !== 'undefined' && DecisionRealtime.active(this); },
    hourMode() { return this.view==='gpt' && this.aggregationMode==='hourly' && typeof DecisionHourly!=='undefined'; },
    instantLayout() { return this.view==='gpt' && !this.hourMode(); },
    graphWidth() { return 1540; },
    graphHeight() { return 970; },
    renderHTML(id, html) {
        const host=document.getElementById(id);
        // Keep the legacy production view unchanged. GPT refreshes patch existing
        // text/attributes so focused controls and running SVG animations survive.
        if (!this.liveMode() || !document.createElement) { host.innerHTML=html; host._decisionHTML=null; return; }
        if (host._decisionHTML===html) return;
        const template=document.createElement('template'); template.innerHTML=html;
        const previous=new Map([...host.querySelectorAll('.decision-node-value')].map(el=>[el,el.textContent]));
        const key=n=>n.nodeType===1 ? n.getAttribute('data-decision-key') || n.getAttribute('data-decision-node') || n.id : null;
        const compatible=(a,b)=>a && a.nodeType===b.nodeType && a.nodeName===b.nodeName && key(a)===key(b);
        const sync=(parent,next)=>{
            let index=0;
            for(const wanted of [...next.childNodes]) {
                let current=parent.childNodes[index];
                if(!compatible(current,wanted)) {
                    const found=key(wanted) && [...parent.childNodes].slice(index+1).find(n=>compatible(n,wanted));
                    if(found) { parent.insertBefore(found,current || null); current=found; }
                    else { parent.insertBefore(wanted.cloneNode(true),current || null); index++; continue; }
                }
                if(wanted.nodeType===1) {
                    for(const attr of [...current.attributes]) if(!wanted.hasAttribute(attr.name)) current.removeAttribute(attr.name);
                    for(const attr of [...wanted.attributes]) if(current.getAttribute(attr.name)!==attr.value) current.setAttribute(attr.name,attr.value);
                    sync(current,wanted);
                    // User interaction dirties option.selected; attributes alone
                    // cannot restore the next card's actual selected source.
                    if(current.nodeName==='SELECT' && current.value!==wanted.value) current.value=wanted.value;
                } else if(current.nodeValue!==wanted.nodeValue) current.nodeValue=wanted.nodeValue;
                index++;
            }
            while(parent.childNodes.length>index) parent.lastChild.remove();
        };
        sync(host,template.content); host._decisionHTML=html;
        if(!this.motionReduced && !this.motionPrinting && !document.hidden) for(const [el,text] of previous) {
            if(el.isConnected && el.textContent!==text && el.animate) {
                el._decisionTransition?.cancel();
                el._decisionTransition=el.animate([{opacity:.6},{opacity:1}],{duration:220,easing:'ease-out'});
            }
        }
    },
    fixedSource(id) { return this.summaryChildren.has(id) || id === 'heavy-amount' || (this.liveMode() && id==='coarse-amount'); },
    sharedSources() { return typeof DecisionSources!=='undefined' && DecisionSources.enabled(this); },
    densityLabel() { return this.hourMode() ? DecisionHourly.presentation()?.density_label || '等待读取' : this.sharedSources() ? DecisionRealtime.result?.density_label || '等待读取' : 'A304'; },
    sourceLocked(id) { return this.fixedSource(id) || (this.liveMode() && (['coarse','density'].includes(id) || (id==='density-current' && !this.sharedSources()))); },
    loadPointChoices() {
        try {
            const saved = JSON.parse(localStorage.getItem(this.choicesKey) || '{}'), choices = Object.create(null);
            if (saved && typeof saved === 'object' && !Array.isArray(saved)) for (const [id,value] of Object.entries(saved)) {
                if (id === 'stoppage' || this.fixedSource(id) || !/^[a-z][a-z0-9-]{0,63}$/.test(id) || typeof value !== 'string' || value.length > 160) continue;
                if (value === '') { choices[id] = ''; continue; }
                let identity;
                try { identity = JSON.parse(value); } catch (_) { continue; }
                if (Array.isArray(identity) && identity.length === 2 && identity.every(v => typeof v === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(v))) choices[id] = value;
            }
            this.pointChoices = choices;
            if (Object.keys(saved || {}).some(id => this.fixedSource(id))) this.savePointChoices();
        } catch (_) { /* An invalid or unavailable local preference does not alter business data. */ }
    },
    savePointChoices() {
        if(typeof DecisionSources!=='undefined' && !this.liveMode()) DecisionSources.legacyChoices={...this.pointChoices};
        try { localStorage.setItem(this.choicesKey, JSON.stringify(this.pointChoices)); }
        catch (_) { /* Keep the current selection in memory if local storage is unavailable. */ }
    },
    loadRetainedValues() {
        try {
            const saved = JSON.parse(localStorage.getItem(this.retainedKey) || '{}');
            if (saved && typeof saved === 'object' && !Array.isArray(saved)) this.retainedValues = saved;
        } catch (_) { /* Server memories remain usable when browser storage is unavailable. */ }
    },
    retainedSample(node, choice, item, current, nonzero) {
        const key = JSON.stringify([node.id, choice]), signature = item?.sample?.definition_id;
        const server = item?.[nonzero ? 'last_nonzero_sample' : 'last_valid_sample'];
        const cached = this.retainedValues[key];
        const candidates = [server, current, cached].filter(sample => sample &&
            Number.isFinite(sample.value) && (!nonzero || sample.value !== 0) &&
            sample.quality === 'good' && !sample.issue && Number.isFinite(Date.parse(sample.received_at)) &&
            (!item || (sample.unit === item.unit && sample.definition_id === signature)));
        const sample = candidates.sort((a,b) => Date.parse(b.received_at) - Date.parse(a.received_at))[0];
        if (!sample) return null;
        const retained = {value:sample.value, quality:'good', issue:null, received_at:sample.received_at,
            source_at:sample.source_at, server_at:sample.server_at, unit:sample.unit,
            definition_id:sample.definition_id};
        if (JSON.stringify(cached) !== JSON.stringify(retained)) {
            delete this.retainedValues[key];
            this.retainedValues[key] = retained;
            const keys = Object.keys(this.retainedValues);
            for (const old of keys.slice(0, Math.max(0, keys.length - 64))) delete this.retainedValues[old];
            try { localStorage.setItem(this.retainedKey, JSON.stringify(this.retainedValues)); } catch (_) { /* In-memory fallback. */ }
        }
        return retained;
    },
    displayPoints() { return [...(window.Operations?.manualMeasurements?.items || []), ...(window.Operations?.telemetry?.items || []), ...(window.Operations?.telemetry?.derived_items || [])]; },
    applyDefaultManualPoint() {
        if(this.sharedSources()) return;
        if (Object.prototype.hasOwnProperty.call(this.pointChoices, 'heavy-ash') || window.Operations?.manualMeasurementsFailed) return;
        const point = window.Operations?.manualMeasurements?.items?.find(p => p.key === 'heavy_clean_coal_ash' && p.available);
        if (point) { this.pointChoices['heavy-ash'] = this.pointIdentity(point); this.savePointChoices(); }
    },
    pointFailed(item) {
        return item?.kind === 'manual_measurement' ? !!window.Operations?.manualMeasurementsFailed : !!window.Operations?.telemetryFailed;
    },
    manualDisplay(item) {
        const sample = item?.sample || {}, failed = !!window.Operations?.manualMeasurementsFailed;
        const valid = !!item?.available && Number.isFinite(sample.value);
        const held = valid && failed;
        const state = failed ? '补录查询失败 · 待刷新' : valid ? '已保存到 SQLite' : '暂无已到时的有效补录';
        return {bound:true, manual:true, item, available:valid && !failed, held, stopped:false, components:null,
            numericValue:valid ? sample.value : null,unit:item?.unit || '',
            value:valid ? new Intl.NumberFormat('zh-CN', {maximumFractionDigits:3, useGrouping:false}).format(sample.value) + ' ' + item.unit : '—',
            sampleAt:sample.observed_at || null, enteredAt:sample.created_at || null,
            source:valid ? `${held ? '补录查询失败 · 上次读取值' : '手工补录'} · ${this.time(sample.observed_at)}` : state,
            title:`SQLite 手工补录；${state}；测量时间 ${this.time(sample.observed_at)}；录入时间 ${this.time(sample.created_at)}`};
    },
    groupValue(components) { return (components || []).map(c => `${c.label} ${c.available && !window.Operations?.telemetryFailed ? c.value ? '开' : '关' : '未知'}`).join(' · '); },
    densityDisplay(item) {
        // Business density is resolved per channel on the server. Never reuse the
        // old whole-average browser cache, which may contain a 1.000 component.
        const effective = item?.effective_density;
        const valid = effective?.policy === 'density-per-channel-nonone-v1' && Number.isFinite(effective.value);
        const held = !!(valid && (effective.status === 'held' || window.Operations?.telemetryFailed));
        const available = !!(valid && effective.status === 'good' && !window.Operations?.telemetryFailed);
        const reason = effective?.reason || (window.Operations?.telemetryFailed ? '采集请求异常 · 沿用最近有效值' : '');
        const parts = effective?.components || (effective ? [effective] : []);
        const detail = parts.map(p => `${p.label || item?.device || '密度'}：${this.number(p.value, 3, App.telemetryDisplayUnit(p))}；${p.reason || '实时读数'}；${this.time(p.received_at)}`).join('；');
        return {bound:true,item,available,held,stopped:false,components:null,
            numericValue:valid ? effective.value : null,unit:effective?.unit || App.telemetryDisplayUnit(item),
            value:valid ? this.number(effective.value, 3, effective.unit || App.telemetryDisplayUnit(item)) : '—',
            sampleAt:effective?.received_at || null,valueSourceAt:effective?.source_at || null,
            source:valid ? (held ? (reason || '沿用本路最近非 1 有效值') : 'KEP · ' + (item.name || '当前密度')) : (reason || '暂无有效密度'),
            title:detail || reason || '暂无有效密度'};
    },
    pointDisplay(node) {
        if(this.hourMode()) return DecisionHourly.display(this,node);
        if (this.liveMode()) {
            if (['coarse','density'].includes(node.id)) return DecisionRealtime.display(this,node);
            if(this.sharedSources()) {
                const shared=DecisionSources.display(this,node);
                if(shared) return shared;
            }
            if(node.id==='coarse-amount') {
                const input=this.pointDisplay(this.nodes.find(n=>n.id==='coal-rate'));
                const valid=Number.isFinite(input.numericValue) && input.numericValue>=0 && input.unit==='t/h' && (this.displayAvailable(input) || input.held);
                const value=valid?input.numericValue/40:null;
                return {calculated:true,formula:'入洗原煤量 ÷ 40',bound:false,value:this.number(value,2,'t/h'),
                    numericValue:value,unit:'t/h',available:valid && !input.held && !input.stopped,held:input.held,stopped:input.stopped,
                    badge:input.stopped?'设备停机':valid?'计算':'不可用',source:'计算 · 入洗原煤量 ÷ 40',sampleAt:input.sampleAt,
                    reason:valid?'入洗：粗精 = 40：1':'入洗原煤量不可用',inputs:[{node:{title:'入洗原煤量'},display:input}]};
            }
            if (node.id === 'density-current') {
                const matches=this.displayPoints().filter(p=>p.item_id==='TIA_4_1.TIA_4_1.A304/MD');
                const display=this.densityDisplay(matches.length===1?matches[0]:null);
                return {...display,calculated:true,formula:'A304 实际密度；缺失或为 1 时仅沿用本路最近有效值',
                    badge:display.available?'采集':display.held?'沿用':'不可用',reason:display.title,
                    inputs:[{node:{title:'A304 实际密度'},display}]};
            }
            if (DecisionRealtime.defaults[node.id] && !Object.prototype.hasOwnProperty.call(this.pointChoices,node.id)) {
                return {bound:true,available:false,held:false,numericValue:null,unit:node.unit,value:'—',source:'等待 KEP 点位',title:'尚未取得唯一的对应采集点'};
            }
        }
        if (node.id === 'heavy-amount') return this.heavyAmountDisplay();
        if (this.summaryChildren.has(node.id)) {
            const summaryParts = this.summaryChildren.get(node.id).map(id => {
                const child = this.nodes.find(n => n.id === id);
                return {node:child,display:child ? this.pointDisplay(child) : {bound:false,value:'—',source:'暂无数据'}};
            });
            const available = summaryParts.every(p => this.displayAvailable(p.display));
            const missing = summaryParts.filter(p => p.display.value === '—').length;
            const held = summaryParts.some(p => p.display.held);
            const badge = missing === 2 ? '数据缺失' : missing ? '部分缺失' : held ? '待刷新' : '汇总';
            return {aggregate:true,bound:false,summaryParts,available,held,badge,
                value:summaryParts[0].display.value,source:`煤量 ${summaryParts[1].display.value}`,
                title:summaryParts.map(p => `${p.node?.title || '数据'}：${p.display.value}；${p.display.source}`).join('；')};
        }
        const choice = this.pointChoices[node.id];
        if (!choice) return {bound:false,value:node.value,numericValue:node.numericValue ?? null,unit:node.unit,source:node.source,sampleAt:node.sampleAt,enteredAt:node.enteredAt};
        const item = this.displayPoints().find(p => this.pointIdentity(p) === choice);
        if (item?.kind === 'manual_measurement' || choice.startsWith('["manual-measurements",')) return this.manualDisplay(item);
        if (node.id === 'density-current' && (!item || item.key === 'a303_a304_density_average' ||
            ['TIA_4_1.TIA_4_1.A303/MD','TIA_4_1.TIA_4_1.A304/MD'].includes(item?.item_id))) return this.densityDisplay(item);
        const sample = item?.sample || {};
        const group = item?.kind === 'state_group';
        const derived = group || item?.kind === 'numeric_average';
        const hasValue = group ? item.member_count > 0 && sample.components?.length === item.member_count && sample.value != null : typeof sample.value === 'boolean' || Number.isFinite(sample.value);
        let available = !!item?.available && !window.Operations?.telemetryFailed && sample.quality === 'good' && !sample.issue && hasValue;
        const nonzero = node.id === 'raw-ash' || node.id === 'coal-rate';
        const stopped = nonzero && available && sample.value === 0;
        const numericRule = nonzero || node.id === 'density-current';
        const previous = numericRule ? this.retainedSample(node, choice, item,
            available && Number.isFinite(sample.value) ? {...sample,unit:item.unit} : null, nonzero) : null;
        const fallback = stopped || (node.id === 'density-current' && !available);
        const held = !!(fallback && previous);
        if (stopped) available = false;
        const shown = held ? previous : sample;
        const shownUnit = shown.unit || App.telemetryDisplayUnit(item);
        let value = '—';
        if (group) value = this.groupValue(sample.components);
        else if (available || held) {
            if (typeof shown.value === 'boolean') value = shown.value ? '1' : '0';
            else {
                const magnitude = Math.abs(shown.value);
                value = magnitude >= 1e7 || (magnitude > 0 && magnitude < .001) ? shown.value.toExponential(3) :
                    new Intl.NumberFormat('zh-CN', {maximumFractionDigits:3, useGrouping:false}).format(shown.value);
            }
            if (shownUnit) value += ' ' + shownUnit;
        }
        const state = stopped ? '设备停机' : item ? this.telemetryState(item) : '已选点位未返回';
        const received = shown.received_at ? new Date(shown.received_at) : null;
        const clock = received && Number.isFinite(received.getTime()) ? received.toLocaleTimeString('zh-CN', {hour12:false}) : '时间未记录';
        return {bound:true,item,available,held,stopped,value,components:group ? sample.components : null,sampleAt:shown.received_at || null,valueSourceAt:shown.source_at || null,
            numericValue:(available || held) && Number.isFinite(shown.value) ? shown.value : null,unit:shownUnit,
            source:held ? `${stopped ? '设备停机' : '采集异常'} · 沿用上次有效值 · ${clock}` :
                stopped ? '设备停机 · 暂无历史有效值' : available ? `${group ? '组合' : derived ? '组合计算' : 'KEP'} · ${derived && !group ? item.name : item.tag_name || item.name} · ${clock}` : `不可用 · ${state}`,
            title:`${derived ? '本系统派生的'+item.name : item?.item_id || sample.node_id || choice}；${state}；${held ? '沿用上次有效值；' : ''}${group ? '状态组合，无单位' : shownUnit ? '单位 '+shownUnit : '单位未配置'}；数值接收时间 ${this.time(shown.received_at)}`};
    },
    heavyAmountDisplay() {
        const formula = '501 + 502 − 浮精量 − 粗精煤泥量';
        if(this.sharedSources()) {
            const result=DecisionRealtime.result, c=result?.cells?.heavy_amount;
            const inputs=[['belt501','501 瞬时带煤量'],['belt502','502 瞬时带煤量'],['float_amount','浮精量'],['coarse_amount','粗精煤泥量']].map(([key,title])=>{
                const input=result?.inputs?.[key] || {};
                return {node:{title},display:{value:this.number(input.value,3,input.unit),source:input.source,sampleAt:input.observed_at}};
            });
            return {calculated:true,bound:false,inputs,formula,unit:'t/h',numericValue:c?.value ?? null,
                available:Number.isFinite(c?.value) && c.status!=='held',held:c?.status==='held',
                value:this.number(c?.value,1,'t/h'),badge:c?.value==null?'不可计算':'计算',
                source:c?.reason || formula,reason:c?.reason || '',sampleAt:c?.observed_at};
        }
        const inputs = ['501','502'].map(belt => {
            const itemId = `TIA_3_4.TIA_3_4.${belt}/COAL_CAP`;
            const matches = (window.Operations?.telemetry?.items || []).filter(p => p.item_id === itemId);
            const item = matches.length === 1 ? matches[0] : null, sample = item?.sample || {};
            const available = !!item?.available && !window.Operations?.telemetryFailed && sample.quality === 'good' && !sample.issue && Number.isFinite(sample.value);
            const unit = App.telemetryDisplayUnit(item || {item_id:itemId});
            const state = matches.length > 1 ? '点位存在多个采集来源' : item ? this.telemetryState(item) : '点位未返回';
            return {node:{title:`${belt} 瞬时带煤量`},display:{bound:true,item,available,held:false,unit,
                numericValue:available ? sample.value : null,value:available ? this.number(sample.value,3,unit) : '—',
                source:`KEP · ${itemId} · ${state}${item && !String(item.unit || '').trim() ? '；单位沿用在线皮带秤' : ''}`,
                sampleAt:sample.received_at,valueSourceAt:sample.source_at}};
        });
        for (const id of ['float-amount','coarse-amount']) {
            const child = this.nodes.find(n => n.id === id);
            inputs.push({node:child || {title:id === 'float-amount' ? '浮精量' : '粗精煤泥量'},
                display:child ? this.pointDisplay(child) : {value:'—',numericValue:null,source:'暂无数据'}});
        }
        const errors = [];
        for (const {node,display:d} of inputs) {
            if ((!this.displayAvailable(d) && !d.held) || !Number.isFinite(d.numericValue)) errors.push(`${node.title}不可用：${d.source}`);
            else if (d.numericValue < 0) errors.push(`${node.title}为负值`);
            else if (d.unit !== 't/h') errors.push(`${node.title}单位不是 t/h`);
        }
        const [a,b] = inputs.map(p => p.display.item);
        if (a && b) {
            const sameBatch = a.source_id === b.source_id && Number.isFinite(Date.parse(a.sample?.received_at)) &&
                a.sample.received_at === b.sample?.received_at &&
                (!(a.sample.batch_id || b.sample.batch_id) || (!!a.sample.batch_id && a.sample.batch_id === b.sample.batch_id));
            if (!sameBatch) errors.push('501、502 不属于同一采集批次');
        }
        // Use unrounded source numbers for the same mass-balance equation as
        // resolveAmount('denseAmount'); an invalid balance is visible, not clamped to zero.
        const values = inputs.map(p => p.display.numericValue);
        let raw = errors.length ? null : values[0] + values[1] - values[2] - values[3];
        const roundoff = Number.EPSILON * Math.max(...values.map(Math.abs)) * 4;
        if (raw < 0 && raw >= -roundoff) raw = 0; // e.g. 0.3 + 0.6 − 0.9; never clamp a real imbalance.
        if (raw !== null && !Number.isFinite(raw)) errors.push('计算结果不是有效数值');
        else if (raw !== null && raw < 0) errors.push('煤量不平衡：浮精量与粗精煤泥量之和超过 501、502 合计');
        const valid = !errors.length, held = valid && inputs.some(p => p.display.held);
        const numericValue = valid ? raw : null;
        return {calculated:true,bound:false,inputs,formula,unit:'t/h',numericValue,available:valid && !held,held,
            value:this.number(numericValue,1,'t/h'),badge:valid ? held ? '待刷新' : '计算' : '不可计算',
            source:valid ? `${formula}${held ? ' · 沿用值待刷新' : ''}` : '不可计算 · ' + errors[0].split('：')[0],
            reason:errors.join('；'),sampleAt:valid ? inputs[0].display.sampleAt : null,
            title:`${formula}；${errors.length ? errors.join('；') : inputs.map(p => `${p.node.title} ${p.display.value}；${p.display.source}`).join('；')}`};
    },
    displayAvailable(display) { return display.bound || display.aggregate || display.calculated ? display.available : display.value !== '—'; },
    telemetryUpdated() {
        if (!this.initialized || !this.active() || document.hidden || !this.snapshot) return;
        this.applyDefaultManualPoint();
        let migrated = false;
        for (const [nodeId,groupKey] of this.sharedSources() ? [] : [['system-switches','four_system_status'],['desliming','desliming_status'],['float-press','float_press_status']]) {
            const old = (window.Operations?.telemetry?.items || []).find(p => this.pointIdentity(p) === this.pointChoices[nodeId]);
            const group = (window.Operations?.telemetry?.derived_items || []).find(p => p.key === groupKey && p.source_id === old?.source_id && p.sample?.components?.some(c => c.point_key === old?.key));
            if (group) { this.pointChoices[nodeId] = this.pointIdentity(group); migrated = true; }
        }
        if (migrated) this.savePointChoices();
        if (this.liveMode()) { if(!this.sharedSources()) DecisionRealtime.bindDefaults(this); DecisionRealtime.update(this); }
        const focused = document.activeElement?.dataset?.decisionNode;
        this.renderGraph(); this.renderContext();
        if (document.activeElement?.id !== 'decision-source-point' || Date.now() > this.sourceInteractionUntil) this.renderInspector();
        if (focused) document.querySelector(`[data-decision-node="${focused}"]`)?.focus({preventScroll:true});
        this.queueInspectorPosition();
    },

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
        this.loadPointChoices();
        this.loadRetainedValues();
        document.querySelectorAll('[data-decision-aggregation]').forEach(button=>button.addEventListener('click',()=>{
            this.aggregationMode=button.dataset.decisionAggregation;
            this.closeInspector(false); this.refresh();
        }));
        if (typeof DecisionBackground !== 'undefined') this.background = new DecisionBackground(document.getElementById('decision-background-flow'));
        document.getElementById('decision-refresh').addEventListener('click', () => {
            this.sourceInteractionUntil = 0;
            this.refresh();
            if (typeof DecisionRecentAsh !== 'undefined') DecisionRecentAsh.update(this,true);
            if (window.Operations) window.Operations.refresh().then(() => this.renderInspector());
        });
        document.getElementById('decision-theme').addEventListener('click', event => {
            const light = document.getElementById('decision-workspace').classList.toggle('is-light');
            event.currentTarget.textContent = light ? '深色视图' : '浅色视图';
            if (typeof DecisionCoarseCharts !== 'undefined') DecisionCoarseCharts.refresh();
            if (typeof DecisionFloatAnalysis !== 'undefined') DecisionFloatAnalysis.refresh();
        });
        const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
        this.motionReduced = reducedMotion.matches;
        reducedMotion.addEventListener('change', event => { this.motionReduced = event.matches; this.syncMotion(); });
        const printing = window.matchMedia('print');
        this.motionPrinting = printing.matches;
        printing.addEventListener('change', event => { this.motionPrinting = event.matches; this.syncMotion(); });
        // Also stop on programmatic navigation, which does not dispatch a nav click.
        this.activityObserver = new MutationObserver(() => {
            this.syncMotion();
            if (!this.active() && typeof DecisionCoarseCharts !== 'undefined' && DecisionCoarseCharts.initialized) DecisionCoarseCharts.resetPointer();
            if (!this.active() && typeof DecisionFloatAnalysis !== 'undefined' && DecisionFloatAnalysis.initialized) DecisionFloatAnalysis.resetPointer();
        });
        this.activityObserver.observe(document.getElementById('page-decision'), {attributes:true, attributeFilter:['class']});
        document.addEventListener('visibilitychange', () => this.syncMotion());
        document.querySelectorAll('[data-decision-model]').forEach(button => button.addEventListener('click', () => {
            this.view = button.dataset.decisionModel;
            if(typeof DecisionSources!=='undefined' && DecisionSources.config) {
                this.pointChoices={...(this.view==='gpt' ? DecisionSources.config.resolved_choices : DecisionSources.legacyChoices || {})};
            }
            document.querySelectorAll('[data-decision-model]').forEach(b => {
                const selected = b.dataset.decisionModel === this.view;
                b.classList.toggle('is-active', selected); b.setAttribute('aria-pressed', String(selected));
            });
            this.refresh();
        }));
        document.getElementById('decision-fit').addEventListener('click', () => { this.zoom = null; this.fit(); });
        document.getElementById('decision-zoom-in').addEventListener('click', () => this.changeZoom(.15));
        document.getElementById('decision-zoom-out').addEventListener('click', () => this.changeZoom(-.15));
        document.getElementById('decision-workspace').addEventListener('click', event => {
            if (event.target.closest('[data-ash-history]') && this.view === 'gpt') { DecisionAshHistory.open(this); return; }
        });
        document.getElementById('decision-canvas-scroll').addEventListener('click', event => {
            const node = event.target.closest('[data-decision-node]');
            if (node) { this.select(node.dataset.decisionNode); return; }
            if (!event.target.closest('.decision-formula,button,a,input,select,textarea,label,[contenteditable="true"]')) this.closeInspector(false);
        });
        let totalAlignFrame=null;
        document.getElementById('decision-canvas-scroll').addEventListener('scroll',()=>{
            if(totalAlignFrame) return;
            totalAlignFrame=requestAnimationFrame(()=>{totalAlignFrame=null;this.alignToolbarTotal();});
        },{passive:true});
        document.getElementById('decision-inspector').addEventListener('change', event => {
            if (event.target.id === 'decision-source-point' && !this.sourceLocked(this.selected)) {
                if(this.sharedSources()) { DecisionSources.change(this,this.selected,event.target.value); return; }
                this.pointChoices[this.selected] = event.target.value;
                this.savePointChoices();
                this.sourceInteractionUntil = Date.now() + 500;
                if (this.liveMode()) DecisionRealtime.update(this);
                this.renderGraph(); this.renderContext(); this.renderInspector();
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
        });
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && this.active()) this.closeInspector();
        });
        for (const container of [document.querySelector('.main-content'),document.getElementById('decision-canvas-scroll')]) {
            container.addEventListener('scroll', () => { this.queueInspectorPosition(); this.queueReveal(); }, {passive:true});
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
        document.getElementById('decision-workspace').closest('.decision-foreground').classList.toggle('is-hourly',this.hourMode());
        document.querySelectorAll('[data-decision-aggregation]').forEach(button=>{
            button.disabled=this.view!=='gpt';
            const selected=button.dataset.decisionAggregation===(this.hourMode()?'hourly':'instant');
            button.classList.toggle('is-active',selected); button.setAttribute('aria-pressed',String(selected));
        });
        this.applyDefaultManualPoint();
        if (this.liveMode() && !this.sharedSources()) DecisionRealtime.bindDefaults(this);
        const focused = document.activeElement?.dataset?.decisionNode;
        const sourceFocused = document.activeElement?.id === 'decision-source-point';
        const closeFocused = document.activeElement?.hasAttribute('data-decision-close');
        this.snapshot = this.buildSnapshot();
        this.nodes = this.buildNodes(this.snapshot);
        if (this.liveMode()) DecisionRealtime.update(this);
        if (typeof DecisionRecentAsh !== 'undefined') DecisionRecentAsh.update(this);
        this.renderGraph(); this.renderContext(); this.renderInspector(); this.fit();
        if (typeof DecisionCoarseCharts !== 'undefined') DecisionCoarseCharts.refresh();
        if (typeof DecisionFloatAnalysis !== 'undefined') DecisionFloatAnalysis.refresh();
        if (focused) document.querySelector(`[data-decision-node="${focused}"]`)?.focus({preventScroll:true});
        if (sourceFocused) document.getElementById('decision-source-point')?.focus({preventScroll:true});
        if (closeFocused) document.querySelector('[data-decision-close]')?.focus({preventScroll:true});
    },
    select(id) {
        this.selected = id; this.renderGraph(); this.renderInspector(true);
        document.getElementById('decision-inspector').classList.add('is-open');
        this.positionInspector(true);
        document.querySelector('[data-decision-close]')?.focus({preventScroll:true});
    },
    closeInspector(restoreFocus = true) {
        const inspector = document.getElementById('decision-inspector');
        if (!inspector?.classList?.contains('is-open')) return;
        inspector.classList.remove('is-open','is-below','is-inline');
        this.queueReveal();
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
        // Browser zoom-out expands the CSS viewport. Keep detail panels beside the centered page.
        const pageBounds=document.getElementById('page-decision').getBoundingClientRect();
        const bounds={left:Math.max(margin,pageBounds.left+margin),right:Math.min(innerWidth-margin,pageBounds.right-margin)};
        const room = {left:rect.left-bounds.left-gap,right:bounds.right-rect.right-gap};
        let placement = node.id === 'density' ? 'bottom' : node.x+node.w/2 < root.x+root.w/2 ? 'right' : 'left';
        const minimumWidth = Math.min(280,bounds.right-bounds.left);
        if (placement !== 'bottom' && room[placement] < minimumWidth) {
            const other = placement === 'left' ? 'right' : 'left';
            placement = room[other] >= minimumWidth ? other : 'bottom';
        }
        inspector.classList.remove('is-inline');
        inspector.classList.toggle('is-below',placement === 'bottom');
        this.queueReveal();
        let width, left, top;
        if (placement === 'bottom') {
            width = Math.min(960,bounds.right-bounds.left);
            if (ensureRoom) {
                const main = document.querySelector('.main-content');
                const minimumHeight = Math.max(100,Math.min(260,innerHeight-topSafe-rect.height-gap-margin));
                const shortage = Math.max(0,rect.bottom+gap+minimumHeight+margin-innerHeight);
                const remaining = Math.max(0,main.scrollHeight-main.clientHeight-main.scrollTop);
                main.scrollTop += Math.min(shortage,remaining,Math.max(0,rect.top-topSafe));
                rect = card.getBoundingClientRect();
            }
            top = Math.max(topSafe,rect.bottom+gap);
            left = Math.max(bounds.left,Math.min((rect.left+rect.right-width)/2,bounds.right-width));
        } else {
            width = Math.min(500,room[placement]);
            left = placement === 'left' ? bounds.left : bounds.right-width;
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
    alignToolbarTotal() {
        const total=document.getElementById('decision-toolbar-total'), toolbar=total?.parentElement;
        if(!toolbar) return;
        toolbar.classList.remove('has-total-row');
        if(total.hidden) return;
        const density=document.querySelector('#decision-board [data-decision-node="density"]');
        if(!density) return;
        const bar=toolbar.getBoundingClientRect(), card=density.getBoundingClientRect();
        const half=total.offsetWidth/2, first=total.querySelector('.decision-ash-metric');
        // Keep the predicted total ash centered above the density card.
        const center=card.left+card.width/2-bar.left+half-(first?.offsetWidth ?? total.offsetWidth)/2;
        total.style.left=`${Math.max(half+12,Math.min(bar.width-half-12,center))}px`;
        toolbar.style.setProperty('--dm-total-height',`${total.offsetHeight}px`);
        const ash=total.getBoundingClientRect();
        const actions=document.getElementById('decision-actions').getBoundingClientRect();
        const zoom=toolbar.querySelector('.decision-zoom').getBoundingClientRect();
        toolbar.classList.toggle('has-total-row',ash.left<actions.right+16 || ash.right>zoom.left-16);
    },
    fit() {
        const viewport = document.getElementById('decision-canvas-scroll');
        const width=this.graphWidth();
        const scale = this.zoom ?? Math.min(1, Math.max(.65, viewport.clientWidth / width));
        const instant=this.instantLayout(), height=this.graphHeight();
        const geometryKey=`${scale}|${viewport.clientWidth}|${innerHeight}|${width}|${height}|${instant}`;
        if(this.liveMode() && this.fitKey===geometryKey) { this.alignToolbarTotal(); this.queueInspectorPosition(); return; }
        this.fitKey=geometryKey;
        this.actualZoom = scale;
        const board=document.getElementById('decision-board');
        board.style.transform = `scale(${scale})`;
        board.style.width = `${width}px`;
        board.style.height = `${height}px`;
        board.classList.toggle('is-instant-layout',instant);
        const space = document.getElementById('decision-canvas-space');
        space.style.width = `${width * scale}px`; space.style.height = `${height * scale}px`;
        document.querySelectorAll('[data-instant-action]').forEach(button=>{button.hidden=!instant;});
        this.alignToolbarTotal();
        this.background?.resize();
        document.getElementById('decision-fit').textContent = `适应画布 · ${Math.round(scale * 100)}%`;
        this.queueInspectorPosition(true);
        this.queueReveal();
    },
    changeZoom(delta) { this.zoom = Math.max(.65, Math.min(1.5, (this.actualZoom || 1) + delta)); this.fit(); },
    syncMotion() {
        const paused = this.motionReduced || this.motionPrinting || document.hidden || !this.active();
        if (paused) this.background?.setRunning(false);
        document.getElementById('decision-workspace').classList.toggle('is-motion-paused', paused);
        this.queueReveal();
    },
    queueReveal() {
        const reveal = document.getElementById('decision-reveal');
        if (!reveal || !this.active() || document.hidden || this.revealFrame) return;
        this.revealFrame = requestAnimationFrame(() => {
            this.revealFrame = null;
            if (!this.active()) return;
            const foreground = document.querySelector('#page-decision .decision-foreground').getBoundingClientRect();
            const viewport = document.querySelector('.main-content').getBoundingClientRect();
            const height = reveal.querySelector('.decision-underlay').clientHeight;
            if (!height) { this.background?.setRunning(false); return; }
            const progress = Math.max(0,Math.min(1,(viewport.bottom-foreground.bottom)/height));
            this.background?.setRunning(!this.motionReduced && !this.motionPrinting && !document.hidden && this.active() && progress > 0);
            reveal.style.setProperty('--dr-progress',this.motionReduced ? '1' : progress.toFixed(3));
            reveal.style.setProperty('--dr-lift',`${this.motionReduced ? 0 : (1-progress)*36}px`);
        });
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
            const rawReference=id==='raw-ash' && this.view==='gpt' && typeof GptExpert9!=='undefined' && GptExpert9.group?.production==='expert8';
            return n(id,title,value,recordSource(s.coarse),rawReference ? '原煤灰分保留采集与展示，不参与当前八因子预测，也不作为建议密度的必要条件。' : this.liveMode() ? '当前卡片采用的数值参与 GPT 实时预测；各项来源和数据时间见下方。' : `所查看的${this.view === 'gpt' ? ' GPT' : '当前生产'}模型输入。${details}。采集点位尚未绑定业务字段。`,
                {...family('coarse',1260,y),parent:'coarse',numericValue:fields.length===1?fields[0].raw:null,sampleAt:s.coarse?.timestamp,reference:s.previewOnly || !s.model || coarseManual,fields,unit:keys.length === 1 ? (keys[0] === 'coal_amount' ? 't/h' : '%') : '状态'});
        };
        const flags = fields => fields.map(field => {
            const short = field.key.replace('sys','').replace('desliming','');
            return `${short} ${field.raw == null ? '—' : field.raw ? '开' : '关'}`;
        }).join(' · ');
        const densityLayout = {x:670,w:245};
        const nodes = [
            n('density','建议密度',s.showAdvice ? this.number(s.guide.rhoNew,3) : '—',s.showAdvice ? (s.ashOk ? '达标保持' : s.guide.direction === 'down' ? '本步建议 · 下调' : '本步建议 · 上调') : s.guide.hold ? '等待新化验 / 新数据' : '暂无有效建议',
                s.guide.reason,{...densityLayout,y:343,h:207,type:'root',unit:'g/cm³',sampleAt:s.reader.ashMeasurementInfo(s.guide.scheme).sampleAt}),
            n('heavy','重介精煤',ash(s.ashes.heavy),`煤量 ${rate(s.amounts.heavy)}`,'汇总左侧重介精煤灰分和重介精煤量，随左侧数据同步更新。',
                {...family('heavy',380,170,{w:220,h:148}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-overview',sampleAt:heavyMeasurement.sampleAt,enteredAt:heavyMeasurement.enteredAt}),
            n('float','浮精',ash(s.ashes.float),`煤量 ${rate(s.amounts.float)}`,'汇总左侧浮精灰分和浮精量，随左侧数据同步更新。',
                {...family('float',380,612,{w:220,h:148}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-float',sampleAt:s.floating?.timestamp}),
            n('coarse','粗精煤泥',ash(s.ashes.coarse),`煤量 ${rate(s.amounts.coarse)} · 生产 ${s.engineLabel}`,`${coarseSource}。当前生产采用值与下面的 GPT 原始预测分开显示；共享 EMA 不作为 GPT 预测值。`,
                {...family('coarse',965,358.5,{w:220,h:176}),type:'component',parent:'density',reference:!s.componentFeeds,page:'page-coarse',sampleAt:coarseManual ? null : s.coarse?.timestamp,enteredAt:coarseManual ? s.store.coarseAshInput?.manualAt : null}),
            n('heavy-ash','重介精煤灰分',ash(s.ashes.heavy),heavyMeasurement.source,'优先有效人工采样值，其次 502 灰分补录 / 导入记录；没有测量记录时使用默认值，不能视作现场实时读数。',
                {...family('heavy',35,45),parent:'heavy',unit:'%',numericValue:s.ashes.heavy,...heavyMeasurement}),
            n('heavy-amount','重介精煤量','—','501 + 502 − 浮精量 − 粗精煤泥量','501、502 瞬时带煤量合计，减去页面当前采用的浮精量和粗精煤泥量。各项单位为 t/h。',
                {...family('heavy',35,183),parent:'heavy',unit:'t/h'}),
            n('density-current','当前分选密度',this.number(s.guide.rhoCur,3,'g/cm³'),'人工输入 / 灰分密度记录 / 默认','建议以当前有效密度为基准，按灰分偏差修正并限制单步调整量。A303/A304 为 1 或缺失时分别沿用本路最近非 1 有效值。',
                {...family('heavy',380,394.5,{w:220}),parent:'density',unit:'g/cm³',...densityInfo}),
            n('float-ash','浮精灰分',ash(s.ashes.float),floatManual ? '人工覆盖' : floatMeasured ? recordSource(s.floating) : '默认值 · 非实时测量','有效人工覆盖优先，其次时间最新的浮精记录；无数据时原计算链使用默认值。历史化验未记录出报告时刻，不能据采样时刻推断当时已知结果。',
                {...family('float',35,463),parent:'float',unit:'%',numericValue:s.ashes.float,...(floatManual ? manualInfo(s.store.floatAshInput) : {sampleAt:floatMeasured ? s.floating.timestamp : null})}),
            n('float-amount','浮精量',rate(s.amounts.float),'人工输入 / 最新浮精记录','有效人工值优先，其次最新浮精记录的 coal_amount。压滤机状态不会自动推导产量。',
                {...family('float',35,581),parent:'float',unit:'t/h',numericValue:s.amounts.float,...amountInfo('floatAmount','float')}),
            n('float-press','浮精压滤机运行',s.floating?.filter_press_running == null ? '—' : s.floating.filter_press_running ? '运行' : '停止',recordSource(s.floating),'工况参考：现有密度计算没有使用压滤机运行状态直接预测浮精量。',
                {...family('float',35,699),parent:'float',reference:true,sampleAt:s.floating?.timestamp}),
            n('float-unmodeled','药剂量 / 原煤煤泥含量','待补充','工艺因素 · 未建字段与模型','原图中的工艺因素，目前没有可追溯的输入字段及预测关系。本页保留这一因素，待现场定义测量方式和模型后接入；不填造数值。',
                {...family('float',35,817),parent:'float',reference:true}),
            feature('raw-ash','原煤灰分',['raw_ash'],25),
            feature('coal-rate','入洗原煤量',['coal_amount'],140),
            feature('system-switches','系统运行状态',['sysA','sysB','sys401','sys402'],255,flags),
            feature('desliming','脱粉系统状态',['desliming473','desliming474'],370,flags),
            feature('tail-level','精磁尾液位',['level'],485),
            n('coarse-amount','粗精煤泥量',rate(s.amounts.coarse),s.reader.coarseAmountLayer() === '计算' ? '315 筛量 − 筛下水量' : s.reader.coarseAmountLayer(),
                this.liveMode()?'自动采用同次入洗原煤量 ÷ 40，入洗：粗精 = 40：1；缺数与停机状态跟随入洗量。':`量的计算：315 筛量 ${rate(s.store.coarseCalc?.screen315)} − 筛下水量 ${rate(s.store.coarseCalc?.waterUnder)}。这是组分产量，与“入洗原煤量”模型因素不同。`,
                {...family('coarse',1260,600),parent:'coarse',unit:'t/h',numericValue:s.amounts.coarse,...amountInfo('coarseAmount','coarse')}),
            n('coarse-slime','原煤煤泥含量','待补充','工艺因素 · 尚未进入模型','目前的粗精模型使用 10 个输入因素；原煤煤泥含量尚无独立输入字段，也未进入模型。后续需要人工采样或可验证的测量来源。',
                {...family('coarse',1260,715),parent:'coarse',reference:true}),
        ];
        return nodes;
    },

    renderContext() {
        if(this.hourMode()) { DecisionHourly.context(this); return; }
        const modeLabel=document.getElementById('decision-mode-label'),modeNote=document.getElementById('decision-mode-note');
        if(modeLabel) modeLabel.textContent=this.liveMode()?'实时计算 · 人工执行':'只读决策视图';
        if(modeNote) modeNote.textContent=this.liveMode()?'GPT 链路：当前输入 → 粗精煤泥灰分预测 → 三组分总灰分 → 建议密度。数据来源与后续小时简报共享；输入变化后重新计算，缺失时显示原因。建议由人工确认执行。虚线表示尚未进入模型的工况参考。':'点击节点选择采集点，卡片随读数更新；点击画布空白处收起。采集显示不改变建议计算，相关连线作为虚线对照。建议密度保留计算值，点位显示为采集对照。';
        if (this.liveMode()) {
            const result=DecisionRealtime.presentation();
            this.renderHTML('decision-context',`<span>模型：<strong>${this.escape(result?.model?.method?.toUpperCase() || '—')}</strong></span><span>${this.escape(result?this.time(result.generated_at):'—')}</span>`);
            return;
        }
        const s = this.snapshot, sources = {manual:'人工总灰分',formula:'三组分公式',entry:'501 导入 / 录入',ash501:'501 灰分取值'};
        const message = s.componentFeeds ? '三组分参与当前建议计算' : '三组分显示公式核算对照，建议由所选总灰分驱动';
        const bound = this.nodes.filter(node => !this.fixedSource(node.id) && this.pointChoices[node.id]).length;
        this.renderHTML('decision-context',`<span>指导口径 <strong>${s.guide.scheme === 'heavy' ? '重介精煤版' : '总灰分版'}</strong></span><span>生产模型 <strong>${this.escape(s.engineLabel)}</strong></span><span>总灰分来源 <strong>${this.escape(sources[s.total.source])}</strong></span><span>${message}</span><span>实时采集 <strong>${bound ? bound+' 张卡片已选点' : '选择点位后更新卡片'}</strong> · 展示不改变建议计算</span>`);
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

    sourceBadge(node,display) {
        if(display.stopped) return '设备停机';
        if(!this.displayAvailable(display) && !(display.pending && Number.isFinite(display.numericValue))) return '不可用';
        if(['heavy','float'].includes(node.id)) return '汇总';
        if(['coarse','heavy-amount'].includes(node.id)) return '计算';
        if(this.liveMode() && node.id==='coarse-amount') return '计算';
        const hourly=this.hourMode();
        const result=hourly?DecisionHourly.presentation():typeof DecisionRealtime!=='undefined'?DecisionRealtime.result:null;
        const key=typeof DecisionSources!=='undefined'?DecisionSources.nodes[node.id]:null;
        const cell=result?.cells?.[key] || {};
        const choice=(hourly?result?.sources?.resolved_choices:this.pointChoices)?.[node.id] || '';
        const source=cell.source || display.source || node.source || '';
        if(display.manual || display.item?.kind==='manual_measurement' || cell.source_id==='manual-measurements' || choice.startsWith('["manual-measurements",')) return '补录';
        if(cell.status==='calculated' || /计算/.test(source) || display.item?.kind==='numeric_average' || cell.point_key==='a303_a304_density_average') return '计算';
        if(cell.item_id || cell.point_key || choice || /KEP|采集/.test(source)) return '采集';
        if(['manual','historical','default'].includes(cell.status) || /手工|人工|补录|历史|默认/.test(source)) return '补录';
        return ['raw-ash','coal-rate','tail-level','density-current','system-switches','desliming','float-press'].includes(node.id)?'采集':'补录';
    },

    heartbeatState(node,display) {
        if(!this.instantLayout() || !this.liveMode() || node.type || node.id==='heavy-amount' || !display.bound) return null;
        const shared=this.sharedSources();
        const key=typeof DecisionSources!=='undefined'?DecisionSources.nodes[node.id]:null;
        const cell=shared?DecisionRealtime.result?.cells?.[key]:null;
        const item=display.item, choice=this.pointChoices[node.id] || '';
        if(display.manual || display.aggregate || item?.kind==='manual_measurement' || item?.kind==='numeric_average' ||
            cell?.source_id==='manual-measurements' || choice.startsWith('["manual-measurements",') ||
            ['manual','historical','default','calculated'].includes(cell?.status) || cell?.point_key==='a303_a304_density_average') return null;
        if(!item && !choice && !cell?.source_id && !cell?.point_key) return null;
        // A stopped meter may still display a retained nonzero number. Its heartbeat must be flat.
        if(display.stopped || cell?.status==='stopped') return {status:'stopped',amplitude:0};
        const valid=display.available && !display.held && !display.pending && !window.Operations?.telemetryFailed &&
            item?.available!==false && (!shared || cell?.status==='good');
        if(!valid) return {status:'unavailable',amplitude:0};
        const parts=display.components;
        let ratio;
        if(parts?.length) {
            if(!parts.every(p=>p.available && typeof p.value==='boolean')) return {status:'unavailable',amplitude:0};
            ratio=parts.filter(p=>p.value).length/parts.length;
            if(!ratio) return {status:'stopped',amplitude:0};
        } else {
            const raw=shared?cell?.value:item?.sample?.value;
            if(typeof raw==='boolean') {
                if(!raw) return {status:'stopped',amplitude:0};
                ratio=1;
            } else {
                const value=display.numericValue;
                if(!Number.isFinite(value)) return {status:'unavailable',amplitude:0};
                if(value===0) return {status:'zero',amplitude:0};
                const unit=display.unit || node.unit || '';
                const scale=node.id==='density-current' || /g\/cm/.test(unit)?2:/t\/h/.test(unit)?1000:100;
                ratio=Math.min(1,Math.abs(value)/scale);
            }
        }
        // A value-driven status illustration, not fabricated sample history.
        return {status:'running',amplitude:Number((4+6*ratio).toFixed(2))};
    },
    heartbeatMarkup(node,display) {
        const state=this.heartbeatState(node,display);
        if(!state) return '';
        const a=state.amplitude, y=f=>Number((14+a*f).toFixed(2));
        const path=a?`M 0 14 H 16 L 22 ${y(-.2)} L 26 14 L 30 ${y(.35)} L 35 ${y(-1)} L 41 ${y(.6)} L 47 ${y(-.2)} L 52 14 H 72`:'M 0 14 H 72';
        const label={running:'采集心跳 · 波幅按当前读数归一化示意',stopped:'设备停机',unavailable:'采集不可用或沿用值 · 心跳暂停',zero:'当前采集值为零'}[state.status];
        return `<svg class="decision-heartbeat" data-decision-key="heartbeat-${node.id}" data-heartbeat-state="${state.status}" viewBox="0 0 72 28" aria-hidden="true" focusable="false"><title>${label}</title><path class="decision-heartbeat-line" d="${path}"/><path class="decision-heartbeat-scan" d="${path}" pathLength="100"/></svg>`;
    },

    renderGraph() {
        const s = this.snapshot, selected = this.nodes.find(n => n.id === this.selected);
        const targetAsh = (typeof CollectPage !== 'undefined' ? CollectPage.instruments.find(i => i.id === 'total_ash') : null);
        const hourly=this.hourMode(),hourResult=hourly?DecisionHourly.presentation():null;
        const hourlyGaps=hourly?DecisionHourly.dataGaps(hourResult):new Map();
        const densityRoot=this.nodes.find(n=>n.id==='density');
        const hourSummary=typeof DecisionHourly!=='undefined'?DecisionHourly.summary(this,densityRoot):'';
        const targetAshRange = `${this.number((hourly?hourResult?.target?.min:targetAsh?.min) ?? 8.5,1)}–${this.number((hourly?hourResult?.target?.max:targetAsh?.max) ?? 9.0,1)}%`;
        const colors = {heavy:'var(--dm-blue)',float:'var(--dm-orange)',coarse:'var(--dm-green)'};
        if(hourSummary) colors.hourly='var(--dm-cyan)';
        const ancestorIds = new Set([this.selected]);
        let current = selected;
        while (current?.parent) { ancestorIds.add(current.parent); current = this.nodes.find(n => n.id === current.parent); }
        const flowingIds = new Set(), now = this.liveMode() ? (this.motionEpoch ??= Date.now()/1000) : Date.now()/1000;
        const displays = new Map(this.nodes.map(node => [node.id,this.pointDisplay(node)]));
        const displayOnly = node => displays.get(node.id).aggregate ? displays.get(node.id).summaryParts.some(p => p.display.bound || p.display.calculated) : displays.get(node.id).calculated || !!this.pointChoices[node.id];
        const paths = this.routeConnections().map(({node,parent,path},index) => {
            const highlight = ancestorIds.has(node.id) && ancestorIds.has(parent.id);
            const summaryLink = this.summaryChildren.get(parent.id)?.includes(node.id);
            const displayReference = this.liveMode() ? ['float-press','float-unmodeled','coarse-slime'].includes(node.id) : !summaryLink && (node.reference || (node.id !== 'density' && displayOnly(node)) || (parent.id !== 'density' && displayOnly(parent)));
            const flowReady=d=>this.displayAvailable(d) || (d.pending && Number.isFinite(d.numericValue));
            const flows = !displayReference && (this.liveMode() ? flowReady(displays.get(node.id)) && flowReady(displays.get(parent.id)) : summaryLink ? this.displayAvailable(displays.get(node.id)) : node.value !== '—' && parent.value !== '—');
            if (flows) { flowingIds.add(node.id); flowingIds.add(parent.id); }
            // GPT retains elements and phase; the legacy view still rebuilds from wall-clock phase.
            const phase = -((now+index*.37)%4).toFixed(3);
            const trace = flows || this.liveMode() ? `<path data-decision-key="flow-${node.id}" class="decision-flow${highlight ? ' is-highlight' : ''}${flows?'':' is-inactive'}" d="${path}" pathLength="100" style="--dm-flow-color:${colors[node.family]};animation-delay:${phase}s"/>` : '';
            return `<path class="decision-edge${displayReference ? ' is-reference' : ''}${highlight ? ' is-highlight' : ''}" data-decision-from="${node.id}" data-decision-to="${parent.id}" d="${path}" stroke="${colors[node.family]}" marker-end="url(#decision-arrow-${node.family})"/>${trace}`;
        }).join('');
        const hourLink=hourSummary?`<path class="decision-edge decision-hourly-link" data-decision-key="hourly-link" data-decision-from="density" data-decision-to="hourly-summary" d="M ${densityRoot.x+densityRoot.w/2} ${densityRoot.y} V ${densityRoot.y-(densityRoot.summaryGap??24)+2}" stroke="${colors.hourly}" marker-end="url(#decision-arrow-hourly)"/>`:'';
        const markers = Object.entries(colors).map(([key,color]) => `<marker id="decision-arrow-${key}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0 L8 4 L0 8" fill="${color}"/></marker>`).join('');
        const cards = this.nodes.map((node,index) => {
            const active = node.id === this.selected;
            const display = displays.get(node.id), root = node.id === 'density';
            const compact=this.view==='gpt' && !root;
            const gap=hourlyGaps.get(node.id);
            const preview = node.id === 'coarse' && !this.liveMode() ? `<span>查看${this.view === 'gpt' ? ' GPT' : '生产'}预测 ${this.number(s.prediction,2,'%')}${!s.model ? ' · 未训练' : s.previewOnly ? ' · 对照' : ' · 未平滑'}</span>` : '';
            const unit = node.id === 'density' ? '<span class="decision-root-unit">g/cm³</span>' : '';
            const sub = node.id === 'density' ? `<span>${hourly?'小时末':'当前'} ${this.number(hourly?hourResult?.guidance?.rhoCur:this.liveMode()?displays.get('density-current').numericValue:s.guide.rhoCur,3)}${this.liveMode()?' · '+this.escape(this.densityLabel()):''}</span><span>目标灰分范围${hourSummary?`<span class="decision-density-target">${targetAshRange}</span>`:' '+targetAshRange}</span>` : '';
            const phase = -((now+index*.41)%5).toFixed(3);
            const badge = compact ? `<span class="decision-badge">${this.sourceBadge(node,display)}</span>` : gap ? `<span class="decision-badge">${this.escape(gap.label)}</span>` : display.aggregate || display.calculated || display.shared ? `<span class="decision-badge">${display.badge}</span>` : display.bound && !root ? `<span class="decision-badge">${display.manual ? (display.held ? '待刷新' : display.available ? '补录' : '无补录') : display.stopped ? '设备停机' : display.held ? '沿用' : display.available ? '采集' : '不可用'}</span>` : node.reference ? '<span class="decision-badge">对照</span>' : '';
            const compare = display.bound && root ? `<span class="decision-live-comparison">采集对照 ${this.escape(display.value)}${display.available ? '' : '（不可用）'}</span>` : '';
            const groupHtml = display.components && !root ? `<span class="decision-state-grid">${display.components.map(c => `<span>${this.escape(c.label)} ${c.available && (display.shared || !window.Operations?.telemetryFailed) ? c.fraction ? this.number(c.value*100,0,'%') : c.value ? '开' : '关' : '未知'}</span>`).join('')}</span>` : this.escape(root && !this.liveMode() ? node.value : display.value);
            const componentAmount=compact && node.type==='component' ? node.id==='coarse'?displays.get('coarse-amount')?.value:display.summaryParts?.[1]?.display.value:null;
            const amountHtml=componentAmount?`<span class="decision-component-amount">煤量 ${this.escape(componentAmount)}</span>`:'';
            const content = root && hourSummary
                ? `<span class="decision-node-heading">即时建议密度</span><span class="decision-node-value">${groupHtml}${unit}</span><span class="decision-node-source"><span>目标灰分 ${targetAshRange}</span></span>`
                : compact ? `<span class="decision-node-heading">${this.escape(hourly && node.id==='density-current'?'小时平均密度':node.title)}${badge}</span><span class="decision-node-value">${groupHtml}${unit}${amountHtml}</span>`
                : `<span class="decision-node-heading">${this.escape(hourly && root?'本小时建议密度':hourSummary && root?'即时建议密度':hourly && node.id==='density-current'?'小时平均密度':node.title)}${badge}</span><span class="decision-node-value">${groupHtml}${unit}</span><span class="decision-node-source"><span>${this.escape(gap ? gap.message : root && !this.liveMode() ? node.source : display.source)}</span>${sub}${preview}${compare}</span>`;
            return `<button type="button" class="decision-node${hourly?DecisionHourly.gapClass(gap):''}${active ? ' is-selected' : ''}${flowingIds.has(node.id) ? ' has-flow' : ''}${display.bound ? ' is-telemetry' : ''}${display.components ? ' is-state-group' : ''}${display.pending ? ' is-updating' : ''}${(display.bound || display.aggregate || display.calculated) && !display.available ? ' is-telemetry-unavailable' : ''}${node.type === 'root' ? ' decision-root' : node.type === 'component' ? ' decision-component' : ''}${root && hourSummary?' decision-density-instant':''}" data-decision-node="${node.id}" data-family="${node.family || 'root'}" aria-pressed="${active}" aria-busy="${!!display.pending}"${display.bound || display.aggregate || display.calculated ? ` title="${this.escape(gap?.reason || display.title)}"` : ''} style="left:${node.x}px;top:${node.y}px;width:${node.w}px;min-height:${node.h}px;--dm-node-height:${node.h}px;--dm-pulse-delay:${phase}s">${content}${this.heartbeatMarkup(node,display)}</button>`;
        }).join('');
        const shown=hourly?hourResult:this.liveMode()?DecisionRealtime.presentation():null,pending=hourly?DecisionHourly.pending():this.liveMode() && DecisionRealtime.pending();
        const totalReason=this.liveMode() ? pending?'更新中 · 上轮结果仅供对照':shown?.total_ash?.reason || (hourly?DecisionHourly.error:DecisionRealtime.error) || '正在计算' : '';
        const total=this.liveMode()?`<strong>${hourly?(shown?.total_ash?.aggregation==='output-mass-v1'?'产煤量加权总灰分':'小时平均总灰分'):pending?'上轮总灰分':'预测总灰分'} ${this.number(shown?.total_ash?.value,2,'%')}</strong><span title="${this.escape(totalReason)}">${this.escape(totalReason.length>45?(hourly && Number.isFinite(shown?.total_ash?.value)?'按已保存口径统计 · 详见小时灰分': '输入待补充，点击建议密度查看原因'):totalReason)}</span>`:`公式核算 ${this.number(s.formula,2,'%')}<br>当前采用 ${this.number(s.total.value,2,'%')}`;
        const ashHistory=this.view==='gpt'?'<button type="button" class="decision-ash-open" data-ash-history aria-haspopup="dialog">查看区间灰分 ↗</button><button type="button" class="decision-ash-open" data-evidence-open>工况分段 / 调密跟踪 ↗</button>':'';
        this.renderHTML('decision-toolbar-total',this.instantLayout()?`<span class="decision-ash-metric" data-decision-key="instant-ash"><span class="decision-total-label">预测总灰分</span><strong>${this.number(this.liveMode()?shown?.total_ash?.value:s.formula,2)}<small> %</small></strong></span>`:'');
        document.getElementById('decision-toolbar-total').hidden=!this.instantLayout();
        const totalPanel=this.instantLayout()
            ? ''
            : `<div class="decision-formula${hourly?DecisionHourly.gapClass(hourlyGaps.get('total-ash')):''}" title="${this.escape(hourlyGaps.get('total-ash')?.reason || '')}"><strong>${hourly?(shown?.total_ash?.aggregation==='output-mass-v1'?'逐次质量平衡 · 产煤量加权':'逐次质量平衡 · 小时平均'):'三组分质量平衡'}</strong>${hourly?(shown?.total_ash?.aggregation==='output-mass-v1'?'Σ（逐次总灰分 × 实际产煤量）/ Σ 实际产煤量':'先按各次组分灰分和煤量计算总灰分，再对有效结果求平均'):'总灰分 = Σ（组分灰分 × 组分煤量）/ Σ 组分煤量'}<br>${total}${ashHistory}</div>`;
        const recentAsh=typeof DecisionRecentAsh!=='undefined'?DecisionRecentAsh.markup(this,this.nodes.find(n=>n.id==='coarse')):'';
        this.renderHTML('decision-board',`<svg class="decision-edges" aria-hidden="true" viewBox="0 0 ${this.graphWidth()} ${this.graphHeight()}" style="width:${this.graphWidth()}px;height:${this.graphHeight()}px"><defs>${markers}</defs>${paths}${hourLink}</svg>${cards}${hourSummary}${totalPanel}${recentAsh}`);
    },

    telemetryState(item, failed = window.Operations?.telemetryFailed) {
        if (item?.kind === 'manual_measurement') return window.Operations?.manualMeasurementsFailed ? '补录查询失败 · 显示上次读取值，待刷新' : item.available ? '已保存到 SQLite · 按测量时间取最新' : '暂无已到时的有效补录';
        const labels = {good:'可用',no_data:'无数据',bad_quality:'坏质量',uncertain_quality:'质量不确定',stale:'接收超时',source_stale:'源时间过期',collector_offline:'采集离线',out_of_range:'超出量程',invalid_value_type:'类型不匹配',non_finite_value:'数值无效',future_source_time:'源时钟异常',incomplete_components:'组合数据未齐',member_unavailable:'部分成员不可用',mixed_batches:'成员接收批次不一致',unit_mismatch:'成员单位不一致'};
        return failed ? '查询中断 · 最后读数不可用' : `${item.sample?.quality || '无质量信息'} · ${labels[item.status] || '不可用'}`;
    },
    pointIdentity(item) { return JSON.stringify([item.source_id,item.key]); },
    renderInspector(force=false) {
        if (!this.snapshot) return;
        const inspector=document.getElementById('decision-inspector');
        if(this.liveMode() && !force && inspector.classList && !inspector.classList.contains('is-open')) return;
        const scrollTop = document.getElementById('decision-inspector').scrollTop || 0;
        const node = this.nodes.find(n => n.id === this.selected) || this.nodes[0], s = this.snapshot;
        const display = this.pointDisplay(node);
        if(this.hourMode()) { DecisionHourly.inspector(this,node); return; }
        if (display.aggregate || display.calculated) {
            const details = (display.summaryParts || display.inputs).map(part => {
                const d = part.display;
                const pairs = [['显示值',d.value],['来源',d.source],['采样 / 记录时间',this.time(d.sampleAt)]];
                if (d.enteredAt) pairs.push(['录入时间',this.time(d.enteredAt)]);
                if (d.valueSourceAt) pairs.push(['原始数据时间',this.time(d.valueSourceAt)]);
                return `<div class="decision-detail"><h4>${this.escape(part.node?.title)}</h4><dl class="decision-facts">${pairs.map(([key,value]) => `<dt>${this.escape(key)}</dt><dd>${this.escape(value)}</dd>`).join('')}</dl></div>`;
            }).join('');
            const calculation = display.calculated ? `<dl class="decision-facts"><dt>计算公式</dt><dd>${this.escape(display.formula)}</dd><dt>计算结果</dt><dd>${this.escape(display.value)}</dd></dl>${display.reason ? `<p class="decision-unavailable">${this.escape(display.reason)}</p>` : ''}` : '';
            const description=display.live ? node.id==='coarse' ? '使用当前各输入卡片的同一份数据调用 GPT 保存模型，输出粗精煤泥灰分。' : '以所选实际密度为基准，使用本次预测总灰分给出建议，由人工执行。' : node.description;
            const warnings=display.live && DecisionRealtime.result?.warnings?.length ? `<p>${this.escape('输入提示：'+DecisionRealtime.result.warnings.join('；'))}</p>` : '';
            this.renderHTML('decision-inspector',`<div class="decision-inspector-header"><span>汇总与来源详情</span><button type="button" class="decision-inspector-close" data-decision-close>收起 ×</button></div><div class="decision-detail"><div class="decision-detail-heading"><h3>${this.escape(node.title)}</h3><span class="decision-badge">${display.badge}</span></div><p>${this.escape(description)}</p>${calculation}${warnings}</div>${details}`);
            document.getElementById('decision-inspector').scrollTop = scrollTop;
            this.queueInspectorPosition();
            return;
        }
        const participates=this.liveMode() && !['float-press','float-unmodeled','coarse-slime'].includes(node.id);
        const pairs = display.manual ? [['卡片显示值',display.value + (display.held ? '（待刷新）' : display.available ? '' : '（不可用）')],
            ['数据来源','SQLite · 手工补录'],['测量时间',this.time(display.sampleAt)],['录入时间',this.time(display.enteredAt)],
            ['状态',display.shared ? display.available?'已读取补录':'补录不可用' : display.item ? this.telemetryState(display.item) : '补录点位尚未返回'],['读取规则','按测量时间取最新；同一时间取最后录入']] :
            [[participates?'当前采用值':'当前业务值',participates?display.value:node.value + (node.id === 'density' ? ' g/cm³' : '')],['当前来源',participates?display.source:node.source],['采样 / 记录时间',this.time(participates?display.sampleAt:node.sampleAt)],['人工录入时间',this.time(participates?display.enteredAt:node.enteredAt)],['化验结果可用时间','未记录（不由采样时刻推断）'],['实时业务绑定',participates?'参与本次实时计算':'尚未接入']];
        if (display.shared && !display.manual) pairs.unshift(['卡片显示值',display.value],['数据状态',display.badge],['单位',display.components?'运行状态 · 无单位':display.unit || '未配置']);
        else if (display.bound && !display.manual) pairs.unshift([node.id === 'density' ? '采集对照值' : '卡片显示值',display.value + (display.held ? '（沿用上次有效值）' : display.available ? '' : '（不可用）')],
            ['卡片状态',display.stopped ? '设备停机' : display.held ? '采集异常 · 沿用历史值' : display.available ? '实时采集' : '不可用'],
            ['卡片采集来源',display.item?.item_id || display.item?.name || '已选点位未返回'],['显示数值原接收时间',this.time(display.sampleAt)],['单位',display.components ? '运行状态 · 无单位' : App.telemetryDisplayUnit(display.item) || '未配置']);
        if (display.held && !display.manual) pairs.splice(4, 0, ['沿用数值原源时间', this.time(display.valueSourceAt)]);
        if (display.alignment) pairs.push(['液位对齐规则','采用预测时刻前 90 秒的采集值；最多向前寻找 30 秒'],
            ['预测时刻',this.time(display.alignment.prediction_at)],['液位目标时刻',this.time(display.alignment.target_at)],
            ['实际采用采集时间',this.time(display.sampleAt)]);
        if (node.id === 'density-current' && display.item?.effective_density) {
            const effective = display.item.effective_density;
            pairs.splice(2, 0, ['密度采用规则', effective.reason || '有效非 1 读数']);
            for (const part of effective.components || [effective]) pairs.push([
                `${part.label || display.item.device || '本路'} 采用值`,
                `${this.number(part.value,3,App.telemetryDisplayUnit(part))} · ${part.reason || '实时读数'} · ${this.time(part.received_at)}`]);
        }
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
        const items = this.displayPoints().filter(p => {
            if(!this.sharedSources()) return true;
            const group={'system-switches':'four_system_status',desliming:'desliming_status','float-press':'float_press_status'}[node.id];
            if(group) return p.key===group;
            if(node.id==='density-current') return p.key==='a303_a304_density_average' || ['TIA_4_1.TIA_4_1.A303/MD','TIA_4_1.TIA_4_1.A304/MD'].includes(p.item_id);
            return p.kind!=='state_group';
        });
        const choice = this.pointChoices[node.id] || '';
        const item = items.find(p => this.pointIdentity(p) === choice);
        const missingOption = choice && !item ? `<option value="${this.escape(choice)}" selected>已选点位暂未返回</option>` : '';
        const options = missingOption + items.slice().sort((a,b) => Number(b.kind === 'manual_measurement') - Number(a.kind === 'manual_measurement') || String(a.item_id || a.name).localeCompare(String(b.item_id || b.name),'zh-CN',{numeric:true})).map(p => `<option value="${this.escape(this.pointIdentity(p))}"${this.pointIdentity(p) === choice ? ' selected' : ''}>${this.escape(p.device && p.tag_name ? `${p.device} · ${p.tag_name} · ${p.description || ''}` : p.name)}</option>`).join('');
        const sample = item?.sample || {};
        const value = item?.kind === 'state_group' ? this.groupValue(sample.components) : typeof sample.value === 'boolean' ? (sample.value ? '开' : '关') : sample.value ?? '—';
        const available = item && item.available && !this.pointFailed(item);
        const live = item?.kind === 'manual_measurement' ? `<dl class="decision-facts"><dt>补录记录</dt><dd>${this.escape(sample.record_id ? '#' + sample.record_id : '暂无补录')}</dd><dt>保存位置</dt><dd>SQLite · manual_measurements</dd><dt>补录信息名</dt><dd>${this.escape(item.name)}</dd><dt>值</dt><dd>${this.escape(value)} ${this.escape(item.unit)}</dd><dt>测量时间</dt><dd>${this.escape(this.time(sample.observed_at))}</dd><dt>录入时间</dt><dd>${this.escape(this.time(sample.created_at))}</dd><dt>读取状态</dt><dd>${this.escape(this.telemetryState(item))}</dd></dl>` : item ? `<dl class="decision-facts"><dt>原始读数（回退前）</dt><dd class="${available ? '' : 'decision-unavailable'}">${this.escape(value)} ${this.escape(App.telemetryDisplayUnit(item))}${available ? '' : '（不可用）'}</dd><dt>质量 / 时效</dt><dd>${this.escape(this.telemetryState(item))}</dd><dt>数据来源</dt><dd>${this.escape(item.item_id || item.source_id + '/' + item.key)}</dd><dt>源时间</dt><dd>${this.escape(this.time(sample.source_at))}</dd><dt>服务器时间</dt><dd>${this.escape(this.time(sample.server_at))}</dd><dt>接收时间</dt><dd>${this.escape(this.time(sample.received_at))}</dd></dl>` : `<p>${choice ? '已选点位暂未返回，卡片保持不可用，等待数据恢复。' : participates ? '选择点位后，用对应读数重新计算整条决策链；缺失输入将显示原因。' : '选择点位后，该卡片立即显示对应读数；清空选择恢复原业务显示。'}</p>`;
        const memberDetails = item?.kind === 'state_group' ? `<dl class="decision-facts">${(sample.components || []).map(c => `<dt>${this.escape(c.label)} ${this.escape(item.member_label || '系统')}</dt><dd>${this.escape(c.available && !window.Operations?.telemetryFailed ? c.value ? '开' : '关' : '未知')} · ${this.escape(c.item_id)}<br>接收 ${this.escape(this.time(c.received_at))} · ${this.escape(c.status || '未知')}${!c.available && typeof c.value === 'boolean' ? `<br>最后读数 ${c.value ? '1' : '0'}（不可用）` : ''}</dd>`).join('')}</dl>` : item?.kind === 'numeric_average' ? `<dl class="decision-facts"><dt>计算方式</dt><dd>${this.escape(sample.formula)} · 每路 50%<br>本系统计算，保留原始采集点。</dd>${(sample.components || []).map(c => `<dt>${this.escape(c.label)} 原始密度</dt><dd>${this.escape(Number.isFinite(c.value) ? c.value : '—')} ${this.escape(App.telemetryDisplayUnit(c) || '（单位未配置）')}${c.available && !window.Operations?.telemetryFailed ? '' : '（不可用）'}<br>${this.escape(c.item_id)}<br>接收 ${this.escape(this.time(c.received_at))} · ${this.escape(c.status || '未知')}</dd>`).join('')}</dl>` : '';
        const collector = `${items.filter(p => p.available && !this.pointFailed(p)).length} / ${items.length} 个点位可用（含组合与手工补录）${window.Operations?.telemetryFailed ? '；采集查询中断，KEP 最后读数不可用' : ''}${window.Operations?.manualMeasurementsFailed ? '；补录查询失败，待刷新' : ''}`;
        this.renderHTML('decision-inspector',`<div class="decision-inspector-header"><span>因素与来源详情</span><button type="button" class="decision-inspector-close" data-decision-close>收起 ×</button></div><div class="decision-detail"><div class="decision-detail-heading"><h3>${this.escape(node.title)}</h3><span class="decision-badge">${display.manual ? '手工补录' : participates ? '计算输入' : node.reference ? '对照 / 预留' : '业务取值'}</span></div><p>${this.escape(display.manual ? '读取已保存的手工补录，按测量时间取最新记录。' : node.description)}</p><dl class="decision-facts">${facts}</dl></div><div class="decision-detail decision-source-panel"><h4>卡片数据 → 数据来源</h4><p class="decision-live-state">${this.escape(collector)}。${participates ? '所选读数参与本页实时计算。' : this.liveMode() ? '此项用作工况参考。' : '所选读数用于卡片展示，尚未参与本页计算。'}</p><label for="decision-source-point">选择数据来源${node.id === 'density' ? '（采集对照）' : ''}</label><select id="decision-source-point"${this.sharedSources() && DecisionSources.saving ? ' disabled' : ''}><option value="">${this.sharedSources()?'自动取值 / 未指定采集点':'不使用采集值'}</option>${options}</select>${live}${memberDetails}</div>`);
        document.getElementById('decision-inspector').scrollTop = scrollTop;
        this.queueInspectorPosition();
    },
};
