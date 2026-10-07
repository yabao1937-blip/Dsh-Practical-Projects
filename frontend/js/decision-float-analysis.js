/* Historical two-component reference analysis. Reuses chart interactions, never writes business data. */
const DecisionFloatAnalysis = Object.assign(Object.create(DecisionCoarseCharts), {
    rootId:'decision-float-analysis',canvasPrefix:'decision-float-',chartIds:['linkage','impact'],
    initialized:false,snapshot:null,signature:null,hits:{},keyboardIndices:{},
    escape(value) { return String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); },
    stamp(value) {
        if(typeof value!=='string' || !/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value)) return null;
        const t=Date.parse(value.replace(' ','T')+'Z');
        return Number.isFinite(t) && new Date(t).toISOString().slice(0,19).replace('T',' ')===value?t:null;
    },
    build(store=App.store) {
        const start=typeof FloatPage!=='undefined'?FloatPage.FLOAT_DATA_START:'2026-06-01';
        const validLogs=(store.calcLogs || []).filter(log=> {
            if(log.calc_type!=='ash_density' || this.stamp(log.timestamp)===null) return false;
            try {const input=typeof log.input_json==='string'?JSON.parse(log.input_json):log.input_json;
                return Number.isFinite(input?.ash_content) && input.ash_content>=0 && input.ash_content<=100;
            } catch(_) {return false;}
        });
        const reader=Object.create(App);reader.store={...store,calcLogs:validLogs};
        const seen=new Set(),rows=[];let duplicates=0,early=0,invalid=0;
        for(const record of store.floatCoal || []) {
            const key=String(record?.timestamp);
            if(seen.has(key)) {duplicates++;continue;}seen.add(key);
            const time=this.stamp(record?.timestamp);
            if(time===null) {invalid++;continue;}
            if(record.timestamp.slice(0,10)<start) {early++;continue;}
            if(!Number.isFinite(record.ash_content) || record.ash_content<0 || record.ash_content>100 ||
                !Number.isFinite(record.coal_amount) || record.coal_amount<0) {invalid++;continue;}
            const matched=validLogs.some(log=>Math.abs(this.stamp(log.timestamp)-time)<=5*60000);
            const heavyAsh=reader.getAshByTime(record.timestamp) ?? 8.50;
            const total=reader.calcTotalAsh(heavyAsh,reader.INFLUENCE_HEAVY_AMT,record.ash_content,record.coal_amount,0,0);
            const influence=reader.influenceOfFloat(record);
            if(!Number.isFinite(total) || !Number.isFinite(influence)) {invalid++;continue;}
            const influenceRaw=total-reader.calcTotalAsh(heavyAsh,reader.INFLUENCE_HEAVY_AMT,0,0,0,0);
            rows.push({timestamp:record.timestamp,time,ash:record.ash_content,amount:record.coal_amount,total,influence,influenceRaw,heavyAsh,
                defaultHeavy:!matched,annotation:record.annotation || '未标注',
                press:record.filter_press_running===true || record.filter_press_running===1?'开':record.filter_press_running===false || record.filter_press_running===0?'关':'未记录'});
        }
        rows.sort((a,b)=>a.time-b.time);
        const values=rows.map(r=>r.influenceRaw),mean=this.mean(values);
        const deviation=values.length?Math.sqrt(this.mean(values.map(v=>(v-mean)**2))):null;
        return {rows,latest:rows.at(-1) || null,deviation,start,heavyAmount:reader.INFLUENCE_HEAVY_AMT,
            defaults:rows.filter(r=>r.defaultHeavy).length,duplicates,early,invalid};
    },
    init() {
        if(this.initialized) return;
        DecisionCoarseCharts.init.call(this);
        document.getElementById('decision-float-open').addEventListener('click',()=>App.goToPage('page-float'));
    },
    refresh() {
        this.init();
        const snapshot=this.build(),signature=JSON.stringify(snapshot);
        if(signature===this.signature) return;
        this.snapshot=snapshot;this.signature=signature;
        const s=this.snapshot,l=s.latest;
        document.getElementById('decision-float-meta').textContent=`历史分析起点 ${s.start} · ${s.rows.length} 条有效采样 · ${s.defaults} 条采用默认重介灰分 8.50%`+
            (l?` · 最近记录 ${l.timestamp}`:' · 暂无历史数据')+
            (s.duplicates?` · ${s.duplicates} 条重复时间保留首条`:'')+
            (s.early?` · ${s.early} 条早期工况未纳入`:'')+
            (s.invalid?` · ${s.invalid} 条无效数据未纳入`:'');
        for(const [id,value,unit] of [['ash',l?.ash,'%'],['amount',l?.amount,'t/h'],['influence',l?.influence,'百分点'],['range',s.deviation,'百分点']]) {
            const text=this.number(value,id==='influence' || id==='range'?3:2);
            document.getElementById('decision-float-stat-'+id).textContent=(Number.isFinite(value)&&id==='influence'&&value>0?'+':Number.isFinite(value)&&id==='range'?'±':'')+text+' '+unit;
        }
        document.getElementById('decision-float-table').innerHTML=s.rows.slice().reverse().map(r=>
            `<tr><td>${r.timestamp}</td><td>${this.number(r.ash)}</td><td>${this.number(r.amount)}</td><td>${this.number(r.heavyAsh)}</td><td>${r.defaultHeavy?'默认基准':'5分钟近邻'}</td><td>${this.number(r.total,3)}</td><td>${this.number(r.influence,3)}</td><td>${r.press}</td><td>${this.escape(r.annotation)}</td></tr>`).join('');
        this.draw();
    },
    draw() {for(const id of this.chartIds) this.drawCard(id);},
    drawCard(id,preserveTooltip=false) {
        const surface=this.surface(id,preserveTooltip);if(!surface) return;
        if(!this.snapshot.rows.length) {this.empty(surface,'暂无有效浮精历史数据');return;}
        this[id](surface);
    },
    range(values,includeZero=false) {
        let min=Math.min(...values),max=Math.max(...values);
        if(includeZero) {min=Math.min(0,min);max=Math.max(0,max);}
        const pad=(max-min || .01)*.12;return {min:min-pad,max:max+pad};
    },
    grid(surface,range,right=false) {
        const {ctx,w,h,c}=surface,p={left:48,right:52,top:25,bottom:46};
        const y=v=>p.top+(h-p.top-p.bottom)*(range.max-v)/(range.max-range.min);
        ctx.lineWidth=1;ctx.fillStyle=right?c.purple:c.muted;ctx.strokeStyle=c.rule;ctx.textAlign=right?'left':'right';
        for(let i=0;i<=4;i++) {
            const v=range.min+(range.max-range.min)*i/4,py=y(v);
            if(!right) {ctx.beginPath();ctx.moveTo(p.left,py);ctx.lineTo(w-p.right,py);ctx.stroke();}
            ctx.fillText(v.toFixed(right?3:1),right?w-p.right+7:p.left-7,py+4);
        }
        ctx.textAlign=right?'right':'left';ctx.fillText(right?'影响 / 百分点':'灰分 / %',right?w-8:p.left,13);
        return {p,y,width:w-p.left-p.right};
    },
    linkage(surface) {
        const {ctx,w,h,c}=surface,rows=this.snapshot.rows;
        const left=this.grid(surface,this.range(rows.flatMap(r=>[r.ash,r.total])));
        const right=this.grid(surface,this.range(rows.map(r=>r.influence),true),true);
        const span=rows.at(-1).time-rows[0].time,x=r=>left.p.left+(span?(r.time-rows[0].time)/span:.5)*left.width;
        ctx.textAlign='center';ctx.fillStyle=c.muted;let last=-Infinity;
        rows.forEach(r=> {const px=x(r);if(px-last>65) {ctx.fillText(r.timestamp.slice(5,10),px,h-left.p.bottom+18);last=px;}});
        for(const [field,color,map,dash] of [['ash',c.blue,left.y,[]],['total',c.orange,left.y,[5,4]],['influence',c.purple,right.y,[2,4]]]) {
            ctx.strokeStyle=color;ctx.fillStyle=color;ctx.lineWidth=1.8;ctx.setLineDash(dash);ctx.beginPath();
            rows.forEach((r,i)=>i?ctx.lineTo(x(r),map(r[field])):ctx.moveTo(x(r),map(r[field])));ctx.stroke();ctx.setLineDash([]);
            rows.forEach(r=>{ctx.beginPath();ctx.arc(x(r),map(r[field]),2.8,0,Math.PI*2);ctx.fill();});
        }
        ctx.fillStyle=c.muted;ctx.fillText('历史采样时间 · 按真实时间间隔',w/2,h-8);
        this.hits.linkage=rows.map(r=>({x:x(r),y:left.y(r.ash),text:this.tip(r)}));
    },
    impact(surface) {
        const {ctx,w,h,c}=surface,rows=this.snapshot.rows.slice(-12),range=this.range(rows.map(r=>r.influence),true);
        const left=48,right=22,top=24,bottom=44,width=w-left-right,height=h-top-bottom;
        const y=v=>top+height*(range.max-v)/(range.max-range.min),zero=y(0),step=width/rows.length;
        ctx.fillStyle=c.muted;ctx.strokeStyle=c.rule;ctx.textAlign='right';ctx.lineWidth=1;
        for(let i=0;i<=4;i++) {const v=range.min+(range.max-range.min)*i/4,py=y(v);ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);ctx.stroke();ctx.fillText(v.toFixed(3),left-6,py+4);}
        ctx.strokeStyle=c.ink;ctx.beginPath();ctx.moveTo(left,zero);ctx.lineTo(w-right,zero);ctx.stroke();
        const tickStep=Math.max(1,Math.ceil(rows.length/Math.max(2,Math.floor(width/56))));
        this.hits.impact=rows.map((r,i)=> {
            const x=left+step*(i+.5),py=y(r.influence),bar=Math.min(36,step*.65);
            ctx.fillStyle=r.influence>0?c.red:r.influence<0?c.green:c.muted;ctx.fillRect(x-bar/2,Math.min(py,zero),bar,Math.max(1,Math.abs(py-zero)));
            if(i%tickStep===0) {ctx.fillStyle=c.muted;ctx.textAlign='center';ctx.fillText(r.timestamp.slice(5,10),x,h-bottom+18);}
            return {x,y:(py+zero)/2,text:this.tip(r)};
        });
        ctx.fillStyle=c.muted;ctx.textAlign='left';ctx.fillText('影响 / 百分点',left,13);ctx.textAlign='center';ctx.fillText('最近 '+rows.length+' 条采样 · 正负值相对零线',w/2,h-7);
    },
    tip(r) {return `${r.timestamp}\n浮精灰分 ${this.number(r.ash)}% · 浮精量 ${this.number(r.amount)} t/h\n重介灰分 ${this.number(r.heavyAsh)}%（${r.defaultHeavy?'默认8.50%':'前后5分钟近邻'}）\n两组分参考灰分 ${this.number(r.total,3)}%\n参考影响 ${this.number(r.influence,3)} 百分点`;},
    hover(id,event) {
        const canvas=event.currentTarget,rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
        let hit=null,distance=Infinity;
        for(const candidate of this.hits[id] || []) {const d=Math.abs(candidate.x-x);if(d<distance){distance=d;hit=candidate;}}
        if(!hit || distance>24) {this.hideTooltip(id);return;}this.showTooltip(id,hit,x,y);
    }
});
