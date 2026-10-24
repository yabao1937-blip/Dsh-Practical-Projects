/* Reusable manual-entry views; one draft/save state, no App.store mutations. */
const ManualMeasurementComponent = {
    mount(root, prefix, controller) {
        if (!root || !/^[a-z][a-z0-9-]*$/.test(prefix)) throw new Error('补录组件挂载位置无效');
        root.classList.add('manual-measurements-component');
        root.dataset.manualComponent = prefix;
        const id = name => `${prefix}-${name}`;
        root.innerHTML = `<div class="panel-header"><h3>业务指标补录</h3><button type="button" class="btn btn-sm" id="${id('refresh')}">刷新补录记录</button></div>
            <div class="panel-body"><form id="${id('form')}">
                <p id="${id('mode')}" class="measurement-help" hidden></p>
                <fieldset id="${id('fields')}" class="measurement-fields">
                    <div class="form-group"><label for="${id('time')}">时间（北京时间，可留空）</label><input type="datetime-local" id="${id('time')}" step="1"><p class="measurement-help">留空时使用提交时的服务器当前时间。</p></div>
                    <div class="form-group"><label for="${id('metric')}">补录信息名</label><select id="${id('metric')}" required><option value="">正在加载补录信息…</option></select><p id="${id('purpose')}" class="measurement-help" hidden></p></div>
                    <div class="form-group"><label for="${id('value')}">值 <span id="${id('unit')}"></span></label><input type="number" id="${id('value')}" min="0" step="any" required placeholder="请输入数值"></div>
                </fieldset>
                <div class="form-actions"><button type="submit" class="btn btn-primary" id="${id('submit')}" disabled>保存补录</button><button type="button" class="btn" id="${id('reset')}">重置</button></div>
                <p id="${id('message')}" class="measurement-message" role="status" aria-live="polite"></p>
            </form>
            <div class="measurement-history-heading"><h4>业务指标补录记录</h4><label>信息名 <select id="${id('filter')}"><option value="">全部信息</option></select></label></div>
            <p class="measurement-help">按录入顺序显示，最新补录在前。</p>
            <div class="table-scroll measurement-history-scroll"><table class="data-table"><thead><tr><th>时间（北京时间）</th><th>补录信息名</th><th>值</th><th>单位</th><th>录入时间</th></tr></thead><tbody id="${id('history')}"><tr><td colspan="5">尚未加载补录记录</td></tr></tbody></table></div>
            <div class="measurement-pagination"><span id="${id('count')}" aria-live="polite"></span><div><button type="button" class="btn btn-sm" id="${id('prev')}" disabled>上一页</button><button type="button" class="btn btn-sm" id="${id('next')}" disabled>下一页</button></div></div></div>`;
        const view = {root, prefix, el:name => document.getElementById(id(name)), definitions:null, history:null};
        view.el('form').addEventListener('submit', event => controller.submit(event, view));
        for (const field of ['metric', 'value', 'time']) {
            view.el(field).addEventListener(field === 'metric' ? 'change' : 'input', () => controller.capture(view));
        }
        view.el('refresh').addEventListener('click', () => controller.refresh());
        view.el('filter').addEventListener('change', () => {
            controller.filter=view.el('filter').value; controller.offset=0; controller.render(); void controller.loadHistory();
        });
        view.el('prev').addEventListener('click', () => {controller.offset=Math.max(0,controller.offset-controller.pageSize);void controller.loadHistory();});
        view.el('next').addEventListener('click', () => {controller.offset+=controller.pageSize;void controller.loadHistory();});
        view.el('reset').addEventListener('click', () => controller.reset());
        return view;
    },
};

const ManualMeasurements = {
    ready:false, busy:false, initialized:false, definitions:[], definitionsPromise:null,
    offset:0, pageSize:20, total:0, sequence:0, pending:null, views:[],
    draft:{metric:'',value:'',time:''}, filter:'', notice:{text:'',status:''},
    history:'<tr><td colspan="5">尚未加载补录记录</td></tr>', count:'', historyLoaded:false,
    current() { return DecisionManualEntry.dialog?.open ? DecisionManualEntry.view : this.views[0]; },
    el(id) { return this.current()?.el(id); },
    active() { return !document.hidden && !!DecisionManualEntry.dialog?.open; },
    escape(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    time(value) {
        const date = new Date(value);
        return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', {timeZone:'Asia/Shanghai',year:'numeric',
            month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).format(date).replace(/\//g,'-') : '—';
    },
    readOnly() { return typeof Api !== 'undefined' && !!Api.isReadOnlyTest?.(); },
    writable() { return ['http:','https:'].includes(location.protocol) && !this.readOnly(); },
    mount(root, prefix) {
        const existing=this.views.find(view => view.root === root);
        if (existing) return existing;
        const view=ManualMeasurementComponent.mount(root, prefix, this);
        this.views.push(view); this.render(); return view;
    },
    render() {
        const writable=this.writable(), definition=this.definitions.find(item => item.key === this.draft.metric);
        for (const view of this.views) {
            const el=view.el;
            if (view.definitions !== this.definitions) {
                const options=this.definitions.map(item => `<option value="${this.escape(item.key)}">${this.escape(item.name)}</option>`).join('');
                el('metric').innerHTML=options || '<option value="">正在加载补录信息…</option>';
                el('filter').innerHTML='<option value="">全部信息</option>'+options;
                view.definitions=this.definitions;
            }
            for (const field of ['metric','value','time']) if (el(field).value !== this.draft[field]) el(field).value=this.draft[field];
            el('filter').value=this.filter;
            el('unit').textContent=definition ? `（${definition.unit}）` : '';
            const heavy=definition?.key === 'heavy_clean_coal_ash';
            el('purpose').hidden=!definition?.archive_only && !heavy;
            el('purpose').textContent=definition?.archive_only ? '仅保存记录，不参与实时计算。' :
                heavy ? '填写实际生产所属时间，不再减两小时；同一小时取最新采样，晚补后更新小时灰分及后续沿用值。' : '';
            if (definition?.maximum != null) el('value').max=definition.maximum;
            else el('value').removeAttribute('max');
            el('fields').disabled=this.busy || !writable;
            el('submit').disabled=this.busy || !this.ready || !writable;
            el('reset').disabled=this.busy || !writable;
            el('submit').textContent=this.busy ? '正在保存…' : '保存补录';
            el('mode').hidden=writable;
            el('mode').textContent=this.readOnly() ? Api.readOnlyMessage : '请通过服务器网页打开此页面，才能将补录保存到数据库。';
            el('message').textContent=this.notice.text; el('message').dataset.status=this.notice.status;
            if (view.history !== this.history) { el('history').innerHTML=this.history; view.history=this.history; }
            el('count').textContent=this.count;
            el('prev').disabled=!this.historyLoaded || this.offset <= 0;
            el('next').disabled=!this.historyLoaded || this.offset+this.pageSize >= this.total;
        }
    },
    capture(view=this.current()) {
        if (!view || this.busy) return;
        this.draft={metric:view.el('metric').value, value:view.el('value').value, time:view.el('time').value};
        this.render();
    },
    message(text, status='') { this.notice={text,status}; this.render(); },
    async refresh() {
        if (!['http:','https:'].includes(location.protocol)) {
            this.message('请通过服务器网页打开此页面，才能将补录保存到数据库。','error'); return;
        }
        try {
            if (!this.ready && !this.definitionsPromise) this.definitionsPromise=(async () => {
                this.definitions=(await Api.getManualMeasurementMetrics()).filter(item => item.key !== 'coarse_slime_amount');
                if (!this.definitions.some(item => item.key === this.draft.metric)) this.draft.metric=this.definitions[0]?.key || '';
                this.ready=true; this.message('');
            })().finally(() => {this.definitionsPromise=null;});
            if (this.definitionsPromise) await this.definitionsPromise;
        } catch (error) { this.message(error.message || '无法连接补录服务，请稍后刷新。','error'); return; }
        this.render(); await this.loadHistory();
    },
    updateUnit() { this.capture(); },
    setBusy(busy) { this.busy=busy; this.render(); },
    reset() {
        if (this.busy || !this.writable()) return;
        this.draft={...this.draft,value:'',time:''}; this.pending=null; this.message('');
    },
    newId() {
        return typeof globalThis.crypto?.randomUUID === 'function' ? crypto.randomUUID()
            : 'manual-'+Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)+Math.random().toString(36).slice(2);
    },
    async submit(event, view=this.current()) {
        event?.preventDefault();
        if (this.busy) return;
        if (!this.writable()) { this.message(this.readOnly() ? Api.readOnlyMessage : '请通过服务器网页保存补录。','error'); return; }
        if (!this.ready) return;
        this.capture(view);
        const raw=this.draft.value.trim(), value=Number(raw), definition=this.definitions.find(item => item.key === this.draft.metric);
        if (!raw || !Number.isFinite(value) || value < 0 || !definition || (definition.maximum != null && value > definition.maximum)) {
            this.message('请输入有效数值：百分比为 0–100，煤量不得为负。','error'); return;
        }
        const payload={metric:definition.key,value,observed_at:this.draft.time ? this.draft.time+'+08:00' : null};
        const signature=JSON.stringify(payload);
        if (this.pending?.signature !== signature) this.pending={signature,id:this.newId()};
        this.setBusy(true); this.message('正在保存到数据库…');
        try {
            const record=await Api.saveManualMeasurement({...payload,client_id:this.pending.id}, definition);
            this.pending=null; this.draft={...this.draft,value:'',time:''};
            this.message(`已保存到数据库：${record.name} ${record.value} ${record.unit}，时间 ${this.time(record.observed_at)}${definition.archive_only?'（仅保存记录，不参与实时计算）':''}`,'success');
            this.offset=0; this.filter='';
            if (window.Operations) window.Operations.manualMeasurementSaved();
            if (definition.key === 'heavy_clean_coal_ash') {
                if (typeof DecisionRecentAsh !== 'undefined' && typeof DecisionOverviewPage !== 'undefined')
                    void DecisionRecentAsh.measurementSaved(DecisionOverviewPage);
                if (typeof DecisionAshHistory !== 'undefined') void DecisionAshHistory.measurementSaved();
            }
            await this.loadHistory();
        } catch (error) { this.message(error.message || '未收到保存确认，已保留填写内容，请刷新记录核对后重试。','error'); }
        finally { this.setBusy(false); }
    },
    async loadHistory() {
        const sequence=++this.sequence, offset=this.offset, metric=this.filter;
        try {
            const result=await Api.getManualMeasurements({offset,metric,limit:this.pageSize});
            if (sequence !== this.sequence) return;
            this.total=result.total; this.historyLoaded=true;
            this.history=result.items.length ? result.items.map(row => `<tr><td>${this.escape(this.time(row.observed_at))}</td><td>${this.escape(row.name)}</td><td>${this.escape(row.value)}</td><td>${this.escape(row.unit)}</td><td>${this.escape(this.time(row.created_at))}</td></tr>`).join('') : '<tr><td colspan="5">暂无业务指标补录记录</td></tr>';
            this.count=result.total ? `共 ${result.total} 条，显示第 ${offset+1}–${offset+result.items.length} 条` : '共 0 条';
        } catch (error) {
            if (sequence !== this.sequence) return;
            this.history=`<tr><td colspan="5">${this.escape(error.message || '补录记录加载失败，请稍后刷新。')}</td></tr>`;
            this.count='记录加载失败'; this.historyLoaded=false;
        }
        this.render();
    },
    init() {
        if (this.initialized) return;
        this.initialized=true;
        DecisionManualEntry.init();
        document.addEventListener('visibilitychange', () => {if(this.active()) void this.refresh();});
        if (this.active()) void this.refresh();
    },
};

// The floating window owns its component. It never relocates collection-page DOM.
const DecisionManualEntry = {
    dialog:null, panel:null, view:null, position:null, drag:null, initialized:false,
    init() {
        if (this.initialized) return;
        this.initialized=true;
        document.getElementById('decision-manual-open')?.addEventListener('click', () => this.open());
    },
    ensure() {
        if (this.dialog) return;
        this.dialog=document.createElement('dialog');
        this.dialog.id='decision-manual-dialog'; this.dialog.className='decision-manual-dialog';
        this.dialog.setAttribute('aria-labelledby','decision-manual-title');
        this.dialog.innerHTML='<header class="manual-dialog-header" tabindex="0" aria-label="拖动标题栏或按方向键移动窗口"><div><h2 id="decision-manual-title">手工信息补录</h2><p>时间按北京时间填写 · 关闭窗口保留未提交内容</p></div><button type="button" aria-label="关闭手工信息补录">×</button></header><div class="manual-dialog-body"></div>';
        document.body.append(this.dialog);
        this.panel=document.createElement('div'); this.panel.id='decision-manual-measurements-panel'; this.panel.className='panel';
        this.dialog.querySelector('.manual-dialog-body').append(this.panel);
        this.view=ManualMeasurements.mount(this.panel,'decision-measurement');
        this.dialog.querySelector('header button').addEventListener('click', () => this.close());
        this.dialog.addEventListener('keydown', event => {
            if (event.key === 'Escape') {event.preventDefault();event.stopPropagation();this.close();}
        });
        this.dialog.addEventListener('close', () => this.stopDrag());
        const header=this.dialog.querySelector('header');
        header.addEventListener('pointerdown', event => {
            if (event.button !== 0 || event.isPrimary === false || event.target.closest('button')) return;
            const rect=this.dialog.getBoundingClientRect();
            this.drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:rect.left,top:rect.top};
            header.setPointerCapture(event.pointerId);event.preventDefault();
        });
        header.addEventListener('pointermove', event => {
            if (this.drag?.id === event.pointerId) this.place(this.drag.left+event.clientX-this.drag.x,this.drag.top+event.clientY-this.drag.y);
        });
        this.stopDrag=() => {
            const id=this.drag?.id;this.drag=null;
            if (id !== undefined && header.hasPointerCapture(id)) header.releasePointerCapture(id);
        };
        for (const type of ['pointerup','pointercancel','lostpointercapture']) header.addEventListener(type,this.stopDrag);
        header.addEventListener('keydown', event => {
            const steps={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]};
            if (event.target !== header || !steps[event.key]) return;
            event.preventDefault();const rect=this.dialog.getBoundingClientRect(),[x,y]=steps[event.key];
            this.place(rect.left+x,rect.top+y);
        });
        window.addEventListener('resize', () => {if(this.dialog.open){const rect=this.dialog.getBoundingClientRect();this.place(rect.left,rect.top);}});
        this.observer=new MutationObserver(() => {
            if (!this.dialog.open) return;
            if (!document.getElementById('page-decision')?.classList.contains('active')) this.close();
            else this.syncTheme();
        });
        for (const id of ['page-decision','decision-workspace']) {
            const element=document.getElementById(id);
            if (element) this.observer.observe(element,{attributes:true,attributeFilter:['class']});
        }
    },
    open() {
        if (!document.getElementById('page-decision')?.classList.contains('active')) return;
        ManualMeasurements.capture(); this.ensure(); this.syncTheme();
        if (this.dialog.open) {this.dialog.querySelector('header').focus();return;}
        this.dialog.show();
        const rect=this.dialog.getBoundingClientRect();
        this.place(this.position?.left ?? window.innerWidth-rect.width-24,this.position?.top ?? 80);
        void ManualMeasurements.refresh();
        (ManualMeasurements.busy || !ManualMeasurements.writable() ? this.dialog.querySelector('header') : this.view.el('value')).focus({preventScroll:true});
    },
    close() {
        if (!this.dialog?.open) return;
        this.dialog.close(); this.stopDrag();
        if (document.getElementById('page-decision')?.classList.contains('active')) document.getElementById('decision-manual-open')?.focus({preventScroll:true});
    },
    syncTheme() {this.dialog.classList.toggle('is-light',!!document.getElementById('decision-workspace')?.classList.contains('is-light'));},
    place(left,top) {
        const rect=this.dialog.getBoundingClientRect(),margin=12;
        this.position={left:Math.max(margin,Math.min(left,window.innerWidth-rect.width-margin)),top:Math.max(margin,Math.min(top,window.innerHeight-rect.height-margin))};
        this.dialog.style.left=this.position.left+'px';this.dialog.style.top=this.position.top+'px';
    },
};
document.addEventListener('DOMContentLoaded', () => ManualMeasurements.init());
