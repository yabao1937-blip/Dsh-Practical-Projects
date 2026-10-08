/* Database-backed single-value manual readings. Does not modify App.store or model inputs. */
const ManualMeasurements = {
    ready: false, busy: false, definitions: [], definitionsPromise: null, offset: 0, pageSize: 20, total: 0, sequence: 0, pending: null,
    el(id) { return document.getElementById('measurement-' + id); },
    active() { return document.getElementById('page-collect')?.classList.contains('active') && !document.hidden; },
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
document.addEventListener('DOMContentLoaded', () => ManualMeasurements.init());
