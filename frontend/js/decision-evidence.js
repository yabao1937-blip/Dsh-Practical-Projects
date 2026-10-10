/* GPT-only evidence, never updates App.store, a model, or a device. */
const DecisionEvidence = {
    dialog:null, seq:0, selected:null, result:null, offset:0, start:'', mode:'segments', busy:false,
    drafts:new Map(), requests:new Map(),
    labels:{raw_ash:'原煤灰分',coal_amount:'入洗原煤量',level:'精磁尾液位',density:'实测密度',heavy_ash:'重介精煤灰分',float_ash:'浮精灰分',coarse_ash:'粗精煤泥灰分',coarse_amount:'粗精煤泥量',heavy_amount:'重介精煤量',float_amount:'浮精量',total_ash:'总灰分',belt501:'501 带煤量',belt502:'502 带煤量',reagent:'药剂量',raw_slime:'原煤煤泥含量',suggested_density:'建议密度'},
    esc(value) {return DecisionAshHistory.escape(value);},
    num(value,d=3) {return Number.isFinite(value)?value.toFixed(d):'—';},
    statusLabel(value) {return {good:'有效采集',held:'沿用输入 · 仅供观察',stopped:'设备停机',missing:'无有效值',manual:'手工补录',historical:'历史值',default:'默认值',predicted:'模型预测',calculated:'计算值'}[value]||value||'—';},
    time(value) {return value?new Date(value).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}):'—';},
    uuid() {return globalThis.crypto?.randomUUID?.() || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const v=Math.random()*16|0;return (c==='x'?v:(v&3)|8).toString(16);});},
    ensure() {
        if(this.dialog) return;
        const d=document.createElement('dialog'); this.dialog=d;
        d.className='decision-ash-dialog decision-evidence-dialog';d.id='decision-evidence-dialog';
        d.setAttribute('aria-labelledby','evidence-title');
        d.innerHTML=`<header class="ash-dialog-header" tabindex="0"><div><h2 id="evidence-title">工况与调密记录</h2><span>拖动标题栏移动 · 北京时间</span></div><button type="button" data-e-close aria-label="关闭工况记录">×</button></header>
          <div class="ash-dialog-body"><nav class="evidence-tabs"><button data-e-tab="segments">工况分段</button><button data-e-tab="samples">逐次明细</button><button data-e-tab="actions">调密跟踪</button></nav>
          <div class="evidence-controls"><select id="evidence-hour" aria-label="选择统计小时"><option value="">本小时 · 进行中</option></select><button data-e-refresh>刷新记录</button></div>
          <p id="evidence-status" role="status" aria-live="polite"></p><div id="evidence-content"></div></div>`;
        document.body.append(d);
        d.addEventListener('click',e=>{
            if(e.target.closest('[data-e-close]')) {this.saveDraft();d.close();return;}
            if(this.busy) return;
            const tab=e.target.closest('[data-e-tab]');
            if(tab) {this.saveDraft();this.mode=tab.dataset.eTab;this.offset=0;this.selected=null;this.load();}
            if(e.target.closest('[data-e-refresh]')) {this.saveDraft();this.load();}
            const action=e.target.closest('[data-e-action]');if(action) {this.saveDraft();this.selected=action.dataset.eAction;this.loadAction();}
            if(e.target.closest('[data-e-back]')) {this.saveDraft();this.selected=null;this.load();}
            const sample=e.target.closest('[data-e-sample]');if(sample) this.loadSample(sample.dataset.eSample);
            const page=e.target.closest('[data-e-page]');if(page) {this.offset=Math.max(0,this.offset+Number(page.dataset.ePage));this.load();}
        });
        d.addEventListener('submit',e=>{e.preventDefault();if(e.target.matches('[data-e-form]')) this.submit(e.target);});
        d.addEventListener('input',()=>this.saveDraft());
        d.querySelector('#evidence-hour').addEventListener('change',e=>{this.start=e.target.value;this.offset=0;this.load();});
        d.addEventListener('keydown',e=>{if(e.key==='Escape') {e.preventDefault();e.stopPropagation();this.saveDraft();d.close();}});
        d.addEventListener('close',()=>{this.seq++;});
        const header=d.querySelector('header');let drag;
        const move=(x,y)=>{d.style.left=Math.max(8,Math.min(x,innerWidth-d.offsetWidth-8))+'px';d.style.top=Math.max(8,Math.min(y,innerHeight-d.offsetHeight-8))+'px';};
        header.addEventListener('pointerdown',e=>{if(e.button!==0||e.target.closest('button'))return;const r=d.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX,y:e.clientY,left:r.left,top:r.top};header.setPointerCapture(e.pointerId);e.preventDefault();});
        header.addEventListener('pointermove',e=>{if(drag?.id===e.pointerId)move(drag.left+e.clientX-drag.x,drag.top+e.clientY-drag.y);});
        for(const type of ['pointerup','pointercancel','lostpointercapture'])header.addEventListener(type,()=>{drag=null;});
        header.addEventListener('keydown',e=>{const xy={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]}[e.key];if(e.target===header&&xy){e.preventDefault();const r=d.getBoundingClientRect();move(r.left+xy[0],r.top+xy[1]);}});
        window.addEventListener('resize',()=>{if(d.open){const r=d.getBoundingClientRect();move(r.left,r.top);}});
        new MutationObserver(()=>{
            if(!d.open)return;
            if(!this.page.active()||this.page.view!=='gpt'){this.saveDraft();d.close();}
            d.classList.toggle('is-light',document.getElementById('decision-workspace').classList.contains('is-light'));
        }).observe(document.getElementById('page-decision'),{subtree:true,attributes:true,attributeFilter:['class','aria-pressed']});
    },
    async open(page) {
        if(page.view!=='gpt')return;
        this.ensure();this.page=page;
        if(this.dialog.open){this.dialog.querySelector('header').focus();return;}
        this.dialog.classList.toggle('is-light',document.getElementById('decision-workspace').classList.contains('is-light'));
        this.dialog.show();this.dialog.style.left=Math.max(8,innerWidth-this.dialog.offsetWidth-24)+'px';this.dialog.style.top='72px';
        const generation=++this.seq;
        try {
            const hours=await Api.decisionEvidence('/hours');
            if(!this.dialog.open||generation!==this.seq)return;
            this.serverStart=hours.current_start;
            const select=this.dialog.querySelector('#evidence-hour');
            select.innerHTML='<option value="">本小时 · 进行中</option>'+hours.items.map(h=>`<option value="${this.esc(h.start)}">${this.esc(this.time(h.start))} · ${h.segment_count} 段${h.policy==='decision-hourly-v1'?'（旧口径）':''}</option>`).join('');
            if(this.page.hourMode())this.start=DecisionHourly.presentation()?.period.start||'';
            select.value=this.start;if(select.selectedIndex<0){this.start='';select.value='';}
            this.load();
        }catch(error){if(this.dialog.open&&generation===this.seq)this.status(error.message);}
    },
    status(message) {this.dialog.querySelector('#evidence-status').textContent=message;},
    content(html) {this.dialog.querySelector('#evidence-content').innerHTML=html;},
    hourStart() {return this.start || this.serverStart;},
    async load() {
        if(this.busy)return;
        const seq=++this.seq;this.status('正在读取…');
        this.dialog.querySelector('#evidence-hour').hidden=this.mode==='actions';
        for(const b of this.dialog.querySelectorAll('[data-e-tab]'))b.setAttribute('aria-pressed',String(b.dataset.eTab===this.mode));
        try {
            if(this.mode==='actions'&&this.selected){await this.loadAction();return;}
            if(!this.start && this.mode==='samples') {
                const hours=await Api.decisionEvidence('/hours?limit=1');
                if(seq!==this.seq||!this.dialog.open)return;
                this.serverStart=hours.current_start;
            }
            const path=this.mode==='segments'?'/segments'+(this.start?'?start='+encodeURIComponent(this.start):''):
                this.mode==='samples'?'/samples?start='+encodeURIComponent(this.hourStart())+'&offset='+this.offset:'/actions?limit=20&offset='+this.offset;
            const result=await Api.decisionEvidence(path);
            if(seq!==this.seq||!this.dialog.open)return;
            this.result=result;this.status('');
            if(this.mode==='segments')this.renderSegments(result);
            else if(this.mode==='samples')this.renderSamples(result);
            else this.renderActions(result);
        } catch(error){if(seq===this.seq){this.content('');this.status(error.message);}}
    },
    renderSegments(r) {
        const e=r.evidence,segments=e?.segments||[];
        const labels={raw_ash:'原煤灰分',coal_amount:'入洗量',level:'精磁尾液位',density:'实测密度',heavy_ash:'重介灰分',float_ash:'浮精灰分',coarse_ash:'粗精煤泥灰分',heavy_amount:'重介煤量',float_amount:'浮精量',coarse_amount:'粗精煤泥量',total_ash:'总灰分'};
        this.content(`<p class="evidence-note">${this.esc(this.time(r.period.start))} — ${this.esc(this.time(r.period.end))}<br>${r.frozen?'已保存小时':'本小时记录尚在积累'} · ${segments.length} 段</p><p class="evidence-note">${this.esc(e?.segmentation.note||r.reason||'暂无逐次记录')}<br>阈值为初始设置；开停机、组合、质量或来源变化立即切段。段内开关保留真实状态。</p>
          ${segments.map((s,i)=>`<details class="evidence-segment"><summary><b>${i+1}. ${this.esc(this.time(s.start).split(' ').pop())} — ${this.esc(this.time(s.end).split(' ').pop())}</b><span>${this.num(s.duration_seconds/60,1)} 分钟 · 灰分 ${this.num(s.total_ash.value,2)} %</span></summary><p>${this.esc(s.start_reason)}</p><p>观测产量 ${this.num(s.total_ash.observed_mass_t)} t · 灰分配对 ${this.num(s.total_ash.mass_t)} t<br>流量有效 ${this.num(s.valid_seconds,0)} 秒 / ${this.num(s.duration_seconds,0)} 秒<br>${Object.entries(s.total_ash.excluded_reasons||{}).map(([reason,n])=>this.esc(reason)+'：'+n+' 次').join('；')}</p><p>${Object.entries(s.state.switches).map(([group,parts])=>Object.entries(parts).map(([name,v])=>this.esc(name)+' '+(v===null?'未知':v?'运行':'停止')).join(' · ')).join('<br>')}</p><div class="evidence-table-scroll"><table><thead><tr><th>因素</th><th>均值 / 产量加权</th><th>波动 / 趋势</th></tr></thead><tbody>${Object.entries(s.factors).map(([key,v])=>`<tr><td>${labels[key]||this.esc(key)}</td><td>${this.num(v.mean)} / ${this.num(v.mass_mean)}<small>${this.esc(v.unit)}${v.manual_record_ids?.length?' · '+v.manual_record_ids.length+' 条独立补录':''}</small></td><td>σ ${this.num(v.stddev)}<br>${this.esc(v.direction||'—')} ${this.num(v.change)}<small>${this.num(v.minimum)}～${this.num(v.maximum)}</small></td></tr>`).join('')}</tbody></table></div><button data-e-sample="${s.first_slot}">查看段首逐次记录</button> <button data-e-sample="${s.last_slot}">段末记录</button></details>`).join('') || '<p>暂无可分段记录；新数据采集后会出现。</p>'}
          <p class="evidence-note">专家经验建议 · 非实验验证的达标区间。等待响应实验；区间宽度不设 ±0.03 限制，单次执行仍限 ±0.03。暂不自动学习或预测下一工况。</p>`);
    },
    pager(total,step) {return `<div class="evidence-pager"><button data-e-page="-${step}" ${this.offset===0?'disabled':''}>上一页</button><span>${this.offset+1}–${Math.min(this.offset+step,total)} / ${total}</span><button data-e-page="${step}" ${this.offset+step>=total?'disabled':''}>下一页</button></div>`;},
    renderSamples(r) {
        this.content(`<p class="evidence-note">每 5 秒最多保存一次；点击时间查看当时输入、来源、质量、模型和保护条件。历史不会用新模型重算。</p><table><thead><tr><th>时间</th><th>实测 / 建议密度</th><th>总灰分</th></tr></thead><tbody>${r.items.map(x=>`<tr><td><button data-e-sample="${x.slot}">${this.esc(this.time(x.sampled_at))}</button></td><td>${this.num(x.values.density)} / ${this.num(x.values.suggested_density)}</td><td>${this.num(x.values.total_ash,2)} %<small>${this.esc(this.statusLabel(x.statuses?.total_ash))}</small></td></tr>`).join('')}</tbody></table>${r.total?this.pager(r.total,40):'<p>该小时暂无逐次记录。</p>'}<div id="evidence-sample-detail"></div>`);
    },
    async loadSample(slot) {
        const seq=++this.seq;this.status('正在读取逐次明细…');
        try {
            const r=await Api.decisionEvidence('/samples/'+slot);
            if(seq!==this.seq||!this.dialog.open)return;
            this.status('');
            let target=this.dialog.querySelector('#evidence-sample-detail');
            if(!target){target=document.createElement('div');target.id='evidence-sample-detail';this.dialog.querySelector('#evidence-content').append(target);}
            target.innerHTML=`<h3>逐次明细 · ${this.esc(this.time(r.sampled_at))}</h3><p>模型：${this.esc(r.snapshot.model?.method)} · 目标 ${this.esc(r.snapshot.target?.min)}–${this.esc(r.snapshot.target?.max)} %</p><table><thead><tr><th>指标</th><th>采用值 / 状态</th><th>来源时间</th></tr></thead><tbody>${Object.entries(r.snapshot.cells).filter(([,c])=>!c.components).map(([k,c])=>`<tr><td>${this.esc(this.labels[k]||k)}</td><td>${this.num(c.value)} ${this.esc(c.unit)}<small>${this.esc(this.statusLabel(c.status))} · ${this.esc(c.reason)}</small></td><td>${this.esc(this.time(c.observed_at))}<small>${this.esc(c.source)} ${this.esc(c.item_id)}</small></td></tr>`).join('')}</tbody></table><details><summary>完整追溯数据（原始值、批次、模型口径）</summary><pre>${this.esc(JSON.stringify(r,null,2))}</pre></details>`;
            target.scrollIntoView({block:'nearest',behavior:'smooth'});
        }catch(error){if(seq===this.seq)this.status(error.message);}
    },
    renderActions(r) {
        this.content(`<p class="evidence-note">自动记录 A303/A304 设定值变化，待人工确认操作来源及建议采纳情况；只读采集，不下发设备指令。</p><table><thead><tr><th>发现时间 / 设备</th><th>设定变化</th><th></th></tr></thead><tbody>${r.items.map(x=>`<tr><td>${this.esc(this.time(x.detected_at))}<small>${this.esc(x.channel)}</small></td><td>${this.num(x.before)} → ${this.num(x.after)}</td><td><button data-e-action="${this.esc(x.id)}">跟踪</button></td></tr>`).join('')}</tbody></table>${r.total?this.pager(r.total,20):'<p>尚未检测到设定值变化。功能启用后的变化会自动记录。</p>'}`);
    },
    saveDraft() {
        if(this.busy||!this.selected||!this.dialog)return;
        const forms=this.dialog.querySelectorAll('[data-e-form]');if(!forms.length)return;
        this.drafts.set(this.selected,Array.from(forms,f=>[f.dataset.eForm,Object.fromEntries(new FormData(f))]));
    },
    async loadManualOptions(id,seq) {
        try {
            const r=await Api.getManualMeasurements({metric:'heavy_clean_coal_ash',limit:100});
            if(seq!==this.seq||id!==this.selected||!this.dialog.open)return;
            const select=this.dialog.querySelector('[data-e-manual]');if(!select)return;
            select.innerHTML='<option value="">独立记录（不关联补录）</option>'+r.items.map(x=>`<option value="${x.id}">#${x.id} · ${this.esc(this.time(x.observed_at))} · ${this.num(x.value,2)} %</option>`).join('');
            select.addEventListener('change',()=>{
                const form=select.form,row=r.items.find(x=>String(x.id)===select.value);
                form.elements.manual_measurement_id.value=row?.id||'';
                if(row){const local=new Date(Date.parse(row.observed_at)+8*3600000).toISOString().slice(0,19);
                    form.elements.value.value=row.value;form.elements.sample_start.value=local;form.elements.sample_end.value=local;}
                this.saveDraft();
            });
        }catch(error){if(seq===this.seq)this.status('补录列表读取失败；可保留独立化验记录，或刷新重试。');}
    },
    async loadAction() {
        const id=this.selected,seq=++this.seq;this.status('正在读取跟踪单…');
        try {
            const r=await Api.decisionEvidence('/actions/'+encodeURIComponent(id));
            if(seq!==this.seq||id!==this.selected||!this.dialog.open)return;
            this.result=r;this.status('');const b=r.evidence.before,a=r.evidence.after;
            this.content(`<button data-e-back>← 返回记录</button><h3>${this.esc(r.channel)} · ${this.num(b.pair.setpoint.value)} → ${this.num(a.pair.setpoint.value)}</h3><p>变化时间：${this.esc(this.time(b.sampled_at))} — ${this.esc(this.time(a.sampled_at))}<br>实测：${this.num(b.pair.actual.value)} → ${this.num(a.pair.actual.value)}<br>变化前建议：${this.num(b.suggestion?.value)}（须人工确认是否采用）</p><button data-e-sample="${b.slot}">调前决策</button> <button data-e-sample="${a.slot}">调后决策</button><p class="evidence-note">${r.warnings.map(x=>this.esc(x)).join('<br>')}</p>
              <details open><summary>确认操作</summary><form data-e-form="confirmation"><label>操作来源<select name="origin"><option value="unknown">未知 / 待确认</option><option value="manual">人工调整</option><option value="automatic">自动调整</option></select></label><label>是否采用建议<select name="adoption"><option value="unknown">无法确定</option><option value="adopted">采用</option><option value="partial">部分采用</option><option value="not_adopted">未采用</option></select></label><label>准确操作时间（可留空）<input name="occurred_at" type="datetime-local" step="1"></label><label>操作员<input name="operator" maxlength="80"></label><label>备注 / 未采用原因<textarea name="note" maxlength="2000"></textarea></label><button type="submit">保存确认</button></form></details>
              <details><summary>关联重介灰分化验</summary><form data-e-form="assay"><label>样品关系<select name="phase"><option value="before">调前样</option><option value="after">调后样</option></select></label><label>已有补录<select data-e-manual><option value="">独立记录（不关联补录）</option></select></label><label>样品编号<input name="sample_code" maxlength="100" required></label><label>灰分 (%)<input name="value" type="number" min="0" max="100" step="any"></label><label>关联补录编号（可留空）<input name="manual_measurement_id" type="number" min="1"></label><label>样品类型<select name="sample_type"><option value="grab">瞬时样（起止相同）</option><option value="composite">混合样 / 区间样</option></select></label><label>采样开始<input name="sample_start" type="datetime-local" step="1" required></label><label>采样结束<input name="sample_end" type="datetime-local" step="1" required></label><label>出报告时间<input name="reported_at" type="datetime-local" step="1" required></label><label>采样位置<input name="sample_location" maxlength="200" required></label><label>记录人<input name="operator" maxlength="80"></label><label>备注<textarea name="note" maxlength="2000"></textarea></label><p class="evidence-note">独立跟踪记录，不覆盖当前手工补录值。历史未知出报告时间请先核实，不默认为采样时刻。</p><button type="submit">保存化验关联</button></form></details>
              <h3>追加记录 · ${r.entries.length} 条</h3>${r.entries.map(e=>`<article><b>${e.content.kind==='assay'?`${e.content.phase==='before'?'调前':'调后'}化验 ${this.num(e.content.value,2)} %`:'操作确认'}</b><p>${this.esc(this.time(e.created_at))} · ${this.esc(e.content.operator||'记录人未填写')}</p><p>${this.esc(e.content.note)}</p><details><summary>记录详情</summary><pre>${this.esc(JSON.stringify({...e.content,request:undefined},null,2))}</pre></details></article>`).join('')||'<p>暂无人工确认或化验关联。</p>'}`);
            this.loadManualOptions(id,seq);
            for(const [kind,values] of this.drafts.get(id)||[])for(const [key,value] of Object.entries(values)){const field=this.dialog.querySelector(`[data-e-form="${kind}"] [name="${key}"]`);if(field)field.value=value;}
        }catch(error){if(seq===this.seq)this.status(error.message);}
    },
    async submit(form) {
        if(this.busy||!this.selected)return;
        this.saveDraft();const id=this.selected,kind=form.dataset.eForm,values=Object.fromEntries(new FormData(form));
        const body={kind,...values,revision:this.result.revision};
        for(const key of ['sample_start','sample_end','reported_at','occurred_at'])if(key in body){if(body[key])body[key]+='+08:00';else delete body[key];}
        for(const key of ['value','manual_measurement_id'])if(key in body){if(body[key]!=='')body[key]=Number(body[key]);else delete body[key];}
        const requestKey=id+':'+kind,signature=JSON.stringify({...body,revision:undefined}),prior=this.requests.get(requestKey);
        body.client_id=prior?.signature===signature?prior.client_id:this.uuid();this.requests.set(requestKey,{signature,client_id:body.client_id});
        this.busy=true;for(const b of this.dialog.querySelectorAll('button,select,input,textarea'))b.disabled=true;
        this.status('正在保存…');
        try {
            await Api.decisionEvidence('/actions/'+encodeURIComponent(id)+'/entries',body);
            this.drafts.set(id,(this.drafts.get(id)||[]).filter(([k])=>k!==kind));
            this.busy=false;
            if(this.dialog.open&&this.selected===id){await this.loadAction();this.status('已保存到独立跟踪表。');}
        }catch(error){this.status(error.message+'；输入已保留。可重试，记录更新冲突时先刷新。');}
        finally {this.busy=false;for(const b of this.dialog.querySelectorAll('button,select,input,textarea'))b.disabled=false;}
    },
};
document.addEventListener('click',event=>{if(event.target.closest('[data-evidence-open]'))DecisionEvidence.open(DecisionOverviewPage);});
