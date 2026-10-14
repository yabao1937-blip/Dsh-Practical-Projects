/* Compact read-only history; independent of density advice and the interval dialog. */
const DecisionRecentAsh = {
    result:null, inFlight:null, error:'', lastAttempt:-Infinity, lastAttemptEnd:null, clockBaseAt:0,
    hourMs:3600000,
    clock() { return performance.now(); },
    enabled(page) { return page.instantLayout() && window.location.protocol.startsWith('http'); },
    latestEnd() {
        // Use the server clock, including when this workstation's clock is wrong.
        const hourly=typeof DecisionHourly==='undefined'?null:DecisionHourly;
        const useHourly=Number.isFinite(Date.parse(hourly?.result?.checked_at)) && hourly.clockBaseAt>=this.clockBaseAt;
        const stamp=useHourly?hourly.result.checked_at:this.result?.generated_at;
        const base=useHourly?hourly.clockBaseAt:this.clockBaseAt;
        const now=Date.parse(stamp)+(this.clock()-base);
        const delay=Number.isFinite(this.result?.publication_delay_seconds)?this.result.publication_delay_seconds*1000:this.hourMs;
        return Number.isFinite(now)?Math.floor((now-delay)/this.hourMs)*this.hourMs:null;
    },
    rows() {
        const end=this.latestEnd();
        if(end===null) return [];
        const saved=new Map((this.result?.items || []).map(item=>[Date.parse(item.start),item]));
        return Array.from({length:10},(_,index)=>{
            const start=end-(index+1)*this.hourMs, item=saved.get(start);
            const valid=item && Date.parse(item.end)===start+this.hourMs &&
                ['complete','partial'].includes(item.status) && Number.isFinite(item.value) && item.value>=0 && item.value<=100;
            return {start,end:start+this.hourMs,value:valid?item.value:null,
                status:valid?item.status:'missing',coverage:valid?item.coverage:null,
                reason:item?.reason || '该小时尚无已保存的统计记录'};
        });
    },
    async update(page,force=false) {
        if(!this.enabled(page) || !page.active() || this.inFlight) return;
        const end=this.latestEnd(), now=this.clock();
        const delay=this.error?30000:this.rows()[0]?.status==='missing'?15000:60000;
        if(!force && end===this.lastAttemptEnd && now-this.lastAttempt<delay) return;
        this.lastAttempt=now; this.lastAttemptEnd=end;
        this.inFlight=(async()=>{
            try {
                // The endpoint's default window is based on server time; take its
                // latest ten slots using the server's assay-attribution policy.
                const result=await Api.getTotalAshHours();
                if(!Number.isFinite(Date.parse(result.generated_at)) || !Number.isFinite(Date.parse(result.latest_end)))
                    throw new Error('小时灰分时间信息异常');
                this.result=result; this.clockBaseAt=now; this.error='';
                this.lastAttemptEnd=Date.parse(result.latest_end);
            } catch(error) { this.error=error.message || '小时灰分读取失败'; }
        })();
        await this.inFlight;
        this.inFlight=null;
        if(this.enabled(page) && page.active()) page.renderGraph();
    },
    async measurementSaved(page) {
        this.lastAttempt=-Infinity;
        // A pre-save request must finish before fetching the newly entered assay.
        while(this.inFlight) await this.inFlight;
        return this.update(page,true);
    },
    parts(stamp) { return new Date(stamp+8*this.hourMs).toISOString().slice(0,16).split('T'); },
    position(page) {
        const table=document.querySelector('#decision-recent-ash-layer .decision-recent-ash');
        const card=document.querySelector('#decision-board [data-decision-node="coarse"]');
        const toolbar=document.querySelector('#decision-workspace .decision-toolbar');
        if(!table || !card || !toolbar) return;
        const workspace=toolbar.parentElement, base=workspace.getBoundingClientRect();
        const bottom=card.getBoundingClientRect(), top=toolbar.getBoundingClientRect();
        const scale=page.actualZoom || 1, gap=16*scale;
        // Span the context row too, so both visible gaps match at every zoom.
        table.style.left=`${bottom.left-base.left-workspace.clientLeft}px`;
        table.style.top=`${top.bottom-base.top-workspace.clientTop+gap}px`;
        table.style.width=`${bottom.width/scale}px`;
        table.style.height=`${(bottom.top-top.bottom-2*gap)/scale}px`;
        table.style.transform=`scale(${scale})`;
    },
    markup(page,anchor) {
        if(!page.instantLayout() || !anchor) return '';
        const rows=this.rows(), html=rows.map(row=>{
            const [date,start]=this.parts(row.start), [endDate,end]=this.parts(row.end);
            const coverage=Number.isFinite(row.coverage)?` · 有效覆盖 ${(row.coverage*100).toFixed(1)}%`:'';
            const title=`${this.error?this.error+' · ':''}${date} ${start} — ${endDate} ${end} · ${row.reason}${coverage}`;
            return `<tr data-decision-key="recent-ash-${row.start}" class="is-${row.status}" title="${page.escape(title)}"><td>${start}–${end}</td><td>${page.number(row.value,2)}</td></tr>`;
        }).join('');
        return `<section class="decision-recent-ash" data-decision-key="recent-ash" aria-label="近10小时平均灰分" aria-busy="${!!this.inFlight}" style="left:${anchor.x}px;top:0;width:${anchor.w}px;height:${anchor.y-22}px"><table aria-label="按重介采样归属更新的最近10小时平均总灰分（%）"><colgroup><col style="width:62%"><col></colgroup><tbody>${html || `<tr><td colspan="2" class="decision-recent-ash-empty" title="${page.escape(this.error || (this.enabled(page)?'正在读取':'请通过服务地址查看'))}">—</td></tr>`}</tbody></table></section>`;
    },
};
