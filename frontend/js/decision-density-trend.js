/* Real observations and actual archived hourly-card revisions, on a time axis. */
const DensityTrendPage = {
    hours: 168, range: null, inited: false, seq: 0, meta: null, page: 0,
    visible: {A304: true, advice: true},
    num(v) { return Number.isFinite(v) ? v.toFixed(3) : '—'; },
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
        if (!a || b.break_before || a.value === null || b.value === null) return false;
        if (b.key === 'advice' && JSON.stringify(a.basis?.channels) !== JSON.stringify(b.basis?.channels)) return false;
        const hour = p => Math.floor(Date.parse(p.apply_start || p.at) / 3600000);
        return b.t >= a.t && b.t - a.t <= 75 * 60000 && hour(b) - hour(a) <= 1;
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
        this.refresh();
    },
    active() { return document.getElementById('page-density-trend')?.classList.contains('active'); },
    status(message) { document.getElementById('density-trend-status').textContent = message; },
    async refresh() {
        const seq = ++this.seq, range = this.range, hours = this.hours;
        this.status('正在读取所选时段…');
        document.getElementById('density-trend-plot').setAttribute('aria-busy', 'true');
        try {
            const query = range ? 'start='+encodeURIComponent(range.start)+'&end='+encodeURIComponent(range.end) : 'limit='+hours;
            const data = await Api.decisionEvidence('/density-trend?' + query);
            if (seq !== this.seq) return;
            this.meta = data; this.series = this.points(data); this.page = 0;
            document.querySelectorAll('[data-t-range]').forEach(b => {
                const chosen = !range && Number(b.dataset.tRange) === hours;
                b.classList.toggle('btn-primary', chosen); b.setAttribute('aria-pressed', String(chosen));
            });
            document.getElementById('density-trend-custom').classList.toggle('btn-primary', !!range);
            const count = data.suggestions.filter(p => Number.isFinite(p.value)).length;
            document.getElementById('density-trend-caption').textContent =
                `${range ? '自选时间' : hours === 168 ? '近7天' : '近30天'} · ${this.time(data.period.start, true)} — ${this.time(data.period.end, true)}`;
            document.getElementById('density-trend-archive-note').textContent =
                `${data.total_actions} 次设定变化 · ${count} 条真实小时建议。${data.archive_note}`;
            this.status(''); this.renderActions(); this.draw();
        } catch (error) {
            if (seq !== this.seq) return;
            this.meta = null; this.series = null; this.draw();
            document.getElementById('density-trend-actions').innerHTML = '<tr><td colspan="4">该范围未能读取，请重试。</td></tr>';
            document.getElementById('density-trend-caption').textContent = '所选时段读取失败';
            document.getElementById('density-trend-archive-note').textContent = '';
            this.status(error.message || '读取失败，请稍后刷新。');
        } finally {
            if (seq === this.seq) document.getElementById('density-trend-plot').setAttribute('aria-busy', 'false');
        }
    },
    renderActions() {
        const actions = [...(this.meta?.actions || [])].reverse(), pages = Math.max(1, Math.ceil(actions.length/20));
        this.page = Math.max(0, Math.min(pages-1, this.page));
        document.getElementById('density-trend-actions').innerHTML = actions.length ? actions.slice(this.page*20, this.page*20+20).map(a =>
            `<tr><td>${this.esc(this.time(a.at))}${a.continuous ? '' : '<small>前后观测有间隔</small>'}</td><td>${this.esc(a.channel)}</td>`+
            `<td>${this.num(a.before)} <span class="density-trend-arrow">→</span> ${this.num(a.after)}</td><td>${Number.isFinite(a.before) && Number.isFinite(a.after) ? (a.after >= a.before ? '+' : '') + this.num(a.after-a.before) : '—'}</td></tr>`).join('') :
            '<tr><td colspan="4">所选时段没有已记录的设定变化。</td></tr>';
        document.getElementById('density-trend-page-info').textContent = `共 ${actions.length} 条 · ${this.page+1} / ${pages}`;
        document.getElementById('density-trend-prev').disabled = this.page === 0;
        document.getElementById('density-trend-next').disabled = this.page === pages-1;
    },
    palette() {
        const css = getComputedStyle(document.getElementById('page-density-trend'));
        const get = (v, fallback) => css.getPropertyValue(v).trim() || fallback;
        return {A304:get('--trend-a304','#25897c'),
            advice:get('--trend-advice','#ce8428'), muted:get('--text-secondary','#68758a'), grid:get('--border-color','#dce1eb')};
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
        const tip = plot.querySelector('.density-trend-tooltip'); tip.hidden = true; canvas._hits = [];
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
        for (const [key, points] of groups) {
            ctx.strokeStyle=colors[key];ctx.fillStyle=colors[key];ctx.lineWidth=2;
            ctx.setLineDash(key==='advice'?[6,4]:[]);ctx.beginPath();
            let previous=null;
            for (const p of points) {
                if (p.value !== null) { this.connected(previous,p)?ctx.lineTo(x(p.t),y(p.value)):ctx.moveTo(x(p.t),y(p.value)); }
                previous=p;
            }
            ctx.stroke();ctx.setLineDash([]);
            for (const p of points) {
                if (p.value===null) continue;
                const px=x(p.t),py=y(p.value), radius=points.length>600?1.8:2.8;
                ctx.beginPath();ctx.arc(px,py,radius,0,2*Math.PI);ctx.fill();
                if (p.origin==='change') {ctx.lineWidth=1.5;ctx.beginPath();ctx.arc(px,py,5,0,2*Math.PI);ctx.stroke();}
                canvas._hits.push({...p,x:px,y:py,text:this.detail(p)});
            }
        }
        canvas._hits.sort((a,b)=>a.t-b.t || a.key.localeCompare(b.key));
        const show = hit => {
            tip.textContent=hit.text;tip.hidden=false;
            tip.style.left=Math.max(4,Math.min(hit.x+14,w-tip.offsetWidth-4))+'px';
            tip.style.top=Math.max(4,Math.min(hit.y+12,h-tip.offsetHeight-4))+'px';
        };
        const point = event => {
            const r=canvas.getBoundingClientRect(),px=event.clientX-r.left,py=event.clientY-r.top;
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
