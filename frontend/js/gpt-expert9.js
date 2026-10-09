/* Independent expert GPT model. Never writes the legacy DS/model mirror. */
const GptExpert9 = {
    features:['raw_ash','coal_amount','sysA','sysB','sys401','sys402','desliming473','desliming474','level'],
    labels:['原煤灰分','小时带煤量','A系统','B系统','401系统','402系统','473脱粉','474脱粉','精磁尾液位'],
    group:null, loaded:false, error:'', view:null, pending:null, lastAttempt:0, panelKey:null,
    escape(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    predict(record,model=this.group?.expert9) {
        if(model?.kind!=='gpt-expert9-v1' || JSON.stringify(model.feature_names)!==JSON.stringify(this.features)) return null;
        const v=this.features.map(k=>record[k]);
        if(!v.every(Number.isFinite) || !(v[0]>0 && v[0]<=100 && v[1]>10 && v[8]>=0 && v[8]<=100 && v.slice(2,8).every(x=>x===0 || x===1))) return null;
        const terms=v.slice(0,8).map((x,j)=>model.coefs[j]*(Math.min(model.bounds[j][1],Math.max(model.bounds[j][0],x))-model.reference[j])/model.scale[j]);
        return 30.5-.3*Math.min(75,Math.max(25,v[8]))+model.correction_limit*Math.tanh(terms.reduce((a,b)=>a+b,0)/model.correction_limit);
    },
    async load(force=false) {
        if(this.pending || (!force && Date.now()-this.lastAttempt<30000) || !location.protocol.startsWith('http')) return this.pending;
        this.lastAttempt=Date.now();
        this.pending=(async()=>{
            try {
                const response=await fetch('/api/v1/gpt-coarse/current',{cache:'no-store',signal:AbortSignal.timeout(8000)});
                if(!response.ok) throw new Error('专家模型暂时读取失败，请刷新重试');
                const data=await response.json();
                if(data.model && (data.model.expert9?.kind!=='gpt-expert9-v1' || !Array.isArray(data.model.rows))) throw new Error('专家模型返回不完整');
                this.group=data.model; this.error='';
            } catch(error) { this.error=error.message; }
            finally {
                this.loaded=true;this.pending=null;
                if(typeof CoarsePage!=='undefined' && document.getElementById('page-coarse')?.classList.contains('active')) CoarsePage.refresh();
                if(typeof DecisionCoarseCharts!=='undefined' && document.getElementById('page-decision')?.classList.contains('active')) DecisionCoarseCharts.refresh();
            }
        })();
        return this.pending;
    },
    observe(result) {
        this.expectedArtifact=result?.model?.method==='expert9'?result.model.artifact_id:null;
        if(result?.model?.method==='expert9' && result.model.artifact_id!==this.group?.artifact_id) {
            if(Date.now()-this.lastAttempt>=5000) this.load(true);
        }
    },
    active() { return (this.view || (this.group || !this.loaded || this.error?'gpt':App.coarseEngine()))==='gpt' && (!!this.group || !this.loaded || !!this.error); },
    choose(engine) {
        if(!this.group && this.loaded && !this.error) return false;
        if(engine==='ds' && document.getElementById('page-coarse').classList.contains('gpt-expert-active')) this.restorePending=true;
        this.view=engine;
        if(engine==='gpt') { CoarsePage.stopAutoRoll(); this.load(); CoarsePage.refresh(); return true; }
        return false;
    },
    restoreLegacy() {
        if(!this.restorePending) return;
        this.restorePending=false;
        if(this.legacyRolling && !CoarsePage.rollTimer) CoarsePage.startAutoRoll();
    },
    chartSnapshot(mode='sample') {
        const group=this.group,model=group.expert9,source=group.rows;
        const samples=source.map(r=>({key:r.timestamp,label:r.timestamp,tick:r.timestamp.slice(5,16),day:r.day,
            n:1,actual:r.ash_content,pred:this.predict(r,model),level:r.level,scatterActual:r.ash_content,
            scatterPred:this.predict(r,model),paired:1}));
        let rows=samples;
        const mean=v=>v.reduce((a,b)=>a+b,0)/v.length;
        if(mode==='day') {
            const days=new Map();
            for(const row of samples) { if(!days.has(row.day)) days.set(row.day,[]); days.get(row.day).push(row); }
            rows=[...days].map(([day,rs])=>({key:day,label:day,tick:day.slice(5),n:rs.length,paired:rs.length,
                ...Object.fromEntries(['actual','pred','level','scatterActual','scatterPred'].map(k=>[k,mean(rs.map(r=>r[k]))]))}));
        }
        const reference={...Object.fromEntries(this.features.slice(0,8).map((k,j)=>[k,model.reference[j]])),level:50};
        const factors=this.features.map((key,j)=>{
            const values=source.map(r=>r[key]),avg=mean(values),std=Math.sqrt(mean(values.map(v=>(v-avg)**2)));
            const binary=j>=2 && j<8,from=binary?0:reference[key],to=binary?1:Math.min(j===8?75:model.bounds[j][1],from+std);
            const value=this.predict({...reference,[key]:to},model)-this.predict({...reference,[key]:from},model);
            return {key,name:this.labels[j],value,change:binary?'停止→运行':`${from.toFixed(2)}→${to.toFixed(2)}（增加1个历史标准差，受范围限制）`};
        }).sort((a,b)=>Math.abs(b.value)-Math.abs(a.value));
        return {expert:true,group,model,which:'expert9',rows,factors,count:source.length,missing:0,excluded:0,mode};
    },
    panel() {
        const page=document.getElementById('page-coarse'),enabled=this.active();
        if(enabled && !page.classList.contains('gpt-expert-active')) this.legacyRolling=!!CoarsePage.rollTimer || !CoarsePage.trendChart;
        page.classList.toggle('gpt-expert-active',enabled);
        if(!enabled) return false;
        CoarsePage.stopAutoRoll();
        CoarsePage.updateAmountCard();
        for(const engine of ['ds','gpt']) {
            const button=document.getElementById('coarse-engine-'+engine);
            button.classList.toggle('btn-primary',engine==='gpt');button.setAttribute('aria-pressed',String(engine==='gpt'));button.disabled=false;
        }
        let panel=document.getElementById('gpt-expert-panel');
        if(!panel) { panel=document.createElement('div');panel.id='gpt-expert-panel';page.append(panel); }
        const key=JSON.stringify([this.group?.artifact_id,this.error,this.loaded]);
        if(this.panelKey===key) return true;
        this.panelKey=key;
        if(!this.group) {panel.innerHTML=`<p role="status">${this.escape(this.error || '正在读取专家基准九因子模型…')}</p>`;return true;}
        const g=this.group,m=g.expert9,r=g.report,e=this.escape,fmt=(v,d=2)=>Number.isFinite(v)?v.toFixed(d):'—';
        const selected=r.tuning_validation.trials.find(t=>t.alpha===r.tuning_validation.selected_alpha);
        const metrics=(title,score,note)=>`<section class="gpt-expert-card"><h4>${title}</h4><strong>${fmt(score.mae)} <small>百分点 MAE</small></strong><p>RMSE ${fmt(score.rmse)} · ${score.n} 条</p><p>${note}</p></section>`;
        const snapshot=this.chartSnapshot(),samples=snapshot.rows;
        const ys=samples.flatMap(x=>[x.actual,x.pred]),low=Math.floor(Math.min(...ys))-1,high=Math.ceil(Math.max(...ys))+1;
        const px=i=>50+i/(samples.length-1)*950,py=v=>220-(v-low)/(high-low)*190;
        const path=k=>samples.map((x,i)=>`${i?'L':'M'}${px(i).toFixed(1)},${py(x[k]).toFixed(1)}`).join(' ');
        panel.innerHTML=`<div class="gpt-expert-heading"><div><p>GPT / 专家基准九因子</p><h3>315 灰分预测</h3></div><span>已接入决策总览 · 待现场验证</span></div>
            ${this.error?`<p role="alert">${e(this.error)}；当前展示上次读取的模型</p>`:''}
            <div class="gpt-expert-anchor"><div><small>精磁尾液位 25%</small><strong>23.00%</strong></div><div><small>参考工况 · 液位为主</small><strong>30.5 − 0.3 × 液位</strong></div><div><small>精磁尾液位 75%</small><strong>8.00%</strong></div></div>
            <p>其余 8 个因素合计修正不超过 ±2 个灰分百分点。液位在 25–75% 外显示边界估算，并暂缓建议调密。</p>
            <div class="gpt-expert-metrics">${metrics('历史拟合',r.historical_fit,'最终模型对训练记录回算')}${metrics('调参验证',selected.metrics,'开发窗口内，按完整日期向后验证')}${metrics('历史留后检验',r.forward_time_check.metrics,`${e(r.forward_time_check.start)} 至 ${e(r.forward_time_check.end)}；未用于本次选参`)}</div>
            <p class="gpt-expert-caution">历史记录与专家关系存在明显差异；历史留后结果未超过均值基线（MAE ${fmt(r.forward_time_check.historical_mean.mae)}）。这些数据曾用于开发，不能作为新的独立验收结果。</p>
            <section class="gpt-expert-chart"><h4>历史化验与新模型回算</h4><p><span class="gpt-series-actual">蓝线：历史化验</span>　<span class="gpt-series-pred">橙线：专家模型回算</span>　${g.n} 条正常生产记录 · ${m.days} 个日期</p>
            <svg viewBox="0 0 1040 255" role="img" aria-label="历史化验与专家模型预测对比；数值详见下方数据表">
            ${[0,1,2,3,4].map(i=>{const v=low+(high-low)*i/4;return `<line x1="50" x2="1000" y1="${py(v)}" y2="${py(v)}" stroke="currentColor" opacity=".12"/><text x="8" y="${py(v)+4}" fill="currentColor" font-size="13">${fmt(v,1)}%</text>`;}).join('')}
            <path d="${path('actual')}" fill="none" stroke="#478ce8" stroke-width="2"/><path d="${path('pred')}" fill="none" stroke="#de972e" stroke-width="2"/>
            <text x="50" y="247" fill="currentColor" font-size="13">${e(samples[0].day)}</text><text x="925" y="247" fill="currentColor" font-size="13">${e(samples.at(-1).day)}</text></svg></section>
            <details><summary>因子与参考工况</summary><p>参考条件：原煤 ${fmt(m.reference[0])}%、带煤量 ${fmt(m.reference[1],0)} t/h；系统/脱粉参考状态见下表。A 系统关闭样本不足，本版对其独立修正取 0，仍检查该输入。</p><table><thead><tr><th>因子</th><th>参考值</th><th>表内有效范围</th></tr></thead><tbody>${this.features.map((k,j)=>`<tr><td>${this.labels[j]}</td><td>${j===8?'专家基准 25–75':j>=2?(m.reference[j]?'开':'停'):fmt(m.reference[j])}</td><td>${g.source.ranges[k].map(v=>fmt(v,j<2||j===8?2:0)).join('–')}</td></tr>`).join('')}</tbody></table></details>
            <details><summary>数据来源与清理记录</summary><p>${e(g.source.source.split(/[\\/]/).at(-1))} · Sheet1!${e(g.source.range)}。原始 ${g.source.original_rows} 条；排除 ${g.source.stopped_rows.length} 条停机记录，${g.source.rejected_rows.length} 条待核实记录。原始表格未修改。</p><p>${g.source.date_repairs.length} 行数字日期按前后日期修复；表内未给年份，按项目 2026 年处理。夜班时刻不推断跨日，按完整日期验证。化验报告时间未知，没有将同日的315灰分化验值用作预测输入。</p><p>模型启用：${e(g.activated_at)} · 版本 ${g.revision}</p></details>
            <details><summary>查看 ${g.n} 条回算数据</summary><div class="gpt-expert-table"><table><thead><tr><th>原表行</th><th>采样时间</th><th>液位 %</th><th>化验灰分 %</th><th>回算灰分 %</th></tr></thead><tbody>${g.rows.map(row=>`<tr><td>${row.source_row}</td><td>${e(row.timestamp)}</td><td>${fmt(row.level)}</td><td>${fmt(row.ash_content)}</td><td>${fmt(this.predict(row))}</td></tr>`).join('')}</tbody></table></div></details>`;
        return true;
    },
};
if(typeof window!=='undefined') {
    window.GptExpert9=GptExpert9;
    document.addEventListener('DOMContentLoaded',()=>{
        GptExpert9.load();
        setInterval(()=>{if(!document.hidden) GptExpert9.load();},30000);
    });
}
