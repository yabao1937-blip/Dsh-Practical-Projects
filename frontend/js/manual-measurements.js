/* Database-backed single-value manual readings. Does not modify App.store or model inputs. */
const ManualMeasurements = {
    ready: false, busy: false, definitions: [], definitionsPromise: null, offset: 0, pageSize: 20, total: 0, sequence: 0, pending: null,
    el(id) { return document.getElementById('measurement-' + id); },
    active() { return !document.hidden && (document.getElementById('page-collect')?.classList.contains('active') || !!DecisionManualEntry.dialog?.open); },
    escape(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    time(value) {
        const date = new Date(value);
        return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',
            month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date).replace(/\//g,'-') : '—';
    },
    message(text, status = '') { this.el('message').textContent = text; this.el('message').dataset.status = status; },
    async refresh() {
        if (location.protocol !== 'http:' && location.protocol !== 'https:') {
            this.message('请通过服务器网页打开此页面，才能将补录保存到数据库。','error');
            this.el('submit').disabled = true;
            return;
        }
        try {
            if (!this.ready && !this.definitionsPromise) this.definitionsPromise = (async () => {
                this.definitions = await Api.getManualMeasurementMetrics();
                const options = this.definitions.map(d => `<option value="${this.escape(d.key)}">${this.escape(d.name)}</option>`).join('');
                this.el('metric').innerHTML = options;
                this.el('filter').innerHTML = '<option value="">全部信息</option>' + options;
                this.ready = true;
                this.updateUnit();
                this.el('submit').disabled = this.busy;
                this.message('');
            })().finally(() => {this.definitionsPromise = null;});
            if (this.definitionsPromise) await this.definitionsPromise;
        } catch (error) {
            this.message(error.message || '无法连接补录服务，请稍后刷新。','error');
            return;
        }
        await this.loadHistory();
    },
    updateUnit() {
        const definition = this.definitions.find(d => d.key === this.el('metric').value);
        this.el('unit').textContent = definition ? `（${definition.unit}）` : '';
        if (definition?.maximum != null) this.el('value').max = definition.maximum;
        else this.el('value').removeAttribute('max');
    },
    setBusy(busy) {
        this.busy = busy;
        this.el('fields').disabled = busy;
        this.el('submit').disabled = busy || !this.ready;
        this.el('reset').disabled = busy;
        this.el('submit').textContent = busy ? '正在保存…' : '保存补录';
    },
    newId() {
        return typeof globalThis.crypto?.randomUUID === 'function' ? crypto.randomUUID()
            : 'manual-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    },
    async submit(event) {
        event.preventDefault();
        if (this.busy || !this.ready) return;
        const raw = this.el('value').value.trim(), value = Number(raw);
        const definition = this.definitions.find(d => d.key === this.el('metric').value);
        if (!raw || !Number.isFinite(value) || value < 0 || !definition || (definition.maximum != null && value > definition.maximum)) {
            this.message('请输入有效数值：百分比为 0–100，煤量不得为负。','error'); return;
        }
        const inputTime = this.el('time').value;
        const payload = {metric: definition.key, value, observed_at: inputTime ? inputTime + '+08:00' : null};
        const signature = JSON.stringify(payload);
        if (this.pending?.signature !== signature) this.pending = {signature, id: this.newId()};
        this.setBusy(true);
        this.message('正在保存到数据库…');
        try {
            const record = await Api.saveManualMeasurement({...payload, client_id: this.pending.id}, definition);
            this.pending = null;
            this.el('value').value = '';
            this.el('time').value = '';
            this.message(`已保存到数据库：${record.name} ${record.value} ${record.unit}，时间 ${this.time(record.observed_at)}`,'success');
            this.offset = 0;
            this.el('filter').value = '';
            if (window.Operations) window.Operations.manualMeasurementSaved();
            await this.loadHistory();
        } catch (error) {
            this.message(error.message || '未收到保存确认，已保留填写内容，请刷新记录核对后重试。','error');
        } finally { this.setBusy(false); }
    },
    async loadHistory() {
        const sequence = ++this.sequence;
        const offset = this.offset, metric = this.el('filter').value;
        try {
            const result = await Api.getManualMeasurements({offset, metric, limit:this.pageSize});
            if (sequence !== this.sequence) return;
            this.total = result.total;
            this.el('history').innerHTML = result.items.length ? result.items.map(row => `<tr><td>${this.escape(this.time(row.observed_at))}</td><td>${this.escape(row.name)}</td><td>${this.escape(row.value)}</td><td>${this.escape(row.unit)}</td><td>${this.escape(this.time(row.created_at))}</td></tr>`).join('') : '<tr><td colspan="5">暂无业务指标补录记录</td></tr>';
            this.el('count').textContent = result.total ? `共 ${result.total} 条，显示第 ${offset + 1}–${offset + result.items.length} 条` : '共 0 条';
            this.el('prev').disabled = offset <= 0;
            this.el('next').disabled = offset + this.pageSize >= result.total;
        } catch (error) {
            if (sequence !== this.sequence) return;
            this.el('history').innerHTML = `<tr><td colspan="5">${this.escape(error.message || '补录记录加载失败，请稍后刷新。')}</td></tr>`;
            this.el('count').textContent = '记录加载失败';
            this.el('prev').disabled = this.el('next').disabled = true;
        }
    },
    init() {
        DecisionManualEntry.init();
        this.el('form').addEventListener('submit', event => this.submit(event));
        this.el('metric').addEventListener('change', () => this.updateUnit());
        this.el('refresh').addEventListener('click', () => this.refresh());
        this.el('filter').addEventListener('change', () => {this.offset=0;this.loadHistory();});
        this.el('prev').addEventListener('click', () => {this.offset=Math.max(0,this.offset-this.pageSize);this.loadHistory();});
        this.el('next').addEventListener('click', () => {this.offset+=this.pageSize;this.loadHistory();});
        this.el('reset').addEventListener('click', () => {this.el('time').value='';this.el('value').value='';this.pending=null;this.message('');});
        new MutationObserver(() => {if(this.active()) this.refresh();}).observe(document.getElementById('page-collect'),{attributes:true,attributeFilter:['class']});
        document.addEventListener('visibilitychange', () => {if(this.active()) this.refresh();});
        if (this.active()) this.refresh();
    },
};

// Move the original panel instead of cloning it: one form, one draft, one
// idempotency key and the same SQLite save/refresh path from either page.
const DecisionManualEntry = {
    dialog: null, panel: null, anchor: null, position: null, drag: null,
    init() {
        document.getElementById('decision-manual-open')?.addEventListener('click', () => this.open());
    },
    ensure() {
        if (this.dialog) return;
        this.panel = document.getElementById('manual-measurements-panel');
        this.anchor = document.createComment('manual measurements original location');
        this.panel.before(this.anchor);
        this.dialog = document.createElement('dialog');
        this.dialog.id = 'decision-manual-dialog';
        this.dialog.className = 'decision-manual-dialog';
        this.dialog.setAttribute('aria-labelledby', 'decision-manual-title');
        this.dialog.innerHTML = `<header class="manual-dialog-header" tabindex="0" aria-label="拖动标题栏或按方向键移动窗口"><div><h2 id="decision-manual-title">手工信息补录</h2><p>与数据采集页共用补录记录 · 时间按北京时间填写</p></div><button type="button" aria-label="关闭手工信息补录">×</button></header><div class="manual-dialog-body"></div>`;
        document.body.append(this.dialog);
        this.dialog.querySelector('header button').addEventListener('click', () => this.close());
        this.dialog.addEventListener('keydown', event => {
            if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); this.close(); }
        });
        this.dialog.addEventListener('close', () => { if (!this.dialog.open) this.restore(); });
        const header = this.dialog.querySelector('header');
        header.addEventListener('pointerdown', event => {
            if (event.button !== 0 || event.isPrimary === false || event.target.closest('button')) return;
            const rect = this.dialog.getBoundingClientRect();
            this.drag = {id:event.pointerId, x:event.clientX, y:event.clientY, left:rect.left, top:rect.top};
            header.setPointerCapture(event.pointerId); event.preventDefault();
        });
        header.addEventListener('pointermove', event => {
            if (this.drag?.id === event.pointerId) this.place(this.drag.left + event.clientX - this.drag.x, this.drag.top + event.clientY - this.drag.y);
        });
        this.stopDrag = () => {
            const id = this.drag?.id; this.drag = null;
            if (id !== undefined && header.hasPointerCapture(id)) header.releasePointerCapture(id);
        };
        for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) header.addEventListener(type, this.stopDrag);
        header.addEventListener('keydown', event => {
            const steps = {ArrowLeft:[-20,0], ArrowRight:[20,0], ArrowUp:[0,-20], ArrowDown:[0,20]};
            if (event.target !== header || !steps[event.key]) return;
            event.preventDefault(); const rect = this.dialog.getBoundingClientRect(), [x,y] = steps[event.key];
            this.place(rect.left + x, rect.top + y);
        });
        window.addEventListener('resize', () => {
            if (this.dialog.open) { const rect = this.dialog.getBoundingClientRect(); this.place(rect.left, rect.top); }
        });
        this.observer = new MutationObserver(() => {
            if (!this.dialog.open) return;
            if (!document.getElementById('page-decision').classList.contains('active')) this.close();
            else this.syncTheme();
        });
        for (const id of ['page-decision', 'decision-workspace']) this.observer.observe(document.getElementById(id), {attributes:true, attributeFilter:['class']});
    },
    open() {
        if (!document.getElementById('page-decision')?.classList.contains('active')) return;
        this.ensure(); this.syncTheme();
        if (this.dialog.open) { this.dialog.querySelector('header').focus(); return; }
        this.dialog.querySelector('.manual-dialog-body').append(this.panel);
        this.dialog.show();
        const rect = this.dialog.getBoundingClientRect();
        this.place(this.position?.left ?? window.innerWidth - rect.width - 24, this.position?.top ?? 80);
        ManualMeasurements.refresh();
        (ManualMeasurements.busy ? this.dialog.querySelector('header') : ManualMeasurements.el('value')).focus({preventScroll:true});
    },
    close() {
        if (!this.dialog?.open) return;
        this.dialog.close(); this.restore();
        if (document.getElementById('page-decision').classList.contains('active')) document.getElementById('decision-manual-open')?.focus({preventScroll:true});
    },
    restore() { this.stopDrag(); this.anchor.after(this.panel); },
    syncTheme() { this.dialog.classList.toggle('is-light', document.getElementById('decision-workspace').classList.contains('is-light')); },
    place(left, top) {
        const rect = this.dialog.getBoundingClientRect(), margin = 12;
        this.position = {left:Math.max(margin, Math.min(left, window.innerWidth - rect.width - margin)), top:Math.max(margin, Math.min(top, window.innerHeight - rect.height - margin))};
        this.dialog.style.left = this.position.left + 'px'; this.dialog.style.top = this.position.top + 'px';
    },
};
document.addEventListener('DOMContentLoaded', () => ManualMeasurements.init());
