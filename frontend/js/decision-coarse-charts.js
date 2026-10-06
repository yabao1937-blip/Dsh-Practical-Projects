/* GPT-only historical charts. Grouping changes presentation, never training or live inputs. */
const DecisionCoarseCharts = {
    mode: 'day', initialized: false, snapshot: null, signature: null, hits: {},
    shortLabels: {raw_ash:'原煤灰分',coal_amount:'入洗煤量',sysA:'A 系统',sysB:'B 系统',sys401:'401 系统',sys402:'402 系统',desliming473:'473 脱粉',desliming474:'474 脱粉',is_stoppage:'停机 / 低负荷',level:'精磁尾液位'},
    mean(values) { return values.length ? values.reduce((sum,v) => sum + v,0) / values.length : null; },
    number(v, digits = 2) { return Number.isFinite(v) ? v.toFixed(digits) : '—'; },
    modelGroup(store) {
        const current = store.coarseModel, saved = store.coarseModelVariants?.gpt;
        if (current && App.coarseEngine(current) === 'gpt') return current;
        return saved && App.coarseEngine(saved) === 'gpt' ? saved : null;
    },
    period(timestamp, mode) {
        const day = timestamp.slice(0,10), hour = Number(timestamp.slice(11,13));
        if (mode === 'day') return {key:day,label:day,tick:day.slice(5),start:day+' 00:00',end:day+' 24:00'};
        // UTC arithmetic is only a calendar operation: timestamps are historical plant wall time.
        const date = new Date(day+'T00:00:00Z');
        if (hour < 8) date.setUTCDate(date.getUTCDate()-1);
        const shiftDay = date.toISOString().slice(0,10), night = hour >= 20 || hour < 8;
        date.setUTCDate(date.getUTCDate()+1);
        return {key:shiftDay+(night?' 20':' 08'),label:shiftDay+(night?' 夜班':' 白班'),tick:shiftDay.slice(5)+(night?' 夜':' 白'),
            start:shiftDay+(night?' 20:00':' 08:00'),end:(night?date.toISOString().slice(0,10):shiftDay)+(night?' 08:00':' 20:00')};
    },
    build(store = App.store, mode = this.mode) {
        const group = this.modelGroup(store), which = group?.production, model = group?.[which];
        const valid = ['mlr','pls'].includes(which) && Number.isFinite(model?.intercept) &&
            model?.coefs?.length === App.MLR_FEATURES.length && model.coefs.every(Number.isFinite) &&
            (!model.feature_names || JSON.stringify(model.feature_names) === JSON.stringify(App.MLR_FEATURES));
        if (!valid) return {group,model:null,rows:[],factors:[],count:0,missing:0,excluded:0,mode};
        const source = App._coarseRows(store.coarseCoal || []), periods = new Map();
        let missing = 0, excluded = 0;
        for (const record of source) {
            const pred = App._coarsePredict(record,model);
            if (!Number.isFinite(pred)) { excluded++; continue; }
            if (App.MLR_FEATURES.some(f => !Number.isFinite(record[f]))) missing++;
            const period = this.period(record.timestamp,mode);
            if (!periods.has(period.key)) periods.set(period.key,{...period,records:[]});
            periods.get(period.key).records.push({actual:record.ash_content,pred,level:record.level});
        }
        const rows = [...periods.values()].map(period => {
            const records = period.records, paired = records.filter(r => Number.isFinite(r.level) && r.level > 0);
            return {...period,n:records.length,actual:this.mean(records.map(r=>r.actual)),pred:this.mean(records.map(r=>r.pred)),
                level:this.mean(paired.map(r=>r.level)),scatterActual:this.mean(paired.map(r=>r.actual)),scatterPred:this.mean(paired.map(r=>r.pred)),paired:paired.length};
        });
        const factors = App.MLR_FEATURES.map((key,index) => ({key,name:App.FEATURE_LABELS[key] || key,value:model.stdCoef?.[index]}))
            .filter(f=>Number.isFinite(f.value)).sort((a,b)=>Math.abs(b.value)-Math.abs(a.value));
        return {group,model,which,rows,factors,count:source.length-excluded,missing,excluded,mode};
    },
    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.querySelectorAll('[data-decision-period]').forEach(button => button.addEventListener('click',()=> {
            this.mode = button.dataset.decisionPeriod === 'shift' ? 'shift' : 'day';
            this.refresh();
        }));
        for (const id of ['trend','factors','level']) {
            const canvas = document.getElementById('decision-coarse-'+id);
            canvas.addEventListener('mousemove',event=>this.hover(id,event));
            canvas.addEventListener('mouseleave',()=>canvas.parentElement.querySelector('.decision-chart-tooltip').hidden=true);
        }
        this.resizeObserver = new ResizeObserver(()=> {
            if (document.getElementById('page-decision').classList.contains('active') && this.snapshot) this.draw();
        });
        document.querySelectorAll('.decision-chart-plot').forEach(el=>this.resizeObserver.observe(el));
    },
    refresh() {
        this.init();
        const group = this.modelGroup(App.store);
        const signature = JSON.stringify([this.mode,group,App.store.coarseTolerance,
            (App.store.coarseCoal || []).map(r=>[r.timestamp,r.ash_content,...App.MLR_FEATURES.map(f=>r[f])])]);
        const colors = this.palette(), paletteKey = JSON.stringify(colors);
        if (signature === this.signature && paletteKey === this.paletteKey) return;
        this.signature = signature; this.paletteKey = paletteKey;
        this.snapshot = this.build();
        document.querySelectorAll('[data-decision-period]').forEach(button=> {
            const selected = button.dataset.decisionPeriod === this.mode;
            button.classList.toggle('is-active',selected); button.setAttribute('aria-pressed',String(selected));
        });
        const s = this.snapshot, mode = this.mode === 'day' ? '天' : '班次';
        const meta = document.getElementById('decision-coarse-meta');
        meta.textContent = !s.model ? '尚未保存可用的 GPT 模型，请在粗精煤泥灰分预测页训练 GPT 版本。' :
            `GPT · ${s.which.toUpperCase()} · 训练时间 ${s.group.trainedAt || '未记录'} · ${s.count} 条历史采样 → ${s.rows.length} 个${mode}`+
            (s.rows.length ? ` · ${s.rows[0].label} 至 ${s.rows.at(-1).label}` : '')+
            (s.missing ? ` · ${s.missing} 条记录的缺失因子按保存模型均值补齐` : '')+
            (s.excluded ? ` · ${s.excluded} 条无法计算，已排除` : '');
        document.getElementById('decision-coarse-table').innerHTML = s.rows.map(r=>
            `<tr><td>${r.label}</td><td>${r.n}</td><td>${this.number(r.actual)}</td><td>${this.number(r.pred)}</td><td>${this.number(r.level)}</td><td>${r.paired}</td><td>${this.number(r.scatterActual)}</td><td>${this.number(r.scatterPred)}</td></tr>`).join('');
        document.getElementById('decision-coarse-factor-list').textContent = s.factors.map(f=>`${f.name}：${this.number(f.value,3)}`).join('；') || '暂无 GPT 标准化系数';
        this.draw();
    },
    palette() {
        const style = getComputedStyle(document.getElementById('decision-coarse-analysis'));
        const get = key=>style.getPropertyValue(key).trim();
        return {ink:get('--ds-ink'),muted:get('--ds-muted'),rule:get('--ds-rule'),blue:get('--dc-blue'),orange:get('--dc-orange'),purple:get('--dc-purple'),green:get('--dc-green'),red:get('--dc-red')};
    },
    surface(id) {
        const canvas = document.getElementById('decision-coarse-'+id), rect = canvas.parentElement.getBoundingClientRect();
        const w = rect.width, h = rect.height;
        if (w <= 0 || h <= 0) return null;
        const dpr = Math.min(window.devicePixelRatio || 1,2);
        canvas.width = Math.round(w*dpr); canvas.height = Math.round(h*dpr);
        const ctx = canvas.getContext('2d');
        if (!ctx) return null;
        ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
        ctx.font = '11px Microsoft YaHei, sans-serif';
        this.hits[id] = [];
        canvas.parentElement.querySelector('.decision-chart-tooltip').hidden = true;
        return {canvas,ctx,w,h,c:this.palette()};
    },
    empty(surface,text) {
        const {ctx,w,h,c} = surface;
        ctx.fillStyle=c.muted; ctx.textAlign='center'; ctx.fillText(text,w/2,h/2);
    },
    axes(surface,ys,xTitle) {
        const {ctx,w,h,c}=surface, left=52,right=20,top=24,bottom=48;
        let min=Math.min(...ys),max=Math.max(...ys),span=max-min || 1;
        min-=span*.12; max+=span*.12;
        const xWidth=w-left-right,yHeight=h-top-bottom;
        const y=value=>top+yHeight-(value-min)/(max-min)*yHeight;
        ctx.lineWidth=1; ctx.strokeStyle=c.rule; ctx.fillStyle=c.muted; ctx.textAlign='right';
        for(let i=0;i<=5;i++) {
            const value=min+(max-min)*i/5,py=y(value);
            ctx.beginPath(); ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();
            ctx.fillText(value.toFixed(1),left-8,py+4);
        }
        ctx.textAlign='left';ctx.fillText('灰分 (%)',left,12);
        ctx.textAlign='center';ctx.fillText(xTitle,left+xWidth/2,h-8);
        return {left,right,top,bottom,xWidth,yHeight,y};
    },
    draw() {
        document.getElementById('decision-coarse-metrics').textContent = this.snapshot.model ? '暂无可配对的历史液位与灰分' : '暂无可用 GPT 模型';
        for(const id of ['trend','factors','level']) {
            const surface=this.surface(id); if (!surface) continue;
            if (!this.snapshot.model) { this.empty(surface,'暂无可用 GPT 模型'); continue; }
            if (id !== 'factors' && !this.snapshot.rows.length) { this.empty(surface,'暂无有效历史采样数据'); continue; }
            this[id](surface);
        }
    },
    trend(surface) {
        const {ctx,w,h,c}=surface,rows=this.snapshot.rows,n=rows.length;
        const a=this.axes(surface,rows.flatMap(r=>[r.actual,r.pred,13]),this.mode==='day'?'采样日期':'班次起始日期 · 白 / 夜');
        const x=i=>a.left+(n===1?.5:i/(n-1))*a.xWidth;
        const step=Math.max(1,Math.ceil(n/Math.max(2,Math.floor(a.xWidth/(this.mode==='day'?65:100)))));
        ctx.fillStyle=c.muted;ctx.textAlign='center';
        for(let i=0;i<n;i+=step) ctx.fillText(rows[i].tick,x(i),h-a.bottom+18);
        for(const [field,color,dash] of [['actual',c.blue,[]],['pred',c.orange,[5,4]]]) {
            ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=2;ctx.setLineDash(dash);ctx.beginPath();
            rows.forEach((r,i)=>i?ctx.lineTo(x(i),a.y(r[field])):ctx.moveTo(x(i),a.y(r[field])));ctx.stroke();ctx.setLineDash([]);
            rows.forEach((r,i)=>{ctx.beginPath();ctx.arc(x(i),a.y(r[field]),2.7,0,Math.PI*2);ctx.fill();});
        }
        ctx.strokeStyle=c.purple;ctx.setLineDash([4,5]);ctx.beginPath();ctx.moveTo(a.left,a.y(13));ctx.lineTo(w-a.right,a.y(13));ctx.stroke();ctx.setLineDash([]);
        this.hits.trend=rows.map((r,i)=>({x:x(i),y:a.y(r.actual),text:`${r.label}\n${r.start} 至 ${r.end}\n${r.n} 条采样 · 实测均值 ${this.number(r.actual)}%\nGPT 预测均值 ${this.number(r.pred)}% · 参考线 13.00%`}));
    },
    factors(surface) {
        const {ctx,w,h,c}=surface,items=this.snapshot.factors;
        if(!items.length) {this.empty(surface,'保存模型未提供标准化系数');return;}
        const left=Math.min(115,w*.34),right=50,top=16,rowHeight=(h-top-32)/items.length;
        const max=Math.max(...items.map(f=>Math.abs(f.value)),.001)*1.12,width=w-left-right;
        ctx.strokeStyle=c.rule;ctx.lineWidth=1;
        for(let i=0;i<=4;i++) { const x=left+width*i/4;ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,h-28);ctx.stroke(); }
        this.hits.factors=items.map((f,i)=> {
            const y=top+rowHeight*i+rowHeight/2,bar=Math.abs(f.value)/max*width;
            ctx.fillStyle=f.value>=0?c.green:c.red;ctx.fillRect(left,y-7,bar,14);
            ctx.fillStyle=c.ink;ctx.textAlign='right';ctx.fillText(this.shortLabels[f.key],left-9,y+4);
            ctx.fillStyle=c.muted;ctx.textAlign='left';ctx.fillText((f.value>=0?'+':'')+f.value.toFixed(3),left+bar+6,y+4);
            return {x:left+bar/2,y,text:`${f.name}\n标准化系数 ${this.number(f.value,3)}\n关联方向${f.value>=0?'为正':'为负'}；不代表因果或准确率`};
        });
        ctx.fillStyle=c.muted;ctx.textAlign='center';ctx.fillText('|标准化系数|',left+width/2,h-7);
    },
    level(surface) {
        const {ctx,w,h,c}=surface,rows=this.snapshot.rows.filter(r=>r.paired>0);
        const caption=document.getElementById('decision-coarse-metrics');
        if(!rows.length) {caption.textContent='暂无可配对的历史液位与灰分';this.empty(surface,'暂无有效历史液位数据');return;}
        const a=this.axes(surface,rows.flatMap(r=>[r.scatterActual,r.scatterPred]),'精磁尾液位 (%) · 同组有效液位采样均值');
        const min=Math.min(...rows.map(r=>r.level))-1,max=Math.max(...rows.map(r=>r.level))+1;
        const x=value=>a.left+(value-min)/(max-min)*a.xWidth;
        ctx.textAlign='center';ctx.fillStyle=c.muted;
        for(let i=0;i<=5;i++) {const value=min+(max-min)*i/5;ctx.fillText(value.toFixed(1),x(value),h-a.bottom+18);}
        const hits=[];
        for(const [field,color,label] of [['scatterPred',c.orange,'GPT 预测'],['scatterActual',c.blue,'实测']]) {
            ctx.fillStyle=color;
            rows.forEach(r=> {
                const px=x(r.level),py=a.y(r[field]);ctx.beginPath();ctx.arc(px,py,3.3,0,Math.PI*2);ctx.fill();
                hits.push({x:px,y:py,text:`${r.label} · ${label}\n${r.paired} 条液位配对采样 · 液位均值 ${this.number(r.level)}%\n实测均值 ${this.number(r.scatterActual)}%\nGPT 预测均值 ${this.number(r.scatterPred)}%`});
            });
        }
        this.hits.level=hits;
        const errors=rows.map(r=>r.scatterPred-r.scatterActual),mean=this.mean(rows.map(r=>r.scatterActual));
        const ss=rows.reduce((sum,r)=>sum+(r.scatterActual-mean)**2,0),sse=errors.reduce((sum,e)=>sum+e*e,0);
        caption.textContent=`历史汇总对照 · ${rows.length} 点 · MAE ${this.number(this.mean(errors.map(Math.abs)))} / RMSE ${this.number(Math.sqrt(sse/rows.length))} 百分点 · R² ${ss>0?this.number(1-sse/ss,3):'—'}`;
    },
    hover(id,event) {
        const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
        const hits=this.hits[id] || []; let best=null,distance=Infinity;
        for(const hit of hits) {
            const d=id==='trend'?Math.abs(hit.x-x):id==='factors'?Math.abs(hit.y-y):Math.hypot(hit.x-x,hit.y-y);
            if(d<distance) {best=hit;distance=d;}
        }
        const tooltip=canvas.parentElement.querySelector('.decision-chart-tooltip');
        if(!best || distance>(id==='factors'?18:id==='trend'?24:15)) {tooltip.hidden=true;return;}
        tooltip.textContent=best.text;tooltip.hidden=false;
        tooltip.style.left=Math.max(4,Math.min(x+14,rect.width-tooltip.offsetWidth-4))+'px';
        tooltip.style.top=Math.max(4,Math.min(y+12,rect.height-tooltip.offsetHeight-4))+'px';
    }
};
