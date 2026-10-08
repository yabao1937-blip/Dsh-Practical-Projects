// No browser/network/database writes: manual point display and refresh races.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App,ctx,elements} = require('./app_vm')();
for (const file of ['operations.js','decision-overview.js']) {
    vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js',file),'utf8'),ctx);
}
const page = vm.runInContext('DecisionOverviewPage',ctx), ops = ctx.window.Operations;
const before = JSON.stringify(App.store);
const point = {source_id:'manual-measurements',key:'heavy_clean_coal_ash',kind:'manual_measurement',
    name:'重介精煤灰分',device:'手工补录',tag_name:'重介精煤灰分',unit:'%',available:true,status:'good',
    sample:{value:8.5,record_id:1,observed_at:'2026-10-08T03:04:13.579+00:00',created_at:'2026-10-08T03:05:00.000+00:00'}};
ops.manualMeasurements = {items:[point]};
ops.telemetryFailed = true;
page.applyDefaultManualPoint();
assert.equal(page.pointChoices['heavy-ash'], JSON.stringify(['manual-measurements','heavy_clean_coal_ash']));
page.snapshot = page.buildSnapshot();
page.nodes = page.buildNodes(page.snapshot);
const node = page.nodes.find(n => n.id === 'heavy-ash');
let display = page.pointDisplay(node);
assert.equal(display.value,'8.5 %');
assert.equal(display.available,true,'KEP failure does not disable manual database records');
assert.equal(display.sampleAt,point.sample.observed_at);
assert.equal(display.enteredAt,point.sample.created_at);
assert.match(display.source,/手工补录/);
elements['decision-inspector'] = {};
page.selected='heavy-ash';
page.renderInspector();
assert.match(elements['decision-inspector'].innerHTML,/SQLite · manual_measurements/);
assert.match(elements['decision-inspector'].innerHTML,/卡片显示值<\/dt><dd>8.5 %/);
assert.match(elements['decision-inspector'].innerHTML,/手工补录 · 重介精煤灰分/);
assert.doesNotMatch(elements['decision-inspector'].innerHTML,/实时采集<\/dd>/);
ops.manualMeasurementsFailed = true;
display = page.pointDisplay(node);
assert.equal(display.value,'8.5 %');
assert.equal(display.available,false);
assert.equal(display.held,true);
assert.match(display.source,/补录查询失败 · 上次读取值/);
ops.manualMeasurementsFailed=false;
point.available=false; point.sample.value=null;
assert.equal(page.pointDisplay(node).value,'—');
point.available=true; point.sample.value=0;
assert.equal(page.pointDisplay(node).value,'0 %','Manual zero is not device stoppage');
assert.equal(page.pointDisplay(node).stopped,false);
page.pointChoices['heavy-ash']='';
page.applyDefaultManualPoint();
assert.equal(page.pointDisplay(node).bound,false,'Explicit opt-out survives periodic refresh');
ctx.localStorage.getItem=()=>JSON.stringify({'heavy-ash':''});
page.loadPointChoices(); page.applyDefaultManualPoint();
assert.equal(page.pointDisplay(node).bound,false,'Explicit opt-out survives reload');
page.pointChoices['heavy-ash']='["kep","p"]';
page.applyDefaultManualPoint();
assert.equal(page.pointChoices['heavy-ash'],'["kep","p"]','Existing explicit sources are preserved');
assert.equal(JSON.stringify(App.store),before,'No model inputs, training state, or legacy business state changes');

(async () => {
    ctx.window.location.protocol='http:';
    let resolvePoll, requests=0;
    ctx.window.Api={getManualMeasurementPoints:()=>{
        requests++;
        if(requests===1) return new Promise(resolve=>{resolvePoll=resolve;});
        return Promise.resolve({items:[{...point,sample:{...point.sample,value:8.9,record_id:2}}]});
    }};
    const poll=ops.refreshManualMeasurements();
    const afterSave=ops.manualMeasurementSaved();
    resolvePoll({items:[point]});
    await Promise.all([poll,afterSave]);
    assert.equal(requests,2,'POST completion must refresh after any older in-flight GET');
    assert.equal(ops.manualMeasurements.items[0].sample.value,8.9);
    ctx.window.Api.getManualMeasurementPoints=async()=>{throw new Error('offline');};
    await ops.refreshManualMeasurements();
    assert.equal(ops.manualMeasurementsFailed,true);
    assert.equal(ops.manualMeasurements.items[0].sample.value,8.9,'Failed request retains only explicitly stale cached data');
    console.log('Manual points: SQLite source, latest reading display, KEP isolation, explicit choices and save/refresh race passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
