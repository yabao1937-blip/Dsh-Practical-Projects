/* Dedicated page: operator setpoint vs suggested density, hourly point-line chart. */
const DensityTrendPage = {
    hours: 168, inited: false, seq: 0, series: null, meta: null, actions: null,
    seriesOf(items) {
        // Pure shape for the point-line chart: null keeps the gap visible.
        return (items || []).map((item, index) => ({index, label: item.label,
            setpoint: Number.isFinite(item.setpoint?.value) ? item.setpoint.value : null,
            setpointOrigin: item.setpoint?.origin || null,
            suggested: Number.isFinite(item.suggested?.value) ? item.suggested.value : null,
            suggestedReason: item.suggested?.reason || '', actions: item.actions || []}));
    },
    num(value, digits = 3) { return Number.isFinite(value) ? value.toFixed(digits) : '—'; },
    time(value) {
        return value ? new Date(value).toLocaleString('zh-CN', {timeZone: 'Asia/Shanghai', hour12: false}) : '—';
    },
    esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    init() {
        if (this.inited) return;
        this.inited = true;
        document.querySelectorAll('#density-trend-ranges [data-t-range]').forEach(button =>
            button.addEventListener('click', () => {
                if (Number(button.dataset.tRange) === this.hours) return;
                this.hours = Number(button.dataset.tRange);
                this.refresh();
            }));
        document.getElementById('density-trend-refresh')?.addEventListener('click', () => this.refresh());
        this.resizeObserver = new ResizeObserver(() => { if (this.series) this.draw(); });
        this.resizeObserver.observe(document.getElementById('density-trend-plot'));
        document.addEventListener('visibilitychange', () => { if (!document.hidden && this.active()) this.refresh(); });
        this.refresh();
    },
    active() { return document.getElementById('page-density-trend')?.classList.contains('active'); },
    status(message) { document.getElementById('density-trend-status').textContent = message || ''; },
    async refresh() {
        const seq = ++this.seq;
        this.status('正在读取趋势数据…');
        try {
            const [trend, actions] = await Promise.all([
                Api.decisionEvidence('/density-trend?limit=' + this.hours),
                Api.decisionEvidence('/actions?limit=50').catch(() => null),
            ]);
            if (seq !== this.seq) return;
            this.meta = trend;
            this.series = this.seriesOf(trend.items);
            this.actions = actions;
            this.status('');
            document.querySelectorAll('#density-trend-ranges [data-t-range]').forEach(button => {
                const selected = Number(button.dataset.tRange) === this.hours;
                button.classList.toggle('btn-primary', selected);
                button.setAttribute('aria-pressed', String(selected));
            });
            const set = this.series.filter(s => s.setpoint !== null).length;
            const advice = this.series.filter(s => s.suggested !== null).length;
            const label = this.hours === 168 ? '近7天' : '近30天';
            document.getElementById('density-trend-caption').textContent =
                `${label} · ${this.series.length} 小时 · 人工设定 ${set} 小时 · 建议密度 ${advice} 小时 · 调密 ${trend.total_actions} 次（${trend.channel === '全部通道' ? '全部通道' : 'MD_SET ' + trend.channel}）`;
            this.renderActions();
            this.draw();
        } catch (error) {
            if (seq === this.seq) this.status(error.message || '趋势数据读取失败，请稍后刷新。');
        }
    },
    renderActions() {
        const body = document.getElementById('density-trend-actions');
        if (!body) return;
        body.innerHTML = this.actions?.items?.length ? this.actions.items.map(x =>
            `<tr><td>${this.esc(this.time(x.detected_at))}</td><td>${this.esc(x.channel)}</td>` +
            `<td>${this.num(x.before)} → ${this.num(x.after)}</td><td>${Number.isFinite(x.after) && Number.isFinite(x.before) ? this.num(x.after - x.before) : '—'}</td></tr>`).join('')
            : '<tr><td colspan="4">尚未检测到设定值变化。功能启用后的变化会自动记录。</td></tr>';
    },
    palette() {
        const style = getComputedStyle(document.getElementById('page-density-trend'));
        const get = (key, fallback) => style.getPropertyValue(key).trim() || fallback;
        return {ink: get('--text-primary', '#22303f'), muted: get('--text-muted', '#7b8794'),
            rule: get('--border-color', '#d6dde5'), blue: get('--accent-blue', '#2f6fed'),
            orange: get('--accent-orange', '#f2994a'), red: get('--accent-red', '#d64545')};
    },
    draw() {
        const canvas = document.getElementById('density-trend-canvas');
        const plot = document.getElementById('density-trend-plot');
        const series = this.series || [];
        const rect = plot.getBoundingClientRect();
        const w = Math.max(320, rect.width), h = Math.max(300, Math.min(520, rect.height || 420));
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
        canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
        ctx.font = '12px Microsoft YaHei, sans-serif';
        const c = this.palette();
        const values = series.flatMap(s => [s.setpoint, s.suggested]).filter(Number.isFinite);
        if (!values.length) {
            ctx.fillStyle = c.muted; ctx.textAlign = 'center';
            ctx.fillText('暂无设定密度与建议密度数据', w / 2, h / 2);
            canvas._hits = []; return;
        }
        const left = 64, right = 24, top = 30, bottom = 52, xWidth = w - left - right, yHeight = h - top - bottom;
        let min = Math.min(...values), max = Math.max(...values), span = max - min || 0.02;
        min -= span * .15; max += span * .15;
        const y = value => top + yHeight - (value - min) / (max - min) * yHeight;
        ctx.lineWidth = 1; ctx.strokeStyle = c.rule; ctx.fillStyle = c.muted; ctx.textAlign = 'right';
        for (let i = 0; i <= 6; i++) {
            const value = min + (max - min) * i / 6, py = y(value);
            ctx.beginPath(); ctx.moveTo(left, py); ctx.lineTo(w - right, py); ctx.stroke();
            ctx.fillText(value.toFixed(3), left - 8, py + 4);
        }
        ctx.textAlign = 'left'; ctx.fillText('g/cm³', left, 16);
        const n = series.length, x = index => left + (n === 1 ? .5 : index / (n - 1)) * xWidth;
        const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(xWidth / 96))));
        ctx.textAlign = 'center';
        for (let i = 0; i < n; i += step) ctx.fillText(series[i].label, x(i), h - bottom + 22);
        ctx.fillText('小时 · 每小时一点', left + xWidth / 2, h - 8);
        const line = (field, color, dash) => {
            ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = n > 240 ? 1.4 : 2; ctx.setLineDash(dash);
            ctx.beginPath(); let started = false;
            series.forEach((s, i) => { if (!Number.isFinite(s[field])) { started = false; return; }
                started ? ctx.lineTo(x(i), y(s[field])) : ctx.moveTo(x(i), y(s[field])); started = true; });
            ctx.stroke(); ctx.setLineDash([]);
            const radius = n > 240 ? 1.7 : 2.7;
            series.forEach((s, i) => { if (!Number.isFinite(s[field])) return;
                ctx.beginPath(); ctx.arc(x(i), y(s[field]), radius, 0, Math.PI * 2); ctx.fill(); });
        };
        line('suggested', c.orange, [5, 4]);
        line('setpoint', c.blue, []);
        series.forEach((s, i) => { if (!s.actions.length || !Number.isFinite(s.setpoint)) return;
            ctx.strokeStyle = c.red; ctx.lineWidth = 1.5; ctx.beginPath();
            ctx.arc(x(i), y(s.setpoint), n > 240 ? 4 : 5.5, 0, Math.PI * 2); ctx.stroke(); });
        canvas._hits = series.map((s, i) => ({x: x(i),
            y: y(Number.isFinite(s.setpoint) ? s.setpoint : (Number.isFinite(s.suggested) ? s.suggested : min)),
            text: `${s.label}:00 小时\n人工设定 ${s.setpoint === null ? '—' : s.setpoint.toFixed(3)} g/cm³` +
                `${s.actions.length ? `（该小时调密 ${s.actions.length} 次：${s.actions.map(a => `${this.num(a.before)}→${this.num(a.after)}`).join('，')}）` : s.setpointOrigin === 'anchor' ? '（本小时无调整）' : ''}\n` +
                `建议密度 ${s.suggested === null ? '—' : s.suggested.toFixed(3)} g/cm³${s.suggested === null && s.suggestedReason ? `（${s.suggestedReason}）` : ''}\n` +
                `建议−设定 ${Number.isFinite(s.suggested) && Number.isFinite(s.setpoint) ? (s.suggested - s.setpoint).toFixed(3) : '—'}`}));
        const tooltip = plot.querySelector('.density-trend-tooltip');
        const show = (hit, px, py) => { tooltip.textContent = hit.text; tooltip.hidden = false;
            tooltip.style.left = Math.max(4, Math.min(px + 14, w - tooltip.offsetWidth - 4)) + 'px';
            tooltip.style.top = Math.max(4, Math.min(py + 12, h - tooltip.offsetHeight - 4)) + 'px'; };
        const nearest = px => { let best = null, distance = Infinity;
            for (const hit of canvas._hits || []) { const d = Math.abs(hit.x - px); if (d < distance) { best = hit; distance = d; } }
            return best && distance <= 28 ? best : null; };
        canvas.onmousemove = event => { const r = canvas.getBoundingClientRect(); const hit = nearest(event.clientX - r.left);
            hit ? show(hit, event.clientX - r.left, event.clientY - r.top) : tooltip.hidden = true; };
        canvas.onmouseleave = () => { if (!canvas.matches(':focus-visible')) tooltip.hidden = true; };
        canvas.onpointerdown = event => { canvas._tap = {x: event.clientX, y: event.clientY}; };
        canvas.onpointerup = event => { const tap = canvas._tap; canvas._tap = null;
            if (tap && Math.hypot(event.clientX - tap.x, event.clientY - tap.y) < 10) {
                const r = canvas.getBoundingClientRect(); const hit = nearest(event.clientX - r.left);
                hit ? show(hit, event.clientX - r.left, event.clientY - r.top) : tooltip.hidden = true; } };
        canvas.onkeydown = event => {
            const hits = canvas._hits || []; if (!hits.length) return;
            const keys = {ArrowLeft: -1, ArrowRight: 1, Home: 'first', End: 'last', Escape: 'close'};
            if (!(event.key in keys)) return;
            event.preventDefault();
            if (event.key === 'Escape') { tooltip.hidden = true; return; }
            canvas._index = Math.max(0, Math.min(hits.length - 1,
                event.key === 'first' ? 0 : event.key === 'last' ? hits.length - 1 : (canvas._index ?? 0) + keys[event.key]));
            const hit = hits[canvas._index]; show(hit, hit.x, hit.y); };
    },
};
