// Synthetic histories only; no training, browser, DB writes, or baseline regeneration.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {App,ctx} = require('./app_vm')();
vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/decision-coarse-charts.js'),'utf8'),ctx);
const charts = vm.runInContext('DecisionCoarseCharts',ctx);
const json = value=>JSON.parse(JSON.stringify(value));
const features=App.MLR_FEATURES;
const model={intercept:1,coefs:features.map(f=>f==='raw_ash'?1:f==='level'?.1:0),imputeMeans:features.map(f=>f==='level'?50:0),
    metrics:{version:2},stdCoef:features.map((f,i)=>i===0?-.7:i===9?.8:0)};
const gpt={production:'mlr',mlr:model,pls:model,trainedAt:'synthetic',n:6};
const ds=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../data/train_js.json'),'utf8')).coarseModel;
const row=(timestamp,ash,raw,level)=>({timestamp,ash_content:ash,raw_ash:raw,level,
    coal_amount:800,sysA:1,sysB:0,sys401:1,sys402:0,desliming473:1,desliming474:0,is_stoppage:0});
const records=[
    row('2026-10-06 07:59:59',10,20,40),row('2026-10-06 08:00:00',12,22,50),
    row('2026-10-06 19:59:59',14,24,null),row('2026-10-06 20:00:00',16,26,60),
    row('2026-10-07 00:00:00',18,28,70),row('2026-10-07 08:00:00',20,30,80),
    row('2026-10-06 08:00:00',13,23,50), // last import wins
    row('2026-10-06 09:00:00',null,25,40),row('bad time',12,25,40),row('2026-10-06 10:00:00',41,25,40)
];
const store={coarseCoal:records,coarseModel:ds,coarseModelVariants:{gpt,ds}};
App.store=store;
App.saveStore=App.switchCoarseEngine=App.retrainCoarseModelAsync=App._trainCoarseRows=()=>{throw Error('Charts must be read-only');};
const before=JSON.stringify(store);
const day=charts.build(store,'day'),shift=charts.build(store,'shift');
assert.equal(day.count,6);assert.equal(day.missing,1);assert.equal(day.rows.length,2);assert.equal(shift.rows.length,4);
assert.deepEqual(json(shift.rows.map(r=>[r.label,r.n])),[['2026-10-05 夜班',1],['2026-10-06 白班',2],['2026-10-06 夜班',2],['2026-10-07 白班',1]]);
assert.equal(day.rows[0].actual,(10+13+14+16)/4);
assert.equal(day.rows[0].pred,(25+29+30+33)/4,'Predict each sample, then average');
assert.equal(day.rows[0].paired,3);assert.equal(day.rows[0].level,50);
assert.equal(day.rows[0].scatterActual,(10+13+16)/3,'Scatter excludes missing-level row from both paired series');
assert.equal(day.rows[0].scatterPred,(25+29+33)/3);
assert.deepEqual(json(day.factors),json(shift.factors),'Display grouping cannot change saved coefficients');
assert.equal(day.factors[0].key,'level');assert.equal(day.factors[1].value,-.7);
assert.equal(JSON.stringify(store),before,'No mutation of records, models, current DS, or business state');
assert.equal(charts.build({...store,coarseModelVariants:{ds}},'day').model,null,'No DS default fallback');
assert.equal(charts.build({...store,coarseModel:gpt,coarseModelVariants:{}},'shift').count,6,'Current GPT works without bank');
assert.equal(charts.build({...store,coarseCoal:[]},'day').rows.length,0);
const noCoefficients=json(gpt);delete noCoefficients.mlr.stdCoef;
assert.equal(charts.build({...store,coarseModelVariants:{gpt:noCoefficients}},'day').factors.length,0,'Missing coefficients must not be fabricated as zero');
const broken=json(gpt);broken.mlr.coefs[0]=null;
assert.equal(charts.build({...store,coarseModelVariants:{gpt:broken}},'day').model,null);
const clipped=json(gpt);clipped.mlr.inputPolicy='clip-training-range';clipped.mlr.inputBounds=features.map(f=>f==='raw_ash'?[0,21]:[0,1000]);
const clippedDay=charts.build({...store,coarseModelVariants:{gpt:clipped}},'day');
assert.equal(clippedDay.rows[0].pred,(25+27+27+28)/4,'Clipping is per original sample, before display aggregation');
assert.deepEqual(json(charts.period('2026-01-01 01:00:00','shift')),{key:'2025-12-31 20',label:'2025-12-31 夜班',tick:'12-31 夜',start:'2025-12-31 20:00',end:'2026-01-01 08:00'});
assert.equal(charts.period('2026-03-01 07:59:59','shift').label,'2026-02-28 夜班');
assert.equal(JSON.stringify(store),before);
console.log('Decision GPT charts: saved GPT isolation, day/shift boundaries, last-import deduplication, paired missing-level handling, clipping-before-mean, fixed factors and read-only state passed.');
