/* Display memory only. Held numbers never become calculation inputs or valid advice. */
const DecisionDensityDisplay = {
    storageKey:'dmcs.decision-density-display.v1', entries:{}, loaded:false,
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
