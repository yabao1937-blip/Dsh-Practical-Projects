// Isolated browser-state races: no network, storage writes or real database.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {App,ctx,elements}=require('./app_vm')();
for(const file of ['decision-overview.js','decision-realtime.js','decision-sources.js'])
    vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const page=vm.runInContext('DecisionOverviewPage',ctx),sources=vm.runInContext('DecisionSources',ctx),live=vm.runInContext('DecisionRealtime',ctx);
const copy=x=>JSON.parse(JSON.stringify(x)),choice='["kep","float_amount"]';
ctx.window.location.protocol='http:';
page.view='gpt';page.renderGraph=page.renderContext=page.renderInspector=()=>{};
page.active=()=>true;page.displayPoints=()=>[];
page.pointChoices={'float-amount':choice,'density-current':'["kep","a303"]'};
const calls=[],toasts=[];
App.showToast=(message,kind)=>toasts.push({message,kind});
let config={revision:0,choices:{},resolved_choices:{'density-current':'["kep","a304"]'},density_label:'A304'};
ctx.Api=ctx.window.Api={getDecisionCurrent:async()=>({sources:config,cells:{}}),decisionSources:async body=>{
    if(body){calls.push(copy(body));config={...config,revision:config.revision+1,choices:{...config.choices,...body.choices}};
        config.resolved_choices={...config.resolved_choices,...body.choices};}
    return copy(config);
}};
async function verify(){
    await sources.ensure(page);
    assert.deepEqual(calls[0],{revision:0,choices:{'float-amount':choice}},'Cold start retains bindings before telemetry loads; old density choice was not used by live GPT');
    assert.equal(page.pointChoices['density-current'],'["kep","a304"]');
    page.view='production';page.pointChoices={'heavy-ash':'["kep","new"]'};page.savePointChoices();
    assert.equal(sources.legacyChoices['heavy-ash'],'["kep","new"]');page.view='gpt';
    const update=live.update; live.update=()=>{};
    elements['decision-source-point']={disabled:false};
    const api=ctx.Api.decisionSources;
    ctx.Api.decisionSources=async body=>{const result=await api(body);if(body) throw Error('response timeout');return result;};
    await sources.change(page,'density-current','["kep","a303"]');
    assert.equal(sources.config.choices['density-current'],'["kep","a303"]');
    assert.equal(sources.error,'');assert.equal(toasts.at(-1).kind,'success','Readback confirms committed PATCH after a lost response');
    assert.equal(elements['decision-source-point'].disabled,true);
    live.result={cells:{float_ash:{value:9,status:'historical',source:'历史浮精记录',unit:'%'}}};
    assert.equal(sources.display(page,{id:'float-ash'}).badge,'历史');
    live.result={cells:{level:{value:75,status:'good',source:'KEPServer · 提前 90 秒',unit:'%',
        observed_at:'2026-10-12T01:29:30Z',reason:'采用预测时刻前 90 秒的液位',
        alignment:{version:'gpt-level-lag-v1',target_at:'2026-10-12T01:29:30Z',prediction_at:'2026-10-12T01:31:00Z'}}}};
    const delayed=sources.display(page,{id:'tail-level'});
    assert.equal(delayed.numericValue,75);assert.equal(delayed.badge,'提前90秒');
    assert.equal(delayed.sampleAt,'2026-10-12T01:29:30Z');assert.equal(delayed.available,true);
    live.result.cells.level={...live.result.cells.level,value:null,status:'missing',reason:'提前 90 秒液位缺失'};
    assert.equal(sources.display(page,{id:'tail-level'}).available,false);
    assert.equal(sources.display(page,{id:'tail-level'}).value,'—');
    ctx.Api.decisionSources=api;live.update=update;
    const pending=[];
    ctx.Api.getDecisionCurrent=()=>new Promise(resolve=>pending.push(resolve));
    live.inFlight=null;live.lastAttempt=0;
    live.update(page);await Promise.resolve();
    const first=live.inFlight;sources.epoch++;
    pending.shift()({sources:config,cells:{density:{value:99}},policy:'decision-live-v1'});
    await first;await Promise.resolve();
    assert.notEqual(live.result?.cells?.density?.value,99,'Changed source invalidates old pending result');
    const second=live.inFlight;
    pending.shift()({sources:config,cells:{density:{value:1.42}},policy:'decision-live-v1'});
    await second;
    assert.equal(live.result.cells.density.value,1.42);
    assert.equal(page.pointChoices['density-current'],'["kep","a303"]');
    console.log('Shared source migration, production preferences, lost-response confirmation, badges and response races passed');
}
verify().catch(error=>{console.error(error);process.exitCode=1;});
