// Read the real server, construct the same UI snapshot, verify calculations.
// Does not write business data, train, switch models or touch a PLC.
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {App,ctx}=require('./app_vm')();
for(const file of ['decision-overview.js','decision-density-display.js','decision-realtime.js']) vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const page=vm.runInContext('DecisionOverviewPage',ctx),live=vm.runInContext('DecisionRealtime',ctx);
async function main() {
    const base='http://127.0.0.1:8000/api/v1';
    const read=async path=>{const r=await fetch(base+path);assert.ok(r.ok);return r.json();};
    const [store,telemetry,manualMeasurements]=await Promise.all(['/state','/telemetry/latest','/manual-measurements/latest'].map(read));
    App.store=store;ctx.window.Operations={telemetry,manualMeasurements,telemetryFailed:false,manualMeasurementsFailed:false};
    page.applyDefaultManualPoint();live.bindDefaults(page);page.snapshot=page.buildSnapshot();page.nodes=page.buildNodes(page.snapshot);
    const body=live.capture(page),before=JSON.stringify(App.store);
    const response=await fetch(base+'/decision/snapshots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    assert.ok(response.ok,await response.clone().text());const result=await response.json();
    const group=App.coarseEngine(store.coarseModel)==='gpt'?store.coarseModel:store.coarseModelVariants?.gpt;
    if(result.coarse_ash.value!==null) {
        const expected=App._coarsePredict(Object.fromEntries(App.MLR_FEATURES.map(k=>[k,body.cells[k].value])),group[group.production]);
        assert.ok(Math.abs(expected-result.coarse_ash.value)<1e-6);
    }
    if(result.heavy_amount.value!==null) assert.ok(Math.abs(page.heavyAmountDisplay().numericValue-result.heavy_amount.value)<1e-6);
    if(result.total_ash.value!==null) {
        const c=body.cells;
        const expected=App.calcTotalAsh(c.heavy_ash.value,result.heavy_amount.value,c.float_ash.value,c.float_amount.value,result.coarse_ash.value,c.coarse_amount.value);
        assert.ok(Math.abs(expected-result.total_ash.value)<1e-6);
    }
    assert.equal(JSON.stringify(App.store),before);
    process.stdout.write(JSON.stringify({verified:true,request:body,result},null,2));
}
main().catch(error=>{console.error(error);process.exitCode=1;});
