/* GPT historical charts: private daily analysis model / saved sampling model; no production writes. */
const DecisionCoarseCharts = {
    mode: 'day', initialized: false, snapshot: null, signature: null, hits: {},
    shortLabels: {raw_ash:'原煤灰分',coal_amount:'入洗煤量',sysA:'A 系统',sysB:'B 系统',sys401:'401 系统',sys402:'402 系统',desliming473:'473 脱粉',desliming474:'474 脱粉',is_stoppage:'停机 / 低负荷',level:'精磁尾液位'},
    mean(values) { return values.length ? values.reduce((sum,v) => sum + v,0) / values.length : null; },
    number(v, digits = 2) { return Number.isFinite(v) ? v.toFixed(digits) : '—'; },
    modelGroup(store) {
        const current = store.coarseModel, saved = store.coarseModelVariants?.gpt;
        if (current && App.coarseEngine(current) === 'gpt') {
            const identity=m=>JSON.stringify([m.production,m.trainedAt,m.n,m.range,m.tolerance,
                ...['mlr','pls'].map(k=>[m[k]?.intercept,m[k]?.coefs,m[k]?.imputeMeans])]);
            return saved && App.coarseEngine(saved)==='gpt' && identity(saved)===identity(current)?saved:current;
        }
        return saved && App.coarseEngine(saved) === 'gpt' ? saved : null;
    },
    validModel(group) {
        const model=group?.[group.production];
        return ['mlr','pls'].includes(group?.production) && Number.isFinite(model?.intercept) &&
            model?.coefs?.length===App.MLR_FEATURES.length && model.coefs.every(Number.isFinite) &&
            (!model.feature_names || JSON.stringify(model.feature_names)===JSON.stringify(App.MLR_FEATURES));
    },
    dailyModel(store,saved) {
        // Pure GPT training uses a private facade for range/tolerance, never the DS/current production state.
        const range=['all','jun_jul','30d'].includes(saved.range)?saved.range:'all';
        const tolerance=Number.isFinite(saved.tolerance)?saved.tolerance:(store.coarseTolerance ?? .8);
        const reader=Object.create(App);reader.store={...store,coarseTolerance:tolerance};
        const trainingDays=reader._coarseDaily(reader.filterTrainRows(range));
        const identity=[saved.trainedAt,saved.n,saved.production,...['mlr','pls'].map(k=>[saved[k]?.intercept,saved[k]?.coefs,saved[k]?.inputPolicy,saved[k]?.inputBounds])];
        const key=JSON.stringify([identity,range,tolerance,trainingDays.map(d=>[d.timestamp,d.ash_content,...App.MLR_FEATURES.map(f=>d[f])])]);
        if (key!==this.dailyCache?.key) {
            const result=reader._trainCoarseRows(trainingDays);
            this.dailyCache={key,result:result?{...result,range,tolerance}:null};
        }
        return this.dailyCache.result;
    },
    build(store = App.store, mode = this.mode) {
        const saved=this.modelGroup(store),empty=reason=>({group:null,model:null,rows:[],factors:[],count:0,missing:0,excluded:0,mode,reason});
        if(!this.validModel(saved)) return empty('gpt-model-missing');
        const group=mode==='day'?this.dailyModel(store,saved):saved;
        if(!this.validModel(group)) return empty('daily-data-insufficient');
        const which=group.production,model=group[which],source=App._coarseRows(store.coarseCoal || []);
        const inputs=mode==='day'?App._coarseDaily(source):source,rows=[];
        let missing = 0, excluded = 0;
        for (const record of inputs) {
            const pred = App._coarsePredict(record,model);
            if (!Number.isFinite(pred)) { excluded++; continue; }
            if (App.MLR_FEATURES.some(f => !Number.isFinite(record[f]))) missing++;
            const day=record.timestamp.slice(0,10),daily=mode==='day';
            const hasLevel=Number.isFinite(record.level) && record.level>0;
            rows.push({key:record.timestamp,label:daily?day:record.timestamp,tick:daily?day.slice(5):record.timestamp.slice(5,16),
                start:daily?day+' 00:00':record.timestamp,end:daily?day+' 24:00':record.timestamp,n:daily?record.n:1,
                actual:record.ash_content,pred,level:hasLevel?record.level:null,
                scatterActual:hasLevel?record.ash_content:null,scatterPred:hasLevel?pred:null,
                paired:hasLevel?(daily?record.records.filter(r=>Number.isFinite(r.level)).length:1):0});
        }
        const factors = App.MLR_FEATURES.map((key,index) => ({key,name:App.FEATURE_LABELS[key] || key,value:model.stdCoef?.[index]}))
            .filter(f=>Number.isFinite(f.value)).sort((a,b)=>Math.abs(b.value)-Math.abs(a.value));
        return {group,model,which,rows,factors,count:source.length,missing,excluded,mode};
    },
    init() {
        if (this.initialized) return;
        this.initialized = true;
        document.querySelectorAll('[data-decision-period]').forEach(button => button.addEventListener('click',()=> {
            this.mode = button.dataset.decisionPeriod === 'sample' ? 'sample' : 'day';
            this.refresh();
        }));
        this.hoverMedia = window.matchMedia('(hover: hover) and (pointer: fine)');
        this.hoverMedia.addEventListener('change',()=>this.resetPointer());
        document.addEventListener('visibilitychange',()=> { if (document.hidden) this.resetPointer(); });
        for (const id of ['trend','factors','level']) {
            const canvas = document.getElementById('decision-coarse-'+id);
            const card = canvas.closest('.decision-chart-card');
            card.addEventListener('pointerenter',event=> {
                if (event.pointerType !== 'touch' && this.hoverMedia.matches) { card.dataset.hovered='true'; this.syncEngagement(id); }
            });
            card.addEventListener('pointerleave',()=> { card.dataset.hovered='false'; this.syncEngagement(id); });
            canvas.addEventListener('focus',()=> {
                this.syncEngagement(id);
                if (canvas.matches(':focus-visible')) this.keyboard(id,{key:'Home',preventDefault(){}});
            });
            canvas.addEventListener('blur',()=> { this.hideTooltip(id); this.syncEngagement(id); });
            canvas.addEventListener('keydown',event=>this.keyboard(id,event));
            canvas.addEventListener('mousemove',event=>this.hover(id,event));
            canvas.addEventListener('mouseleave',()=> { if (!canvas.matches(':focus-visible')) this.hideTooltip(id); });
            canvas.addEventListener('pointerdown',event=> { if (event.pointerType==='touch') canvas._tapOrigin={x:event.clientX,y:event.clientY}; });
            canvas.addEventListener('pointerup',event=> {
                const origin=canvas._tapOrigin; canvas._tapOrigin=null;
                if (event.pointerType==='touch' && origin && Math.hypot(event.clientX-origin.x,event.clientY-origin.y)<10) this.hover(id,event);
            });
            canvas.addEventListener('pointercancel',()=> { canvas._tapOrigin=null; this.hideTooltip(id); });
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
        const s = this.snapshot,daily=this.mode==='day',mode=daily?'天':'采样点';
        const meta = document.getElementById('decision-coarse-meta');
        meta.textContent = !s.model ? (s.reason==='daily-data-insufficient'?'GPT 日级数据不足：训练范围内至少需要 12 个有效日。请切换按采样查看。':'尚未保存可用的 GPT 模型，请在粗精煤泥灰分预测页训练 GPT 版本。') :
            `GPT · ${s.which.toUpperCase()} · ${daily?'日级分析模型（仅图表缓存） · '+s.group.n+' 个训练日':'采样保存模型 · 训练时间 '+(s.group.trainedAt || '未记录')} · ${s.count} 条历史采样 → ${s.rows.length} 个${mode}`+
            (s.rows.length ? ` · ${s.rows[0].label} 至 ${s.rows.at(-1).label}` : '')+
            (s.missing ? ` · ${s.missing} 个${mode}的缺失因子按对应模型均值补齐` : '')+
            (s.excluded ? ` · ${s.excluded} 条无法计算，已排除` : '');
        document.getElementById('decision-coarse-table').innerHTML = s.rows.map(r=>
            `<tr><td>${r.label}</td><td>${r.n}</td><td>${this.number(r.actual)}</td><td>${this.number(r.pred)}</td><td>${this.number(r.level)}</td><td>${r.paired}</td><td>${this.number(r.scatterActual)}</td><td>${this.number(r.scatterPred)}</td></tr>`).join('');
        document.getElementById('decision-coarse-factor-list').textContent = s.factors.map(f=>`${f.name}：${this.number(f.value,3)}`).join('；') || '暂无 GPT 标准化系数';
        document.querySelectorAll('[data-decision-series="actual"]').forEach(el=>el.textContent=daily?'实测日均值':'实测灰分');
        document.querySelectorAll('[data-decision-series="predicted"]').forEach(el=>el.textContent=daily?'GPT 日级预测':'GPT 采样预测');
        document.getElementById('decision-coarse-factor-caption').textContent=daily?'日级分析模型的标准化系数，仅用于历史分析。':'采样保存模型的标准化系数，保留原生产模型。';
        this.draw();
    },
    syncEngagement(id) {
        const canvas=document.getElementById('decision-coarse-'+id),card=canvas.closest('.decision-chart-card');
        const visible=!document.hidden && document.getElementById('page-decision').classList.contains('active');
        const engaged=visible && (card.dataset.hovered==='true' || canvas.matches(':focus-visible'));
        if (card.classList.contains('is-engaged')===engaged) return;
        card.classList.toggle('is-engaged',engaged);
        if (this.snapshot) this.drawCard(id,true);
    },
    resetPointer() {
        for (const id of ['trend','factors','level']) {
            document.getElementById('decision-coarse-'+id).closest('.decision-chart-card').dataset.hovered='false';
            this.hideTooltip(id);
            this.syncEngagement(id);
        }
    },
    palette(element = document.getElementById('decision-coarse-analysis')) {
        const style = getComputedStyle(element);
        const get = key=>style.getPropertyValue(key).trim();
        return {ink:get('--ds-ink'),muted:get('--ds-muted'),rule:get('--ds-rule'),blue:get('--dc-blue'),orange:get('--dc-orange'),purple:get('--dc-purple'),green:get('--dc-green'),red:get('--dc-red')};
    },
    surface(id, preserveTooltip = false) {
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
        if (!preserveTooltip) this.hideTooltip(id);
        return {canvas,ctx,w,h,c:this.palette(canvas.closest('.decision-chart-card'))};
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
        document.getElementById('decision-coarse-metrics').textContent = this.snapshot.model ? '暂无可配对的历史液位与灰分' : this.emptyReason();
        for(const id of ['trend','factors','level']) this.drawCard(id);
    },
    drawCard(id,preserveTooltip = false) {
        const surface=this.surface(id,preserveTooltip); if (!surface) return;
        if (!this.snapshot.model) { this.empty(surface,this.emptyReason()); return; }
        if (id !== 'factors' && !this.snapshot.rows.length) { this.empty(surface,'暂无有效历史采样数据'); return; }
        this[id](surface);
    },
    emptyReason() { return this.snapshot.reason==='daily-data-insufficient'?'日级数据不足，请切换按采样':'暂无可用 GPT 模型'; },
    trend(surface) {
        const {ctx,w,h,c}=surface,rows=this.snapshot.rows,n=rows.length;
        const a=this.axes(surface,rows.flatMap(r=>[r.actual,r.pred,13]),this.mode==='day'?'采样日期 · 每日一点':'采样时间 · 每条记录一点');
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
        this.hits.trend=rows.map((r,i)=>({x:x(i),y:a.y(r.actual),text:`${r.label}\n${this.mode==='day'?r.n+' 条采样 · 实测日均值':'实测灰分'} ${this.number(r.actual)}%\nGPT ${this.mode==='day'?'日级':'采样'}预测 ${this.number(r.pred)}% · 参考线 13.00%`}));
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
        const a=this.axes(surface,rows.flatMap(r=>[r.scatterActual,r.scatterPred]),this.mode==='day'?'精磁尾液位 (%) · 日均值':'精磁尾液位 (%) · 采样值');
        const min=Math.min(...rows.map(r=>r.level))-1,max=Math.max(...rows.map(r=>r.level))+1;
        const x=value=>a.left+(value-min)/(max-min)*a.xWidth;
        ctx.textAlign='center';ctx.fillStyle=c.muted;
        for(let i=0;i<=5;i++) {const value=min+(max-min)*i/5;ctx.fillText(value.toFixed(1),x(value),h-a.bottom+18);}
        const hits=[];
        for(const [field,color,label] of [['scatterPred',c.orange,'GPT 预测'],['scatterActual',c.blue,'实测']]) {
            ctx.fillStyle=color;
            rows.forEach(r=> {
                const px=x(r.level),py=a.y(r[field]);ctx.beginPath();ctx.arc(px,py,3.3,0,Math.PI*2);ctx.fill();
                hits.push({x:px,y:py,text:`${r.label} · ${label}\n液位${this.mode==='day'?'日均值':'采样值'} ${this.number(r.level)}% · ${r.paired} 条有液位的采样\n实测${this.mode==='day'?'日均值':'灰分'} ${this.number(r.scatterActual)}%\nGPT ${this.mode==='day'?'日级':'采样'}预测 ${this.number(r.scatterPred)}%`});
            });
        }
        this.hits.level=hits;
        const errors=rows.map(r=>r.scatterPred-r.scatterActual),mean=this.mean(rows.map(r=>r.scatterActual));
        const ss=rows.reduce((sum,r)=>sum+(r.scatterActual-mean)**2,0),sse=errors.reduce((sum,e)=>sum+e*e,0);
        caption.textContent=`历史${this.mode==='day'?'日级':'采样'}回算对照 · ${rows.length} 点 · MAE ${this.number(this.mean(errors.map(Math.abs)))} / RMSE ${this.number(Math.sqrt(sse/rows.length))} 百分点 · R² ${ss>0?this.number(1-sse/ss,3):'—'}`;
    },
    hover(id,event) {
        const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
        const hits=this.hits[id] || []; let best=null,distance=Infinity;
        for(const hit of hits) {
            const d=id==='trend'?Math.abs(hit.x-x):id==='factors'?Math.abs(hit.y-y):Math.hypot(hit.x-x,hit.y-y);
            if(d<distance) {best=hit;distance=d;}
        }
        if(!best || distance>(id==='factors'?18:id==='trend'?24:15)) {this.hideTooltip(id);return;}
        this.showTooltip(id,best,x,y);
    },
    hideTooltip(id) { document.getElementById('decision-coarse-'+id).parentElement.querySelector('.decision-chart-tooltip').hidden=true; },
    showTooltip(id,hit,x,y,keyboard=false) {
        const canvas=document.getElementById('decision-coarse-'+id),rect=canvas.getBoundingClientRect();
        const tooltip=canvas.parentElement.querySelector('.decision-chart-tooltip');
        tooltip.setAttribute('aria-live',keyboard?'polite':'off');
        tooltip.textContent=hit.text;tooltip.hidden=false;
        tooltip.style.left=Math.max(4,Math.min(x+14,rect.width-tooltip.offsetWidth-4))+'px';
        tooltip.style.top=Math.max(4,Math.min(y+12,rect.height-tooltip.offsetHeight-4))+'px';
    },
    keyboard(id,event) {
        if (event.key==='Escape') { this.hideTooltip(id);return; }
        if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key)) return;
        const hits=this.hits[id] || []; if (!hits.length) return;
        event.preventDefault();this.syncEngagement(id);
        if (!this.keyboardIndices) this.keyboardIndices={};
        let index=this.keyboardIndices[id] || 0;
        if (event.key==='Home') index=0;
        else if(event.key==='End') index=hits.length-1;
        else index=Math.max(0,Math.min(hits.length-1,index+(['ArrowLeft','ArrowUp'].includes(event.key)?-1:1)));
        this.keyboardIndices[id]=index;
        this.showTooltip(id,hits[index],hits[index].x,hits[index].y,true);
    }
};
