// Synthetic samples only; never connects to KEPServer or changes business/model state.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const storage = new Map();
function setup() {
    const {App,ctx} = require('./app_vm')();
    ctx.localStorage = {getItem:k=>storage.get(k) || null,setItem:(k,v)=>storage.set(k,v)};
    ctx.window.Operations = {telemetryFailed:false,telemetry:{items:[]}};
    vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/decision-overview.js'),'utf8'),ctx);
    const page = vm.runInContext('DecisionOverviewPage',ctx);
    page.loadRetainedValues();
    return {page,ops:ctx.window.Operations,App,ctx};
}
let {page,ops,App,ctx} = setup();
const before = JSON.stringify(App.store);
const start = '2026-10-08T01:00:00.000Z', later = '2026-10-08T01:00:05.000Z';
const sample = (value,when=start) => ({value,received_at:when,source_at:when,unit:'%',quality:'good',issue:null,definition_id:'v1'});
const point = {source_id:'kep',key:'ash',unit:'%',available:true,status:'good',sample:sample(42)};
ops.telemetry.items = [point];
const bind = (id,p=point) => {page.pointChoices[id] = page.pointIdentity(p);return {id};};
const ash = bind('raw-ash'), coal = bind('coal-rate');
assert.equal(page.pointDisplay(ash).value,'42 %');
assert.equal(page.pointDisplay(coal).value,'42 %');
point.sample = sample(0,later);
for (const node of [ash,coal]) {
    const held = page.pointDisplay(node);
    assert.equal(held.value,'42 %'); assert.equal(held.stopped,true); assert.equal(held.held,true);
    assert.equal(held.available,false); assert.equal(held.sampleAt,start); assert.equal(held.valueSourceAt,start);
    assert.match(held.source,/设备停机.*沿用/);
}
point.sample = sample(43,later);
assert.equal(page.pointDisplay(ash).value,'43 %');
assert.equal(page.pointDisplay(ash).stopped,false);
// Initial page load can obtain an earlier nonzero from the server, without seeing it live.
page.retainedValues = {};
point.sample = sample(0,later); point.last_nonzero_sample = sample(41);
assert.equal(page.pointDisplay(ash).value,'41 %');
point.last_nonzero_sample = null; point.sample = {...sample(0),definition_id:'v2'};
assert.equal(page.pointDisplay(ash).value,'—','A changed point definition cannot reuse the old value');
point.sample = sample(0); point.unit = 't/h';
assert.equal(page.pointDisplay(ash).value,'—','Unit changes cannot reuse cached percent');
point.unit = '%'; point.sample = sample(false);
assert.equal(page.pointDisplay(ash).value,'0 %'); assert.equal(page.pointDisplay(ash).stopped,false);
point.sample = {...sample(0),quality:'bad',issue:'bad_quality'}; point.available = false;
assert.equal(page.pointDisplay(ash).stopped,false,'A bad quality zero is not proof of a stop');
point.available = true; point.sample = sample(0);
assert.equal(page.pointDisplay(bind('other')).value,'0 %','Other numeric cards preserve real zeros');
// Unrelated numeric tags retain their old semantics; the new rule targets A303/A304.
const density = bind('density-current');
point.sample = sample(1.48); assert.equal(page.pointDisplay(density).value,'1.48 %');
point.sample = sample(1,later); assert.equal(page.pointDisplay(density).value,'1 %');
point.available = false; point.sample = {...sample(null),quality:'bad',issue:'bad_quality'};
let held = page.pointDisplay(density);
assert.equal(held.value,'1 %'); assert.equal(held.available,false); assert.equal(held.held,true);
ops.telemetryFailed = true; ops.telemetry = null;
assert.equal(page.pointDisplay(density).value,'—','Missing point identity must never reuse a legacy density cache');
// Reload while the very first request is unavailable: use the persisted display memory.
const reloaded = setup();
reloaded.page.pointChoices['density-current'] = page.pointChoices['density-current'];
reloaded.ops.telemetry = null; reloaded.ops.telemetryFailed = true;
assert.equal(reloaded.page.pointDisplay(density).value,'—');
reloaded.page.pointChoices['density-current'] = JSON.stringify(['other-source','ash']);
assert.equal(reloaded.page.pointDisplay(density).value,'—','Different source cannot inherit a value');
// Use per-channel server decisions; raw and legacy whole-pair values remain diagnostic.
const effective = (value,status='held') => ({policy:'density-per-channel-nonone-v1',value,status,
    unit:'g/cm³',received_at:start,reason:status==='held'?'A303：读数为 1.000 · 沿用本路最近非 1 有效值':'',
    components:[{label:'A303',value:1.5,status:'held',reason:'沿用本路最近非 1 有效值',received_at:start},
        {label:'A304',value:1.6,status:'good',reason:'',received_at:later}]});
const average = {...point,key:'a303_a304_density_average',kind:'numeric_average',sample:{...sample(1.3),definition_id:'pair'},
    last_valid_sample:{...sample(1.3),definition_id:'pair'},effective_density:effective(1.55)};
ops.telemetryFailed = false; ops.telemetry = {items:[],derived_items:[average]};
assert.equal(page.pointDisplay(bind('density-current',average)).value,'1.550 g/cm³');
assert.equal(page.pointDisplay(density).held,true);
assert.match(page.pointDisplay(density).title,/A303.*非 1.*A304/);
ctx.localStorage.setItem = () => {throw Error('storage disabled');};
average.effective_density = effective(null,'missing');
assert.equal(page.pointDisplay(density).value,'—','No per-channel history cannot fall back to old mean');
average.effective_density = effective(1.55,'good');
ops.telemetryFailed = true;
assert.equal(page.pointDisplay(density).held,true);
assert.equal(page.pointDisplay(density).available,false);
ops.telemetryFailed = false;
const a303 = {...point,available:true,item_id:'TIA_4_1.TIA_4_1.A303/MD',sample:sample(1),effective_density:effective(1.47)};
ops.telemetry.items = [a303];
assert.equal(page.pointDisplay(bind('density-current',a303)).value,'1.470 g/cm³');
assert.equal(page.pointDisplay(bind('other',a303)).value,'1 %','Raw diagnostic display remains truthful');
assert.equal(JSON.stringify(App.store),before,'Display retention never modifies models, records or business state');
console.log('Retained telemetry: zero holds, channel density policy, missing history, old-cache isolation, outages and read-only display passed.');
