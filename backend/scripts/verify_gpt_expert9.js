/* Pure VM verification. No browser, server mutations or production database. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const artifact=JSON.parse(fs.readFileSync('backend/models/gpt_expert9/20261009.json','utf8'));
vm.runInThisContext(fs.readFileSync('frontend/js/gpt-expert9.js','utf8'));
vm.runInThisContext(fs.readFileSync('frontend/js/decision-coarse-charts.js','utf8'));
const group={expert9:artifact.model,rows:artifact.rows,n:artifact.rows.length,artifact_id:'fixture'};
const frozen=JSON.stringify(group);
GptExpert9.group=group;GptExpert9.loaded=true;
const sample=GptExpert9.chartSnapshot('sample'),daily=GptExpert9.chartSnapshot('day');
assert.equal(sample.rows.length,177);assert.equal(daily.rows.length,51);assert.equal(sample.factors.length,9);
for(const row of daily.rows) {
    const rs=sample.rows.filter(x=>x.day===row.key);
    assert.ok(Math.abs(row.pred-rs.reduce((n,r)=>n+r.pred,0)/rs.length)<1e-12);
    assert.ok(Math.abs(row.actual-rs.reduce((n,r)=>n+r.actual,0)/rs.length)<1e-12);
}
assert.equal(sample.factors.find(f=>f.key==='sysA').value,0);
assert.equal(sample.factors.find(f=>f.key==='sys401').change,'停止→运行');
assert.equal(JSON.stringify(group),frozen);
assert.equal(DecisionCoarseCharts.build({},'day').expert,true);
GptExpert9.loaded=false;
assert.equal(DecisionCoarseCharts.build({}).reason,'expert-loading');
GptExpert9.loaded=true;GptExpert9.expectedArtifact='different';
assert.equal(DecisionCoarseCharts.build({}).reason,'expert-loading');
GptExpert9.expectedArtifact='fixture';GptExpert9.error='failed';
assert.equal(DecisionCoarseCharts.build({}).reason,'expert-loading');
console.log('Expert GPT: 177 sample replay / 51 daily averages, nine legal factor effects, immutable source, and loading/version isolation passed.');
