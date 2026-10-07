// Isolated VM: GPT daily analysis may train privately; no production/DB/baseline writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {App,ctx}=require('./app_vm')();
vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/decision-coarse-charts.js'),'utf8'),ctx);
const charts=vm.runInContext('DecisionCoarseCharts',ctx),json=v=>JSON.parse(JSON.stringify(v)),features=App.MLR_FEATURES;
const fixture=JSON.parse(fs.readFileSync(path.resolve(__dirname,'../data/train_js.json'),'utf8'));
const model={intercept:1,coefs:features.map(f=>f==='raw_ash'?1:f==='level'?.1:0),imputeMeans:features.map(f=>f==='level'?50:0),
    metrics:{version:2},stdCoef:features.map((f,i)=>i===0?-.7:i===9?.8:0)};
const gpt={production:'mlr',mlr:model,pls:model,trainedAt:'synthetic',n:6,range:'all',tolerance:.4};
const row=(timestamp,ash,raw,level)=>({timestamp,ash_content:ash,raw_ash:raw,level,
    coal_amount:800,sysA:1,sysB:0,sys401:1,sys402:0,desliming473:1,desliming474:0,is_stoppage:0});
const records=[row('2026-10-06 07:59:59',10,20,40),row('2026-10-06 08:00:00',12,22,50),
    row('2026-10-06 19:59:59',14,24,null),row('2026-10-06 20:00:00',16,26,60),
    row('2026-10-07 00:00:00',18,28,70),row('2026-10-07 08:00:00',20,30,80),
    row('2026-10-06 08:00:00',13,23,50),row('2026-10-06 09:00:00',null,25,40),row('bad time',12,25,40)];
const store={coarseCoal:records,coarseModel:fixture.coarseModel,coarseModelVariants:{gpt},coarseTolerance:9};App.store=store;
App.saveStore=App.switchCoarseEngine=App.retrainCoarseModelAsync=App.trainCoarseModel=()=>{throw Error('No production mutation allowed');};
const train=App._trainCoarseRows;let calls=0;
App._trainCoarseRows=function(days){calls++;assert.notEqual(this.store,App.store);assert.equal(this.store.coarseTolerance,.4);return train.call(this,days);};
const before=JSON.stringify(store),sample=charts.build(store,'sample');
assert.equal(calls,0);assert.equal(sample.count,6);assert.equal(sample.rows.length,6);assert.equal(sample.missing,1);
assert.equal(sample.rows[1].actual,13,'Last duplicate import wins');assert.equal(sample.rows[1].pred,29);
assert.equal(sample.rows[2].pred,30,'Saved model imputes historical missing feature');assert.equal(sample.rows[2].paired,0);
assert.ok(sample.rows.every(r=>r.n===1));assert.equal(sample.factors[0].key,'level');
const insufficient=charts.build(store,'day');assert.equal(insufficient.reason,'daily-data-insufficient');assert.equal(insufficient.model,null);
assert.equal(insufficient.factors.length,0,'Never label sampling factors as daily model');
charts.build(store,'day');assert.equal(calls,1,'Null daily result is cached too');
const dailyStore={...store,coarseCoal:json(fixture.coarseCoal)};const dailyBefore=JSON.stringify(dailyStore);
const day=charts.build(dailyStore,'day');assert.ok(day.model);assert.equal(calls,2);
assert.equal(day.rows.length,App._coarseDaily(dailyStore.coarseCoal).length);
assert.equal(day.group.tolerance,.4);assert.notDeepEqual(json(day.factors),json(sample.factors));
const first=App._coarseDaily(dailyStore.coarseCoal)[0];
assert.equal(day.rows[0].actual,first.ash_content);assert.equal(day.rows[0].pred,App._coarsePredict(first,day.model));
assert.equal(day.rows[0].level,first.level);assert.equal(day.rows[0].scatterActual,first.ash_content,'Use the same daily aggregate for scatter and model');
assert.equal(charts.build(dailyStore,'day').model,day.model,'Repeated switch reuses daily cache');assert.equal(calls,2);
charts.build(dailyStore,'sample');assert.equal(calls,2);
assert.equal(JSON.stringify(dailyStore),dailyBefore);assert.equal(JSON.stringify(store),before);
assert.equal(charts.build({...store,coarseModelVariants:{}},'sample').model,null,'No DS fallback');
assert.equal(charts.build({...store,coarseModel:gpt,coarseModelVariants:{}},'sample').count,6);
const complete=json(gpt);complete.mlr.inputPolicy='clip-training-range';complete.mlr.inputBounds=features.map(f=>f==='raw_ash'?[0,21]:[0,1000]);
assert.equal(charts.modelGroup({...store,coarseModel:json(gpt),coarseModelVariants:{gpt:complete}}),complete,'Use full bank metadata for matching current model');
assert.equal(charts.build({...store,coarseModelVariants:{gpt:complete}},'sample').rows[1].pred,27);
const noCoefs=json(gpt);delete noCoefs.mlr.stdCoef;
assert.equal(charts.build({...store,coarseModelVariants:{gpt:noCoefs}},'sample').factors.length,0);
const broken=json(gpt);broken.mlr.coefs[0]=null;assert.equal(charts.build({...store,coarseModelVariants:{gpt:broken}},'day').model,null);
dailyStore.coarseCoal[0].level+=5;charts.build(dailyStore,'day');assert.equal(calls,3,'Changed daily input invalidates only private cache');
assert.equal(JSON.stringify(store),before);
console.log('Decision charts: GPT daily/sample models, private cache/tolerance, sample dedup, daily scatter consistency, bank metadata, insufficient-day handling and production isolation passed.');
