/* Eight-factor browser consumers, without any production writes. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const artifact=JSON.parse(fs.readFileSync('backend/models/gpt_expert8/20261012/production.json','utf8'));
vm.runInThisContext(fs.readFileSync('frontend/js/gpt-expert9.js','utf8'));
vm.runInThisContext(fs.readFileSync('frontend/js/decision-coarse-charts.js','utf8'));
vm.runInThisContext(fs.readFileSync('frontend/js/decision-hourly.js','utf8'));
const group={production:'expert8',expert8:artifact.model,rows:artifact.rows,report:artifact.report,
    source:artifact.source,n:artifact.rows.length,artifact_id:'eight'};
GptExpert9.group=group;GptExpert9.loaded=true;
const before=JSON.stringify(group);
const samples=GptExpert9.chartSnapshot(),days=GptExpert9.chartSnapshot('day');
assert.equal(samples.which,'expert8');assert.equal(samples.rows.length,177);assert.equal(days.rows.length,51);
assert.equal(samples.factors.length,8);assert(!samples.factors.some(f=>f.key==='raw_ash'));
assert(samples.factors.every(f=>Number.isFinite(f.value)));
assert.equal(samples.factors.find(f=>f.key==='sysA').value,0);
for(const day of days.rows) {
    const rows=samples.rows.filter(r=>r.day===day.key);
    assert(Math.abs(day.pred-rows.reduce((sum,r)=>sum+r.pred,0)/rows.length)<1e-12);
}
const original=GptExpert9.predict(artifact.rows[0]);
for(const raw of [null,0,40,100,NaN,'invalid'])
    assert.equal(GptExpert9.predict({...artifact.rows[0],raw_ash:raw}),original);
assert.equal(DecisionCoarseCharts.build({},'day').which,'expert8');
GptExpert9.observe({model:{method:'expert8',artifact_id:'eight'}});
assert.equal(GptExpert9.expectedArtifact,'eight');
assert.equal(JSON.stringify(group),before);

const hour={model:{method:'expert8'},period:{start:'2026-10-12T02:00:00Z',end:'2026-10-12T03:00:00Z'},
    suggested_density:{value:null},density_basis:{value:1.48,status:'good',observed_at:'2026-10-12T02:59:55Z'},
    coverage:{ratio:0,required_ratio:.9,longest_gap_seconds:3600,max_gap_seconds:60,cadence_seconds:5,expected_count:720},
    cells:{raw_ash:{value:null,status:'missing',reason:'原煤灰分仪表缺数'}}};
assert.equal(DecisionHourly.dataGaps(hour).get('raw-ash').severity,'warning');
hour.model.method='expert9';
assert.equal(DecisionHourly.dataGaps(hour).get('raw-ash').severity,'error','Historical nine-factor semantics are preserved');
hour.model.method='expert8';hour.cells.raw_ash={value:40,status:'average'};
hour.evidence={segments:[{first_slot:Date.parse(hour.period.start)/5000,last_slot:Date.parse(hour.period.start)/5000+719,
    duration_seconds:3600,state:{quality:{raw_ash:'held'}}}]};
assert.equal(DecisionHourly.dataGaps(hour).get('raw-ash').severity,'warning','Raw-only gap evidence is never an eight-factor blocker');
console.log('Eight-factor UI: 177 predictions, 51 daily means, 8 effects, raw independence, version sync and non-blocking raw gap passed.');
