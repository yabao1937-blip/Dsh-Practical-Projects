/* Previous completed natural hour. Read-only server summaries; never changes App.store. */
const DecisionHourly = {
    result:null, inFlight:null, lastAttempt:0, error:'', receivedAt:0, resultEpoch:0,
    keys:{'raw-ash':'raw_ash','coal-rate':'coal_amount','tail-level':'level','system-switches':'systems',
        desliming:'desliming','heavy-ash':'heavy_ash','heavy-amount':'heavy_amount','float-ash':'float_ash',
        'float-amount':'float_amount','float-press':'float_press','float-unmodeled':'reagent',
        'coarse-amount':'coarse_amount','coarse-slime':'raw_slime','density-current':'density',
        coarse:'coarse_ash',density:'suggested_density'},
    epoch() { return typeof DecisionSources==='undefined'?0:DecisionSources.epoch; },
    clock() { return typeof performance==='undefined'?Date.now():performance.now(); },
    presentation() {
        const r=this.result;
        const serverNow=r ? Date.parse(r.checked_at)+(this.clock()-this.receivedAt) : NaN;
        return r && this.resultEpoch===this.epoch() && serverNow<Date.parse(r.period.apply_end) ? r : null;
    },
    pending() { return !this.presentation() && !!this.inFlight; },
    async update(page,force=false) {
        if(!page.hourMode() || this.inFlight || (!force && Date.now()-this.lastAttempt<4500)) return;
        const epoch=this.epoch();
        this.lastAttempt=Date.now(); this.error='';
        const work=(async()=>{
            try {
                const result=await Api.getDecisionHourly();
                if(epoch!==this.epoch()) return;
                this.result=result; this.receivedAt=this.clock(); this.resultEpoch=epoch;
            } catch(error) { if(epoch===this.epoch()) {this.result=null; this.error=error.message || '小时统计读取失败';} }
        })();
        this.inFlight=work;
        await work;
        this.inFlight=null;
        if(page.active() && page.hourMode()) {
            page.renderGraph(); page.renderContext(); page.renderInspector();
            if(epoch!==this.epoch()) this.update(page,true);
        }
    },
    display(page,node) {
        if(page.summaryChildren.has(node.id)) {
            const summaryParts=page.summaryChildren.get(node.id).map(id=>{
                const child=page.nodes.find(n=>n.id===id);
                return {node:child,display:this.display(page,child)};
            });
            return {hourly:true,aggregate:true,summaryParts,available:summaryParts.every(p=>p.display.available),
                value:summaryParts[0].display.value,source:`煤量 ${summaryParts[1].display.value}`,badge:'小时均值',
                title:summaryParts.map(p=>`${p.node.title} ${p.display.value}`).join('；')};
        }
        const result=this.presentation(),key=this.keys[node.id],c=result?.cells?.[key];
        const available=Number.isFinite(c?.value), isAdvice=node.id==='density';
        const components=c?.components && !Array.isArray(c.components) ? Object.entries(c.components).map(([label,part])=>({
            label,value:part.value,available:Number.isFinite(part.value),fraction:true})) : null;
        const reason=c?.reason || this.error || '正在读取上一小时统计';
        const source=isAdvice ? available ? `用于 ${result.period.apply_label} · ${result.guidance.direction==='down'?'整体下调':result.guidance.direction==='up'?'整体上调':'保持'}` : '覆盖不足或当前状态暂缓' :
            c?.carried ? `沿用 · ${page.time(c.observed_at)}` : c?.sample_count ? `${c.sample_count} 条有效样本 · 小时平均` : components ? '有效样本中的运行占比' : reason;
        return {hourly:true,shared:true,calculated:true,inputs:[],available:available || !!components?.some(c=>c.available),
            numericValue:available?c.value:null,unit:c?.unit || '',sampleAt:c?.observed_at,components,
            value:components ? components.map(c=>`${c.label} ${c.available?page.number(c.value*100,0,'%'):'未知'}`).join(' · ') : page.number(c?.value,isAdvice || key==='density'?3:key?.endsWith('_amount')?1:2,isAdvice?'':c?.unit),
            source,title:reason,reason,pending:this.pending(),
            badge:isAdvice ? available?'小时建议':'暂缓' : c?.carried?'沿用':available || components?'小时均值':'无数据'};
    },
    context(page) {
        const r=this.presentation(),coverage=r?.coverage;
        document.getElementById('decision-mode-label').textContent='小时累加 · 人工执行';
        document.getElementById('decision-mode-note').textContent=(r?.policy==='decision-hourly-v2'?'总灰分按 501+502 实际产煤量加权；工况按系统组合、开停机和明显变化分段。':'该小时为旧口径样本均值。')+' 手工无新增沿用并注明时间。过渡期仍取平均建议偏移加到小时末密度上；专家经验建议 · 非实验验证的达标区间，单次执行限 ±0.03。';
        page.renderHTML('decision-context',r ? `<span>统计时段 <strong>${page.escape(r.period.label)}（北京时间）</strong></span><span>建议用于 <strong>${page.escape(r.period.apply_label)}</strong></span><span>密度来源 <strong>${page.escape(r.density_label)}</strong></span><span>有效建议覆盖 <strong>${(coverage.ratio*100).toFixed(1)}% · ${coverage.valid_count}/${coverage.expected_count}</strong></span><span class="decision-hour-status" role="status">${Number.isFinite(r.suggested_density.value)?`上调 ${coverage.up} · 下调 ${coverage.down} · 保持 ${coverage.stable}`:'数据不足或保护生效 · 暂缓调密'}</span>` : `<span role="status">${page.escape(this.error || '正在读取上一完整小时…')}</span>`);
    },
    inspector(page,node) {
        const r=this.presentation(),d=this.display(page,node),c=r?.cells?.[this.keys[node.id]],pairs=[
            ['统计时段',r?.period.label || '等待读取'],['显示值',d.value],['统计口径',d.reason || '左侧两项小时统计汇总']];
        if(c) {
            if(c.sample_count!==undefined) pairs.push(['有效样本',String(c.sample_count)],['排除样本',String(c.excluded_count)]);
            if(c.source) pairs.push(['来源',c.source]);
            if(c.item_id) pairs.push(['采集点',c.item_id]);
            if(c.observed_at) pairs.push([c.carried?'沿用值采样时间':'末条有效采样',page.time(c.observed_at)]);
        }
        if(node.id==='density' && r) pairs.push(['小时末有效密度',page.number(r.guidance.rhoCur,3,'g/cm³')],
            ['基准采样时间',page.time(r.guidance.basis_at)],['平均建议偏移',page.number(r.guidance.mean_delta,5,'g/cm³')],
            ['单次执行步幅',`±${page.number(r.guidance.maxStep,3)}`],['验证状态','专家经验建议 · 非实验验证的达标区间'],
            ['达标区间','等待响应实验；不把执行步幅当成区间宽度'],['建议使用时段',r.period.apply_label],
            ['有效覆盖',`${r.coverage.valid_count}/${r.coverage.expected_count}（至少 ${r.coverage.required_ratio*100}%）`],
            ['最长连续缺口',`${r.coverage.longest_gap_seconds} 秒（不超过 ${r.coverage.max_gap_seconds} 秒）`]);
        if(c?.components) for(const [label,part] of Object.entries(c.components)) pairs.push([
            label,`${Number.isFinite(part.value)?page.number(part.value*100,1,'%'):'—'} 运行 · ${part.sample_count} 条有效样本`]);
        if(d.summaryParts) for(const p of d.summaryParts) pairs.push([p.node.title,p.display.value]);
        const facts=pairs.map(([label,value])=>`<dt>${page.escape(label)}</dt><dd>${page.escape(value)}</dd>`).join('');
        page.renderHTML('decision-inspector',`<div class="decision-inspector-header"><span>小时统计详情</span><button type="button" class="decision-inspector-close" data-decision-close>收起 ×</button></div><div class="decision-detail"><h3>${page.escape(node.title)}</h3><dl class="decision-facts">${facts}</dl><p>统计来源沿用该小时的决策总览配置；在即时模式中调整后，从后续采样开始生效。</p>${node.id==='density'?`<p>${page.escape(r?.suggested_density.reason || this.error)}</p>`:''}</div>`);
        page.queueInspectorPosition();
    },
};
