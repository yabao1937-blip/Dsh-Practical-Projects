/* Previous completed natural hour. Read-only server summaries; never changes App.store. */
const DecisionHourly = {
    result:null, inFlight:null, lastAttempt:0, error:'', clockBaseAt:0, resultEpoch:0, sourceKeyAtRequest:'',
    keys:{'raw-ash':'raw_ash','coal-rate':'coal_amount','tail-level':'level','system-switches':'systems',
        desliming:'desliming','heavy-ash':'heavy_ash','heavy-amount':'heavy_amount','float-ash':'float_ash',
        'float-amount':'float_amount','float-press':'float_press','float-unmodeled':'reagent',
        'coarse-amount':'coarse_amount','coarse-slime':'raw_slime','density-current':'density',
        coarse:'coarse_ash',density:'suggested_density'},
    epoch() { return typeof DecisionSources==='undefined'?0:DecisionSources.epoch; },
    clock() { return typeof performance==='undefined'?Date.now():performance.now(); },
    enabled(page) { return page.view==='gpt'; },
    sourceKey() {
        const choices=typeof DecisionSources==='undefined'?{}:DecisionSources.config?.resolved_choices || {};
        return JSON.stringify(Object.keys(choices).sort().map(key=>[key,choices[key]]));
    },
    presentation() {
        const r=this.result;
        const serverNow=r ? Date.parse(r.checked_at)+(this.clock()-this.clockBaseAt) : NaN;
        // Another workstation may change the selected sources without changing
        // this browser's epoch. Wait for a new server check before showing advice.
        const current=typeof DecisionSources==='undefined'?null:DecisionSources.config?.resolved_choices;
        const previous=r?.sources?.resolved_choices;
        // A stale local configuration must not overrule a newer server check.
        if(Number.isFinite(r?.suggested_density?.value) && this.sourceKey()!==this.sourceKeyAtRequest && current && previous &&
            [...new Set([...Object.keys(current),...Object.keys(previous)])].some(key=>
                !(r.model?.method==='expert8' && key==='raw-ash') && current[key]!==previous[key])) return null;
        return r && this.resultEpoch===this.epoch() && serverNow<Date.parse(r.period.apply_end) ? r : null;
    },
    pending() { return !this.presentation() && !!this.inFlight; },
    dataGaps(result=this.presentation()) {
        const gaps=new Map(), advice=result?.suggested_density;
        if(!advice) return gaps;
        const blocked=!Number.isFinite(advice.value), coverage=result.coverage || {};
        const coverageLow=Number.isFinite(coverage.ratio) && coverage.ratio<coverage.required_ratio;
        const gapLong=Number.isFinite(coverage.longest_gap_seconds) && coverage.longest_gap_seconds>coverage.max_gap_seconds;
        const adviceGap=blocked && (coverageLow || gapLong);
        const put=(id,severity,label,message,reason=message)=>gaps.set(id,{severity,label,message,reason});
        // Only inputs used by the recommendation chain. Reference-only cards do
        // not become blockers, and one carried manual reading is still valid.
        const fields={'raw-ash':'raw_ash','coal-rate':'coal_amount','tail-level':'level',
            'heavy-ash':'heavy_ash','heavy-amount':'heavy_amount','float-ash':'float_ash',
            'float-amount':'float_amount','coarse-amount':'coarse_amount','density-current':'density',
            coarse:'coarse_ash','total-ash':'total_ash'};
        const referenceOnly=id=>result.model?.method==='expert8' && id==='raw-ash';
        for(const [id,key] of Object.entries(fields)) {
            const cell=result.cells?.[key];
            if(!Number.isFinite(cell?.value) || ['missing','held','invalid'].includes(cell.status)) {
                // Missing computed history does not prove an input failure. A
                // weighted total can also lack flow pairing despite valid advice.
                const error=adviceGap && !referenceOnly(id) && !['coarse_ash','heavy_amount','total_ash'].includes(key);
                put(id,error?'error':'warning',error?'数据不足':'统计缺数',cell?.reason || '该小时缺少有效数据');
            }
        }
        const groups={'system-switches':['systems',['401','402','A','B']],desliming:['desliming',['473','474']]};
        for(const [id,[key,labels]] of Object.entries(groups)) {
            const missing=labels.filter(label=>!Number.isFinite(result.cells?.[key]?.components?.[label]?.value));
            if(missing.length) put(id,adviceGap?'error':'warning',adviceGap?'数据不足':'统计缺数',`${missing.join('、')} 状态缺少有效数据`);
        }
        const basis=result.density_basis || {value:result.guidance?.rhoCur,observed_at:result.guidance?.basis_at};
        const age=(Date.parse(result.period?.end)-Date.parse(basis.observed_at))/1000;
        const basisMissing=!Number.isFinite(basis.value) || basis.value<=0 || Math.abs(basis.value-1)<=1e-9 ||
            ['missing','held','invalid'].includes(basis.status) || !(age>0 && age<=30);
        if(blocked && basisMissing)
            put('density-current','error','基准缺失','小时末 30 秒内没有有效实测密度；小时均值仅供查看');
        // A valid hourly mean can conceal a period of missing/held readings.
        // Use recorded segment quality, never infer sensor gaps from sample counts.
        const valid=new Set(['good','manual','historical','default','predicted','calculated','average','carried']);
        const missing=new Map(), cadence=coverage.cadence_seconds, expected=coverage.expected_count;
        const firstSlot=Date.parse(result.period?.start)/1000/cadence;
        const slotEvidence=Number.isInteger(firstSlot) && Number.isInteger(expected) && expected>0 && cadence>0;
        for(const segment of result.evidence?.segments || []) {
            const affected=new Set();
            for(const [id,key] of Object.entries(fields)) {
                const quality=segment.state?.quality?.[key];
                if(quality && !valid.has(quality)) affected.add(id);
            }
            for(const [id,[key,labels]] of Object.entries(groups)) {
                const switches=segment.state?.switches?.[key];
                if(switches && labels.some(label=>typeof switches[label]!=='boolean')) affected.add(id);
            }
            for(const id of affected) {
                const entry=missing.get(id) || {seconds:0,slots:new Set()};
                entry.seconds+=Math.max(0,segment.duration_seconds||0);
                // Only recorded invalid slots can be attributed to this input.
                // Do not bridge unrecorded periods or use wall time as slot coverage.
                if(slotEvidence && Number.isInteger(segment.first_slot) && Number.isInteger(segment.last_slot))
                    for(let slot=Math.max(firstSlot,segment.first_slot);slot<=Math.min(firstSlot+expected-1,segment.last_slot);slot++) entry.slots.add(slot);
                missing.set(id,entry);
            }
        }
        for(const [id,{seconds,slots}] of missing) {
            let longest=0,run=0,previous=-Infinity;
            for(const slot of [...slots].sort((a,b)=>a-b)) {
                run=slot===previous+1?run+1:1; longest=Math.max(longest,run); previous=slot;
            }
            const tooLong=longest*cadence>coverage.max_gap_seconds;
            const tooFew=slotEvidence && 1-slots.size/expected<coverage.required_ratio;
            const error=adviceGap && !referenceOnly(id) && (tooLong || tooFew);
            const existing=gaps.get(id);
            if(existing && (existing.severity==='error' || !error)) continue;
            const message=error ? tooLong?`该项连续无效 ${longest*cadence} 秒，超过 ${coverage.max_gap_seconds} 秒门槛`:
                `该项无效时隙占比 ${(slots.size/expected*100).toFixed(1)}%，有效覆盖不足 ${coverage.required_ratio*100}%` :
                seconds>0?`小时内累计约 ${Math.round(seconds)} 秒无效；显示其余有效均值`:'小时内存在无效数据；显示其余有效均值';
            put(id,error?'error':'warning',error?'数据不足':seconds>0 && seconds<=60?'短时缺失':'部分缺失',message,
                message+(error?'；该项缺失已超过小时建议的数据门槛':'；局部缺失提示，不代表该项导致建议暂缓'));
        }
        for(const [id,children] of [['heavy',['heavy-ash','heavy-amount']],['float',['float-ash','float-amount']]]) {
            const entries=children.map(key=>gaps.get(key)).filter(Boolean);
            if(entries.length) {
                const error=entries.some(g=>g.severity==='error');
                put(id,error?'error':'warning',error?'数据不足':'部分缺失',
                    [...new Set(entries.map(g=>g.message))].join('；'),[...new Set(entries.map(g=>g.reason))].join('；'));
            }
        }
        if(adviceGap || blocked && basisMissing) {
            const messages=[];
            if(coverageLow) messages.push(`有效建议 ${(coverage.ratio*100).toFixed(1)}%（需 ≥${coverage.required_ratio*100}%）`);
            if(gapLong) messages.push(`连续无有效建议 ${coverage.longest_gap_seconds} 秒（限 ${coverage.max_gap_seconds} 秒）`);
            if(basisMissing) messages.push('小时末密度基准缺失');
            put('density','error','建议不足',messages.join('；'),advice.reason || messages.join('；'));
        }
        return gaps;
    },
    gapClass(gap) { return gap ? gap.severity==='error'?' is-hourly-gap':' is-hourly-warning' : ''; },
    ashSummary(page) {
        if(!this.enabled(page) || page.hourMode()) return '';
        const r=this.presentation(), ash=r?.total_ash;
        const value=ash?.status==='average' && Number.isFinite(ash.value) && ash.value>=0 && ash.value<=100 ? ash.value : null;
        const title=[r?.period?.label || '上一完整小时',ash?.reason || this.error || (value===null?'该小时暂无有效总灰分':'小时平均总灰分')].join(' · ');
        return `<span class="decision-ash-metric" data-decision-key="hourly-ash" title="${page.escape(title)}"><span class="decision-total-label">小时灰分</span><strong>${page.number(value,2)}<small> %</small></strong></span>`;
    },
    summary(page,root) {
        if(!this.enabled(page) || page.hourMode() || !root) return '';
        const r=this.presentation(), value=r?.suggested_density?.value, available=Number.isFinite(value);
        const gap=r?this.dataGaps(r).get('density'):null;
        const updatedAt=new Date(r?.period?.apply_start || r?.period?.end || '');
        const updatedTime=Number.isFinite(updatedAt.getTime())?updatedAt.toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}):'—';
        const statusClass=this.gapClass(gap) || (r && !available?' is-hourly-warning':'');
          const height=root.summaryHeight??201, spacing=root.summaryGap??24;
          return `<section class="decision-node decision-hourly-summary${statusClass}" data-decision-key="hourly-summary" aria-label="本小时建议密度" aria-busy="${this.pending()}" style="left:${root.x}px;top:${root.y-height-spacing}px;width:${root.w}px;height:${height}px"><span class="decision-node-heading">本小时建议密度</span><span class="decision-node-value">${page.number(value,3)} <span class="decision-root-unit">g/cm³</span></span><span class="decision-node-source">更新于 ${page.escape(updatedTime)}</span></section>`;
    },
    async update(page,force=false) {
        if(!this.enabled(page) || this.inFlight || (typeof DecisionSources!=='undefined' && DecisionSources.saving) ||
            (!force && Date.now()-this.lastAttempt<4500)) return;
        const epoch=this.epoch(), requestedAt=this.clock(), sourceKey=this.sourceKey();
        this.lastAttempt=Date.now(); this.error='';
        const work=(async()=>{
            try {
                const result=await Api.getDecisionHourly();
                if(epoch!==this.epoch()) return;
                // Include request time conservatively, so a slow response cannot
                // extend an old recommendation beyond its applicable hour.
                this.result=result; this.clockBaseAt=requestedAt; this.resultEpoch=epoch; this.sourceKeyAtRequest=sourceKey;
            } catch(error) { if(epoch===this.epoch()) {this.result=null; this.error=error.message || '小时统计读取失败';} }
        })();
        this.inFlight=work;
        await work;
        this.inFlight=null;
        if(page.active() && this.enabled(page)) {
            page.renderGraph();
            if(page.hourMode()) { page.renderContext(); page.renderInspector(); }
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
        const gap=this.dataGaps(r).get(node.id);
        if(gap) pairs.unshift([gap.label,gap.reason]);
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
