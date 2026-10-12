/* SQLite is authoritative for shared GPT inputs; legacy production preferences stay local. */
const DecisionSources = {
    config:null, loading:null, saving:false, error:'', epoch:0, legacyChoices:null,
    nodes:{'raw-ash':'raw_ash','coal-rate':'coal_amount','tail-level':'level','heavy-ash':'heavy_ash',
        'float-ash':'float_ash','float-amount':'float_amount','coarse-amount':'coarse_amount','density-current':'density',
        'system-switches':'systems','desliming':'desliming','float-press':'float_press','float-unmodeled':'reagent','coarse-slime':'raw_slime'},
    enabled(page) { return page.liveMode() && !!window.Api?.getDecisionCurrent && window.location.protocol.startsWith('http'); },
    apply(page, config) {
        this.config=config;
        if (page.liveMode()) page.pointChoices={...config.resolved_choices};
    },
    async ensure(page) {
        if (this.config) return this.config;
        if (this.loading) return this.loading;
        this.legacyChoices ||= {...page.pointChoices};
        this.loading=(async()=>{
            let config=await Api.decisionSources();
            if (!config.revision) {
                const choices=Object.fromEntries(Object.entries(this.legacyChoices).filter(([node,value])=>
                    node in this.nodes && node!=='density-current'));
                try { config=await Api.decisionSources({revision:0,choices}); }
                catch (error) { config=await Api.decisionSources(); if(!config.revision) throw error; }
            }
            this.apply(page,config); this.error=''; return config;
        })().finally(()=>{this.loading=null;});
        return this.loading;
    },
    async change(page,node,value) {
        if(this.saving) return;
        this.saving=true; this.error=''; this.epoch++;
        const selector=document.getElementById('decision-source-point');
        if(selector) selector.disabled=true;
        DecisionRealtime.result=null; DecisionRealtime.previousResult=null;
        try {
            const config=await this.ensure(page);
            this.apply(page,await Api.decisionSources({revision:config.revision,choices:{[node]:value}}));
            App.showToast('来源已共享，后续小时简报同步采用','success');
        } catch(error) {
            this.error=error.message;
            let confirmed=null;
            try { confirmed=await Api.decisionSources(); this.apply(page,confirmed); } catch(_) { /* Keep last confirmed configuration. */ }
            if(confirmed?.choices[node]===value) {
                this.error=''; App.showToast('已确认来源保存成功，后续小时简报同步采用','success');
            } else {
                App.showToast((confirmed?'未保存所选来源，已恢复服务器配置：':'来源保存结果未确认，请稍后重试：')+this.error,'error');
            }
        } finally {
            this.saving=false; this.epoch++; DecisionRealtime.lastAttempt=0;
            page.renderGraph(); page.renderContext(); page.renderInspector();
            DecisionRealtime.update(page);
        }
    },
    display(page,node) {
        const key=this.nodes[node.id];
        if(!key) return null;
        const c=DecisionRealtime.result?.cells?.[key];
        const choice=this.config?.resolved_choices[node.id];
        const item=page.displayPoints().find(p=>page.pointIdentity(p)===choice);
        if(!c) return {bound:true,item,available:false,value:'—',numericValue:null,source:this.error || '等待读取共享输入'};
        const group=c.value && typeof c.value==='object';
        const value=group ? Object.entries(c.value).map(([name,on])=>`${name} ${on===true?'开':on===false?'关':'未知'}`).join(' · ') :
            typeof c.value==='boolean' ? c.value?'运行':'停止' : page.number(c.value,node.id==='density-current'?3:2,c.unit);
        const parts=group ? Object.entries(c.value).map(([label,value])=>({label,value,available:typeof value==='boolean',received_at:c.components?.[label]?.observed_at})) : null;
        const aligned=node.id==='tail-level' && c.alignment?.version==='gpt-level-lag-v1';
        const badge=aligned && c.status==='good' ? '提前90秒' : c.raw_value===0 && ['raw-ash','coal-rate'].includes(node.id) ? '设备停机' :
            {manual:'补录',historical:'历史',default:'默认',calculated:'计算',held:'沿用',missing:'不可用'}[c.status] || '采集';
        return {bound:true,shared:true,badge,item,available:c.status!=='missing' && c.status!=='held' && c.value!=null,
            held:c.status==='held',stopped:c.raw_value===0 && ['raw-ash','coal-rate'].includes(node.id),manual:c.status==='manual',
            value,numericValue:Number.isFinite(c.value)?c.value:null,unit:c.unit || '',components:parts,
            source:aligned && c.status==='good' ? `采集于 ${page.time(c.observed_at)}` : [c.source,c.reason].filter(Boolean).join(' · '),
            title:c.reason || c.source,sampleAt:c.observed_at,enteredAt:c.created_at,alignment:c.alignment};
    },
};
