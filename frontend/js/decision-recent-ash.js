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
        return Number.isFinite(now)?Math.floor(now/this.hourMs)*this.hourMs:null;
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
                // latest ten slots without replaying or recalculating history.
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
    parts(stamp) { return new Date(stamp+8*this.hourMs).toISOString().slice(0,16).split('T'); },
    markup(page,anchor) {
        if(!page.instantLayout() || !anchor) return '';
        const rows=this.rows(), html=rows.map(row=>{
            const [date,start]=this.parts(row.start), [endDate,end]=this.parts(row.end);
            const coverage=Number.isFinite(row.coverage)?` · 有效覆盖 ${(row.coverage*100).toFixed(1)}%`:'';
            const title=`${date} ${start} — ${endDate} ${end} · ${row.reason}${coverage}`;
            return `<tr data-decision-key="recent-ash-${row.start}" class="is-${row.status}" title="${page.escape(title)}"><td>${start}–${end}</td><td>${page.number(row.value,2)}</td></tr>`;
        }).join('');
        const note=this.error?'读取失败 · 点击刷新数据重试':'北京时间 · 最新小时在上';
        return `<section class="decision-recent-ash" data-decision-key="recent-ash" aria-labelledby="decision-recent-ash-title" aria-busy="${!!this.inFlight}" style="left:${anchor.x}px;top:0;width:${anchor.w}px;height:${anchor.y-22}px"><h3 id="decision-recent-ash-title">近10小时平均灰分</h3><table aria-label="最近10个已结束小时的平均总灰分"><thead><tr><th scope="col">时段</th><th scope="col">灰分 (%)</th></tr></thead><tbody>${html || `<tr><td colspan="2" class="decision-recent-ash-empty">${this.error?'暂时无法读取':this.enabled(page)?'正在读取…':'请通过服务地址查看'}</td></tr>`}</tbody></table><p class="decision-recent-ash-note${this.error?' is-error':''}" title="${page.escape(this.error || '沿用已保存小时统计；悬停查看日期、覆盖情况与缺数原因')}">${note}</p></section>`;
    },
};
