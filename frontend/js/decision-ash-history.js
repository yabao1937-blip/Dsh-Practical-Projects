/* Minute-bounded saved prediction history. Never replay a model in the browser. */
const DecisionAshHistory = {
    dialog:null, requestId:0, controller:null, result:null, page:null, position:null, drag:null,
    escape(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    number(value,digits=2) { return Number.isFinite(value)?value.toFixed(digits):'—'; },
    localParts(stamp) { return new Date(Date.parse(stamp)+8*3600000).toISOString().slice(0,16).split('T'); },
    label(stamp) { return this.localParts(stamp).join(' '); },
    ensure() {
        if(this.dialog) return;
        this.dialog=document.createElement('dialog');
        this.dialog.id='decision-ash-dialog'; this.dialog.className='decision-ash-dialog';
        this.dialog.setAttribute('aria-labelledby','decision-ash-title');
        this.dialog.innerHTML=`<header class="ash-dialog-header" tabindex="0" aria-label="拖动标题栏或按方向键移动窗口"><div><h2 id="decision-ash-title">总灰分 · 区间统计</h2><span>拖动标题栏移动 · 北京时间</span></div><button type="button" data-ash-close aria-label="关闭区间灰分弹窗">×</button></header>
            <div class="ash-dialog-body"><form id="ash-range-form"><fieldset id="ash-range-fields"><div class="ash-range-fields">
            <label>开始时间<span><input type="date" id="ash-start-date" aria-label="开始日期" required><input type="time" id="ash-start-time" aria-label="开始时分" step="60" required></span></label>
            <label>结束时间<span><input type="date" id="ash-end-date" aria-label="结束日期" required><input type="time" id="ash-end-time" aria-label="结束时分" step="60" required></span></label>
            <button type="submit" class="ash-query">查询区间</button></div></fieldset></form>
            <div id="ash-history-status" role="status" aria-live="polite"></div><div id="ash-history-result"></div></div>`;
        document.body.append(this.dialog);
        this.dialog.querySelector('[data-ash-close]').addEventListener('click',()=>this.dialog.close());
        const header=this.dialog.querySelector('.ash-dialog-header');
        header.addEventListener('pointerdown',event=>{
            if(event.button!==0 || event.isPrimary===false || event.target.closest('button')) return;
            const r=this.dialog.getBoundingClientRect();
            this.drag={id:event.pointerId,x:event.clientX,y:event.clientY,left:r.left,top:r.top};
            header.setPointerCapture(event.pointerId); header.classList.add('is-dragging');
            event.preventDefault();
        });
        header.addEventListener('pointermove',event=>{
            if(this.drag?.id!==event.pointerId) return;
            this.place(this.drag.left+event.clientX-this.drag.x,this.drag.top+event.clientY-this.drag.y);
        });
        const stopDrag=()=>{
            const pointer=this.drag?.id; this.drag=null; header.classList.remove('is-dragging');
            if(pointer!==undefined && header.hasPointerCapture(pointer)) header.releasePointerCapture(pointer);
        };
        for(const type of ['pointerup','pointercancel','lostpointercapture']) header.addEventListener(type,stopDrag);
        header.addEventListener('keydown',event=>{
            const steps={ArrowLeft:[-20,0],ArrowRight:[20,0],ArrowUp:[0,-20],ArrowDown:[0,20]};
            if(event.target!==header || !steps[event.key]) return;
            event.preventDefault(); const r=this.dialog.getBoundingClientRect(),[x,y]=steps[event.key];
            this.place(r.left+x,r.top+y);
        });
        this.dialog.addEventListener('keydown',event=>{
            if(event.key==='Escape') {event.preventDefault();event.stopPropagation();this.dialog.close();}
        });
        this.dialog.addEventListener('close',()=>{
            stopDrag();
            this.requestId++; this.controller?.abort(); this.controller=null;
            if(this.page?.active()) document.querySelector('[data-ash-history]:not([hidden])')?.focus({preventScroll:true});
        });
        window.addEventListener('resize',()=>{
            if(this.dialog.open) {const r=this.dialog.getBoundingClientRect();this.place(r.left,r.top);}
        });
        // A modeless window follows the overview theme and closes on navigation.
        this.pageObserver=new MutationObserver(()=>{
            if(!this.dialog.open) return;
            if(!this.page?.active() || this.page.view!=='gpt') {this.dialog.close();return;}
            this.syncTheme();
        });
        for(const node of [document.getElementById('page-decision'),document.getElementById('decision-workspace'),
            document.querySelector('[data-decision-model="production"]')].filter(Boolean))
            this.pageObserver.observe(node,{attributes:true,attributeFilter:['class','aria-pressed']});
        this.dialog.querySelector('#ash-range-form').addEventListener('submit',event=>{
            event.preventDefault(); this.load(this.selectedRange());
        });
        this.dialog.querySelector('#ash-range-fields').addEventListener('change',()=>{
            // An old response must not be presented under newly edited dates.
            this.requestId++; this.controller?.abort(); this.result=null;
            this.dialog.querySelector('#ash-history-result').innerHTML='';
            this.status('时间已更改，请点击“查询区间”。');
        });
    },
    open(page) {
        if(page.view!=='gpt') return;
        this.ensure(); this.page=page;
        this.syncTheme();
        if(this.dialog.open) {this.dialog.querySelector('.ash-dialog-header').focus();return;}
        this.dialog.show();
        const r=this.dialog.getBoundingClientRect();
        this.place(this.position?.left ?? window.innerWidth-r.width-24,this.position?.top ?? 72);
        this.load();
    },
    syncTheme() { this.dialog.classList.toggle('is-light',document.getElementById('decision-workspace').classList.contains('is-light')); },
    place(left,top) {
        const r=this.dialog.getBoundingClientRect(),margin=12;
        this.position={left:Math.max(margin,Math.min(left,window.innerWidth-r.width-margin)),
            top:Math.max(margin,Math.min(top,window.innerHeight-r.height-margin))};
        this.dialog.style.left=this.position.left+'px'; this.dialog.style.top=this.position.top+'px';
    },
    selectedRange() {
        const value=id=>this.dialog.querySelector('#'+id).value;
        return {start:`${value('ash-start-date')}T${value('ash-start-time')}:00+08:00`,
            end:`${value('ash-end-date')}T${value('ash-end-time')}:00+08:00`};
    },
    setRange(period,latest) {
        for(const key of ['start','end']) {
            const [date,time]=this.localParts(period[key]);
            this.dialog.querySelector(`#ash-${key}-date`).value=date;
            this.dialog.querySelector(`#ash-${key}-time`).value=time;
            this.dialog.querySelector(`#ash-${key}-date`).max=this.localParts(latest)[0];
        }
    },
    status(text,error=false) {
        const node=this.dialog.querySelector('#ash-history-status');
        node.textContent=text; node.classList.toggle('is-error',error);
    },
    async load(range) {
        const id=++this.requestId;
        this.controller?.abort(); this.controller=new AbortController();
        const controller=this.controller, timeout=setTimeout(()=>controller.abort(),10000);
        const fields=this.dialog.querySelector('#ash-range-fields');
        fields.disabled=!range;
        this.result=null; this.dialog.querySelector('#ash-history-result').innerHTML='';
        this.status('正在读取区间统计…');
        try {
            const result=await Api.getTotalAshInterval(range || {},controller.signal);
            if(id!==this.requestId || !this.dialog.open) return;
            this.result=result; this.setRange(result.period,result.latest_end);
            this.render(result); this.status('');
        } catch(error) {
            if(id!==this.requestId || !this.dialog.open) return;
            this.status(error.name==='AbortError'?'读取超时，请重新查询。':error.message || '区间灰分读取失败，请重试',true);
        } finally {
            clearTimeout(timeout);
            if(id===this.requestId) {fields.disabled=false;this.controller=null;}
        }
    },
    render(result) {
        const s=result.summary, esc=v=>this.escape(v);
        const rows=[...result.items].reverse().map(item=>{
            const [date,time]=this.localParts(item.start),end=this.localParts(item.end)[1];
            const state={complete:'完整',partial:'部分数据',missing:'无有效数据'}[item.status] || '无有效数据';
            return `<tr><td><strong>${time}–${end}</strong><span>${date}</span></td><td class="ash-table-value">${this.number(item.value)}</td><td title="${esc(item.reason)}"><span class="ash-state ${esc(item.status)}">${state}</span><small>${item.aggregation==='output-mass-v1'?this.number(item.mass_t,2)+' t · 产煤量加权':item.aggregation==='mixed'?'不同统计口径':'旧样本均值'}</small><small>${item.sample_count} 条 · ${this.number(item.coverage*100,1)}%</small></td></tr>`;
        }).join('');
        const averageHint=s.aggregation==='mixed'?'区间含不同统计口径，无法合并平均值':s.value===null?'此区间暂无有效总灰分':result.method;
        this.dialog.querySelector('#ash-history-result').innerHTML=`<section class="ash-summary" aria-label="区间平均灰分"><div><p>区间平均灰分</p><div class="ash-average" title="${esc(averageHint)}">${this.number(s.value)}<span>%</span></div><p class="ash-summary-range">${esc(this.label(result.period.start))} — ${esc(this.label(result.period.end))}</p></div><dl><div><dt>有值时段</dt><dd>${s.valid_segments}<span> / ${s.total_segments} 段</span></dd></div><div><dt>有效样本覆盖</dt><dd>${this.number(s.coverage*100,1)}<span>%</span></dd></div></dl></section>
            <div class="ash-table-title"><h3>分时段平均灰分</h3><span>按小时分段 · 单位 %</span></div>
            <div class="ash-table-scroll" tabindex="0" role="region" aria-label="区间灰分记录"><table><thead><tr><th>统计时段</th><th>灰分 (%)</th><th>数据 / 覆盖</th></tr></thead><tbody>${rows}</tbody></table></div>
            <p class="ash-history-footnote">${result.settlement_policy?'延迟2小时结算 · ': '预测值 · '}精确到分钟 · 悬停数据状态查看说明</p>`;
    },
};
