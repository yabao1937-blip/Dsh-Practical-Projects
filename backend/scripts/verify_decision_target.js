// Controller integration with isolated server state; never writes production.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {App,ctx,elements}=require('./app_vm')();
for(const file of ['decision-realtime.js','decision-sources.js','decision-hourly.js','decision-target.js'])
    vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const [live,sources,hourly,editor]=vm.runInContext('[DecisionRealtime,DecisionSources,DecisionHourly,DecisionTarget]',ctx);
const copy=x=>JSON.parse(JSON.stringify(x));
let bindings=0;
for(const id of ['open','form','min','max','fields','cancel','status']) elements['decision-target-'+id]={
    value:'',hidden:true,disabled:false,textContent:'',setAttribute(){},focus(){},addEventListener(){bindings++;}};
const page={liveMode:()=>true,view:'gpt',active:()=>true,hourMode:()=>false,pointChoices:{},
    renderGraph(){},renderContext(){},renderInspector(){}};
ctx.window.location.protocol='http:';
let server={revision:1,target_min:8.5,target_max:9,choices:{},resolved_choices:{'density-current':'A304'}},patches=0,loseResponse=false;
ctx.Api=ctx.window.Api={getDecisionCurrent:async()=>({sources:copy(server),target:{min:server.target_min,max:server.target_max},cells:{}}),
    decisionSources:async body=>{
        if(body) {
            patches++;
            if(body.revision!==server.revision) throw Error('409 配置已被其他页面修改');
            server={...server,revision:server.revision+1,target_min:body.target_min,target_max:body.target_max};
            if(loseResponse) throw Error('保存响应超时');
        }
        return copy(server);
    }};
live.update=()=>{};
const hourlyUpdate=hourly.update;
hourly.update=()=>{};
(async()=>{
    editor.init(page);const firstBindings=bindings;editor.init(page);
    assert.equal(bindings,firstBindings,'page revisits must not duplicate handlers');
    await editor.open(page);
    assert.equal(elements['decision-target-min'].value,8.5);
    elements['decision-target-min'].value='8.65';elements['decision-target-max'].value='9.25';
    sources.apply(page,{...copy(server),revision:2});server.revision=2;
    assert.equal(elements['decision-target-min'].value,'8.65','polls never overwrite draft');
    assert.equal(patches,0,'typing does not PATCH');
    await editor.submit(page);
    assert(editor.opened,'revision conflict retains draft');
    assert(elements['decision-target-status'].textContent.includes('409'));
    assert.equal(server.target_min,8.5,'never retries stale edit using a newer revision');
    editor.close();await editor.open(page);
    elements['decision-target-min'].value='9.5';elements['decision-target-max'].value='8';
    const before=patches;await editor.submit(page);assert.equal(patches,before);
    elements['decision-target-min'].value='';await editor.submit(page);assert.equal(patches,before);
    elements['decision-target-min'].value='8.65';elements['decision-target-max'].value='9.25';
    live.result={sources:copy(server),target:{min:8.5,max:9},cells:{heavy_ash:{value:8.5}},suggested_density:{value:1.46}};
    loseResponse=true;await editor.submit(page);
    assert(!editor.opened,'readback confirms committed save despite timeout');
    assert.equal(sources.config.target_min,8.65);assert.equal(sources.config.target_max,9.25);
    assert.equal(live.result.cells.heavy_ash.value,8.5,'advice update preserves measurement cards');
    assert.equal(live.result.suggested_density.value,null,'old advice invalidated');
    assert.equal(live.result.target.min,8.65);
    assert.equal(sources.apply(page,{...copy(server),revision:1,target_min:4}),false,'late response cannot restore old config');
    live.result={...live.result,sources:copy(server),suggested_density:{value:1.46}};
    sources.apply(page,{...copy(server),revision:server.revision+1,target_max:9.5});
    assert.equal(live.result.suggested_density.value,null,'peer target update masks old instant advice');
    assert.equal(live.result.target.max,9.5);
    hourly.result={advice_sources:copy(server),suggested_density:{value:1.46},checked_at:new Date().toISOString(),
        period:{apply_end:new Date(Date.now()+3600000).toISOString()}};
    hourly.resultEpoch=sources.epoch;
    assert.equal(hourly.presentation(),null,'old hourly advice hidden when peer target is newer');
    // An hourly-only client must receive the newest objective too.
    hourly.update=hourlyUpdate;hourly.inFlight=null;
    server={...sources.config,revision:sources.config.revision+1,target_min:8.75};
    ctx.Api.getDecisionHourly=async()=>({...hourly.result,advice_sources:copy(server),target:{min:8.75,max:9.5}});
    await hourly.update(page,true);
    assert.equal(sources.config.target_min,8.75);
    assert.equal(hourly.presentation().target.min,8.75);
    console.log('Target editor: drafts, invalid input, conflicts, timeout confirmation, stale responses and cross-client sync passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
