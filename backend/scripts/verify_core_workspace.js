// Isolated navigation/loading contracts; no database, network or production writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=name=>fs.readFileSync(path.join(__dirname,'../../frontend/js/'+name+'.js'),'utf8');
const classes=(initial=[])=>{const values=new Set(initial);return {add:v=>values.add(v),remove:v=>values.delete(v),contains:v=>values.has(v),toggle(v,on){const next=on ?? !values.has(v);next?values.add(v):values.delete(v);return next;}};};
const tick=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
function navigation() {
    const ids=['page-decision','page-maintenance','page-coarse','page-overview'];
    const pages=Object.fromEntries(ids.map(id=>[id,{id,classList:classes(id==='page-decision'?['active']:[])}]));
    const items=ids.map(id=>({dataset:{page:id},classList:classes(),listeners:[],addEventListener(_,fn){this.listeners.push(fn);}}));
    const pending=[],timers=[],toasts=[],initialized=[];
    const ctx=vm.createContext({console,document:{addEventListener(){},getElementById:id=>pages[id],
        querySelector(selector){return items.find(item=>selector===`.nav-item[data-page="${item.dataset.page}"]`);},
        querySelectorAll(selector){return selector==='.nav-item'?items:selector==='.page'?Object.values(pages):[];}},
        window:{Api:{isReadOnlyTest:()=>true,getState:()=>new Promise((resolve,reject)=>pending.push({resolve,reject}))}},
        setTimeout:fn=>timers.push(fn)});
    vm.runInContext(source('app'),ctx);const app=vm.runInContext('App',ctx);
    app.showToast=(text,kind)=>toasts.push({text,kind});app.initPage=id=>initialized.push(id);
    app._readOnlyDefaults=JSON.parse(JSON.stringify(app.store));app._syncReady=false;
    return {app,ctx,pages,items,pending,timers,toasts,initialized};
}
(async()=>{
    const f=navigation();f.app.bindNav();f.app.bindNav();
    assert.ok(f.items.every(item=>item.listeners.length===1),'early navigation binds exactly once');
    f.items[2].listeners[0]({preventDefault(){}});
    assert.equal(f.app._pendingPage,'page-coarse','click during startup is retained');
    assert.equal(f.pending.length,0,'startup click cannot start a parallel full read');
    f.app._syncReady=true;
    const old=f.app.goToPage('page-coarse');
    const duplicate=f.app.goToPage('page-overview');
    assert.equal(f.pending.length,1,'historical navigation shares one full-state request');
    assert.equal(f.pages['page-decision'].classList.contains('active'),true,'core stays visible while loading');
    f.app.goToPage('page-maintenance');
    f.pending[0].resolve({coarseCoal:[{id:'host'}],floatCoal:[],calcLogs:[]});
    await Promise.all([old,duplicate]);f.timers.forEach(fn=>fn());
    assert.equal(f.pages['page-maintenance'].classList.contains('active'),true,'old completion cannot steal navigation');
    assert.deepEqual(f.initialized,['page-maintenance'],'stale scheduled pages cannot initialize');
    assert.equal(f.app.store.coarseCoal[0].id,'host');
    f.app.goToPage('page-coarse');assert.equal(f.pending.length,1,'loaded original history is reused');
    const bad=navigation();bad.app._syncReady=true;
    const fail=bad.app.goToPage('page-coarse');bad.pending[0].reject(new Error('unavailable'));await fail;
    assert.equal(bad.pages['page-decision'].classList.contains('active'),true);
    assert.match(bad.toasts.at(-1).text,/核心页面仍可使用/);
    const retry=bad.app.goToPage('page-overview');bad.pending[1].resolve({coarseCoal:[]});await retry;
    assert.equal(bad.pages['page-overview'].classList.contains('active'),true,'failed lazy load remains retryable');
    const gated=navigation();let legacy=0;
    gated.ctx.FloatPage={refresh(){legacy++;}};gated.ctx.CollectPage={loadHistory(){legacy++;}};gated.ctx.ImportPage={loadLogs(){legacy++;}};
    gated.app.refreshAllPages();assert.equal(legacy,0,'light startup never refreshes hidden legacy readers');

    let analysis=false,fetches=0;
    const modelCtx=vm.createContext({console,Date,location:{protocol:'http:'},AbortSignal,
        window:{Api:{isReadOnlyTest:()=>true},CoreWorkspace:{needsAnalysis:()=>analysis}},
        document:{addEventListener(){},getElementById:()=>({classList:{contains:()=>false}})},
        fetch:async()=>{fetches++;return {ok:true,json:async()=>({model:{production:'expert8',expert8:{kind:'gpt-expert8-no-raw-v1'},rows:[]}})};}});
    vm.runInContext(source('gpt-expert9'),modelCtx);const model=vm.runInContext('GptExpert9',modelCtx);
    model.observe({model:{method:'expert8',artifact_id:'v2'}});await tick();
    assert.equal(model.expectedArtifact,'v2');assert.equal(fetches,0,'runtime observation does not download analysis');
    assert.equal(model.lastAttempt,0,'deferred load cannot consume retry budget');
    analysis=true;await model.load(true);assert.equal(fetches,1,'explicit history permits original model loading');

    let resolveHistory,loads=0,charts=0;
    const elements={'decision-history-analysis':{open:true},'page-decision':{classList:{contains:()=>true}},
        'core-history-status':{textContent:''},'core-history-retry':{hidden:true}};
    const historyCtx=vm.createContext({console,window:{},document:{getElementById:id=>elements[id]},
        App:{ensureReadOnlyHistory:()=>new Promise(resolve=>{resolveHistory=resolve;})},
        GptExpert9:{async load(){loads++;},error:''},
        DecisionCoarseCharts:{refresh(){charts++;}},DecisionFloatAnalysis:{refresh(){charts++;}}});
    vm.runInContext(source('core-workspace'),historyCtx);const shell=vm.runInContext('CoreWorkspace',historyCtx);
    const opening=shell.loadAnalysis();elements['decision-history-analysis'].open=false;resolveHistory();await opening;
    assert.equal(loads,0,'closing during history read prevents hidden model download');
    elements['decision-history-analysis'].open=true;const reopening=shell.loadAnalysis();resolveHistory();await reopening;
    assert.equal(loads,1);assert.equal(charts,2,'reopening can complete both charts');
    console.log(JSON.stringify({passed:true,earlyNavigation:true,lazyHistorySingleFlight:true,staleNavigationProtected:true,
        historyFailureIsolated:true,hiddenLegacyReaders:0,modelObservationDeferred:true,closeReopenAnalysis:true}));
})().catch(error=>{console.error(error);process.exit(1);});
