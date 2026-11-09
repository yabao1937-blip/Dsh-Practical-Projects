/* Completed-hour final advice from the server, alongside real historical observations. */
const DensityTrendPage = {
    hours: 168, range: null, inited: false, seq: 0, meta: null,
    visible: {A304: true, actual: false, advice: true},
    num(v) { return Number.isFinite(v) ? v.toFixed(3) : '—'; },
    signed(v) {
        if (!Number.isFinite(v)) return '—';
        const rounded=Number(v.toFixed(3));
        return (rounded>0?'+':'') + rounded.toFixed(3);
    },
    esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    time(v, short = false) {
        if (!v) return '—';
        return new Date(v).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hourCycle:'h23',
            ...(short ? {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'} : {})});
    },
    reasonFor(point,parameters={}) {
        const codes=point?.reason_codes || [],ash=point?.ash || {};
        if(codes.includes('actual_unavailable')){
            const reading=point.observation?.actual_reading;
            return reading?.status==='invalid' && Number.isFinite(reading.raw_value)?`实测密度无效（${this.num(reading.raw_value)}），未生成建议`:'当时缺少有效实测密度，未生成建议';
        }
        if(codes.includes('operating_unknown')) return '当时运行状态未确认，未生成建议';
        if(codes.includes('reference_provenance')) return '缺少当时在线计算依据，未生成建议';
        if(codes.includes('reference_unavailable')){
            if(point.source?.context_reason) return point.source.context_reason;
            if(!Number.isFinite(ash.value)) return '有效灰分数据缺失，未生成建议';
            if(Number.isFinite(ash.coverage) && Number.isFinite(parameters.min_coverage) && ash.coverage<=parameters.min_coverage) return '有效灰分覆盖不足，未生成建议';
            return '本小时未形成有效计算结果';
        }
        return point?.reason || '未记录具体原因';
    },
    // 停机小时判据：301 入洗煤量读数为零、且该行是 held 沿用值。这段的总灰分、设定密度、实测密度、
    // 小时最终建议全部是延续值（图上原来靠红色沿用线补满），按现场要求整段从图中移除。
    stoppedHour(row) {
        const operating = row?.input_evidence?.operating;
        return !!operating && Number.isFinite(operating.raw_value) && operating.raw_value === 0;
    },
    stoppedSpans(rows) {
        const out = [];
        for (const row of [...(rows || [])].sort((a, b) => Date.parse(a.hour_end) - Date.parse(b.hour_end))) {
            if (!this.stoppedHour(row)) continue;
            const start = Date.parse(row.hour_start), end = Date.parse(row.hour_end);
            if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
            const last = out.at(-1);
            if (last && start - last.end <= 60000) last.end = Math.max(last.end, end);
            else out.push({start, end});
        }
        return out;
    },
    inStopped(t) {
        return (this.stoppedStretches || []).some(span => t >= span.start && t < span.end);
    },
    stoppedOnlyBetween(rows, from, to) {
        const inner = rows.filter(row => row.t > from && row.t < to);
        return inner.length > 0 && inner.every(row => this.stoppedHour(row));
    },
    // 压缩时间轴：每段有效小时按真实时长占宽，停机段不占宽度，段与段之间留一道固定接缝。
    axisBlocks() {
        const blocks = [];
        let current = null;
        for (const row of this.hourGrid || []) {
            if (this.stoppedHour(row)) { current = null; continue; }
            const end = row.t, start = end - 3600000;
            if (current && start - current.endT <= 1000) { current.endT = end; current.hours += 1; }
            else { current = {startT: start, endT: end, hours: 1}; blocks.push(current); }
        }
        const period = this.meta?.period;
        if (period && blocks.length) {
            const from = Date.parse(period.start), to = Date.parse(period.end);
            if (Number.isFinite(from) && blocks[0].startT - from <= 3600000) blocks[0].startT = Math.min(blocks[0].startT, from);
            if (Number.isFinite(to) && to - blocks.at(-1).endT < 5400000) blocks.at(-1).endT = Math.max(blocks.at(-1).endT, to);
        }
        return blocks;
    },
    layout(width) {
        const blocks = this.axisBlocks(), seam = 10, gaps = Math.max(0, blocks.length - 1) * seam;
        const total = blocks.reduce((sum, block) => sum + (block.endT - block.startT), 0) || 1;
        const usable = Math.max(1, width - gaps);
        let cursor = 0;
        return blocks.map(block => {
            const span = (block.endT - block.startT) / total * usable;
            const laid = {...block, x0: cursor, x1: cursor + span};
            cursor += span + seam;
            return laid;
        });
    },
    blockHit(t) {
        // 小时网格与灰分结算点都落在整点，直接按段区间吸附；停机区间内的点返回 null，由调用方整点丢弃。
        for (const [index, block] of (this.laid || []).entries()) {
            if (t >= block.startT && t <= block.endT) {
                const ratio = Math.max(0, Math.min(1, (t - block.startT) / Math.max(1, block.endT - block.startT)));
                return {x: block.x0 + ratio * (block.x1 - block.x0), block: index};
            }
        }
        return null;
    },
    blockOf(t) { const hit = this.blockHit(t); return hit ? hit.block : null; },
    xAt(t) { const hit = this.blockHit(t); return hit ? hit.x : null; },
    sameBlock(a, b) { const left = this.blockOf(a), right = this.blockOf(b); return left !== null && left === right; },
    tAt(px) {
        for (const block of this.laid || []) {
            if (px >= block.x0 - 10 && px <= block.x1 + 10) {
                const ratio = Math.max(0, Math.min(1, (px - block.x0) / Math.max(1e-6, block.x1 - block.x0)));
                return block.startT + ratio * (block.endT - block.startT);
            }
            if (px < block.x0) return null;
        }
        return null;
    },
    async ensureCarryContext(data,range,hours) {
        const fixed=data.source?.fixed_window || data.period,key=JSON.stringify([fixed.start,fixed.end]);
        if(this.fullWindowKey!==key){this.fullHourly=null;this.fullHourlyPromise=null;this.fullWindowKey=key;}
        this.carryFixed=fixed;this.carryParameters=data.parameters || this.carryParameters || {};
        if(!range && hours===168){this.fullHourly=data.hourly_suggestions;this.carryContextWarning='';return;}
        if(this.fullHourly) return;
        if(!this.fullHourlyPromise)this.fullHourlyPromise=Api.decisionEvidence('/density-series?limit=168').then(full=>{
            if(full.density_policy!=='density-candidate-v1' || !Array.isArray(full.hourly_suggestions)) throw new Error('full window unavailable');
            this.fullHourly=full.hourly_suggestions;this.carryParameters=full.parameters || this.carryParameters;this.carryContextWarning='';
        }).catch(()=>{this.fullHourlyPromise=null;this.carryContextWarning='窗口前有效值未取得，按七天初值1.490显示';});
        await this.fullHourlyPromise;
    },
    carryDisplay(records,fixed,period,localRecords,parameters={}) {
        const start=Date.parse(period.start),end=Date.parse(period.end),origin=Date.parse(fixed.start);
        const stops=this.stoppedSpans(records);this.stoppedStretches=stops;
        // Limit the entire causal path to the current query end BEFORE clipping its left edge.
        const rows=(records || []).filter(p=>p.complete===true && Date.parse(p.hour_end)<=end).map(p=>({...p,
            t:Date.parse(p.hour_end),valid:p.final_valid===true && Number.isFinite(p.value)})).sort((a,b)=>a.t-b.t);
        const segments=[],groups=[];let previous=null,lastValid=null,carry=1.490,missing=true,gapStart=origin,lastGroup=null,resumeFrom=null;
        const addGroup=(a,b,reason,value,point,synthetic=false)=>{
            if(b<=a)return;
            const prior=groups.at(-1);
            if(lastGroup===prior && prior && prior.end===a && prior.reason===reason && prior.value===value && !point?.hourly_break_before){prior.end=b;if(point)prior.records.push(point);lastGroup=prior;return;}
            lastGroup={start:a,end:b,reason,value,records:point?[point]:[],synthetic};groups.push(lastGroup);
        };
        for(const row of rows){
            // 停机小时整段移除：既不留红色沿用线，也不进"无建议时段与原因"列表。
            if(this.stoppedHour(row)){
                const stopStart=Date.parse(row.hour_start);
                if(lastGroup)lastGroup.end=Math.min(lastGroup.end,stopStart);
                lastGroup=null;resumeFrom=Math.max(resumeFrom ?? -Infinity,row.t);continue;
            }
            row.display_value=row.valid?row.value:carry;row.is_carried=!row.valid;row.carry_source_at=lastValid?.hour_end || null;
            const seam=!!previous && row.t-previous.t>3600000 && this.stoppedOnlyBetween(rows,previous.t,row.t);
            if(row.valid){
                if(!missing && previous && !seam && (row.t-previous.t!==3600000 || row.hourly_break_before)){
                    missing=true;gapStart=Math.max(previous.t,resumeFrom ?? previous.t);lastGroup=null;addGroup(gapStart,row.t,'未返回该时段的小时最终记录，具体原因未记录',carry,null,true);
                }
                if(missing){
                    const from=Math.max(gapStart,resumeFrom ?? gapStart);
                    if(!lastGroup)addGroup(from,row.t,'该时段尚无已完成小时结果',carry,null,true);
                    else lastGroup.end=Math.max(lastGroup.end,row.t);
                    if(row.t>from)segments.push({kind:'carry',start:from,end:row.t,from:carry,to:carry,restore:true});
                    segments.push({kind:'restore',start:row.t,end:row.t,from:carry,to:row.value});
                }else if(previous&&!seam)segments.push({kind:'valid',start:previous.t,end:row.t,from:previous.value,to:row.value});
                carry=row.value;lastValid=row;missing=false;lastGroup=null;
            }else{
                if(!missing){missing=true;gapStart=Math.max(previous?.t ?? origin,resumeFrom ?? -Infinity);lastGroup=null;}
                addGroup(Math.max(previous?.t ?? origin,resumeFrom ?? -Infinity),row.t,this.reasonFor(row,parameters),carry,row);
            }
            previous=row;
        }
        if(missing){
            // 尾部沿用线止于停机段起点，起点也不早于最近一段停机的结束。
            const from=Math.max(gapStart,resumeFrom ?? gapStart);
            const tailStop=stops.reduce((acc,span)=>span.start>from&&span.start<=end&&(acc===null||span.start<acc)?span.start:acc,null);
            const tailEnd=tailStop===null?end:Math.min(end,tailStop);
            if(tailEnd>from && lastGroup)lastGroup.end=Math.max(lastGroup.end,tailEnd);
            else if(tailEnd>from)addGroup(from,tailEnd,'该时段尚无已完成小时结果',carry,null,true);
            if(tailEnd>from)segments.push({kind:'carry',start:from,end:tailEnd,from:carry,to:carry,restore:false});
        }
        if(!(localRecords || []).length && !missing){
            segments.push({kind:'carry',start,end,from:carry,to:carry,restore:false});
            addGroup(start,end,'该时段尚无已完成小时结果',carry,null,true);
        }
        const clipped=segments.filter(s=>s.end>=start && s.start<=end).map(s=>{
            if(s.kind==='restore')return {...s};
            const a=Math.max(start,s.start),b=Math.min(end,s.end),ratio=t=>s.end===s.start?0:(t-s.start)/(s.end-s.start);
            return {...s,start:a,end:b,from:s.from+(s.to-s.from)*ratio(a),to:s.from+(s.to-s.from)*ratio(b)};
        }).filter(s=>s.kind==='restore' || s.end>s.start);
        const visibleGroups=groups.filter(g=>g.end>start && g.start<end).map(g=>({...g,start:Math.max(g.start,start),end:Math.min(g.end,end)}));
        return {rows,segments:clipped,groups:visibleGroups,lastValue:carry,lastValidAt:lastValid?.hour_end || null};
    },
    points(data) {
        const shape=(rows,key)=>(rows || []).map(p=>({...p,key,t:Date.parse(p.at),value:Number.isFinite(p.value)?p.value:null})).filter(p=>Number.isFinite(p.t));
        const local=(data.hourly_suggestions || []).filter(p=>p.complete===true && Date.parse(p.hour_end)<=Date.parse(data.period.end));
        // 有效小时网格（停机段已排除），横轴按它压缩排布。
        this.hourGrid=(this.fullHourly || local || []).filter(p=>p.complete===true && Date.parse(p.hour_end)<=Date.parse(data.period.end) && Date.parse(p.hour_end)>Date.parse(data.period.start))
            .map(p=>({...p,t:Date.parse(p.hour_end)})).sort((a,b)=>a.t-b.t);
        const carry=this.carryDisplay(this.fullHourly || local,this.carryFixed || data.source?.fixed_window || data.period,data.period,local,this.carryParameters || data.parameters || {});
        this.stoppedStretches=this.stoppedSpans(this.hourGrid);
        const byTime=new Map(carry.rows.map(p=>[p.t,p]));
        const advice=local.map(p=>{const endT=Date.parse(p.hour_end),t=Date.parse(p.hour_start),shown=byTime.get(endT);return {...p,key:'advice',at:p.hour_end,t,
            value:p.final_valid===true && Number.isFinite(p.value)?p.value:null,display_value:shown?.display_value ?? 1.490,
            is_carried:!(p.final_valid===true && Number.isFinite(p.value)),carry_source_at:shown?.carry_source_at || null,break_before:p.hourly_break_before===true};});
        return {A304:shape(data.channels?.A304,'A304'),actual:shape(data.channels?.actual,'actual').map(p=>({...p,value:p.value>1 && p.value<3 && !['invalid','bad','stale','future'].includes(p.status)?p.value:null})),advice,ash:shape(data.ash,'ash'),carry};
    },
    connected(a, b) {
        if (!a || !b || a.key !== b.key || !Number.isFinite(a.value) || !Number.isFinite(b.value)) return false;
        // 设定密度是阀门保持值：删掉停机段的采样后仍连成一条线，跨段处按"保持上一个设定"画水平再接台阶。
        // 实测密度是测量值，停机段没有测量，仍在接缝处断开；建议线由下面的整点规则断开。
        if ((this.laid || []).length && !this.sameBlock(a.t, b.t)) return a.key === 'A304' && b.t > a.t;
        if (b.break_before) return false;
        if(b.key==='advice') return b.t-a.t===3600000;
        return b.t>=a.t;
    },
    init() {
        if (this.inited) return;
        this.inited = true;
        const byId = id => document.getElementById('density-trend-' + id);
        document.getElementById('page-density-trend').classList.add('is-light');
        byId('theme').onclick = () => {
            const light = document.getElementById('page-density-trend').classList.toggle('is-light');
            byId('theme').textContent = light ? '深色视图' : '浅色视图'; this.draw();
        };
        document.querySelectorAll('[data-t-range]').forEach(b => b.onclick = () => {
            this.hours = Number(b.dataset.tRange); this.range = null;
            byId('custom-fields').hidden=true;byId('custom').setAttribute('aria-expanded','false');this.refresh();
        });
        byId('custom').onclick = () => {
            const fields = byId('custom-fields'); fields.hidden = !fields.hidden;
            byId('custom').setAttribute('aria-expanded', String(!fields.hidden));
            if (!byId('end').value && this.availablePeriod) {
                const local=value=>new Date(Date.parse(value)+8*3600000).toISOString().slice(0,16);
                byId('start').value=local(this.availablePeriod.start);byId('end').value=local(this.availablePeriod.end);
            }
        };
        byId('query').onclick = () => {
            const start = byId('start').value, end = byId('end').value;
            if (!start || !end || end <= start) { this.status('请选择有效的起止时间，结束须晚于开始。'); return; }
            const range={start:start+':00+08:00',end:end+':00+08:00'};
            if(this.availablePeriod && (Date.parse(range.start)<Date.parse(this.availablePeriod.start) || Date.parse(range.end)>Date.parse(this.availablePeriod.end))){this.status('自选时间须在已重算的固定7天窗口内，近30天尚未提供。');return;}
            this.range=range;this.refresh();
        };
        byId('refresh').onclick = () => this.refresh();
        document.querySelectorAll('[data-density-series]').forEach(b => b.onclick = () => {
            const key = b.dataset.densitySeries; this.visible[key] = !this.visible[key];
            b.setAttribute('aria-pressed', String(this.visible[key]));
            if(key==='actual') b.innerHTML='<i></i>'+(this.visible[key]?'隐藏实测':'显示实测');
            this.draw();
        });
        this.observer = new ResizeObserver(() => this.draw()); this.observer.observe(byId('stack'));
        this.themeObserver = new MutationObserver(() => this.draw());
        this.themeObserver.observe(document.documentElement, {attributes:true, attributeFilter:['data-theme','class']});
        this.themeObserver.observe(document.body, {attributes:true, attributeFilter:['data-theme','class']});
        document.addEventListener('pointerdown',event=>{
            document.querySelectorAll('#page-density-trend .density-trend-plot canvas').forEach(canvas=>{
                if(canvas!==event.target){canvas._tipPinned=false;const tip=canvas.parentElement.querySelector('.density-trend-tooltip');if(tip)tip.hidden=true;}
            });
        });
        document.addEventListener('visibilitychange', () => { if (!document.hidden && this.active()) this.refresh(); });
        this.timer = setInterval(() => {
            if (!document.hidden && this.active() && !this.range && !this.loading) this.refresh({keepPage:true});
        }, 3600000);
        this.refresh();
    },
    active() { return document.getElementById('page-density-trend')?.classList.contains('active'); },
    status(message) { document.getElementById('density-trend-status').textContent = message; },
    async refresh({keepPage = false} = {}) {
        const seq = ++this.seq, range = this.range, hours = this.hours;
        this.loading = true;
        this.status('正在读取所选时段…');
        document.getElementById('density-candidate-panel').setAttribute('aria-busy','true');
        document.getElementById('density-trend-stack').setAttribute('aria-busy', 'true');
        try {
            const query = range ? 'start='+encodeURIComponent(range.start)+'&end='+encodeURIComponent(range.end) : 'limit='+hours;
            const data = await Api.decisionEvidence('/density-series?' + query);
            if(data.density_policy!=='density-candidate-v1' || !Array.isArray(data.hourly_suggestions) || !data.channels) throw new Error('新版数据库时序格式未确认，暂不显示曲线。');
            if (seq !== this.seq) return;
            await this.ensureCarryContext(data,range,hours);
            if(seq!==this.seq)return;
            this.meta = data; this.series = this.points(data);
            this.renderSource(data);
            document.querySelectorAll('[data-t-range]').forEach(b => {
                const chosen = !range && Number(b.dataset.tRange) === hours;
                b.classList.toggle('btn-primary', chosen); b.setAttribute('aria-pressed', String(chosen));
            });
            document.getElementById('density-trend-custom').classList.toggle('btn-primary', !!range);
            document.getElementById('density-trend-caption').textContent =
                `${range ? '自选时间' : hours === 168 ? '近7天' : '近30天'} · ${this.time(data.period.start, true)} — ${this.time(data.period.end, true)}`;
            this.status(''); this.renderHourlyCard(data);this.renderMissing(); this.draw();

        } catch (error) {
            if (seq !== this.seq) return;
            this.meta = null; this.series = null; this.draw();

            document.getElementById('density-candidate-current').textContent='未取得小时最终建议，请刷新重试。';
            document.getElementById('density-trend-missing-body').textContent='原因未能读取，请刷新重试。';
            document.getElementById('density-trend-missing-summary').textContent='无建议时段与原因（未读取）';
            document.getElementById('density-trend-caption').textContent = '所选时段读取失败';
            this.status(error.message || '读取失败，请稍后刷新。');
        } finally {
            if (seq === this.seq) {
                this.loading = false;
                document.getElementById('density-trend-stack').setAttribute('aria-busy', 'false');
                document.getElementById('density-candidate-panel').setAttribute('aria-busy','false');
            }
        }
    },
    renderSource(data) {
        const source=data.source || {},fixed=source.fixed_window || data.period;
        this.availablePeriod=fixed;
        const local=value=>new Date(Date.parse(value)+8*3600000).toISOString().slice(0,16);
        for(const id of ['start','end']){const input=document.getElementById('density-trend-'+id);input.min=local(fixed.start);input.max=local(fixed.end);}
        const thirty=document.querySelector('[data-t-range="720"]');
        if(thirty){thirty.disabled=true;thirty.title='当前仅提供已重算的固定7天窗口，近30天尚未重算';}
        const stops=this.stoppedStretches || [], stoppedHours=Math.round(stops.reduce((sum,span)=>sum+(span.end-span.start),0)/3600000);
        const stopNote=stoppedHours?` 停机 ${stops.length} 段共 ${stoppedHours} 小时（301 入洗煤量为零）已从图中移除，横轴按有效小时压缩。`:'';
        document.getElementById('density-trend-source').textContent='每小时一条最终建议。灰分与建议都按各自描述的那一小时起点落位；每条建议用的是两小时前那一格灰分（悬停可看依据）。没有建议的小时不画点、不占位，前后建议直接连成一条线；缺失原因见下方列表。'+stopNote+(this.carryContextWarning?' '+this.carryContextWarning:'');
    },
    renderHourlyCard(data) {
        const latest=data.latest_completed_hour,point=latest?.complete===true && Date.parse(latest.hour_end)<=Date.parse(data.period.end)?latest:null,host=document.getElementById('density-candidate-current');
        document.getElementById('density-candidate-title').textContent=point?'最近已完成小时':'所选时段尚无新小时建议';
        const valid=point?.complete===true && Date.parse(point.hour_end)<=Date.parse(data.period.end) && point.final_valid===true && Number.isFinite(point.value);
        const row=point?this.series?.advice.find(p=>p.t===Date.parse(point.hour_start)):null;
        const value=valid?point.value:row?.display_value ?? this.series?.carry.lastValue ?? 1.490;
        const reason=valid?'':point?this.reasonFor(point,this.carryParameters):'该时段尚无已完成小时结果';
        host.innerHTML=`<div class="density-hourly-final-value${valid?'':' is-carried'}"><p class="density-candidate-number">${this.num(value)} <span>g/cm³${valid?'':' · 沿用（显示）'}</span></p><p class="density-hourly-final-period">${this.esc(this.time(point?.hour_start || data.period.start))} — ${this.esc(this.time(point?.hour_end || data.period.end))}</p><p class="density-trend-note">${valid?'小时最终建议 · 新版历史重算':'无新建议；'+this.esc(reason)}</p></div>`;
    },
    renderMissing() {
        const groups=this.series?.carry.groups || [];
        document.getElementById('density-trend-missing-summary').textContent=`无建议时段与原因 · ${groups.length} 段`;
        document.getElementById('density-trend-missing-body').innerHTML=groups.length?`<ul class="density-missing-list">${groups.map(g=>`<li><div><strong>${this.esc(this.time(g.start))} — ${this.esc(this.time(g.end))}</strong><p>${this.esc(g.reason)}</p></div><span>无建议 ${Math.round((g.end-g.start)/3600000)} 小时</span></li>`).join('')}</ul>`:'<p class="density-trend-note">所选时段没有无建议区间。</p>';
    },
    carryInfoAt(t) {
        const carry=this.series?.carry;if(!carry)return null;
        const segment=carry.segments.find(s=>s.kind==='carry' && t>=s.start && (t<s.end || !s.restore && t<=s.end));
        if(!segment)return null;
        const group=carry.groups.find(g=>t>=g.start && (t<g.end || !segment.restore && t<=g.end));
        return {value:segment.from,start:group?.start ?? segment.start,end:group?.end ?? segment.end,reason:group?.reason || '该时段尚无新的有效小时建议'};
    },
    carryText(info) { return `无新建议 · 红线沿用 ${this.num(info.value)} g/cm³（显示）\n${this.time(info.start)} — ${this.time(info.end)}\n原因：${info.reason}`; },
    palette() {
        const css = getComputedStyle(document.getElementById('page-density-trend'));
        const get = (v, fallback) => css.getPropertyValue(v).trim() || fallback;
        return {A304:get('--trend-a304','#25897c'),actual:get('--trend-actual','#417dc3'),
            advice:get('--trend-advice','#ce8428'), muted:get('--text-secondary','#68758a'), grid:get('--border-color','#dce1eb'),
            background:get('--bg-card','#fff'),
            missingAdvice:get('--trend-advice-missing','#d94f4f'), ash:get('--trend-ash','#8a7ae0')};
    },
    detail(p) {
        // 无建议的小时已经不画点、不画红线了；这条分支只在极端情况下兜底，别再说"红线沿用"。
        if(p.key==='advice' && p.is_carried) return `该小时未生成建议\n建议小时 ${this.time(p.hour_start)} — ${this.time(p.hour_end)}\n原因：${this.reasonFor(p,this.carryParameters)}`;
        if(p.key==='advice') {
            // 建议依据的灰分就是它自己那一行里的 ash 窗口，直接写出来，省得读图时去数格子。
            const basis=p.ash&&Number.isFinite(p.ash.value)
                ? `\n依据灰分 ${this.time(p.ash.start,true)} — ${this.time(p.ash.end,true)} = ${p.ash.value.toFixed(2)}%${p.ash.provisional?'（暂估）':''}`
                : '\n依据灰分未记录';
            return `小时最终建议 ${Number.isFinite(p.value)?this.num(p.value)+' g/cm³':'暂无有效结果'}\n建议小时 ${this.time(p.hour_start)} — ${this.time(p.hour_end)}${basis}`;
        }
        return `A304 ${p.key==='actual'?'实测密度':'实际设定'} ${this.num(p.value)} g/cm³\n观测时刻 ${this.time(p.at)}`;
    },
    ashDetail(p) {
        // 灰分点按小时起点落位，接口不返回 end；没有 end 时补 +1 小时，别在提示里留"——"。
        const start = p.start || p.at, end = p.end || (start ? new Date(Date.parse(start) + 3600000).toISOString() : null);
        return `实际灰分 ${Number.isFinite(p.value)?p.value.toFixed(2):'—'} %\n生产时段 ${this.time(start,true)} — ${this.time(end,true)}`;
    },
    crosshair: null,
    crosshairAt(hit) {
        this.crosshair = hit ? {t: hit.t, x: hit.x} : null;
        this.drawCrosshair();
    },
    hideCrosshair() { this.crosshair = null; this.drawCrosshair(); },
    stepValueAt(points, t, hold) {
        // Latest recorded observation at/before the cursor; never a future reading.
        let current = null;
        for (const p of points) {
            if (p.value === null) continue;
            if (p.t <= t) current = p; else break;
        }
        return current && t - current.t <= hold ? current : null;
    },
    nearestValueAt(points, t, window) {
        let best = null;
        for (const p of points) {
            if (p.value === null) continue;
            const d = Math.abs(p.t - t);
            if (d <= window && (!best || d < Math.abs(best.t - t))) best = p;
        }
        return best;
    },
    chip(ctx, x, y, lines, colors, w, h, paneTop = 0) {
        ctx.font = 'bold 12px "Microsoft YaHei", sans-serif';
        const text = lines.map((l, i) => ({l, c: colors[i]}));
        const width = Math.max(...text.map(t => ctx.measureText(t.l).width)) + 16;
        const height = lines.length * 17 + 8;
        const preferredLeft = x + width > w - 30 ? x - width - 10 : x + 10;
        const left = Math.max(4, Math.min(preferredLeft, w - width - 4));
        const top = Math.max(paneTop + 4, Math.min(y, h - height - 4));
        ctx.fillStyle = this.palette().background;
        ctx.strokeStyle = this.palette().muted; ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.roundRect ? ctx.roundRect(left, top, width, height, 3) : ctx.rect(left, top, width, height);
        ctx.fill(); ctx.stroke();
        ctx.textAlign = 'left';
        text.forEach((t, i) => { ctx.fillStyle = t.c; ctx.fillText(t.l, left + 8, top + 17 + i * 17); });
    },
    drawCrosshair() {
        const stack = document.querySelector('.density-trend-stack'), overlay = document.getElementById('density-trend-crosshair');
        if (!stack || !overlay) return;
        const w = stack.clientWidth, h = stack.clientHeight;
        const ratio = Math.min(devicePixelRatio || 1, 2);
        overlay.width = Math.max(1, w*ratio); overlay.height = Math.max(1, h*ratio);
        const ctx = overlay.getContext('2d');
        ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
        const state = this.crosshair;
        if (!state || !this.series || !this.meta) return;
        const colors = this.palette();
        const start = Date.parse(this.meta.period.start), end = Date.parse(this.meta.period.end);
        // Recompute from time after resizing instead of reusing an old pixel x.
        const offset = this.xAt(state.t);
        if (offset === null) { this.crosshair = null; return; }
        const x = 78 + offset;
        state.x = x;
        // One dashed vertical guide through all three panes at the snapped instant.
        ctx.strokeStyle = colors.muted; ctx.setLineDash([5, 4]); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(x, 12); ctx.lineTo(x, h - 52); ctx.stroke();
        ctx.setLineDash([]);
        const ash=this.nearestValueAt(this.series.ash || [],state.t,45*60000);
        const setpoint=this.stepValueAt(this.series.A304 || [],state.t,5*60000);
        const actual=this.stepValueAt(this.series.actual || [],state.t,5*60000);
        const advice=(this.series.advice || []).reduce((best,p)=>Math.abs(p.t-state.t)<=30*60000 && (!best || Math.abs(p.t-state.t)<Math.abs(best.t-state.t))?p:best,null);
        const ashName='实际灰分',carried=this.carryInfoAt(state.t);
        const panes=[
            ['ash-plot',ash?`${ashName} ${ash.value.toFixed(2)}%${ash.provisional?'（暂估）':''}`:`${ashName} —`,colors.ash],
            ['plot',`设定 ${this.num(setpoint?.value)} · 实测 ${this.num(actual?.value)}`,colors.A304],
            ['advice-plot',advice?`小时最终建议 ${this.num(advice.value)}`:'小时最终建议 —',advice?colors.advice:colors.muted]
        ];
        for (const [id, text, color] of panes) {
            const pane = document.getElementById('density-trend-' + id), top = pane.offsetTop;
            this.chip(ctx, x, top + 8, [text], [color], w, top + pane.clientHeight, top);
        }
        this.chip(ctx, x, h - 56, [this.time(state.t, true)], [colors.muted], w, h);
    },
    axes(ctx, w, h, labels, timeAxis = false) {
        const left = 78, right = 44, top = 12, bottom = timeAxis ? 52 : 4;
        const width = w-left-right, height = h-top-bottom;
        const min = Number(labels[0]), max = Number(labels.at(-1));
        this.laid = this.layout(width);
        const x = t => { const px = this.xAt(t); return px === null ? null : left + px; }, y = v => top+(max-v)/(max-min)*height;
        const colors = this.palette();
        ctx.font = '13px "Microsoft YaHei", sans-serif'; ctx.textAlign = 'right'; ctx.lineWidth = 1;
        for (const label of labels) {
            const py = y(Number(label)); ctx.strokeStyle = colors.grid; ctx.setLineDash([3,5]);
            ctx.beginPath(); ctx.moveTo(left,py); ctx.lineTo(w-right,py); ctx.stroke();
            ctx.fillStyle = colors.muted; ctx.fillText(label,left-10,py+4);
        }
        ctx.setLineDash([]);
        if (timeAxis && this.meta && this.laid.length) {
            // 刻度只标有效小时，密度按压缩后的占宽重算，停机段不留刻度也不占宽度。
            const hours = this.laid.reduce((sum,block)=>sum+(block.endT-block.startT)/3600000,0) || 1;
            const stride = Math.max(1, Math.ceil(112 / Math.max(1, width / hours)));
            let index = 0, lastX = -Infinity;
            const tickAt = (px, t, align, minGap = 64) => {
                if (px - lastX < minGap) return;
                lastX = px;
                ctx.textAlign = align;
                const label = this.time(t, true);
                if (width < 310) {
                    const [date, time] = label.split(' ');
                    ctx.fillText(date, px, h - 30); ctx.fillText(time || '', px, h - 12);
                } else ctx.fillText(label, px, h - 20);
            };
            for (const block of this.laid) {
                const count = Math.round((block.endT - block.startT) / 3600000);
                for (let i = 0; i < count; i++) {
                    if (index++ % stride) continue;
                    const px = left + block.x0 + i / count * (block.x1 - block.x0);
                    tickAt(px, block.startT + i * 3600000, 'center');
                }
            }
            const last = this.laid.at(-1);
            tickAt(left + last.x1, last.endT, 'right', 120);
        }
        return {x, y, left, top, width, height, w, h};
    },
    bindCanvas(canvas, plot, frame) {
        const {left, top, width, height, w, h} = frame;
        const tip = plot.querySelector('.density-trend-tooltip'); tip.hidden = true;canvas._tipPinned=false;
        const show = hit => {
            tip.textContent=hit.text || (hit.key==='ash'?this.ashDetail(hit):this.detail(hit));tip.hidden=false;
            const rect=plot.getBoundingClientRect(),main=plot.closest('main')?.getBoundingClientRect();
            const topLimit=Math.max(8,main?.top+8 || 8),bottomLimit=Math.min(innerHeight-8,main?.bottom-8 || innerHeight-8);
            tip.style.maxHeight=Math.max(80,bottomLimit-topLimit)+'px';
            tip.style.left=Math.max(4,Math.min(hit.x+14,w-tip.offsetWidth-4,innerWidth-rect.left-tip.offsetWidth-8))+'px';
            tip.style.top=Math.max(topLimit-rect.top,Math.min(hit.y+12,bottomLimit-rect.top-tip.offsetHeight))+'px';
        };
        const point = event => {
            const r=canvas.getBoundingClientRect(),px=event.clientX-r.left,py=event.clientY-r.top;
            let best=null,distance=Infinity;
            if(canvas.id==='density-trend-advice-canvas' && px>=left && px<=left+width){
                // 没有建议密度的时段已整点移除，图上不再有沿用线可悬停；接缝处直接落空。
                const t=this.tAt(px-left);
                if(t===null){canvas._tipPinned=false;tip.hidden=true;this.hideCrosshair();return;}
            }
            if (px>=left && px<=left+width && py>=top && py<=top+height) {
                for (const hit of canvas._hits) {
                    // Clipped points remain inspectable by time; their stored
                    // value and y stay truthful, never snapped to the axis edge.
                    const outside = hit.y<top || hit.y>top+height;
                    const d=outside?Math.abs(hit.x-px):Math.hypot(hit.x-px,hit.y-py);
                    if(d<distance){best=hit;distance=d;}
                }
            }
            if(best&&distance<32){show(best);this.crosshairAt(best);}else{canvas._tipPinned=false;tip.hidden=true;this.hideCrosshair();}
        };
        canvas.onmousemove=event=>{if(!canvas._tipPinned)point(event);};
        canvas.onclick=event=>{canvas._tipPinned=true;point(event);};
        canvas.onmouseleave=()=>{if(!canvas._tipPinned){tip.hidden=true;this.hideCrosshair();}};
        canvas.onkeydown=event=>{
            const hits=canvas._hits;
            if(!['Home','End','ArrowLeft','ArrowRight','Escape'].includes(event.key)||!hits.length)return;
            event.preventDefault();if(event.key==='Escape'){canvas._tipPinned=false;tip.hidden=true;this.hideCrosshair();return;}
            canvas._index=event.key==='Home'?0:event.key==='End'?hits.length-1:
                Math.max(0,Math.min(hits.length-1,(canvas._index??-1)+(event.key==='ArrowRight'?1:-1)));
            canvas._tipPinned=true;show(hits[canvas._index]);this.crosshairAt(hits[canvas._index]);
        };
    },
    drawAsh() {
        const canvas = document.getElementById('density-trend-ash-canvas'), plot = document.getElementById('density-trend-ash-plot');
        if (!canvas || !plot) return;
        const w = plot.clientWidth, h = plot.clientHeight, ratio = Math.min(devicePixelRatio || 1, 2);
        canvas.width = Math.max(1, w*ratio); canvas.height = Math.max(1, h*ratio);
        const ctx = canvas.getContext('2d'); ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,w,h);
        canvas._hits = []; canvas._index = 0;
        if (w < 160) return;
        const frame = this.axes(ctx,w,h,['7.5','7.75','8.0','8.25','8.5','8.75','9.0','9.25','9.5','9.75','10.0','10.25','10.5','10.75','11.0']);
        this.bindCanvas(canvas,plot,frame);
        const colors = this.palette(), points = this.series?.ash || [];
        if (!points.length || !this.meta) {
            ctx.fillStyle = colors.muted; ctx.textAlign = 'center';
            ctx.fillText(!this.meta ? '等待读取趋势数据' : '该时段暂无结算总灰分', w/2, h/2);
            return;
        }
        const {x,y,left,top,width,height} = frame;
        ctx.save(); ctx.beginPath(); ctx.rect(left,top,width,height); ctx.clip();
        ctx.strokeStyle=colors.ash; ctx.fillStyle=colors.ash; ctx.lineWidth=2;
        let link=null;
        for (const p of points) {
            // 没有总灰分的时刻直接跳过：不画点，也不打断曲线 —— 前后有值的时刻直接连成一条线，
            // 包括跨过停机接缝和缺灰分的小时。
            if (p.value===null || this.inStopped(p.t-60000)) continue;
            const px=x(p.t);
            if (px===null) continue;
            if (link) {
                ctx.beginPath();ctx.moveTo(x(link.t),y(link.value));ctx.lineTo(px,y(p.value));ctx.stroke();
            }
            link=p;
        }
        for (const p of points) {
            if(p.value===null) continue;
            // 灰分点描述的是它前面那个生产小时；该小时停机就整点丢弃。
            if(this.inStopped(p.t-60000)) continue;
            const px=x(p.t);if(px===null) continue;
            const py=y(p.value);
            ctx.beginPath();ctx.arc(px,py,2.2,0,2*Math.PI);
            if (p.provisional) ctx.stroke();
            else ctx.fill();
            canvas._hits.push({...p,x:px,y:py,key:'ash'});
        }
        ctx.restore();
        canvas._hits.sort((a,b)=>a.t-b.t);
    },
    densityLabels() {
        // Both density panes share a scale covering all returned valid values.
        // Hidden observations remain available; showing them expands both density axes.
        const values = ['A304','actual','advice'].filter(key=>this.visible[key]).flatMap(key=>key==='advice'?[...(this.series?.advice || []).map(p=>p.value),...(this.series?.carry.segments || []).flatMap(s=>[s.from,s.to])]:(this.series?.[key] || []).map(p=>p.value)).filter(Number.isFinite);
        if (!values.length) return ['1.44','1.46','1.48','1.50','1.52','1.54','1.56'];
        const min = values.reduce((a,b)=>Math.min(a,b),Infinity), max = values.reduce((a,b)=>Math.max(a,b),-Infinity);
        const margin = Math.max(.005, (max-min)*.08);
        const rawStep = (max-min+2*margin)/7;
        const power = 10**Math.floor(Math.log10(rawStep));
        const step = [1,2,2.5,5,10].map(value => value*power).find(value => value >= rawStep) || power*10;
        const low = Math.floor((min-margin)/step)*step, high = Math.ceil((max+margin)/step)*step;
        const count = Math.round((high-low)/step);
        return Array.from({length:count+1}, (_,index) => (low+index*step).toFixed(3));
    },
    drawCarryAdvice(ctx,frame,canvas,colors,points) {
        const {x,y,width}=frame;
        // 没有建议密度的时刻整点丢弃：不画沿用红线、不留点；前后有真实建议的小时直接连成一条线，
        // 跨缺灰分小时和跨停机接缝都连（与总灰分同一口径）。
        const drawn=points.filter(p=>p.value!==null && x(p.t)!==null && !this.stoppedHour(p));
        ctx.strokeStyle=colors.advice;ctx.lineWidth=1.8;ctx.setLineDash([]);
        for(let i=1;i<drawn.length;i++){
            ctx.beginPath();ctx.moveTo(x(drawn[i-1].t),y(drawn[i-1].value));ctx.lineTo(x(drawn[i].t),y(drawn[i].value));ctx.stroke();
        }
        const stride=Math.max(1,Math.ceil(drawn.length/Math.max(1,width/7)));
        for(const [index,p] of drawn.entries()){
            const px=x(p.t),py=y(p.value);
            if(index%stride===0 || index===drawn.length-1){ctx.fillStyle=colors.advice;ctx.beginPath();ctx.arc(px,py,2,0,2*Math.PI);ctx.fill();}
            canvas._hits.push({...p,x:px,y:py,key:'advice'});
        }
    },
    drawDensity(key) {
        const suffix=key==='A304'?'':'advice-',canvas=document.getElementById('density-trend-'+suffix+'canvas'),plot=document.getElementById('density-trend-'+suffix+'plot');
        if(!canvas || !plot) return;
        const w=plot.clientWidth,h=plot.clientHeight,ratio=Math.min(devicePixelRatio || 1,2);
        canvas.width=Math.max(1,w*ratio);canvas.height=Math.max(1,h*ratio);
        const ctx=canvas.getContext('2d');ctx.setTransform(ratio,0,0,ratio,0,0);ctx.clearRect(0,0,w,h);canvas._hits=[];canvas._index=0;
        if(w<160) return;
        const frame=this.axes(ctx,w,h,this.densityLabels(),key==='advice');this.bindCanvas(canvas,plot,frame);
        const colors=this.palette(),keys=key==='A304'?['A304','actual']:['advice'];
        if(!this.meta){ctx.fillStyle=colors.muted;ctx.textAlign='center';ctx.fillText('等待读取小时最终建议',w/2,h/2);return;}
        const {x,y,left,top,width,height}=frame;
        ctx.save();ctx.beginPath();
        const markerPad=key==='advice'?3:0;
        ctx.rect(left-markerPad,top,width+2*markerPad,height);ctx.clip();
        for(const kind of keys){
            if(!this.visible[kind]) continue;
            const points=this.series?.[kind] || [];
            if(kind==='advice'){this.drawCarryAdvice(ctx,frame,canvas,colors,points);continue;}
            let previous=null;
            const validCount=points.filter(row=>row.value!==null).length;
            const stride=kind==='advice'?Math.max(1,Math.ceil(validCount/Math.max(1,width/7))):1;
            for(const [index,p] of points.entries()){
                const px=x(p.t);
                // 停机段里的采样先跳过：既不画点，也不推进 previous（设定密度线在接缝处保持上一个设定）。
                // 这一步必须在画线之前做，否则 null 坐标会被 canvas 当成 0，把线画回最左边。
                if(px===null) continue;
                if(p.value!==null && this.connected(previous,p)){
                    ctx.strokeStyle=colors[kind];ctx.lineWidth=kind==='advice'?1.8:kind==='actual'?1.4:2;
                    ctx.setLineDash(kind==='actual'?[3,3]:[]);
                    ctx.beginPath();ctx.moveTo(x(previous.t),y(previous.value));
                    if(kind==='A304')ctx.lineTo(px,y(previous.value));
                    ctx.lineTo(px,y(p.value));ctx.stroke();
                }
                const py=p.value===null?top+height-4:y(p.value);
                if(p.value!==null){
                    const marker=kind==='advice'?(index%stride===0 || !this.connected(previous,p)):
                        !previous || previous.value===null || Math.abs(p.value-previous.value)>=.0005;
                    if(marker){ctx.fillStyle=colors[kind];ctx.beginPath();ctx.arc(px,py,kind==='advice'?2:1.8,0,2*Math.PI);ctx.fill();}
                }
                canvas._hits.push({...p,x:px,y:py,key:kind});previous=p;
            }
        }
        ctx.restore();canvas._hits.sort((a,b)=>a.t-b.t || a.key.localeCompare(b.key));
    },
    draw() {
        this.drawAsh();
        this.drawDensity('A304');
        this.drawDensity('advice');
        this.drawCrosshair();
    },
};
