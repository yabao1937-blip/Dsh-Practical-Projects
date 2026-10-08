// Pure VM tests and JS/Python parity; never persist fixtures or train models.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const {App,ctx,elements}=require('./app_vm')();
if(process.argv.includes('--parity-json')) {
    const cases=JSON.parse(fs.readFileSync(0,'utf8'));
    const values=cases.map(({cells,model})=>{
        const v=k=>cells[k].value;
        const coarse=App._coarsePredict(Object.fromEntries(App.MLR_FEATURES.map(k=>[k,v(k)])),model);
        const heavy=v('belt501')+v('belt502')-v('float_amount')-v('coarse_amount');
        const total=App.calcTotalAsh(v('heavy_ash'),heavy,v('float_ash'),v('float_amount'),coarse,v('coarse_amount'));
        const reader=Object.create(App);
        reader.store={...App.store,guideScheme:'total',ashTargetTol:0};
        reader.resolveDensity=()=>v('density'); reader.getHeavyAsh=()=>v('heavy_ash');
        reader.resolveTotalAsh=()=>total; reader.totalAshIsConstant=()=>false;
        reader.densityGuardState=()=>({hold:false,placeholder:false});
        reader.getDensityGuide=()=>({maxStep:.02,stepwise:true}); reader.densityGainK=()=>null;
        return {coarse_ash:coarse,heavy_amount:heavy,total_ash:total,suggested_density:reader.computeDensityGuidance(Math.min(9,Math.max(8.5,total))).rhoNew};
    });
    process.stdout.write(JSON.stringify(values)); process.exit(0);
}
for(const file of ['decision-overview.js','decision-realtime.js']) vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const page=vm.runInContext('DecisionOverviewPage',ctx), live=vm.runInContext('DecisionRealtime',ctx);
const initial=JSON.stringify(App.store);
const node=id=>page.nodes.find(n=>n.id===id);
page.snapshot=page.buildSnapshot(); page.nodes=page.buildNodes(page.snapshot);
const stamp='2026-10-08T06:00:00Z';
const point=(key,item_id,value,unit='')=>({source_id:'kep',key,item_id,available:true,status:'good',unit,
    sample:{value,quality:'good',received_at:stamp,batch_id:'batch1'}});
const raw=point('raw',live.defaults['raw-ash'],40),coal=point('coal',live.defaults['coal-rate'],600),level=point('level',live.defaults['tail-level'],50);
const density=point('a304','TIA_4_1.TIA_4_1.A304/MD',1.48);
density.effective_density={policy:'density-per-channel-nonone-v1',status:'good',value:1.48,received_at:stamp};
const groups=[['four_system_status',['A','B','401','402']],['desliming_status',['473','474']]].map(([key,labels])=>({
    ...point(key,'',1),kind:'state_group',member_count:labels.length,
    sample:{value:1,quality:'good',components:labels.map(label=>({label,value:true,available:true,received_at:stamp}))}}));
const ops=ctx.window.Operations={telemetryFailed:false,manualMeasurementsFailed:false,telemetry:{items:[raw,coal,level,density,
    point('501','TIA_3_4.TIA_3_4.501/COAL_CAP',260),point('502','TIA_3_4.TIA_3_4.502/COAL_CAP',240)],derived_items:groups}};
for(const [id,value,unit] of [['heavy-ash',8.5,'%'],['float-ash',9,'%'],['float-amount',40,'t/h'],['coarse-amount',20,'t/h']]) {
    Object.assign(node(id),{numericValue:value,value:page.number(value,2,unit),unit,source:'默认值'});
}
live.bindDefaults(page);
assert.equal(Object.keys(page.pointChoices).length,5);
let body=live.capture(page);
assert.equal(body.cells.raw_ash.value,40); assert.equal(body.cells.coal_amount.unit,'t/h');
assert.equal(body.cells.sysA.value,1); assert.equal(body.cells.desliming474.value,1);
assert.equal(body.cells.density.value,1.48); assert.equal(body.cells.belt501.value,260);
assert.equal(body.cells.is_stoppage.value,0);
coal.sample.value=0; body=live.capture(page);
assert.equal(body.cells.coal_amount.value,600,'Stopped raw coal retains the last nonzero display input');
assert.equal(body.cells.coal_amount.status,'stopped');
assert.equal(body.cells.is_stoppage.value,1,'Stoppage uses raw zero, never retained coal'); coal.sample.value=600;
density.effective_density.status='held'; assert.equal(live.capture(page).cells.density.status,'held'); density.effective_density.status='good';
ops.telemetryFailed=true; assert.equal(live.capture(page).cells.raw_ash.status,'missing'); ops.telemetryFailed=false;
groups[0].sample.components[0].available=false;
assert.equal(live.capture(page).cells.sysA.value,null); groups[0].sample.components[0].available=true;
page.pointChoices['raw-ash']=''; live.bindDefaults(page); assert.equal(page.pointChoices['raw-ash'],'','Explicit opt-out persists');
delete page.pointChoices['raw-ash']; live.bindDefaults(page);
page.pointChoices['density-current']='["kep","a303"]';
assert.equal(live.capture(page).cells.density.item_id,'TIA_4_1.TIA_4_1.A304/MD');
assert.ok(page.sourceLocked('density-current')); assert.ok(page.sourceLocked('coarse'));
page.view='production'; assert.ok(!page.sourceLocked('coarse')); page.view='gpt';

// An old response must never overwrite a newer input, nor survive a failed request.
async function verifyRequests() {
    const requests=[];
    ctx.window.location.protocol='http:';
    ctx.window.Api={calculateDecision:body=>new Promise((resolve,reject)=>requests.push({body,resolve,reject}))};
    page.active=()=>true; page.renderGraph=()=>{};page.renderContext=()=>{};page.renderInspector=()=>{};
    live.update(page); const first=live.inFlight;
    raw.sample.value=41; live.update(page);
    requests[0].resolve({coarse_ash:{value:1,status:'predicted'}}); await first;
    assert.equal(live.result,null); assert.equal(requests.length,2);
    requests[1].resolve({coarse_ash:{value:9.3,status:'predicted'},suggested_density:{value:1.48,status:'calculated'}}); await live.inFlight;
    assert.equal(live.result.coarse_ash.value,9.3);
    assert.equal(live.display(page,node('coarse')).value,'9.30 %');
    raw.sample.value=42; live.update(page); assert.equal(live.result,null);
    requests[2].reject(new Error('离线')); await live.inFlight;
    assert.equal(live.result,null); assert.match(live.display(page,node('density')).reason,/离线/);
    assert.equal(JSON.stringify(App.store),initial,'Live calculation never writes legacy model/store state');
    console.log('Decision realtime: input mapping, A304, retained zero, missing states, source changes, async ordering, failure clearing and read isolation passed.');
}
verifyRequests().catch(error=>{console.error(error);process.exitCode=1;});
