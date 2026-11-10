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
    retainedBlocks(start, end, exclusions) {
        const blocks = [];
        let cursor = start;
        for (const span of exclusions) {
            if (span.start > cursor) blocks.push({startT:cursor, endT:span.start});
            cursor = Math.max(cursor, span.end);
        }
        if (end > cursor) blocks.push({startT:cursor, endT:end});
        return blocks;
    },
    layout(width) {
        const blocks = this.series?.blocks || [];
        if (!blocks.length) return [];
        const gaps = blocks.length - 1;
        const seam = gaps ? Math.min(10, width * .12 / gaps) : 0;
        const total = blocks.reduce((sum, block) => sum + block.endT - block.startT, 0);
        const usable = width - gaps * seam;
        let cursor = 0;
        return blocks.map((block, i) => {
            const span = (block.endT - block.startT) / total * usable;
            const laid = {...block, x0:cursor, x1:i === blocks.length-1 ? width : cursor+span};
            cursor += span + seam;
            return laid;
        });
    },
    blockHit(t) {
        for (const [index, block] of (this.laid || []).entries()) {
            if (t >= block.startT && t <= block.endT) {
                const ratio = (t - block.startT) / (block.endT - block.startT);
                return {x: block.x0 + ratio * (block.x1 - block.x0), block: index};
            }
        }
        return null;
    },
    blockOf(t) { return this.blockHit(t)?.block ?? null; },
    xAt(t) { return this.blockHit(t)?.x ?? null; },
    sameBlock(a, b) { const left = this.blockOf(a); return left !== null && left === this.blockOf(b); },
    tAt(px) {
        for (const block of this.laid || []) {
            if (px < block.x0) return null;
            if (px <= block.x1) return block.startT + (px-block.x0)/(block.x1-block.x0)*(block.endT-block.startT);
        }
        return null;
    },
    points(data) {
        const start = Date.parse(data.period.start), end = Date.parse(data.period.end);
        const exclusions = (data.trend_exclusions || []).map(p=>({...p,start:Date.parse(p.start),end:Date.parse(p.end)}));
        this.stoppedStretches = exclusions.filter(p=>p.reason==='stopped');
        const blocks = data.summary?.total_samples === 0 && !exclusions.length
            ? [] : this.retainedBlocks(start,end,exclusions);
        const overlaps = (from,to) => blocks.filter(b=>b.endT>from && b.startT<to);
        const shape = (rows,key)=>(rows || []).map(p=>({...p,key,t:Date.parse(p.at),
            value:Number.isFinite(p.value)?p.value:null}));
        const raw = (rows,key)=>{
            const retained=[];
            let breakBefore=false;
            for (const p of shape(rows,key)) {
                breakBefore ||= p.break_before===true;
                if (!blocks.some(b=>p.t>=b.startT && p.t<b.endT)) continue;
                retained.push({...p,break_before:breakBefore});
                breakBefore=false;
            }
            return retained;
        };
        // Preserve the real hour. If its anchor was cut, place its marker at the
        // first retained instant inside that same hour, never in a neighboring one.
        const anchor = (row,from,to)=>{
            const block = overlaps(from,to)[0];
            if (!block) return null;
            return {...row,t:Math.max(from,block.startT),anchorT:from,atTrimEdge:block.startT>from};
        };
        const advice = (data.hourly_suggestions || []).filter(p=>p.complete===true && Date.parse(p.hour_end)<=end)
            .map(p=>anchor({...p,key:'advice',at:p.hour_start,value:p.final_valid===true && Number.isFinite(p.value)?p.value:null},
                Date.parse(p.hour_start),Date.parse(p.hour_end))).filter(Boolean);
        const ash = shape(data.ash,'ash').map(p=>anchor(p,Date.parse(p.start || p.at),
            p.end ? Date.parse(p.end) : p.t+3600000)).filter(Boolean);
        const groups = [];
        for (const row of advice) {
            if (row.value!==null) continue;
            const from=Date.parse(row.hour_start), to=Date.parse(row.hour_end);
            const reason=this.reasonFor(row,data.parameters);
            for (const block of overlaps(from,to)) {
                const left=Math.max(from,block.startT), right=Math.min(to,block.endT), last=groups.at(-1);
                if (last?.end===left && last.reason===reason) last.end=right;
                else groups.push({start:left,end:right,reason});
            }
        }
        return {blocks,exclusions,groups,advice,ash,A304:raw(data.channels?.A304,'A304'),actual:raw(data.channels?.actual,'actual')};
    },
    connected(a, b) {
        if (!a || !Number.isFinite(a.value) || !Number.isFinite(b.value) || b.break_before) return false;
        // A304 is a held setting; bridge the removed part with the previous
        // setpoint. Actual observations break at both real gaps and trim seams.
        return b.t>=a.t && (a.key==='A304' || this.sameBlock(a.t,b.t));
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
            if (!document.hidden && this.active() && !this.range && !this.loading) this.refresh();
        }, 3600000);
        this.refresh();
    },
    active() { return document.getElementById('page-density-trend')?.classList.contains('active'); },
    status(message) { document.getElementById('density-trend-status').textContent = message; },
    async refresh() {
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
            this.meta = null; this.series = null; this.stoppedStretches = []; this.draw();

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
        const removed = this.series.exclusions.reduce((sum,span)=>sum+span.end-span.start,0);
        const trimNote = !Array.isArray(data.trend_exclusions) ? ' 当前接口未提供逐次裁剪信息，图中仍保留全部时间。'
            : removed ? ` 已省略约 ${this.duration(removed)}停机或实测无效时段，小时点仍归原时段。` : '';
        document.getElementById('density-trend-source').textContent='缺建议时只连接已有建议，原因见下方。灰分、设定和建议按原时钟显示。建议有固有两小时灰分滞后。'+trimNote;
    },
    duration(ms) {
        const minutes=Math.max(1,Math.round(ms/60000));
        return (minutes>=60?`${Math.floor(minutes/60)} 小时`:'')+(minutes%60?` ${minutes%60} 分钟`:'');
    },
    emptyText(fallback) {
        return this.series?.exclusions.length && !this.series.blocks.length ? '所选时段均为停机或实测无效' : fallback;
    },
    renderHourlyCard(data) {
        const point=this.series.advice.at(-1),host=document.getElementById('density-candidate-current');
        document.getElementById('density-candidate-title').textContent=point?'最近保留的已完成小时':'所选时段尚无小时建议';
        const value=point?.value;
        const message=point ? (value===null ? this.reasonFor(point,data.parameters) : '小时最终建议 · 新版历史重算')
            : this.emptyText('所选时段没有可显示的已完成小时');
        host.innerHTML=`<div class="density-hourly-final-value"><p class="density-candidate-number">${this.num(value)}${Number.isFinite(value)?' <span>g/cm³</span>':''}</p><p class="density-hourly-final-period">${this.esc(this.time(point?.hour_start || data.period.start))} — ${this.esc(this.time(point?.hour_end || data.period.end))}</p><p class="density-trend-note">${this.esc(message)}</p></div>`;
    },
    renderMissing() {
        const groups=this.series.groups;
        document.getElementById('density-trend-missing-summary').textContent=`无建议时段与原因 · ${groups.length} 段`;
        document.getElementById('density-trend-missing-body').innerHTML=groups.length?`<ul class="density-missing-list">${groups.map(g=>`<li><div><strong>${this.esc(this.time(g.start))} — ${this.esc(this.time(g.end))}</strong><p>${this.esc(g.reason)}</p></div><span>无建议 ${this.duration(g.end-g.start)}</span></li>`).join('')}</ul>`:`<p class="density-trend-note">${this.emptyText('保留时段没有已记录的缺建议原因。')}</p>`;
    },
    palette() {
        const css = getComputedStyle(document.getElementById('page-density-trend'));
        const get = (v, fallback) => css.getPropertyValue(v).trim() || fallback;
        return {A304:get('--trend-a304','#25897c'),actual:get('--trend-actual','#417dc3'),
            advice:get('--trend-advice','#ce8428'), muted:get('--text-secondary','#68758a'), grid:get('--border-color','#dce1eb'),
            background:get('--bg-card','#fff'),
            ash:get('--trend-ash','#8a7ae0')};
    },
    detail(p) {
        if(p.key==='advice') {
            // 时间轴不做平移：这里只报这一个小时自己的建议值，不带两小时前的灰分。
            const result=Number.isFinite(p.value)?this.num(p.value)+' g/cm³':'暂无有效结果';
            const reason=p.value===null?'\n原因：'+this.reasonFor(p,this.meta?.parameters):'';
            const edge=p.atTrimEdge && p.value!==null?'\n小时起点已省略，点位于本小时保留边界。':'';
            return `小时最终建议 ${result}\n建议小时 ${this.time(p.hour_start)} — ${this.time(p.hour_end)}${reason}${edge}`;
        }
        return `A304 ${p.key==='actual'?'实测密度':'实际设定'} ${this.num(p.value)} g/cm³\n观测时刻 ${this.time(p.at)}`;
    },
    ashDetail(p) {
        // 灰分点按小时起点落位，接口不返回 end；没有 end 时补 +1 小时，别在提示里留"——"。
        const start = p.start || p.at, end = p.end || (start ? new Date(Date.parse(start) + 3600000).toISOString() : null);
        const edge=p.atTrimEdge?'\n小时起点已省略，点位于本小时保留边界。':'';
        return `实际灰分 ${Number.isFinite(p.value)?p.value.toFixed(2):'—'} %\n生产时段 ${this.time(start,true)} — ${this.time(end,true)}${edge}`;
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
            if (p.t > t) break;
            if (this.sameBlock(p.t,t)) current = p;
        }
        return Number.isFinite(current?.value) && t-current.t<=hold ? current : null;
    },
    nearestValueAt(points, t, window) {
        let best = null;
        for (const p of points) {
            if (p.value === null || !this.sameBlock(p.t,t)) continue;
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
        const actual=this.stepValueAt(this.series.actual || [],state.t,90000);
        const advice=this.series.advice.find(p=>state.t>=p.anchorT && state.t<p.anchorT+3600000);
        const ashName='实际灰分';
        const panes=[
            ['ash-plot',ash?`${ashName} ${ash.value.toFixed(2)}%${ash.provisional?'（暂估）':''}`:`${ashName} —`,colors.ash],
            ['plot',`设定 ${this.num(setpoint?.value)} · 实测 ${this.num(actual?.value)}`,colors.A304],
            ['advice-plot',`小时最终建议 ${this.num(advice?.value)}`,Number.isFinite(advice?.value)?colors.advice:colors.muted]
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
        ctx.save(); ctx.fillStyle=colors.muted; ctx.globalAlpha=.09;
        for (let i=1;i<this.laid.length;i++) {
            const from=this.laid[i-1].x1;
            ctx.fillRect(left+from,top,this.laid[i].x0-from,height);
        }
        ctx.restore();
        ctx.font = '13px "Microsoft YaHei", sans-serif'; ctx.textAlign = 'right'; ctx.lineWidth = 1;
        for (const label of labels) {
            const py = y(Number(label)); ctx.strokeStyle = colors.grid; ctx.setLineDash([3,5]);
            ctx.beginPath(); ctx.moveTo(left,py); ctx.lineTo(w-right,py); ctx.stroke();
            ctx.fillStyle = colors.muted; ctx.fillText(label,left-10,py+4);
        }
        ctx.setLineDash([]);
        if (timeAxis && this.meta && this.laid.length) {
            let lastX = -Infinity;
            const tickAt = (px, t, align, minGap = width < 310 ? 60 : 112) => {
                if (px - lastX < minGap) return;
                lastX = px;
                ctx.textAlign = align;
                const label = this.time(t, true);
                if (width < 310) {
                    const [date, time] = label.split(' ');
                    ctx.fillText(date, px, h - 30); ctx.fillText(time || '', px, h - 12);
                } else ctx.fillText(label, px, h - 20);
            };
            const last = this.laid.at(-1), endX = left + last.x1;
            const edgeGap = width < 310 ? 64 : 120;
            tickAt(left,this.laid[0].startT,'left');
            for (const block of this.laid)
                for (let t=Math.ceil(block.startT/3600000)*3600000;t<block.endT;t+=3600000) {
                    const px=x(t);
                    if (px>=left+edgeGap && px<=endX-edgeGap) tickAt(px,t,'center');
                }
            tickAt(endX,last.endT,'right',edgeGap);
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
            const t=this.tAt(px-left);
            if (t!==null && py>=top && py<=top+height) {
                const missing=canvas.id==='density-trend-advice-canvas'
                    && this.series.advice.find(p=>p.value===null && t>=p.anchorT && t<p.anchorT+3600000);
                if(missing){const hit={...missing,t,x:px,y:py};show(hit);this.crosshairAt(hit);return;}
                for (const hit of canvas._hits) {
                    if (!this.sameBlock(hit.t,t) || hit.key==='advice' && hit.value===null) continue;
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
        const colors = this.palette(), points = (this.series?.ash || []).filter(p=>p.value!==null);
        if (!points.length || !this.meta) {
            ctx.fillStyle = colors.muted; ctx.textAlign = 'center';
            ctx.fillText(!this.meta ? '等待读取趋势数据' : this.emptyText('该时段暂无结算总灰分'), w/2, h/2);
            return;
        }
        const {x,y,left,top,width,height} = frame;
        ctx.save(); ctx.beginPath(); ctx.rect(left-3,top,width+6,height); ctx.clip();
        ctx.strokeStyle=colors.ash; ctx.fillStyle=colors.ash; ctx.lineWidth=2;
        let previous=null;
        for (const p of points) {
            const px=x(p.t), py=y(p.value);
            if (px===null) continue;
            // Keep the existing line between settled values, including across seams.
            if (previous) {
                ctx.beginPath();ctx.moveTo(previous.x,previous.y);ctx.lineTo(px,py);ctx.stroke();
            }
            previous={x:px,y:py};
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
        const values = ['A304','actual','advice'].filter(key=>this.visible[key]).flatMap(key=>(this.series?.[key] || []).map(p=>p.value)).filter(Number.isFinite);
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
    drawAdvice(ctx,frame,canvas,colors,points) {
        const {x,y,width,top,height}=frame;
        const hits=points.map(p=>({...p,x:x(p.t),y:p.value===null ? top+height/2 : y(p.value)}))
            .filter(p=>p.x!==null);
        const drawn=hits.filter(p=>p.value!==null);
        if (!drawn.length) {
            ctx.fillStyle=colors.muted;ctx.textAlign='center';
            ctx.fillText('保留时段暂无小时最终建议',frame.w/2,frame.h/2);
        }
        ctx.strokeStyle=colors.advice;ctx.lineWidth=1.8;ctx.setLineDash([]);
        for(let i=1;i<drawn.length;i++){
            ctx.beginPath();ctx.moveTo(drawn[i-1].x,drawn[i-1].y);ctx.lineTo(drawn[i].x,drawn[i].y);ctx.stroke();
        }
        const stride=Math.max(1,Math.ceil(drawn.length/Math.max(1,width/7)));
        for(const [index,p] of drawn.entries()){
            if(index%stride===0 || index===drawn.length-1){ctx.fillStyle=colors.advice;ctx.beginPath();ctx.arc(p.x,p.y,2,0,2*Math.PI);ctx.fill();}
        }
        canvas._hits.push(...hits);
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
        if (!this.series.blocks.length) {
            ctx.fillStyle=colors.muted;ctx.textAlign='center';
            ctx.fillText(this.emptyText('所选时段没有观测数据'),w/2,h/2);
            return;
        }
        const {x,y,left,top,width,height}=frame;
        ctx.save();ctx.beginPath();
        const markerPad=key==='advice'?3:0;
        ctx.rect(left-markerPad,top,width+2*markerPad,height);ctx.clip();
        for(const kind of keys){
            if(!this.visible[kind]) continue;
            const points=this.series?.[kind] || [];
            if(kind==='advice'){this.drawAdvice(ctx,frame,canvas,colors,points);continue;}
            let previous=null;
            for(const p of points){
                const px=x(p.t);
                // 停机段里的采样先跳过：既不画点，也不推进 previous（设定密度线在接缝处保持上一个设定）。
                // 这一步必须在画线之前做，否则 null 坐标会被 canvas 当成 0，把线画回最左边。
                if(px===null) continue;
                if(p.value!==null && this.connected(previous,p)){
                    ctx.strokeStyle=colors[kind];ctx.lineWidth=kind==='actual'?1.4:2;
                    ctx.setLineDash(kind==='actual'?[3,3]:[]);
                    ctx.beginPath();ctx.moveTo(x(previous.t),y(previous.value));
                    if(kind==='A304')ctx.lineTo(px,y(previous.value));
                    ctx.lineTo(px,y(p.value));ctx.stroke();
                }
                const py=p.value===null?top+height-4:y(p.value);
                if(p.value!==null){
                    const marker=!previous || previous.value===null || Math.abs(p.value-previous.value)>=.0005;
                    if(marker){ctx.fillStyle=colors[kind];ctx.beginPath();ctx.arc(px,py,1.8,0,2*Math.PI);ctx.fill();}
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
