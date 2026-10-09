/* Frozen hourly prediction history. Never recompute old hours in the browser. */
const DecisionAshHistory = {
    dialog:null, requestId:0, controller:null, result:null, page:null,
    escape(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    number(value,digits=2) { return Number.isFinite(value)?value.toFixed(digits):'—'; },
    localParts(stamp) { return new Date(Date.parse(stamp)+8*3600000).toISOString().slice(0,16).split('T'); },
    label(stamp) { return this.localParts(stamp).join(' '); },
    hours() { return Array.from({length:24},(_,i)=>`<option value="${String(i).padStart(2,'0')}">${String(i).padStart(2,'0')}:00</option>`).join(''); },
    ensure() {
        if(this.dialog) return;
        this.dialog=document.createElement('dialog');
        this.dialog.id='decision-ash-dialog'; this.dialog.className='decision-ash-dialog';
        this.dialog.setAttribute('aria-labelledby','decision-ash-title');
        this.dialog.innerHTML=`<header class="ash-dialog-header"><div><p>QUALITY / HOURLY HISTORY</p><h2 id="decision-ash-title">总灰分 · 小时统计</h2><span>决策总览预测值 · 北京时间</span></div><button type="button" data-ash-close aria-label="关闭小时灰分弹窗">×</button></header>
            <div class="ash-dialog-body"><form id="ash-range-form"><fieldset id="ash-range-fields"><div class="ash-range-fields">
            <label>开始时间<span><input type="date" id="ash-start-date" aria-label="开始日期" required><select id="ash-start-hour" aria-label="开始小时">${this.hours()}</select></span></label>
            <label>结束时间<span><input type="date" id="ash-end-date" aria-label="结束日期" required><select id="ash-end-hour" aria-label="结束小时">${this.hours()}</select></span></label>
            <button type="submit" class="ash-query">查询区间</button></div></fieldset>
            <div class="ash-range-hint"><span>按整点选择，包含开始小时、不包含结束小时；最长 31 天。</span><button type="button" data-ash-latest>最近 24 小时</button></div></form>
            <div id="ash-history-status" role="status" aria-live="polite"></div><div id="ash-history-result"></div></div>`;
        document.body.append(this.dialog);
        this.dialog.querySelector('[data-ash-close]').addEventListener('click',()=>this.dialog.close());
        this.dialog.addEventListener('click',event=>{if(event.target===this.dialog) {
            const r=this.dialog.getBoundingClientRect();
            if(event.clientX<r.left || event.clientX>r.right || event.clientY<r.top || event.clientY>r.bottom) this.dialog.close();
        }});
        this.dialog.addEventListener('close',()=>{
            this.requestId++; this.controller?.abort(); this.controller=null;
            if(this.page?.active()) document.querySelector('[data-ash-history]')?.focus({preventScroll:true});
        });
        this.dialog.querySelector('#ash-range-form').addEventListener('submit',event=>{
            event.preventDefault(); this.load(this.selectedRange());
        });
        this.dialog.querySelector('[data-ash-latest]').addEventListener('click',()=>this.load());
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
        this.dialog.classList.toggle('is-light',document.getElementById('decision-workspace').classList.contains('is-light'));
        if(!this.dialog.open) this.dialog.showModal();
        this.load();
    },
    selectedRange() {
        const value=id=>this.dialog.querySelector('#'+id).value;
        return {start:`${value('ash-start-date')}T${value('ash-start-hour')}:00:00+08:00`,
            end:`${value('ash-end-date')}T${value('ash-end-hour')}:00:00+08:00`};
    },
    setRange(period,latest) {
        for(const key of ['start','end']) {
            const [date,time]=this.localParts(period[key]);
            this.dialog.querySelector(`#ash-${key}-date`).value=date;
            this.dialog.querySelector(`#ash-${key}-hour`).value=time.slice(0,2);
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
        this.status('正在读取小时统计…');
        try {
            const result=await Api.getTotalAshHours(range || {},controller.signal);
            if(id!==this.requestId || !this.dialog.open) return;
            this.result=result; this.setRange(result.period,result.latest_end);
            this.render(result); this.status('');
        } catch(error) {
            if(id!==this.requestId || !this.dialog.open) return;
            this.status(error.name==='AbortError'?'读取超时，请重新查询。':error.message || '小时灰分读取失败，请重试',true);
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
            return `<tr><td><strong>${time}–${end}</strong><span>${date}</span></td><td class="ash-table-value">${this.number(item.value)}</td><td>${item.sample_count} / ${item.expected_count}<span>${this.number(item.coverage*100,1)}%</span></td><td><span class="ash-state ${esc(item.status)}">${state}</span><small>${esc(item.reason)}</small></td></tr>`;
        }).join('');
        this.dialog.querySelector('#ash-history-result').innerHTML=`<section class="ash-summary" aria-label="区间平均灰分"><div><p>区间平均灰分</p><div class="ash-average">${this.number(s.value)}<span>%</span></div><p class="ash-summary-range">${esc(this.label(result.period.start))} — ${esc(this.label(result.period.end))}</p></div><dl><div><dt>有值小时</dt><dd>${s.valid_hours}<span> / ${s.total_hours} 小时</span></dd></div><div><dt>有效样本覆盖</dt><dd>${this.number(s.coverage*100,1)}<span>%</span></dd></div></dl></section>
            <p class="ash-method">${esc(result.method)}${s.partial_hours || s.missing_hours ? ` 本区间含 ${s.partial_hours} 个部分数据小时、${s.missing_hours} 个无数据小时，均值仅代表已有有效样本。`:''}</p>
            ${s.value===null?'<p class="ash-empty">此区间暂无有效总灰分。可选择其他时间；历史未保存的预测不会补算。</p>':''}
            <div class="ash-table-title"><h3>每小时平均灰分</h3><span>最新小时在前 · 单位 %</span></div>
            <div class="ash-table-scroll" tabindex="0" role="region" aria-label="每小时灰分记录"><table><thead><tr><th>统计时段</th><th>平均灰分 (%)</th><th>有效样本 / 覆盖率</th><th>数据情况</th></tr></thead><tbody>${rows}</tbody></table></div>
            <p class="ash-history-footnote">只展示已结束小时的保存结果，保留当时模型与数据口径。尚未结束的当前小时不纳入统计。</p>`;
    },
};
