// Historical float reference calculations only; no DOM, DB, training or storage writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {App,ctx}=require('./app_vm')();
for(const file of ['float.js','decision-coarse-charts.js','decision-float-analysis.js']) vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const view=vm.runInContext('DecisionFloatAnalysis',ctx),coarse=vm.runInContext('DecisionCoarseCharts',ctx);
const row=(timestamp,ash,amount)=>({timestamp,ash_content:ash,coal_amount:amount,filter_press_running:0,annotation:'<img onerror=bad>'});
App.store.floatCoal=[row('2026-05-01 08:00:00',10,20),row('2026-06-01 08:00:00',10,20),row('2026-06-01 08:00:00',99,99),
    row('2026-06-02 08:00:00',8,0),row('2026-06-03 08:00:00',7,25),row('bad',10,20),
    row('2026-06-04 08:00:00',null,20),row('2026-06-05 08:00:00',10,'20'),row('2026-06-06 08:00:00',10,-2),row('2026-06-07 08:00:00',0,10)];
App.store.calcLogs=[{calc_type:'ash_density',timestamp:'2026-06-01 08:03:00',input_json:JSON.stringify({ash_content:9})}];
App.saveStore=App.retrainCoarseModelAsync=App._trainCoarseRows=App.resolveFloatAsh=()=>{throw Error('Float analysis must not write/train/resolve live data');};
const before=JSON.stringify(App.store),s=view.build();
assert.equal(s.rows.length,4);assert.equal(s.duplicates,1);assert.equal(s.early,1);assert.equal(s.invalid,4);assert.equal(s.defaults,3);
assert.equal(s.rows[0].heavyAsh,9);assert.equal(s.rows[0].defaultHeavy,false,'Existing ±5 minute matching may use a later record');
assert.equal(s.rows[0].amount,20,'First duplicate wins, matching the original float page');
for(const r of s.rows) {
    const original=App.store.floatCoal.find(d=>d.timestamp===r.timestamp);
    assert.equal(r.influence,App.influenceOfFloat(original),'Use the existing reference influence formula');
    assert.equal(r.total,App.calcTotalAsh(r.heavyAsh,250,r.ash,r.amount,0,0));
}
assert.equal(s.rows[1].influence,0,'Zero coal rate is valid');assert.ok(s.rows[2].influence<0);assert.equal(s.rows[3].ash,0,'Zero ash is preserved');
assert.equal(s.latest.timestamp,'2026-06-07 08:00:00');assert.ok(Number.isFinite(s.deviation));
const raw=s.rows.map(r=>r.total-App.calcTotalAsh(r.heavyAsh,250,0,0,0,0)),mean=raw.reduce((a,b)=>a+b,0)/raw.length;
assert.equal(s.deviation,Math.sqrt(raw.reduce((a,b)=>a+(b-mean)**2,0)/raw.length),'Population deviation follows unrounded mass-balance differences');
App.store.floatCoal.reverse();assert.equal(view.build().latest.timestamp,s.latest.timestamp,'Latest is time-sorted');App.store.floatCoal.reverse();
assert.equal(view.build({...App.store,floatCoal:[]}).latest,null);assert.equal(view.build({...App.store,floatCoal:[]}).deviation,null);
assert.equal(view.stamp('2026-02-30 08:00:00'),null);assert.equal(view.stamp(null),null);
assert.equal(view.escape('<img>'),'&lt;img&gt;');assert.notEqual(view.hits,coarse.hits);assert.notEqual(view.canvasPrefix,coarse.canvasPrefix);
assert.equal(JSON.stringify(App.store),before,'History, annotations, models, autoState and business values are unchanged');
console.log('Decision float analysis: original formula parity, first-dedup, date cutoff, matched/default baseline, valid zeros, signed influence, missing-data handling, escaping and read isolation passed.');
