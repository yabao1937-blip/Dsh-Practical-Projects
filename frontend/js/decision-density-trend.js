/* Real observations and actual archived hourly-card revisions, on a time axis. */
const DensityTrendPage = {
    hours: 168, range: null, inited: false, seq: 0, meta: null, page: 0,
    visible: {A304: true, advice: true},
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
    points(data) {
        const shape = (points, key) => (points || []).map(p => ({...p, key, t: Date.parse(p.at),
            value: Number.isFinite(p.value) ? p.value : null})).filter(p => Number.isFinite(p.t));
        return {A304: shape(data.channels?.A304, 'A304'),
            advice: shape(data.suggestions, 'advice')};
    },
    connected(a, b) {
        if (!a || !b || a.key !== b.key || !Number.isFinite(a.value) || !Number.isFinite(b.value)) return false;
        // Adjacent hourly anchors already form a continuous hourly series;
        // their few-second capture jitter is not a missing-data gap. Preserve
        // that existing cadence rule, then fill real gaps up to 1 h only.
        if (b.key === 'A304') {
            const elapsed = b.t-a.t;
            const nearHour = p => Math.abs(p.t-Date.parse(p.apply_start))<=30000;
            const adjacentAnchors = !b.break_before && a.origin==='anchor' && b.origin==='anchor' &&
                Date.parse(b.apply_start)-Date.parse(a.apply_start)===3600000 && nearHour(a) && nearHour(b);
            return elapsed>=0 && (adjacentAnchors || elapsed<=3600000);
        }
        if (b.break_before) return false;
        if (b.key === 'advice' && JSON.stringify(a.basis?.channels) !== JSON.stringify(b.basis?.channels)) return false;
        const hour = p => Math.floor(Date.parse(p.apply_start || p.at) / 3600000);
        return b.t >= a.t && b.t - a.t <= 75 * 60000 && hour(b) - hour(a) <= 1;
    },
    trace(ctx, points, key, x, y) {
        let previous = null;
        for (const p of points) {
            if (p.value !== null) {
                if (this.connected(previous, p)) {
                    if (key === 'A304') ctx.lineTo(x(p.t), y(previous.value));
                    ctx.lineTo(x(p.t), y(p.value));
                } else ctx.moveTo(x(p.t), y(p.value));
            }
            previous = p;
        }
    },
    segments(points, key) {
        const observations = key==='A304' ? points.filter(p => p.value!==null) : points;
        const result = [];
        for (let i=1;i<observations.length;i++) {
            const a=observations[i-1], b=observations[i];
            if (b.t>a.t && this.connected(a,b)) result.push({start:a.t,end:b.t,
                from:a.value,to:key==='A304'?a.value:b.value});
        }
        return result;
    },
    differenceAreas() {
        if (!this.visible.A304 || !this.visible.advice || !this.series) return [];
        const setpoints=this.segments(this.series.A304,'A304'), advice=this.segments(this.series.advice,'advice');
        const areas=[];
        let i=0,j=0;
        while (i<setpoints.length && j<advice.length) {
            const a=setpoints[i], b=advice[j], start=Math.max(a.start,b.start), end=Math.min(a.end,b.end);
            if (end>start) {
                const at=t=>b.from+(b.to-b.from)*(t-b.start)/(b.end-b.start);
                const from=at(start), to=at(end), d0=from-a.from, d1=to-a.from;
                // Ignore storage float noise; don't invent a visible gap for equal values.
                if (Math.max(Math.abs(d0),Math.abs(d1))>1e-6) {
                    if (d0*d1<0) {
                        const crossing=start+(end-start)*d0/(d0-d1);
                        areas.push({start,end:crossing,setpoint:a.from,from,to:a.from},
                            {start:crossing,end,setpoint:a.from,from:a.from,to});
                    } else areas.push({start,end,setpoint:a.from,from,to});
                }
            }
            if (a.end<=b.end) i++;
            if (b.end<=a.end) j++;
        }
        return areas;
    },
    shadeDifference(ctx, areas, x, y, color) {
        if (!areas.length) return;
        ctx.save();ctx.fillStyle=color;ctx.globalAlpha=.2;ctx.beginPath();
        for (const a of areas) {
            ctx.moveTo(x(a.start),y(a.setpoint));ctx.lineTo(x(a.end),y(a.setpoint));
            ctx.lineTo(x(a.end),y(a.to));ctx.lineTo(x(a.start),y(a.from));ctx.closePath();
        }
        // Fill only: polygon boundaries must never become connecting strokes.
        ctx.fill();ctx.restore();
    },
    adviceRange() {
        const times = (this.series?.advice || []).filter(p => Number.isFinite(p.value)).map(p => p.t);
        if (!times.length || !this.meta) return null;
        const start = Math.max(Math.floor(Date.parse(this.meta.period.start)/60000)*60000,
            Math.floor((Math.min(...times)-30*60000)/60000)*60000);
        const end = Math.min(Math.ceil(Date.parse(this.meta.period.end)/60000)*60000,
            Math.ceil((Math.max(...times)+30*60000)/60000)*60000);
        return {start: new Date(start).toISOString(), end: new Date(end).toISOString()};
    },
    focusAdvice() {
        const range = this.adviceRange();
        if (!range) return;
        this.range = range;
        for (const field of ['start', 'end']) document.getElementById('density-trend-'+field).value =
            new Date(Date.parse(range[field])+8*3600000).toISOString().slice(0,16);
        document.getElementById('density-trend-custom-fields').hidden = false;
        document.getElementById('density-trend-custom').setAttribute('aria-expanded', 'true');
        this.visible.advice = true;
        document.querySelector('[data-density-series="advice"]').setAttribute('aria-pressed', 'true');
        this.refresh();
    },
    showPoint(previous, point) {
        if (point.value === null) return false;
        if (point.key === 'A304') return point.origin === 'change';
        return this.connected(previous, point) && Math.abs(point.value - previous.value) >= .0005;
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
            this.hours = Number(b.dataset.tRange); this.range = null; this.refresh();
        });
        byId('custom').onclick = () => {
            const fields = byId('custom-fields'); fields.hidden = !fields.hidden;
            byId('custom').setAttribute('aria-expanded', String(!fields.hidden));
            if (!byId('end').value) {
                const local = offset => new Date(Date.now() + offset + 8 * 3600000).toISOString().slice(0,16);
                byId('start').value = local(-168 * 3600000); byId('end').value = local(0);
            }
        };
        byId('query').onclick = () => {
            const start = byId('start').value, end = byId('end').value;
            if (!start || !end || end <= start) { this.status('请选择有效的起止时间，结束须晚于开始。'); return; }
            this.range = {start: start + ':00+08:00', end: end + ':00+08:00'}; this.refresh();
        };
        byId('refresh').onclick = () => this.refresh();
        byId('focus-advice').onclick = () => this.focusAdvice();
        document.querySelectorAll('[data-density-series]').forEach(b => b.onclick = () => {
            const key = b.dataset.densitySeries; this.visible[key] = !this.visible[key];
            b.setAttribute('aria-pressed', String(this.visible[key])); this.draw();
        });
        byId('prev').onclick = () => { this.page--; this.renderActions(); };
        byId('next').onclick = () => { this.page++; this.renderActions(); };
        this.observer = new ResizeObserver(() => this.draw()); this.observer.observe(byId('plot'));
        this.themeObserver = new MutationObserver(() => this.draw());
        this.themeObserver.observe(document.documentElement, {attributes:true, attributeFilter:['data-theme','class']});
        this.themeObserver.observe(document.body, {attributes:true, attributeFilter:['data-theme','class']});
        document.addEventListener('visibilitychange', () => { if (!document.hidden && this.active()) this.refresh(); });
        this.timer = setInterval(() => {
            if (!document.hidden && this.active() && !this.range && !this.loading) this.refresh({keepPage:true});
        }, 60000);
        this.refresh();
    },
    active() { return document.getElementById('page-density-trend')?.classList.contains('active'); },
    status(message) { document.getElementById('density-trend-status').textContent = message; },
    async refresh({keepPage = false} = {}) {
        const seq = ++this.seq, range = this.range, hours = this.hours;
        this.loading = true;
        this.status('正在读取所选时段…');
        document.getElementById('density-trend-plot').setAttribute('aria-busy', 'true');
        try {
            const query = range ? 'start='+encodeURIComponent(range.start)+'&end='+encodeURIComponent(range.end) : 'limit='+hours;
            const data = await Api.decisionEvidence('/density-trend?' + query);
            if (seq !== this.seq) return;
            this.meta = data; this.series = this.points(data); if (!keepPage) this.page = 0;
            document.querySelectorAll('[data-t-range]').forEach(b => {
                const chosen = !range && Number(b.dataset.tRange) === hours;
                b.classList.toggle('btn-primary', chosen); b.setAttribute('aria-pressed', String(chosen));
            });
            document.getElementById('density-trend-custom').classList.toggle('btn-primary', !!range);
            const advice = this.series.advice.filter(p => Number.isFinite(p.value)), latest = advice.at(-1);
            document.getElementById('density-trend-caption').textContent =
                `${range ? '自选时间' : hours === 168 ? '近7天' : '近30天'} · ${this.time(data.period.start, true)} — ${this.time(data.period.end, true)}`;
            document.getElementById('density-trend-focus-advice').disabled = !latest;
            this.status(''); this.renderActions(); this.draw();
        } catch (error) {
            if (seq !== this.seq) return;
            this.meta = null; this.series = null; this.draw();
            document.getElementById('density-trend-actions').innerHTML = '<tr><td colspan="5">该范围未能读取，请重试。</td></tr>';
            document.getElementById('density-trend-caption').textContent = '所选时段读取失败';
            document.getElementById('density-trend-focus-advice').disabled = true;
            this.status(error.message || '读取失败，请稍后刷新。');
        } finally {
            if (seq === this.seq) {
                this.loading = false;
                document.getElementById('density-trend-plot').setAttribute('aria-busy', 'false');
            }
        }
    },
    renderActions() {
        const actions = [...(this.meta?.actions || [])].reverse(), pages = Math.max(1, Math.ceil(actions.length/20));
        this.page = Math.max(0, Math.min(pages-1, this.page));
        document.getElementById('density-trend-actions').innerHTML = actions.length ? actions.slice(this.page*20, this.page*20+20).map(a => {
            const advice=a.hourly_advice, value=advice?.value;
            const tip=Number.isFinite(value) ? `调后观测 ${this.time(a.comparison_at)} · 建议存档于 ${this.time(advice.generated_at)} · 适用小时 ${this.time(advice.apply_start,true)}` :
                '调后观测时刻没有对应小时的有效建议存档';
            return `<tr><td>${this.esc(this.time(a.at))}${a.continuous ? '' : '<small>前后观测有间隔</small>'}</td><td>${this.esc(a.channel)}</td>`+
                `<td>${this.num(a.before)} <span class="density-trend-arrow">→</span> <strong>${this.num(a.after)}</strong></td>`+
                `<td title="${this.esc(tip)}">${this.num(value)}</td><td>${this.signed(a.difference)}</td></tr>`;
        }).join('') : '<tr><td colspan="5">所选时段没有已记录的设定变化。</td></tr>';
        document.getElementById('density-trend-page-info').textContent = `共 ${actions.length} 条 · ${this.page+1} / ${pages}`;
        document.getElementById('density-trend-prev').disabled = this.page === 0;
        document.getElementById('density-trend-next').disabled = this.page === pages-1;
    },
    palette() {
        const css = getComputedStyle(document.getElementById('page-density-trend'));
        const get = (v, fallback) => css.getPropertyValue(v).trim() || fallback;
        return {A304:get('--trend-a304','#25897c'),
            advice:get('--trend-advice','#ce8428'), muted:get('--text-secondary','#68758a'), grid:get('--border-color','#dce1eb'),
            background:get('--bg-card','#fff'), difference:get('--trend-difference','#579ee8')};
    },
    detail(p) {
        if (p.key !== 'advice') return `${p.key} 设定密度  ${this.num(p.value)} g/cm³\n${this.time(p.at)}\n` +
            ({anchor:'本小时基准观测',before:'调密前观测',change:'检测到设定变化'}[p.origin] || '历史观测') +
            (p.break_before ? ' · 前后观测有间隔' : '');
        return `本小时建议  ${this.num(p.value)} g/cm³\n存档于 ${this.time(p.at)}\n适用小时 ${this.time(p.apply_start, true)}\n` +
            `固定设定基准 ${this.num(p.basis?.value)} (${p.basis?.channels?.join(' / ') || '—'})\n`+
            `小时灰分 ${Number.isFinite(p.ash?.value) ? p.ash.value.toFixed(2)+'%' : '—'}${p.ash?.provisional ? '（暂估）' : ''}`;
    },
    draw() {
        const canvas = document.getElementById('density-trend-canvas'), plot = document.getElementById('density-trend-plot');
        if (!canvas || !plot) return;
        const w = plot.clientWidth, h = plot.clientHeight;
        if (w < 100) return;
        const ratio = Math.min(devicePixelRatio || 1, 2);
        canvas.width = w*ratio; canvas.height = h*ratio;
        const ctx = canvas.getContext('2d'); ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,w,h);
        const tip = plot.querySelector('.density-trend-tooltip'); tip.hidden = true; canvas._hits = []; canvas._adviceLabel = null;
        const colors = this.palette();
        const groups = Object.entries(this.series || {}).filter(([key]) => this.visible[key]);
        const values = groups.flatMap(([, points]) => points.map(p => p.value)).filter(Number.isFinite);
        ctx.font = '13px "Microsoft YaHei", sans-serif'; ctx.fillStyle = colors.muted;
        if (!values.length || !this.meta) {
            ctx.textAlign = 'center'; ctx.fillText(this.meta ? '所选曲线暂无有效历史记录' : '等待读取趋势数据', w/2, h/2); return;
        }
        const left = 64, right = 24, top = 22, bottom = 52, width = w-left-right, height = h-top-bottom;
        const lo = Math.min(...values), hi = Math.max(...values), pad = Math.max(.004,(hi-lo)*.18);
        const min = lo-pad, max = hi+pad, start = Date.parse(this.meta.period.start), end = Date.parse(this.meta.period.end);
        const x = t => left + (t-start)/(end-start)*width, y = v => top+(max-v)/(max-min)*height;
        ctx.textAlign = 'right'; ctx.lineWidth = 1;
        for (let i=0;i<=5;i++) {
            const v=min+(max-min)*i/5, py=y(v); ctx.strokeStyle=colors.grid; ctx.setLineDash([3,5]);
            ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();
            ctx.fillStyle=colors.muted;ctx.fillText(v.toFixed(3),left-10,py+4);
        }
        ctx.setLineDash([]);
        const ticks = Math.max(2,Math.floor(width/155));
        for (let i=0;i<=ticks;i++) {
            const t=start+(end-start)*i/ticks;
            ctx.textAlign=i===0?'left':i===ticks?'right':'center';
            ctx.fillText(this.time(t,true),x(t),h-20);
        }
        this.shadeDifference(ctx,this.differenceAreas(),x,y,colors.difference);
        for (const [key, observations] of groups) {
            // Missing A304 observations may be crossed only when the enclosing
            // real endpoints meet the 1 h rule. Advice gaps stay untouched.
            const points = key === 'A304' ? observations.filter(p => p.value !== null) : observations;
            ctx.strokeStyle=colors[key];ctx.fillStyle=colors[key];ctx.lineWidth=key==='advice'?3:2;
            ctx.setLineDash(key==='advice'?[6,4]:[]);ctx.beginPath();
            this.trace(ctx, points, key, x, y);
            ctx.stroke();ctx.setLineDash([]);
            let previous=null;
            for (const [index,p] of points.entries()) {
                const marked=this.showPoint(previous,p);
                const isolated=!this.connected(previous,p) && !(points[index+1] && this.connected(p,points[index+1]));
                previous=p;
                if (p.value===null) continue;
                const px=x(p.t),py=y(p.value);
                if (marked) {ctx.beginPath();ctx.arc(px,py,3.5,0,2*Math.PI);ctx.fill();}
                else if (isolated) {ctx.beginPath();ctx.moveTo(px-4,py);ctx.lineTo(px+4,py);ctx.stroke();}
                canvas._hits.push({...p,x:px,y:py,text:this.detail(p)});
            }
        }
        // A newly started archive can span less than one pixel on a 7/30-day
        // axis. Label its real endpoint, without moving or backdating records.
        const latest = this.visible.advice && this.series.advice.filter(p => p.value !== null).at(-1);
        if (latest) {
            const px=x(latest.t), py=y(latest.value), label=`小时建议 ${this.num(latest.value)}`;
            ctx.font='bold 13px "Microsoft YaHei", sans-serif';
            const lw=ctx.measureText(label).width+20, lh=28;
            const lx=Math.max(left,Math.min(px-lw/2,w-right-lw));
            const ly=py-top>=44?py-44:py+16;
            ctx.strokeStyle=colors.advice;ctx.lineWidth=1;ctx.setLineDash([]);
            ctx.fillStyle=colors.background;ctx.fillRect(lx,ly,lw,lh);ctx.strokeRect(lx,ly,lw,lh);
            ctx.fillStyle=colors.advice;ctx.textAlign='left';ctx.fillText(label,lx+10,ly+19);
            canvas._adviceLabel={...latest,x:px,y:py,text:this.detail(latest),left:lx,top:ly,width:lw,height:lh};
        }
        canvas._hits.sort((a,b)=>a.t-b.t || a.key.localeCompare(b.key));
        const show = hit => {
            tip.textContent=hit.text;tip.hidden=false;
            tip.style.left=Math.max(4,Math.min(hit.x+14,w-tip.offsetWidth-4))+'px';
            tip.style.top=Math.max(4,Math.min(hit.y+12,h-tip.offsetHeight-4))+'px';
        };
        const point = event => {
            const r=canvas.getBoundingClientRect(),px=event.clientX-r.left,py=event.clientY-r.top;
            const label=canvas._adviceLabel;
            if(label && px>=label.left && px<=label.left+label.width && py>=label.top && py<=label.top+label.height){show(label);return;}
            let best=null,distance=Infinity;
            for(const hit of canvas._hits) {const d=Math.hypot(hit.x-px,hit.y-py);if(d<distance){best=hit;distance=d;}}
            if(best&&distance<32)show(best);else tip.hidden=true;
        };
        canvas.onmousemove=point;canvas.onclick=point;canvas.onmouseleave=()=>{tip.hidden=true;};
        canvas.onkeydown=event=>{
            const hits=canvas._hits;
            if(!['Home','End','ArrowLeft','ArrowRight','Escape'].includes(event.key)||!hits.length)return;
            event.preventDefault();if(event.key==='Escape'){tip.hidden=true;return;}
            canvas._index=event.key==='Home'?0:event.key==='End'?hits.length-1:
                Math.max(0,Math.min(hits.length-1,(canvas._index??-1)+(event.key==='ArrowRight'?1:-1)));
            show(hits[canvas._index]);
        };
    },
};
