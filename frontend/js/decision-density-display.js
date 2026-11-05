/* Display memory only. Held numbers never become calculation inputs or valid advice. */
const DecisionDensityDisplay = {
    storageKey:'dmcs.decision-density-display.v1', entries:{}, loaded:false,
    newPolicy:'density-candidate-v1',
    newVersionOnly() { return !!window.Api?.isReadOnlyTest?.(); },
    newStatus(status) { return ({adjust:'一步调整',hold:'保持当前设定',wait:'等待观察',unavailable:'无法给出'})[status] || '等待读取新版建议'; },
    newResult(kind) {
        return kind==='hourly' ? (typeof DecisionHourly!=='undefined' ? DecisionHourly.presentation() : null) :
            (typeof DecisionRealtime!=='undefined' ? DecisionRealtime.presentation() : null);
    },
    explicitNew(result) {
        if (result?.density_policy!==this.newPolicy) return null;
        const candidate=result.density_suggestion?.candidate || result.suggested_density || {};
        const status=candidate.status;
        const value=['adjust','hold','wait'].includes(status) && this.valid(candidate.value) ? candidate.value : null;
        const reason=window.DensityCandidatePanel?.reasonText(candidate,result.density_suggestion?.parameters) || result.suggested_density?.reason || candidate.reasons?.join('；') || this.newStatus(status);
        return {value,status,reason,at:result.checked_at || result.generated_at,retained:false};
    },
    newDisplay(page,kind) {
        const result=this.newResult(kind), shown=this.explicitNew(result);
        const reason=shown?.reason || '新版建议尚未取得有效响应；不沿用旧版建议';
        const available=Number.isFinite(shown?.value);
        return {live:true,shared:true,calculated:true,newVersion:true,pending:!result,held:false,available,
            numericValue:available?shown.value:null,value:available?page.number(shown.value,3):'暂无有效值',
            unit:'g/cm³',source:'新版规则 · '+this.newStatus(shown?.status),badge:this.newStatus(shown?.status),
            reason,title:reason,inputs:[],sampleAt:shown?.at,
            formula:'按当前设定与当时可用灰分依据运行新版单步规则；等待不等于调整，缺失不沿用旧建议。'};
    },
    newInspector(page,kind) {
        const result=this.newResult(kind),shown=this.explicitNew(result),data=result?.density_suggestion;
        const ash=data?.basis?.ash || {},reading=data?.observation?.actual_reading || {};
        const rows=[['新版建议',Number.isFinite(shown?.value)?page.number(shown.value,3)+' g/cm³':'暂无有效值'],
            ['规则状态',this.newStatus(shown?.status)],['原因',shown?.reason || '新版响应未就绪，不沿用旧建议'],
            ['当前设定',page.number(data?.setting?.value,3)+' g/cm³'],['当前实测',reading.usable?page.number(reading.value,3)+' g/cm³':'不可用于判断'],
            ['灰分依据',page.number(ash.value,2)+' %'+(ash.provisional?' · 暂估':'')],
            ['灰分生产时段',page.time(ash.start)+' — '+page.time(ash.end)],['规则版本',result?.density_policy || '未取得新版标记']];
        return `<div class="decision-inspector-header"><span>新版建议与依据</span><button type="button" class="decision-inspector-close" data-decision-close>收起 ×</button></div><div class="decision-detail"><h3>建议密度（新版）</h3><dl class="decision-facts">${rows.map(([key,value])=>`<dt>${page.escape(key)}</dt><dd>${page.escape(value)}</dd>`).join('')}</dl><p>由人工确认执行。等待不等于调整，历史重算不代表已发布或执行后的效果。</p></div>`;
    },
    newSummary(page,root) {
        if (!root || page.hourMode()) return '';
        const shown=this.explicitNew(this.newResult('hourly')),value=shown?.value;
        const height=root.summaryHeight??201,spacing=root.summaryGap??24;
        return `<section class="decision-node decision-hourly-summary" data-decision-key="hourly-summary" aria-label="建议密度（新版·小时）" title="${page.escape(shown?.reason || '新版小时建议尚未读取，不沿用旧值')}" style="left:${root.x}px;top:${root.y-height-spacing}px;width:${root.w}px;height:${height}px"><span class="decision-node-heading">建议密度（新版·小时）</span><span class="decision-node-value">${Number.isFinite(value)?page.number(value,3):'暂无有效值'} <span class="decision-root-unit">g/cm³</span></span><span class="decision-node-source">${page.escape(this.newStatus(shown?.status))}</span></section>`;
    },
    load() {
        if(this.loaded) return;
        this.loaded=true;
        try {
            const saved=JSON.parse(localStorage.getItem(this.storageKey) || '{}');
            if(saved && typeof saved==='object' && !Array.isArray(saved)) this.entries=Object.fromEntries(
                Object.entries(saved).filter(([,v])=>v && this.valid(v.value) && Number.isFinite(Date.parse(v.at))));
        } catch(_) { /* Keep the in-memory display if browser storage is unavailable. */ }
    },
    scope(page,result) {
        const config=typeof DecisionSources==='undefined'?null:DecisionSources.config;
        const sources=config || result?.advice_sources || result?.sources;
        const choices=sources?.resolved_choices || page.pointChoices || {};
        const target=config?{min:config.target_min,max:config.target_max}:result?.target;
        return JSON.stringify([Object.keys(choices).sort().map(k=>[k,choices[k]]),target?.min ?? 8.5,target?.max ?? 9]);
    },
    model(result) { return JSON.stringify(result?.model || null); },
    valid(value) { return Number.isFinite(value) && value>1 && value<3; },
    persist() {
        try { localStorage.setItem(this.storageKey,JSON.stringify(this.entries)); } catch(_) { /* In-memory fallback. */ }
    },
    remember(kind,page,result,acceptValue=true) {
        this.load();
        if(this.newVersionOnly()) return;
        if(!result) return;
        const config=typeof DecisionSources==='undefined'?null:DecisionSources.config;
        const sources=result.advice_sources || result.sources;
        if(config && sources && config.revision>sources.revision) return;
        const key=JSON.stringify([kind,this.scope(page,result)]), previous=this.entries[key];
        const policy=result.guidance?.policy || result.advice_policy;
        // Remember a model/policy change even when its first response has no value.
        // A later network failure must not resurrect the previous model's advice.
        if(previous && ((result.model && previous.model!==this.model(result)) ||
            (policy && previous.policy && policy!==previous.policy))) {
            delete this.entries[key];this.persist();
        }
        const output=result.suggested_density;
        if(!acceptValue || !this.valid(output?.value) || ['held','missing','invalid'].includes(output.status) || result.advice_pending) return;
        const at=kind==='hourly'?result.checked_at || result.generated_at:result.generated_at;
        if(!Number.isFinite(Date.parse(at))) return;
        if(previous && Date.parse(previous.at)>Date.parse(at)) return;
        const saved={value:output.value,at,applyStart:result.period?.apply_start || result.period?.end,
            model:this.model(result),policy:result.guidance?.policy || result.advice_policy};
        if(JSON.stringify(previous)===JSON.stringify(saved)) return;
        this.entries[key]=saved;
        const keys=Object.keys(this.entries).sort((a,b)=>Date.parse(this.entries[b].at)-Date.parse(this.entries[a].at));
        for(const old of keys.slice(24)) delete this.entries[old];
        this.persist();
    },
    present(kind,page,result,fresh,reason) {
        if(this.newVersionOnly()) return this.explicitNew(result) || {value:null,retained:false,reason:'新版建议未取得有效响应，不沿用旧值'};
        this.load();
        this.remember(kind,page,result,fresh);
        const saved=this.entries[JSON.stringify([kind,this.scope(page,result)])];
        const usable=saved && this.valid(saved.value) && Number.isFinite(Date.parse(saved.at)) &&
            (!result?.model || saved.model===this.model(result));
        if(fresh) return {value:result.suggested_density.value,at:kind==='hourly'?result.checked_at:result.generated_at,
            applyStart:result.period?.apply_start || result.period?.end,retained:false,reason};
        if(!usable) return {value:null,retained:false,reason};
        const time=new Date(saved.at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
        return {...saved,retained:true,reason:`沿用最后一次有效建议（${time}）；${reason || '等待新的有效建议'}`};
    },
};
