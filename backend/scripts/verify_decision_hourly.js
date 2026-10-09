/* Pure-controller regression. Does not launch browsers or contact production. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let clock=0,resolve,reject;
const context={Date,Number,Math,Object,Array,String,performance:{now:()=>clock},DecisionSources:{epoch:0},
    Api:{getDecisionHourly:()=>new Promise((a,b)=>{resolve=a;reject=b;})}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/js/decision-hourly.js','utf8')+';this.hourly=DecisionHourly;',context);
const h=context.hourly;
const page={hourMode:()=>true,active:()=>true,renderGraph(){},renderContext(){},renderInspector(){},
    summaryChildren:new Map(),number:(v,d=2,u='')=>Number.isFinite(v)?v.toFixed(d)+(u?' '+u:''):'—',time:v=>v};
const response=()=>({period:{end:'2026-10-09T03:00:00Z',apply_end:'2026-10-09T04:00:00Z',apply_label:'11:00–12:00'},
    checked_at:'2026-10-09T03:24:00Z',cells:{raw_ash:{value:40,status:'average',unit:'%',sample_count:3},
        systems:{components:{'401':{value:.5},'402':{value:0}}}},coverage:{}});
(async()=>{
    let pending=h.update(page,true); resolve(response()); await pending;
    assert(h.presentation(),'Server time, not the local calendar, determines the valid hour');
    assert.equal(h.display(page,{id:'raw-ash'}).value,'40.00 %');
    const parts=h.display(page,{id:'system-switches'}).components;
    assert.equal(parts[0].value,.5); assert.equal(parts[1].value,0); assert(parts[1].available && parts[1].fraction);
    clock=36*60*1000;
    assert.equal(h.presentation(),null,'The old hour expires at the server boundary');
    clock=0;
    pending=h.update(page,true);
    context.DecisionSources.epoch++;
    resolve(response()); await pending;
    assert.equal(h.presentation(),null,'Source changes reject responses issued under old configuration');
    resolve(response()); await h.inFlight;
    await Promise.resolve();
    assert(h.presentation());
    pending=h.update(page,true); reject(new Error('小时请求失败')); await pending;
    assert.equal(h.presentation(),null); assert.equal(h.error,'小时请求失败');
    console.log('Hourly UI: server clock, rollover, arithmetic display, state percentages, source races and failure isolation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
