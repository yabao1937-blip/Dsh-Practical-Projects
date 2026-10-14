// Display retention, reloads and failures without a browser or production writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const saved=new Map();
function context() {
    const c=vm.createContext({console,localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},
        DecisionSources:{config:{revision:4,resolved_choices:{'density-current':'A304'},target_min:8.5,target_max:9}}});
    vm.runInContext(fs.readFileSync('frontend/js/decision-density-display.js','utf8')+';this.memory=DecisionDensityDisplay;',c);
    return c;
}
const page={pointChoices:{'density-current':'A304'}};
let c=context(),m=c.memory;
const result=(value,at='2026-10-14T06:24:16Z')=>({generated_at:at,checked_at:at,
    sources:c.DecisionSources.config,target:{min:8.5,max:9},model:{method:'expert8',id:'original'},
    period:{apply_start:'2026-10-14T06:00:00Z',apply_end:'2026-10-14T07:00:00Z'},
    suggested_density:{value,status:value===null?'missing':'calculated',reason:'当前停机'}});
assert.equal(m.present('instant',page,result(null),false,'无输入').value,null);
const first=result(1.488),hour=result(1.480),before=JSON.stringify(first);
m.remember('instant',page,first);m.remember('hourly',page,hour);
assert.equal(JSON.stringify(first),before,'Display cache cannot alter a valid server snapshot');
let unavailable=result(null,'2026-10-14T07:01:00Z');
const stoppedBefore=JSON.stringify(unavailable);
for(const kind of ['instant','hourly']) {
    const show=m.present(kind,page,unavailable,false,'当前停机');
    assert.equal(show.value,kind==='instant'?1.488:1.480);
    assert.equal(show.at,first.generated_at,'Missing responses cannot advance the retained timestamp');
    assert.equal(show.applyStart,'2026-10-14T06:00:00Z','New hour cannot relabel an old recommendation');
    assert(show.retained && show.reason.includes('沿用最后一次有效建议') && show.reason.includes('当前停机'));
    assert.equal(m.present(kind,page,null,false,'网络失败').value,show.value);
}
assert.equal(JSON.stringify(unavailable),stoppedBefore,'No held value may enter the calculation response');
// Actual controllers must render the cache while keeping the current result invalid.
vm.runInContext(fs.readFileSync('frontend/js/decision-realtime.js','utf8')+';this.live=DecisionRealtime;',c);
vm.runInContext(fs.readFileSync('frontend/js/decision-hourly.js','utf8')+';this.hourly=DecisionHourly;',c);
c.App={FEATURE_LABELS:{},MLR_FEATURES:[]};
Object.assign(page,{view:'gpt',hourMode:()=>false,summaryChildren:new Map(),nodes:[{id:'coarse-amount'}],
    pointDisplay:()=>({value:'20.00 t/h'}),number:(v,d=3,u='')=>Number.isFinite(v)?v.toFixed(d)+(u?' '+u:''):'—',
    escape:s=>String(s??'')});
c.live.result=null;c.live.error='断采';
const display=c.live.display(page,{id:'density'});
assert.equal(display.value,'1.488');assert.equal(display.available,false);assert.equal(display.numericValue,null);
assert(display.held && display.title.includes('沿用最后一次有效建议'));
c.hourly.result=null;c.hourly.error='统计缺数';
const hourDisplay=c.hourly.display(page,{id:'density'});
assert.equal(hourDisplay.value,'1.480');assert.equal(hourDisplay.available,false);assert.equal(hourDisplay.numericValue,null);
assert(c.hourly.summary(page,{x:0,y:300,w:240}).includes('更新于 14:00'));
assert.equal(c.hourly.presentation(),null);assert.equal(c.hourly.dataGaps().size,0);
c=context();m=c.memory;
assert.equal(m.present('instant',page,null,false,'刷新后离线').value,1.488,'Reload restores persisted display');
assert.equal(m.present('hourly',page,null,false,'刷新后离线').value,1.480);
for(const value of [null,NaN,Infinity,0,1,3]) m.remember('instant',page,result(value,'2026-10-14T07:02:00Z'));
assert.equal(m.present('instant',page,null,false,'异常值').value,1.488);
m.remember('instant',page,result(1.49,'2026-10-14T07:03:00Z'));
m.remember('instant',page,result(1.45,'2026-10-14T06:00:00Z'));
assert.equal(m.present('instant',page,null,false,'等待').value,1.49,'Late replies cannot overwrite newer memory');
c.DecisionSources.config.target_min=8;
assert.equal(m.present('instant',page,null,false,'目标修改').value,null);
c.DecisionSources.config.target_min=8.5;
c.DecisionSources.config.resolved_choices['density-current']='A303';
assert.equal(m.present('hourly',page,null,false,'点位修改').value,null);
c.DecisionSources.config.resolved_choices['density-current']='A304';
unavailable=result(null);unavailable.model.id='new-model';
assert.equal(m.present('instant',page,unavailable,false,'模型修改').value,null);
assert.equal(m.present('instant',page,null,false,'模型修改后断网').value,null);
c=context();m=c.memory;
assert.equal(m.present('instant',page,null,false,'模型修改后刷新').value,null);
saved.set(m.storageKey,'broken JSON');c=context();
assert.equal(c.memory.present('instant',page,null,false,'首次加载').value,null);
console.log('Density display memory: independent values, missing input, offline, reload, rollover, timestamps and configuration isolation passed.');
