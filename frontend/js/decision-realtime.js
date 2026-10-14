/* Decision-only live input orchestration. The server evaluates one immutable input snapshot. */
const DecisionRealtime = {
    result:null, resultKey:null, currentKey:null, inFlight:null, lastAttempt:0, error:'', previousResult:null, completedAt:0, previewUntil:0,
    pending() { return !this.result && !!this.inFlight && !this.error; },
    presentation() {
        if(this.result) return this.result;
        // This is a labelled visual preview only, never the current valid result.
        return this.pending() && Date.now()<this.previewUntil && Date.now()-this.completedAt<30000 ? this.previousResult : null;
    },
    defaults:{'raw-ash':'TIA_3_1.TIA_3_1.202/ASH_R_INS','coal-rate':'TIA_3_1.TIA_3_1.301/COAL_CAP',
        'tail-level':'TIA_3_5.TIA_3_5.JMCWC/LEVEL_B','system-switches':'four_system_status','desliming':'desliming_status'},
    active(page) { return page.view === 'gpt'; },
    bindDefaults(page) {
        if (!this.active(page)) return;
        let changed=false;
        for (const [id,identity] of Object.entries(this.defaults)) {
            if (Object.prototype.hasOwnProperty.call(page.pointChoices,id)) continue;
            const matches=page.displayPoints().filter(p=>p.item_id===identity || p.key===identity);
            if (matches.length===1) {page.pointChoices[id]=page.pointIdentity(matches[0]);changed=true;}
        }
        if (changed) page.savePointChoices();
    },
    cell(page,d) {
        const usable=page.displayAvailable(d) || d.held;
        const status=!usable || !Number.isFinite(d.numericValue) ? 'missing' : d.stopped ? 'stopped' : d.held ? 'held' : d.manual ? 'manual' :
            d.bound ? 'good' : /默认/.test(d.source) ? 'default' : /人工/.test(d.source) ? 'manual' : 'historical';
        const time=t=>t==null ? null : Number.isFinite(new Date(t).getTime()) ? new Date(t).toISOString() : null;
        return {value:status==='missing'?null:d.numericValue,status,unit:d.unit || '',source:d.source || '',
            reason:status==='missing' ? d.reason || d.source || '数据缺失' : '',observed_at:time(d.sampleAt),entered_at:time(d.enteredAt),
            item_id:d.item?.item_id || '',source_id:d.item?.source_id || '',batch_id:d.item?.sample?.batch_id || null};
    },
    capture(page) {
        const get=id=>page.pointDisplay(page.nodes.find(n=>n.id===id));
        const cells={};
        for (const [key,id] of Object.entries({raw_ash:'raw-ash',coal_amount:'coal-rate',level:'tail-level',
            heavy_ash:'heavy-ash',float_ash:'float-ash',float_amount:'float-amount',coarse_amount:'coarse-amount',density:'density-current'})) {
            cells[key]=this.cell(page,get(id));
        }
        for (const [id,mapping] of [['system-switches',{A:'sysA',B:'sysB','401':'sys401','402':'sys402'}],
            ['desliming',{'473':'desliming473','474':'desliming474'}]]) {
            const d=get(id), node=page.nodes.find(n=>n.id===id);
            for (const [label,key] of Object.entries(mapping)) {
                const part=d.components?.find(c=>c.label===label);
                const old=node.fields?.find(f=>f.key===key)?.raw;
                const value=d.bound ? (part?.available && !window.Operations?.telemetryFailed && typeof part.value==='boolean' ? Number(part.value) : null) : old;
                cells[key]=this.cell(page,{...d,numericValue:value,unit:'',available:d.bound ? !!d.available && Number.isFinite(value) : undefined,
                    sampleAt:part?.received_at || d.sampleAt});
            }
        }
        const coal=get('coal-rate'), raw=coal.item?.sample?.value;
        const amount=coal.bound ? raw : coal.numericValue;
        cells.is_stoppage={...cells.coal_amount,unit:'',value:Number.isFinite(amount) ? Number(amount<=10) : null};
        if (cells.is_stoppage.value===null) cells.is_stoppage.status='missing';
        const balance=page.heavyAmountDisplay();
        cells.belt501=this.cell(page,balance.inputs[0].display);
        cells.belt502=this.cell(page,balance.inputs[1].display);
        const target=typeof CollectPage!=='undefined' ? CollectPage.instruments.find(i=>i.id==='total_ash') : null;
        return {cells,target_min:target?.min ?? 8.5,target_max:target?.max ?? 9.0};
    },
    update(page) {
        if (!this.active(page)) {this.currentKey=null;return;}
        if(typeof DecisionHourly!=='undefined') DecisionHourly.update(page);
        if(page.hourMode?.()) return;
        if(typeof DecisionSources!=='undefined' && DecisionSources.enabled(page)) { this.updateShared(page); return; }
        const body=this.capture(page), key=JSON.stringify(body);
        this.currentKey=key;
        if (this.resultKey!==key) {
            if(this.result) { this.previousResult=this.result; this.previewUntil=Date.now()+5000; }
            this.result=null;
        }
        const eight=(this.result || this.previousResult)?.model?.method==='expert8';
        const safePreview=Object.entries(body.cells).filter(([key])=>!eight || key!=='raw_ash').every(([,c])=>Number.isFinite(c.value) && !['missing','held','stopped'].includes(c.status) &&
            (c.status!=='good' || (Date.now()-Date.parse(c.observed_at)>=-5000 && Date.now()-Date.parse(c.observed_at)<=30000))) &&
            body.cells.is_stoppage.value===0 && body.cells.density.value>0 && body.cells.density.value!==1;
        if(!safePreview) this.previousResult=null;
        if (this.inFlight || (this.resultKey===key && Date.now()-this.lastAttempt<4500)) return;
        this.lastAttempt=Date.now(); this.error='';
        if (!window.Api?.calculateDecision || !window.location.protocol.startsWith('http')) {
            this.result=null;this.previousResult=null;this.error='请通过服务器地址打开页面以运行实时决策';return;
        }
        this.inFlight=(async()=>{
            try {
                const result=await window.Api.calculateDecision(body);
                if(typeof GptExpert9!=='undefined') GptExpert9.observe(result);
                if (this.currentKey!==key) return;
                this.result=result;this.resultKey=key;this.previousResult=null;this.completedAt=Date.now();
                DecisionDensityDisplay.remember('instant',page,result);
            } catch(error) {
                if (this.currentKey!==key) return;
                this.result=null;this.previousResult=null;this.resultKey=null;this.error=error.message || '后台计算暂不可用';
            } finally {
                this.inFlight=null;
                if (page.active() && this.active(page)) {
                    if (this.currentKey!==key) this.update(page);
                    page.renderGraph();page.renderContext();
                    if (document.activeElement?.id!=='decision-source-point') page.renderInspector();
                }
            }
        })();
    },
    updateShared(page) {
        if(this.inFlight || DecisionSources.saving || Date.now()-this.lastAttempt<4500) return;
        this.lastAttempt=Date.now();
        const epoch=DecisionSources.epoch;
        this.inFlight=(async()=>{
            try {
                await DecisionSources.ensure(page);
                const result=await Api.getDecisionCurrent();
                if(typeof GptExpert9!=='undefined') GptExpert9.observe(result);
                if(epoch!==DecisionSources.epoch) return;
                if(!DecisionSources.apply(page,result.sources)) return;
                this.result=result; this.previousResult=null; this.completedAt=Date.now(); this.error='';
                DecisionDensityDisplay.remember('instant',page,result);
            } catch(error) {
                if(epoch!==DecisionSources.epoch) return;
                this.result=null; this.previousResult=null; this.error=error.message;
            } finally {
                this.inFlight=null;
                if(page.active() && this.active(page)) {
                    page.renderGraph(); page.renderContext(); page.renderInspector();
                    if(epoch!==DecisionSources.epoch) {this.lastAttempt=0; this.updateShared(page);}
                }
            }
        })();
    },
    display(page,node) {
        const key=node.id==='coarse'?'coarse_ash':'suggested_density', result=this.presentation(),pending=this.pending();
        const output=result?.[key], value=output?.value;
        const reason=pending ? result ? '正在更新；显示上一轮结果，仅供对照，本轮完成后自动替换' : '正在按当前输入计算' : output?.reason || this.error || '正在按当前输入计算';
        const available=!pending && Number.isFinite(value) && output.status!=='held';
        const inputKeys=node.id==='coarse' ? ['expert9','expert8'].includes(result?.model?.method)?result.features.map(f=>f.key):App.MLR_FEATURES : ['density','heavy_ash','float_ash','float_amount','coarse_amount'];
        const inputs=inputKeys.map(key=>{
            const c=result?.inputs?.[key] || {};
            const feature=result?.features?.find(f=>f.key===key);
            return {node:{title:App.FEATURE_LABELS[key] || ({density:'当前分选密度',heavy_ash:'重介精煤灰分',float_ash:'浮精灰分',float_amount:'浮精量',coarse_amount:'粗精煤泥量'})[key]},
                display:{value:page.number(c.value,3,c.unit),source:(c.source || '等待输入')+(feature?.clipped ? `；模型限幅后 ${feature.used}` : ''),sampleAt:c.observed_at,enteredAt:c.entered_at}};
        });
        if(node.id==='density') inputs.unshift(
            {node:{title:'预测总灰分'},display:{value:page.number(result?.total_ash?.value,2,'%'),source:result?.total_ash?.reason || reason,sampleAt:result?.generated_at}},
            {node:{title:'粗精煤泥预测灰分'},display:{value:page.number(result?.coarse_ash?.value,2,'%'),source:result?.coarse_ash?.reason || reason}},
            {node:{title:'重介精煤量'},display:{value:page.number(result?.heavy_amount?.value,3,'t/h'),source:'501 + 502 − 浮精量 − 粗精煤泥量'}});
        const amount=page.pointDisplay(page.nodes.find(n=>n.id==='coarse-amount'));
        const guide=result?.guidance;
        const shown=node.id==='density'?DecisionDensityDisplay.present('instant',page,result,available,reason):null;
        return {live:true,calculated:true,pending,bound:false,numericValue:Number.isFinite(value)?value:null,available,held:shown?.retained || output?.status==='held',inputs,
            value:shown ? Number.isFinite(shown.value)?page.number(shown.value,3):'暂无有效值' : page.number(value,2,'%'),
            badge:pending?'更新中':available ? node.id==='coarse'?'预测':'建议' : Number.isFinite(value)?'沿用输入':/暂缓|已按|刚调整/.test(reason)?'暂缓':'待计算',
            source:pending ? result ? '上轮结果 · 等待本轮计算' : '等待本轮计算' : node.id==='coarse' ? `煤量 ${amount.value} · GPT${output?.status==='held'?'沿用输入预测':['expert9','expert8'].includes(result?.model?.method)?'专家基准预测':'实时预测'}` :
                Number.isFinite(value) ? guide?.direction==='down'?'本步建议 · 下调':guide?.direction==='up'?'本步建议 · 上调':'密度保持' : '等待有效输入',
            formula:node.id==='coarse'?`GPT / ${result?.model?.method?.toUpperCase() || '保存模型'} · ${result?.model?.id || '等待模型'}`:`总灰分 ${page.number(result?.target?.min ?? 8.5,2)}–${page.number(result?.target?.max ?? 9.0,2)}% 范围内保持，范围外向最近边界调整`,
            reason:shown?.reason || reason,title:shown?.reason || reason,sampleAt:shown?.at || result?.generated_at};
    },
};
