// Pure VM acceptance: no production requests, storage writes or real timers.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const stamp = '2026-10-17T04:05:06+00:00';
const makeBundle = () => ({schema_version:1, generated_at:stamp, health:{ok:true,status:'ok'},
    telemetry:{sources:[{source_id:'kep',state:'connected'}],items:[{
        source_id:'kep',key:'switch',available:true,status:'good',
        sample:{value:false,received_at:stamp,source_time:'2026-10-17T04:05:05+00:00'},
    }]},
    manual_measurements:{items:[{source_id:'manual-measurements',kind:'manual_measurement',key:'heavy_clean_coal_ash',
        unit:'%',available:true,status:'good',sample:{value:0,record_id:9,observed_at:'2026-10-17T02:47:00+00:00',created_at:stamp}}]},
});
const reply = value => ({ok:true,status:200,json:async()=>value});

function fixture(readOnly = true, protocol = 'http:') {
    const requests = [], events = {}, timers = new Map(), timeouts = [];
    let timerId = 0, renders = 0;
    const ctx = vm.createContext({console, URLSearchParams,
        AbortSignal:{timeout(ms){timeouts.push(ms);return undefined;}},
        setTimeout(callback,ms){const id=++timerId;timers.set(id,{callback,ms});return id;},
        clearTimeout(id){timers.delete(id);},
        window:{location:{protocol},addEventListener(name,callback){events[name]=callback;}},
        document:{visibilityState:'visible',
            querySelector(selector){return readOnly && selector==='meta[name="dmcs-readonly"]'?{content:'true'}:null;},
            getElementById(){return null;},addEventListener(name,callback){events[name]=callback;}},
        fetch:async(url,options={})=>{requests.push({url,options});return reply(makeBundle());},
    });
    for (const name of ['api','operations']) vm.runInContext(
        fs.readFileSync(path.join(__dirname,`../../frontend/js/${name}.js`),'utf8'),ctx);
    const Api=ctx.window.Api,ops=ctx.window.Operations;
    ops.render=()=>{renders++;};
    return {ctx,Api,ops,requests,events,timers,timeouts,renders:()=>renders};
}

(async()=>{
    const a=fixture();
    let finish;
    a.ctx.fetch=(url,options)=>{a.requests.push({url,options});return new Promise(resolve=>{finish=resolve;});};
    const first=a.Api.getReadOnlyOperations(), duplicate=a.Api.getReadOnlyOperations();
    assert.equal(first,duplicate,'all browser consumers share the same in-flight bundle');
    assert.equal(a.requests.length,1);
    const bundle=makeBundle();finish(reply(bundle));
    assert.equal(await first,bundle);
    assert.equal(a.Api._readOnlyOperationsInFlight,null);
    assert.equal(a.timeouts[0],10000);
    const fresh=a.Api.getReadOnlyOperations();
    assert.equal(a.requests.length,2,'browser does not keep a completed bundle cache');
    finish(reply(makeBundle()));await fresh;

    const b=fixture();
    b.Api.getHealth=b.Api.getTelemetry=b.Api.getManualMeasurementPoints=()=>{throw Error('legacy reads must not run');};
    let release;
    b.ctx.fetch=(url,options)=>{b.requests.push({url,options});return new Promise(resolve=>{release=resolve;});};
    const refreshing=b.ops.refresh();
    assert.equal(b.ops.refresh(),refreshing);
    const manual=b.ops.refreshManualMeasurements();
    assert.equal(b.requests.length,1,'manual-only and full refresh coalesce at the API boundary');
    release(reply(bundle));await Promise.all([refreshing,manual]);
    assert.equal(b.ops.serverState,'online');
    assert.equal(b.ops.telemetryFailed,false);assert.equal(b.ops.manualMeasurementsFailed,false);
    assert.equal(b.ops.telemetry.items[0].sample.value,false);
    assert.equal(b.ops.telemetry.items[0].sample.received_at,stamp);
    assert.equal(b.ops.telemetry.items[0].sample.source_time,'2026-10-17T04:05:05+00:00');
    assert.equal(b.ops.manualMeasurements.items[0].sample.value,0);
    assert.equal(b.ops.manualMeasurements.items[0].sample.observed_at,'2026-10-17T02:47:00+00:00');
    assert.equal(b.ops.manualMeasurements.items[0].sample.created_at,stamp);
    assert.equal([...b.timers.values()][0].ms,5000,'poll delay starts after completion');

    const oldTelemetry=b.ops.telemetry,oldManual=b.ops.manualMeasurements;
    const partial=makeBundle();partial.telemetry=null;partial.errors={telemetry:{error_code:'readonly_telemetry_unavailable'}};
    b.ctx.fetch=async()=>reply(partial);await b.ops.refresh();
    assert.equal(b.ops.serverState,'online','telemetry failure is independent of service health');
    assert.equal(b.ops.telemetryFailed,true);assert.equal(b.ops.telemetry,oldTelemetry);
    assert.equal(b.ops.manualMeasurementsFailed,false);
    const opposite=makeBundle();opposite.manual_measurements=null;
    opposite.errors={manual_measurements:{error_code:'readonly_manual_unavailable'}};
    b.ctx.fetch=async()=>reply(opposite);await b.ops.refresh();
    assert.equal(b.ops.telemetryFailed,false);assert.equal(b.ops.telemetry,opposite.telemetry);
    assert.equal(b.ops.manualMeasurementsFailed,true);
    assert.equal(b.ops.manualMeasurements,partial.manual_measurements,'failed section keeps only its last display snapshot');
    const unavailable=makeBundle();unavailable.health={ok:false,status:'unavailable'};
    unavailable.errors={health:{error_code:'readonly_health_unavailable'}};
    b.ctx.fetch=async()=>reply(unavailable);await b.ops.refresh();
    assert.equal(b.ops.serverState,'unavailable');
    assert.equal(b.ops.telemetryFailed,false);assert.equal(b.ops.manualMeasurementsFailed,false);

    const malformed=[{...makeBundle(),schema_version:2},{...makeBundle(),generated_at:'invalid'},
        {...makeBundle(),health:{status:'ok'}},{...makeBundle(),telemetry:null},
        {...makeBundle(),manual_measurements:null},{...makeBundle(),manual_measurements:{items:[]}},
        {...makeBundle(),manual_measurements:{items:[{...bundle.manual_measurements.items[0],sample:{value:0}}]}},
        {...makeBundle(),errors:{telemetry:{}}}];
    const missing=makeBundle();delete missing.telemetry;malformed.push(missing);
    for(const invalid of malformed){
        a.ctx.fetch=async()=>reply(invalid);
        await assert.rejects(a.Api.getReadOnlyOperations(),/响应格式异常/);
        assert.equal(a.Api._readOnlyOperationsInFlight,null,'invalid responses release the single-flight slot');
    }

    const c=fixture();c.ops.telemetry=oldTelemetry;c.ops.manualMeasurements=oldManual;
    c.ctx.fetch=async(url)=>{c.requests.push({url});
        if(url.endsWith('/health/live'))return reply({status:'ok',version:'test'});
        throw Error('bundle unavailable');};
    await c.ops.refresh();
    assert.equal(c.ops.serverState,'busy','constant liveness distinguishes a slow read from a dead process');
    assert.equal(c.ops.telemetryFailed,true);assert.equal(c.ops.manualMeasurementsFailed,true);
    assert.equal(c.ops.telemetry,oldTelemetry);assert.equal(c.ops.manualMeasurements,oldManual);
    assert.deepEqual(c.requests.map(r=>r.url),['/api/v1/readonly-runtime/operations','/api/v1/health/live']);
    c.ctx.fetch=async()=>{throw Error('offline');};await c.ops.refresh();
    assert.equal(c.ops.serverState,'offline');
    c.ctx.fetch=async()=>reply(makeBundle());await c.ops.refresh();
    assert.equal(c.ops.serverState,'online');assert.equal(c.ops.telemetryFailed,false);assert.equal(c.ops.manualMeasurementsFailed,false);

    const d=fixture();d.ops.init();await d.ops._inFlight;
    assert.equal(d.requests.length,1);assert.equal(d.timers.size,1);
    d.ctx.document.visibilityState='hidden';d.events.visibilitychange();
    assert.equal(d.timers.size,0);d.events.online();
    assert.equal(d.requests.length,1,'hidden read-only tabs do not resume on an online event');
    await d.ops.refresh();assert.equal(d.timers.size,0,'a completed hidden refresh schedules no poll');
    d.ctx.document.visibilityState='visible';d.events.visibilitychange();await d.ops._inFlight;
    assert.equal(d.requests.length,3);assert.equal(d.timers.size,1);

    const renderFailure=fixture();
    renderFailure.ops.render=()=>{throw Error('one-time render failure');};
    await assert.rejects(renderFailure.ops.refresh(),/one-time render failure/,'render failures remain observable');
    assert.equal(renderFailure.ops._inFlight,null,'render failure must release the refresh slot');
    assert.equal(renderFailure.timers.size,1,'render failure still schedules a visible page retry');
    renderFailure.ops.render=()=>{};
    await renderFailure.ops.refresh();
    assert.equal(renderFailure.requests.length,2,'a later refresh can retry after a render failure');
    assert.equal(renderFailure.ops._inFlight,null);
    assert.equal(renderFailure.ops.serverState,'online');

    const prod=fixture(false);prod.ctx.document.visibilityState='hidden';
    prod.ctx.fetch=async(url)=>{prod.requests.push({url});
        return reply(url.endsWith('/health')?{status:'ok'}:url.endsWith('/latest')&&url.includes('manual')?bundle.manual_measurements:bundle.telemetry);};
    await prod.ops.refresh();await prod.ops._manualInFlight;
    assert.equal(prod.ops.serverState,'online');assert.equal(prod.requests.length,3);
    assert(prod.requests.every(r=>!r.url.includes('readonly-runtime')),'production preserves all existing read endpoints');
    await assert.rejects(prod.Api.getReadOnlyOperations(),/仅用于测试服/);
    const local=fixture(false,'file:');await local.ops.refresh();await local.ops.refreshManualMeasurements();
    assert.equal(local.requests.length,0);assert.equal(local.renders(),1);
    console.log('PASS: read-only Operations bundle, coalescing, partial failures, source times/zero/false, liveness fallback, visibility, production and file behavior');
})().catch(error=>{console.error(error);process.exitCode=1;});
