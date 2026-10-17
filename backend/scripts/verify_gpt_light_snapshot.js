// Pure VM regression: no network, host queries, database, training or file writes.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {App,ctx,elements}=require('./app_vm')();
for(const file of ['decision-overview.js','decision-density-display.js','decision-realtime.js','decision-sources.js','decision-hourly.js'])
    vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../frontend/js/'+file),'utf8'),ctx);
const page=vm.runInContext('DecisionOverviewPage',ctx),live=vm.runInContext('DecisionRealtime',ctx);
const sources=vm.runInContext('DecisionSources',ctx),hourly=vm.runInContext('DecisionHourly',ctx);
const displayMemory=vm.runInContext('DecisionDensityDisplay',ctx);
const copy=value=>JSON.parse(JSON.stringify(value));
ctx.window.location.protocol='http:';
let readOnly=false;
ctx.Api=ctx.window.Api={isReadOnlyTest:()=>readOnly,getDecisionCurrent(){throw Error('test must never fetch');}};
ctx.window.Operations={telemetryFailed:false,manualMeasurementsFailed:false,telemetry:{items:[]}};
page.view='gpt';page.queueInspectorPosition=()=>{};
for(const id of ['decision-board','decision-toolbar-total','decision-recent-ash-layer','decision-context',
    'decision-mode-label','decision-mode-note','decision-inspector']) elements[id]={scrollTop:0,classList:{contains:()=>true}};
const shape=nodes=>copy(nodes.map(node=>Object.fromEntries(
    ['id','title','family','parent','type','x','y','w','h','summaryHeight','summaryGap']
        .filter(key=>node[key]!==undefined).map(key=>[key,node[key]]))));
const routes=()=>copy(page.routeConnections().map(({node,parent,path})=>({from:node.id,to:parent.id,path})));
const legacySnapshot=page.buildSnapshot();
page.nodes=page.buildNodes(legacySnapshot);
const expectedShape=shape(page.nodes),expectedRoutes=routes();
const originalFacade=page.readFacade, originalStore=App.store, originalMethods={};
let legacyCalls=0;
for(const key of ['computeDensityGuidance','resolveTotalAshEx','resolveTotalAsh','resolveAmount','resolveFloatAsh',
    'resolveCoarseAsh','getHeavyAsh','formulaTotalAsh','_coarsePredict','_latestByTime']) {
    originalMethods[key]=App[key];App[key]=()=>{legacyCalls++;throw Error('legacy calculation '+key);};
}
page.readFacade=()=>{legacyCalls++;throw Error('history clone forbidden');};
Object.defineProperty(App,'store',{configurable:true,get(){throw Error('App.store read forbidden');}});
readOnly=true;

function render(snapshot) {
    page.snapshot=snapshot;page.nodes=page.buildNodes(snapshot);
    assert.deepEqual(shape(page.nodes),expectedShape,'card dimensions, positions, order and hierarchy are unchanged');
    assert.deepEqual(routes(),expectedRoutes,'connector geometry is unchanged');
    page.renderGraph();page.renderContext();
    for(const id of ['density','coarse','heavy','float','heavy-ash','tail-level','system-switches']) {
        page.selected=id;page.renderInspector(true);
    }
}

// First response is absent: even a seeded or old laptop store must be inaccessible.
live.result=null;sources.config=null;
let snapshot=page.buildSnapshot();
assert.equal(snapshot.showAdvice,false);
assert.equal(snapshot.model,null);
assert.equal(snapshot.coarse,null);
assert.equal(snapshot.floating,null);
assert.equal(snapshot.formula,null);
assert.equal(snapshot.target.min,null);
assert.equal(snapshot.target.max,null);
assert.equal(snapshot.inputs.length,10);
assert.ok(snapshot.inputs.every(input=>input.raw===null && input.used===null && input.missing));
assert.ok(Object.values(snapshot.ashes).every(value=>value===null));
assert.ok(Object.values(snapshot.amounts).every(value=>value===null));
render(snapshot);
assert.equal(page.pointDisplay(page.nodes.find(node=>node.id==='float-ash')).value,'—');
assert.equal(page.pointDisplay(page.nodes.find(node=>node.id==='density')).value,'暂无有效值');
assert.doesNotMatch(elements['decision-board'].innerHTML,/1\.450|9\.85|默认值 · 非实时测量/);

const stamp='2026-10-17T03:20:00Z',entered='2026-10-17T03:21:00Z';
const cell=(value,unit='%',status='good')=>({value,unit,status,source:'主机测试夹具',observed_at:stamp,created_at:entered,source_id:'fixture'});
const cells={raw_ash:cell(0),coal_amount:cell(640,'t/h'),level:cell(76),heavy_ash:cell(8.15,'%','manual'),
    float_ash:cell(8.9,'%','manual'),float_amount:cell(0,'t/h','manual'),coarse_amount:cell(16,'t/h','calculated'),
    heavy_amount:cell(300,'t/h','calculated'),density:cell(1.487,'g/cm³'),coarse_ash:cell(9.25,'%','predicted'),
    total_ash:cell(8.333,'%','calculated'),suggested_density:cell(1.492,'g/cm³','suggested'),
    systems:cell({'A':false,'B':true,'401':true,'402':false},''),desliming:cell({'473':false,'474':true},'')};
const referenceStamp='2026-10-17T03:11:00Z',referenceEntered='2026-10-17T03:12:00Z';
for(const [key,value,unit] of [['float_press',{'A415':true,'A416':false,'A417':false,'A418':true},''],
    ['reagent',0,'kg/t'],['raw_slime',7.9,'%']]) {
    cells[key]={...cell(value,unit),source:'主机参考-'+key,observed_at:referenceStamp,created_at:referenceEntered};
}
const inputs={raw_ash:cell(0),coal_amount:cells.coal_amount,level:cells.level,is_stoppage:cell(0,''),
    heavy_ash:cells.heavy_ash,float_ash:cells.float_ash,float_amount:cells.float_amount,coarse_amount:cells.coarse_amount,
    density:cells.density,belt501:cell(200,'t/h'),belt502:cell(116,'t/h')};
const result={policy:'decision-live-v1',generated_at:stamp,cells,inputs,
    coarse_ash:cells.coarse_ash,heavy_amount:cells.heavy_amount,total_ash:cells.total_ash,suggested_density:cells.suggested_density,
    target:{min:8.3,max:8.7},model:{engine:'gpt',method:'expert8',id:'fixture-model',trained_at:'2026-10-01T00:00:00Z',artifact_id:'fixture-v1'},
    features:[{key:'level',raw:76,used:75,clipped:true},{key:'coal_amount',raw:640,used:640,clipped:false}],
    guidance:{rhoCur:1.489,rhoNew:1.492,direction:'up',maxStep:.03,deadband:0},warnings:[],
    sources:{revision:2,target_min:8.3,target_max:8.7,resolved_choices:{}}};
live.result=result;sources.config=result.sources;
const resultBefore=JSON.stringify(result);
snapshot=page.buildSnapshot();
assert.equal(snapshot.inputs.length,10,'EXPERT8 cannot remove required reference/placeholder display keys');
assert.equal(snapshot.inputs.find(input=>input.key==='raw_ash').raw,0);
assert.equal(snapshot.inputs.find(input=>input.key==='raw_ash').used,null,'reference raw ash is not silently fed into a model');
assert.equal(snapshot.inputs.find(input=>input.key==='sysA').raw,0,'false group members remain valid zero inputs');
assert.equal(snapshot.inputs.find(input=>input.key==='sysB').raw,1);
assert.equal(snapshot.inputs.find(input=>input.key==='desliming473').raw,0);
assert.equal(snapshot.inputs.find(input=>input.key==='is_stoppage').raw,0);
assert.equal(snapshot.inputs.find(input=>input.key==='level').used,75,'used values come from the server feature explanation');
assert.equal(snapshot.inputs.find(input=>input.key==='level').clipped,true);
assert.equal(snapshot.inputs.find(input=>input.key==='sysB').used,null,'missing server explanation must not trigger local imputation');
assert.equal(snapshot.ashes.heavy,8.15);assert.equal(snapshot.amounts.float,0);
assert.equal(snapshot.sum,316,'finite host component quantities retain their exact sum, including zero');
assert.equal(snapshot.formula,8.333,'server total is displayed verbatim, never recomputed from component values');
assert.equal(snapshot.prediction,9.25);assert.equal(snapshot.guide.rhoCur,1.489);assert.equal(snapshot.guide.rhoNew,1.492);
assert.equal(snapshot.guide.deadband,0);assert.equal(snapshot.showAdvice,true);
assert.deepEqual(copy(snapshot.target),{min:8.3,max:8.7});
assert.equal(snapshot.model.id,'fixture-model');assert.equal(snapshot.modelGroup.production,'expert8');
assert.equal(snapshot.modelGroup.trainedAt,result.model.trained_at);
assert.equal(snapshot.reader.ashMeasurementInfo('heavy').sampleAt,stamp);
assert.equal(snapshot.reader.ashMeasurementInfo('heavy').enteredAt,entered);
render(snapshot);
assert.match(page.nodes.find(node=>node.id==='raw-ash').description,/不参与当前八因子预测/,
    'EXPERT8 reference explanation uses runtime metadata without downloading the analysis model');
page.selected='tail-level';page.renderInspector(true);
assert.match(elements['decision-inspector'].innerHTML,/主机测试夹具/);
assert.ok(elements['decision-inspector'].innerHTML.includes(page.time(stamp)),'inspector retains source observation time');
assert.ok(elements['decision-inspector'].innerHTML.includes(page.time(entered)),'inspector retains manual entry time');
assert.equal(page.pointDisplay(page.nodes.find(node=>node.id==='float-amount')).numericValue,0);
assert.equal(page.pointDisplay(page.nodes.find(node=>node.id==='system-switches')).components.find(part=>part.label==='A').value,false);
for(const id of ['float-press','float-unmodeled','coarse-slime']) {
    page.selected=id;page.renderInspector(true);
    const node=page.nodes.find(node=>node.id===id),shown=page.pointDisplay(node);
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>当前业务值</dt><dd>${page.escape(shown.value)}</dd>`),
        id+' reference detail uses the same API value as its card');
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>当前来源</dt><dd>${page.escape(shown.source)}</dd>`));
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>采样 / 记录时间</dt><dd>${page.time(referenceStamp)}</dd>`));
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>人工录入时间</dt><dd>${page.time(referenceEntered)}</dd>`));
    readOnly=false;page.renderInspector(true);
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>当前业务值</dt><dd>${page.escape(node.value)}</dd>`),
        id+' production detail keeps the original template path');
    assert.ok(elements['decision-inspector'].innerHTML.includes(`<dt>当前来源</dt><dd>${page.escape(node.source)}</dd>`));
    readOnly=true;
}
assert.equal(JSON.stringify(result),resultBefore,'display projection cannot mutate API snapshots or feature evidence');

// Exercise the existing component-facts renderer as well as its live calculation
// inspector, so the lightweight DTO retains the mass-share/contribution contract.
const pointDisplay=page.pointDisplay;
page.pointDisplay=function(node){const shown=pointDisplay.call(this,node);
    return node.id==='coarse'?{...shown,calculated:false,aggregate:false}:shown;};
page.selected='coarse';page.renderInspector(true);
assert.match(elements['decision-inspector'].innerHTML,/组分质量占比<\/dt><dd>5\.1 %/);
assert.match(elements['decision-inspector'].innerHTML,/加权灰分贡献<\/dt><dd>0\.468 百分点/);
live.result={...result,cells:{...cells,float_amount:cell(null,'t/h','missing')}};
page.snapshot=page.buildSnapshot();
assert.equal(page.snapshot.sum,null,'missing component quantity must not become zero in the inspector total');
page.renderInspector(true);
assert.doesNotMatch(elements['decision-inspector'].innerHTML,/组分质量占比|加权灰分贡献/);
live.result={...result,cells:{...cells,heavy_amount:cell(0,'t/h'),float_amount:cell(0,'t/h'),coarse_amount:cell(0,'t/h')}};
page.snapshot=page.buildSnapshot();assert.equal(page.snapshot.sum,0,'an all-zero balance remains a valid zero sum');
page.renderInspector(true);assert.doesNotMatch(elements['decision-inspector'].innerHTML,/NaN|Infinity|组分质量占比/);
page.pointDisplay=pointDisplay;live.result=result;

// The same projection uses the displayed hourly result when aggregate mode is selected.
const hour={...result,policy:'decision-hourly-v2',target:{min:8.1,max:8.4},cells:{...cells,total_ash:cell(8.222),
    suggested_density:cell(1.499,'g/cm³','suggested')},guidance:{rhoCur:1.49,direction:'up',maxStep:.03},
    model:{...result.model,id:'hour-fixture'}};
const originalPresentation=hourly.presentation;hourly.presentation=()=>hour;
page.aggregationMode='hourly';snapshot=page.buildSnapshot();
assert.equal(snapshot.formula,8.222);assert.equal(snapshot.guide.rhoNew,1.499);
assert.equal(snapshot.model.id,'hour-fixture');assert.equal(snapshot.target.max,8.4);
assert.equal(legacyCalls,0,'both GPT modes avoid history cloning and every legacy calculation');
page.aggregationMode='instant';hourly.presentation=originalPresentation;

// Missing new outputs still use the existing explicitly-labelled display-only memory.
live.result=null;sources.config=result.sources;
page.snapshot=page.buildSnapshot();page.nodes=page.buildNodes(page.snapshot);
const held=page.pointDisplay(page.nodes.find(node=>node.id==='density'));
assert.equal(held.value,'1.492');assert.equal(held.held,true);assert.match(held.reason,/沿用最后一次有效建议/);

// All original paths retain their exact code: explicit production, no meta, or file URL.
Object.defineProperty(App,'store',{configurable:true,writable:true,value:originalStore});
Object.assign(App,originalMethods);
page.readFacade=function(){legacyCalls++;return originalFacade.call(this);};
const storeBefore=JSON.stringify(App.store);
const before=legacyCalls;page.buildSnapshot('production');
assert.equal(legacyCalls,before+1,'explicit production view always keeps the original DS/legacy path');
readOnly=false;page.buildSnapshot('gpt');assert.equal(legacyCalls,before+2,'no meta retains production behavior');
readOnly=true;ctx.window.location.protocol='file:';page.buildSnapshot('gpt');
assert.equal(legacyCalls,before+3,'file mode cannot silently enter the server projection');
assert.equal(JSON.stringify(App.store),storeBefore,'legacy views remain isolated from the real store');
displayMemory.entries={};
console.log('GPT light snapshot: zero store/legacy reads, empty startup, authoritative API projection, zero/group states, unchanged layout/routes, held advice and original production/file paths passed');
