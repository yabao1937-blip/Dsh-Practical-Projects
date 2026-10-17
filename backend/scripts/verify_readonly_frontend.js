// Isolated browser-state fixtures: no production network, database or storage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(readOnly, failState = false) {
    const oldStore = {__merged:2, coarseCoal:[{id:'old-laptop'}], rawSlime:[{id:'old-raw'}],
        heavySamples:[{client_id:'old-unsent', synced:false}], _revision:'laptop'};
    const host = {coarseCoal:[{id:'host'}], floatCoal:[], calcLogs:[], heavySamples:[{client_id:'host-sample',synced:false}],
        rawSlime:[], manualEntries:[{id:'host-entry'}], _revision:'host'};
    const pending = JSON.stringify({wall:1,stamp:'old-pending',body:JSON.stringify(oldStore)});
    const storage = new Map([['dmcs_store',JSON.stringify(oldStore)], ['dmcs_mirror_pending',pending], ['theme','dark']]);
    const writes = [], requests = [], timers = [], toasts = [], timeouts = [];
    const elements = {'status-sync':{}, 'main-content':{hidden:false},
        'dmcs-readonly-status':{textContent:'测试服 · 本地前后端代码 · 主机数据只读：http://host.invalid/'}};
    const events = {};
    const ctx = vm.createContext({console, Blob, URLSearchParams,
        AbortSignal:{timeout(value){timeouts.push(value);return undefined;}},
        setTimeout(callback){timers.push(callback);return timers.length;},clearTimeout(){},
        window:{location:{protocol:'http:',hash:''},addEventListener(name,callback){events[name]=callback;}},
        document:{visibilityState:'visible',
            querySelector(selector){
                if (selector === 'main.main-content') return elements['main-content'];
                return readOnly && selector === 'meta[name="dmcs-readonly"]' ? {content:'true'} : null;
            },
            addEventListener(name,callback){events[name]=callback;},
            getElementById(id){return elements[id] || null;}},
        localStorage:{getItem(key){return storage.get(key)||null;},setItem(key,value){writes.push(key);storage.set(key,value);},
            removeItem(key){writes.push(key);storage.delete(key);}},
        fetch:async(url,options={})=>{
            requests.push({url,options});
            if (url.endsWith('/state') && (!options.method || options.method === 'GET')) {
                if (failState) throw new Error('host unavailable');
                return {ok:true,json:async()=>JSON.parse(JSON.stringify(host))};
            }
            return {ok:true,status:200,json:async()=>({ok:true,revision:'saved'})};
        },
    });
    for (const name of ['api','app','operations']) vm.runInContext(
        fs.readFileSync(path.join(__dirname,`../../frontend/js/${name}.js`),'utf8'), ctx);
    const App = vm.runInContext('App',ctx);
    App.showToast = (message,kind)=>toasts.push({message,kind});
    for (const name of ['_syncGuideSchemeBtn','_syncTotalAshBtn','bindNav','startClock','checkAlerts',
        'initPage','_onExternalInput','refreshAllPages']) App[name]=()=>{};
    return {App,Api:ctx.window.Api,ops:ctx.window.Operations,ctx,host,storage,writes,requests,timers,toasts,timeouts,elements,events,pending};
}

async function pollingFixture(readOnly, name) {
    let now=100000;
    const pending=[];
    const ctx=vm.createContext({console,
        Date:class extends Date {static now(){return now;}},
        DecisionDensityDisplay:{remember(){}},
        DecisionSources:{epoch:0,saving:false,config:{resolved_choices:{}},ensure:async()=>({}),apply:()=>true},
    });
    ctx.Api={isReadOnlyTest:()=>readOnly}; ctx.window={Api:ctx.Api};
    ctx.Api[name==='DecisionRealtime'?'getDecisionCurrent':'getDecisionHourly']=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
    vm.runInContext(fs.readFileSync(path.join(__dirname,'../../frontend/js/'+
        (name==='DecisionRealtime'?'decision-realtime.js':'decision-hourly.js')),'utf8'),ctx);
    const controller=vm.runInContext(name,ctx);
    const page={view:'gpt',active:()=>true,hourMode:()=>false,renderGraph(){},renderContext(){},renderInspector(){}};
    const update=force=>name==='DecisionRealtime'?controller.updateShared(page,force):controller.update(page,force);
    const tick=async()=>{for(let i=0;i<6;i++)await Promise.resolve();};
    const finish=async(fail=false)=>{
        const flight=controller.inFlight;
        const next=pending.at(-1);
        if(fail)next.reject(new Error('slow host read failed'));
        else next.resolve(name==='DecisionRealtime'?{cells:{},sources:{}}:{status:'pending',reason:'calculating'});
        await flight;await tick();
    };
    return {ctx,controller,pending,update,tick,finish,advance:ms=>{now+=ms;}};
}

(async()=>{
    const readOnly = fixture(true);
    let historyChanges=0;
    readOnly.App._completeDensityDecisionResponses=readOnly.App.recomputeInfluenceValues=()=>historyChanges++;
    readOnly.ctx.window.DMCS_SEED_STORE={__merged:2,coarseCoal:[{id:'demo'}]};
    await readOnly.App.init();
    assert.equal(readOnly.App.store.coarseCoal[0].id,'host','initial state comes from host even when old local store is populated');
    assert.equal(readOnly.App.store.heavySamples.length,1,'never append local unsent samples to host state');
    assert.equal(readOnly.App.store.manualEntries[0].id,'host-entry','host business data is authoritative');
    assert.equal(historyChanges,0,'read-only startup does not rewrite historical calculations');
    assert.equal(readOnly.requests.length,1,'initialization reads state once and sends no mirror/sample writes');
    assert.equal(readOnly.requests[0].url,'/api/v1/state','local backend remains the API entrypoint');
    assert.equal(readOnly.elements['main-content'].hidden,false,'host data success reveals business pages');
    assert.match(readOnly.elements['dmcs-readonly-status'].textContent,/主机数据只读：http:\/\/host\.invalid\//);
    readOnly.App.saveStore();
    readOnly.App._scheduleMirror('{}');
    await readOnly.App._flushMirror(true);
    readOnly.App.retryMirrorIfPending();
    await readOnly.App.syncHeavySamples();
    readOnly.events.pagehide();
    readOnly.events.online();
    assert.equal((await readOnly.App.flushMirrorNow()).readOnly,true,'explicit persistence also reports read-only');
    assert.equal(readOnly.requests.length,1,'automatic retries, unload and save must send zero write requests');
    assert.equal(readOnly.writes.length,0,'do not store business snapshots, clear pending data, or overwrite UI preferences');
    assert.equal(readOnly.storage.get('dmcs_mirror_pending'),readOnly.pending,'old pending record stays untouched and is never replayed');
    assert.equal(readOnly.storage.get('theme'),'dark');
    readOnly.ops.renderSync();
    assert.equal(readOnly.elements['status-sync'].textContent,'只读主机数据');

    const blocked = [
        ()=>readOnly.Api.saveManualMeasurement({}),
        ()=>readOnly.Api.decisionSources({target:8.5}),
        ()=>readOnly.Api.decisionEvidence('/feedback',{}),
        ()=>readOnly.Api.reconnectTelemetry(),
        ()=>readOnly.Api.syncHeavySample({}),
        ()=>readOnly.Api.retrainCoarseModel('all'),
    ];
    for (const write of blocked) await assert.rejects(write,/测试服只读主机数据/);
    assert.equal((await readOnly.Api.putStateBody('{}')).readOnly,true);
    assert.equal(readOnly.requests.length,1,'manual mutations fail explicitly before fetch');

    const offline=fixture(true,true);
    offline.ctx.window.DMCS_SEED_STORE={__merged:2,coarseCoal:[{id:'demo'}]};
    await offline.App.init();
    assert.equal(offline.App.store.coarseCoal.length,0,'host read failure must not fall back to old laptop data or seed');
    assert.equal(offline.App.store.heavySamples.length,0);
    assert.match(offline.App._readOnlyLoadError,/主机数据读取失败/);
    assert.match(offline.toasts[0].message,/未加载笔记本历史库或演示数据/);
    offline.ops.renderSync();
    assert.equal(offline.elements['status-sync'].textContent,'主机数据读取失败');
    assert.equal(offline.elements['main-content'].hidden,true,'failed host load hides legacy instrument defaults and business pages');
    assert.match(offline.elements['dmcs-readonly-status'].textContent,/主机数据读取失败.*刷新页面重试/);
    assert.equal(offline.writes.length,0);
    offline.ctx.fetch=async()=>({ok:true,json:async()=>offline.host});
    await offline.App.refreshReadOnlyState();
    assert.equal(offline.elements['main-content'].hidden,false,'successful retry restores business pages');
    assert.match(offline.elements['dmcs-readonly-status'].textContent,/主机数据只读：http:\/\/host\.invalid\//,
        'successful retry restores original test-server banner');

    const production=fixture(false);
    assert.equal(production.Api.isReadOnlyTest(),false);
    assert.equal(production.App.loadStore(),true,'no meta retains normal local store behavior');
    assert.equal(production.App.store.coarseCoal[0].id,'old-laptop');
    assert.ok(production.App._readPendingSlot(),'production still recognizes its durable pending snapshot');
    production.App._syncReady=true;
    production.App._scheduleMirror(JSON.stringify(production.App.store));
    await production.App._flushMirror(false);
    assert.equal(production.requests.length,1);
    assert.equal(production.requests[0].options.method,'PUT','no meta retains real mirror path');
    assert.equal(production.App.store._revision,'saved');
    assert.equal(production.App._readPendingSlot(),null,'normal successful write clears its own pending slot');

    for(const f of [readOnly,production]) {
        f.ctx.fetch=async url=>({ok:true,json:async()=>url.endsWith('/decision/current')
            ?{policy:'decision-live-v1',cells:{},sources:{}}
            :{policy:'decision-hourly-v2',cells:{},period:{},coverage:{}}});
        await f.Api.getDecisionCurrent(); await f.Api.getDecisionHourly();
        assert.deepEqual(f.timeouts.slice(-2),f.Api.isReadOnlyTest()?[30000,30000]:[8000,8000],
            'only read-only mode extends current/hourly timeouts');
    }
    for(const name of ['DecisionRealtime','DecisionHourly']) {
        const poll=await pollingFixture(true,name);
        poll.update();poll.update();await poll.tick();
        assert.equal(poll.pending.length,1,name+' coalesces in-flight polling');
        poll.advance(15000);await poll.finish();
        poll.advance(29999);poll.update();await poll.tick();
        assert.equal(poll.pending.length,1,name+' waits 30 seconds after slow completion');
        poll.advance(1);poll.update();await poll.tick();
        assert.equal(poll.pending.length,2);
        poll.advance(15000);await poll.finish(true);
        poll.advance(29999);poll.update();await poll.tick();
        assert.equal(poll.pending.length,2,name+' also throttles failed reads after completion');
        poll.update(true);await poll.tick();
        assert.equal(poll.pending.length,3,name+' explicit refresh bypasses cooldown');
        poll.update(true);poll.update(true);await poll.tick();
        assert.equal(poll.pending.length,3,name+' force requests never overlap an existing flight');
        await poll.finish();
        assert.equal(poll.pending.length,4,name+' merges queued force refreshes into one follow-up');
        poll.ctx.DecisionSources.epoch++;
        poll.update();poll.update();await poll.tick();
        assert.equal(poll.pending.length,4,name+' epoch changes also wait for the old flight');
        await poll.finish();
        assert.equal(poll.pending.length,5,name+' new epoch refresh is not lost to the cooldown');
        await poll.finish();
        poll.ctx.DecisionSources.epoch++;
        poll.update();await poll.tick();
        assert.equal(poll.pending.length,6,name+' new epoch bypasses cooldown without a flight');
        await poll.finish();

        const normal=await pollingFixture(false,name);
        normal.update();await normal.tick();normal.advance(15000);await normal.finish();
        normal.update();await normal.tick();
        assert.equal(normal.pending.length,2,name+' production keeps its original 4.5-second start-based interval');
        await normal.finish();
    }
    console.log('Read-only frontend: host-only initialization, offline empty failure, zero automatic writes, explicit mutation rejection, and normal production mirrors passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
