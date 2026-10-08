// Read-only fixtures: no browser, live DB or training writes.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const {App,ctx,elements}=require('./app_vm')();
vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/decision-overview.js'),'utf8'),ctx);
const page=vm.runInContext('DecisionOverviewPage',ctx);
const original=JSON.stringify(App.store);
const ops=ctx.window.Operations={telemetryFailed:false,manualMeasurementsFailed:false,telemetry:{items:[]}};
page.snapshot=page.buildSnapshot(); page.nodes=page.buildNodes(page.snapshot);
const heavy=page.nodes.find(n=>n.id==='heavy-amount');
const float=page.nodes.find(n=>n.id==='float-amount'), coarse=page.nodes.find(n=>n.id==='coarse-amount');
const belts=['501','502'].map(belt=>({source_id:'kep',key:`belt${belt}`,item_id:`TIA_3_4.TIA_3_4.${belt}/COAL_CAP`,
    available:true,status:'good',unit:'',sample:{value:0,quality:'good',received_at:'2026-10-08T06:00:00Z',batch_id:'batch1'}}));
ops.telemetry.items=belts;
function inputs(a,b,f,c) {
    belts[0].sample.value=a; belts[1].sample.value=b;
    for(const [node,value] of [[float,f],[coarse,c]]) Object.assign(node,{numericValue:value,value:page.number(value,1,'t/h')});
    return page.pointDisplay(heavy);
}
const cases=[[260.5,235.2,40,30],[219.76272583007812,127.60417175292969,40,20],[0,100,40,20],[0,0,0,0],[100.0004,100.0004,40.04,20.01],[0.3,0.6,0.9,0]];
const parity=cases.map(values=>{const result=inputs(...values);return {inputs:values,value:Number(result.numericValue.toFixed(1))};});
if(process.argv.includes('--parity-json')) { process.stdout.write(JSON.stringify(parity)); process.exit(0); }
let d=inputs(260.5,235.2,40,30);
assert.equal(d.value,'425.7 t/h'); assert.equal(d.available,true);
assert.equal(page.pointDisplay(page.nodes.find(n=>n.id==='heavy')).source,'煤量 425.7 t/h');
assert.equal(App.telemetryDisplayUnit(belts[0]),'t/h'); assert.equal(belts[0].unit,'','Display unit does not modify raw metadata');
assert.equal(inputs(100.0004,100.0004,40.04,20.01).value,'140.0 t/h','Compute with raw numeric precision before rounding');
assert.equal(inputs(0,100,40,20).value,'40.0 t/h','A stopped belt contributes a real zero');
assert.equal(inputs(0,0,0,0).value,'0.0 t/h');
assert.equal(inputs(0.3,0.6,0.9,0).value,'0.0 t/h','Machine roundoff must not make a balanced flow negative');
assert.equal(inputs(0.3,0.6,0.90001,0).value,'—','Real negative balances are still rejected, even below display precision');
assert.match(inputs(0,0,40,20).reason,/煤量不平衡/);
assert.equal(page.pointDisplay(heavy).value,'—','A negative balance is not clamped to a plausible zero');
for(const value of [null,NaN,Infinity,true,-1]) assert.equal(inputs(value,100,40,20).value,'—');
inputs(260.5,235.2,40,30);
for(const change of [{available:false},{sample:{...belts[0].sample,quality:'bad'}},{sample:{...belts[0].sample,issue:'bad_value'}},{unit:'kg/h'},
    {source_id:'other'},{sample:{...belts[0].sample,batch_id:'batch2'}},{sample:{...belts[0].sample,received_at:'2026-10-08T05:59:59Z'}}]) {
    const saved=ops.telemetry.items[0]; ops.telemetry.items[0]={...saved,...change};
    assert.equal(page.pointDisplay(heavy).value,'—',JSON.stringify(change)); ops.telemetry.items[0]=saved;
}
ops.telemetryFailed=true; assert.equal(page.pointDisplay(heavy).value,'—'); ops.telemetryFailed=false;
ops.telemetry.items=[belts[0]]; assert.equal(page.pointDisplay(heavy).value,'—'); ops.telemetry.items=belts;
const manual={source_id:'manual-measurements',key:'coarse_slime_amount',kind:'manual_measurement',unit:'t/h',available:true,sample:{value:25,observed_at:'2026-10-08T05:50:00Z'}};
ops.manualMeasurements={items:[manual]}; page.pointChoices['coarse-amount']=page.pointIdentity(manual);
assert.equal(page.pointDisplay(heavy).value,'430.7 t/h','Deduct the selected manual coarse amount');
manual.sample.value=35;
assert.equal(page.pointDisplay(heavy).value,'420.7 t/h','A changed child source immediately changes the balance');
ops.manualMeasurementsFailed=true;
d=page.pointDisplay(heavy); assert.equal(d.value,'420.7 t/h'); assert.equal(d.held,true); assert.equal(d.available,false); assert.equal(d.badge,'待刷新');
ops.manualMeasurementsFailed=false;
manual.available=false; assert.equal(page.pointDisplay(heavy).value,'—'); manual.available=true;
manual.unit='%'; assert.match(page.pointDisplay(heavy).reason,/单位不是 t\/h/); manual.unit='t/h';
page.pointChoices['heavy-amount']='["kep","obsolete"]';
assert.equal(page.pointDisplay(heavy).value,'420.7 t/h','The amount card always follows the requested formula');
page.selected='heavy-amount'; elements['decision-inspector']={}; page.renderInspector();
assert.doesNotMatch(elements['decision-inspector'].innerHTML,/<select/);
for(const text of ['501 瞬时带煤量','502 瞬时带煤量','浮精量','粗精煤泥量','420.7 t/h']) assert.ok(elements['decision-inspector'].innerHTML.includes(text));
assert.equal(JSON.stringify(App.store),original,'Realtime display does not mutate model inputs or legacy business state');
console.log('Decision heavy amount: 501+502 minus visible float/coarse, precision, zero, invalid/batch/unit guards, held manual values and read isolation passed.');
