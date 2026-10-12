/* Pure-controller regression. Does not launch browsers or contact production. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let clock=0,resolve,reject;
const context={Date,Number,Math,Object,Array,String,performance:{now:()=>clock},DecisionSources:{epoch:0},
    Api:{getDecisionHourly:()=>new Promise((a,b)=>{resolve=a;reject=b;})}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/js/decision-hourly.js','utf8')+';this.hourly=DecisionHourly;',context);
const h=context.hourly;
const page={view:'gpt',hourMode:()=>true,active:()=>true,renderGraph(){},renderContext(){},renderInspector(){},
    summaryChildren:new Map(),number:(v,d=2,u='')=>Number.isFinite(v)?v.toFixed(d)+(u?' '+u:''):'—',time:v=>v,escape:v=>String(v??'')};
const response=()=>({period:{start:'2026-10-09T02:00:00Z',end:'2026-10-09T03:00:00Z',apply_end:'2026-10-09T04:00:00Z',apply_label:'11:00–12:00'},
    checked_at:'2026-10-09T03:24:00Z',cells:{raw_ash:{value:40,status:'average',unit:'%',sample_count:3},
        systems:{components:{'401':{value:.5},'402':{value:0}}}},coverage:{}});
const completeHour=()=>({...response(),
    suggested_density:{value:null,reason:'当前状态暂缓调密'},
    density_basis:{value:1.48,status:'good',observed_at:'2026-10-09T02:59:55Z'},
    cells:{...Object.fromEntries(['raw_ash','coal_amount','level','heavy_ash','heavy_amount','float_ash',
        'float_amount','coarse_amount','density','coarse_ash','total_ash'].map(key=>[key,{value:8.5,status:'average'}])),
        heavy_ash:{value:8.5,status:'carried',carried:true,sample_count:0},
        float_amount:{value:0,status:'average'},
        systems:{components:Object.fromEntries(['401','402','A','B'].map(key=>[key,{value:0}]))},
        desliming:{components:{'473':{value:0},'474':{value:0}}},
        reagent:{value:null},raw_slime:{value:null},float_press:{value:null}},
    coverage:{ratio:1,required_ratio:.9,longest_gap_seconds:0,max_gap_seconds:60,cadence_seconds:5,expected_count:720}});
const segment=(offset,count,state,seconds=count*5)=>({
    first_slot:Date.parse(response().period.start)/5000+offset,
    last_slot:Date.parse(response().period.start)/5000+offset+count-1,duration_seconds:seconds,state});
const badSwitches={switches:{systems:{'401':null,'402':null,A:null,B:null}}};
(async()=>{
    assert.equal(h.dataGaps(null).size,0,'Loading or failed queries are not known data gaps');
    let hour=completeHour();
    assert.equal(h.dataGaps(hour).size,0,'Valid carry, zero flow, stopped switches and reference-only gaps are not blockers');
    hour.cells.heavy_ash={value:null,status:'missing',reason:'缺少重介化验'};
    hour.coverage.ratio=0;
    let gaps=h.dataGaps(hour);
    assert(gaps.has('heavy-ash') && gaps.has('heavy') && gaps.has('density'));
    assert.equal(gaps.get('heavy-ash').severity,'error');
    assert(!gaps.has('float') && !gaps.has('float-unmodeled'));
    hour.suggested_density.value=1.48;
    assert.equal(h.dataGaps(hour).get('heavy-ash').severity,'warning','A valid recommendation must not be labelled blocked by a missing statistic');
    assert(!h.dataGaps(hour).has('density'));
    hour=completeHour(); delete hour.cells.systems.components.A;
    assert(h.dataGaps(hour).has('system-switches'),'One missing required switch must mark the grouped card');
    hour=completeHour(); hour.density_basis.observed_at='2026-10-09T02:59:29Z';
    assert.equal(h.dataGaps(hour).get('density-current').severity,'error','Hourly mean does not replace the missing hour-end basis');
    hour.density_basis.observed_at='2026-10-09T02:59:30Z';
    assert(!h.dataGaps(hour).has('density-current'),'Exactly 30 seconds is a valid basis');
    hour=completeHour();
    hour.evidence={segments:[{duration_seconds:80,state:{quality:{coal_amount:'held',coarse_ash:'held',total_ash:'held',heavy_ash:'manual'}}}]};
    const before=JSON.stringify(hour);
    gaps=h.dataGaps(hour);
    assert(gaps.has('coal-rate') && gaps.has('coarse') && gaps.has('total-ash') && !gaps.has('density'));
    assert(gaps.get('coal-rate').message.includes('80') && !gaps.has('heavy-ash'));
    assert.equal(gaps.get('coal-rate').severity,'warning','Legacy wall time alone cannot prove an advice blocker');
    assert.equal(JSON.stringify(hour),before,'Highlighting never mutates frozen values');
    hour=completeHour(); hour.coverage.longest_gap_seconds=80;
    assert.deepEqual([...h.dataGaps(hour).keys()],['density'],'Advice gaps alone must not blame healthy input cards');
    hour.evidence={segments:[segment(8,1,badSwitches,6.153),segment(9,5,badSwitches,25.02)]};
    gaps=h.dataGaps(hour);
    assert.equal(gaps.get('system-switches').severity,'warning');
    assert.equal(gaps.get('system-switches').label,'短时缺失');
    assert(gaps.get('system-switches').message.includes('31'));
    hour.suggested_density.value=1.48;
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'warning','Brief gaps remain visible with a successful recommendation');
    assert(!h.dataGaps(hour).has('density'));
    hour=completeHour(); hour.coverage.longest_gap_seconds=80;
    hour.evidence={segments:[segment(10,7,badSwitches),segment(18,7,badSwitches)]};
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'warning','Two separated 35s gaps must not become a continuous 70s gap');
    hour.evidence.segments=[segment(10,6,badSwitches),segment(16,6,badSwitches)];
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'warning','Adjacent invalid segments at exactly 60s pass the duration gate');
    hour.evidence.segments.push(segment(22,1,badSwitches));
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'error','Adjacent invalid segments are one 65s run');
    hour.coverage.longest_gap_seconds=60;
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'warning','Current protection alone must not promote historical gaps to blockers');
    hour=completeHour(); hour.coverage.ratio=647/720;
    hour.evidence={segments:Array.from({length:72},(_,i)=>segment(i*2,1,badSwitches))};
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'warning','Exactly 10% missing slots do not block');
    hour.evidence.segments.push(segment(144,1,badSwitches));
    assert.equal(h.dataGaps(hour).get('system-switches').severity,'error','More than 10% known missing slots block');
    hour.evidence.segments.push(segment(144,1,badSwitches));
    assert(h.dataGaps(hour).get('system-switches').message.includes('10.1%'),'Overlapping slots count only once');
    hour=completeHour(); hour.coverage.longest_gap_seconds=60; hour.coverage.ratio=.9;
    assert(!h.dataGaps(hour).has('density'),'Overall 90% coverage and 60s gap pass both gates');
    hour.cells.total_ash={value:null,status:'missing',reason:'没有实际产煤量配对记录'};
    hour.coverage.ratio=0;
    assert.equal(h.dataGaps(hour).get('total-ash').severity,'warning','Missing mass-weighted statistic is not a per-cycle advice blocker');
    hour.cells.coarse_ash=hour.cells.heavy_amount={value:null,status:'missing',reason:'尚无该小时有效的逐次计算记录'};
    assert.equal(h.dataGaps(hour).get('coarse').severity,'warning','Missing decision archives do not prove a sensor failure');
    assert.equal(h.dataGaps(hour).get('heavy-amount').severity,'warning');
    hour.evidence={segments:[segment(10,13,{quality:{coarse_ash:'missing'}})]};
    assert.equal(h.dataGaps(hour).get('coarse').severity,'error','Recorded invalid output slots can establish the actual blocker');
    hour.cells.heavy_ash.value=null;
    hour.evidence={segments:[segment(10,1,{quality:{heavy_amount:'missing'}})]};
    assert.equal(h.dataGaps(hour).get('heavy').severity,'error','Aggregate inherits the highest child severity');
    assert.equal(h.gapClass({severity:'warning'}),' is-hourly-warning');
    assert.equal(h.gapClass({severity:'error'}),' is-hourly-gap');
    assert.equal(h.gapClass(null),'');
    let pending=h.update(page,true); resolve(response()); await pending;
    assert(h.presentation(),'Server time, not the local calendar, determines the valid hour');
    assert.equal(h.display(page,{id:'raw-ash'}).value,'40.00 %');
    const parts=h.display(page,{id:'system-switches'}).components;
    assert.equal(parts[0].value,.5); assert.equal(parts[1].value,0); assert(parts[1].available && parts[1].fraction);
    clock=36*60*1000;
    assert.equal(h.presentation(),null,'The old hour expires at the server boundary');
    assert.equal(h.dataGaps().size,0,'Expired hours cannot leave red cards behind');
    clock=0;
    pending=h.update(page,true);
    context.DecisionSources.epoch++;
    resolve(response()); await pending;
    assert.equal(h.presentation(),null,'Source changes reject responses issued under old configuration');
    resolve(response()); await h.inFlight;
    await Promise.resolve();
    assert(h.presentation());
    pending=h.update(page,true); reject(new Error('小时请求失败')); await pending;
    assert.equal(h.presentation(),null); assert.equal(h.error,'小时请求失败');
    // Enter instant mode directly: one shared hourly request, no mode switch required.
    page.hourMode=()=>false;
    const root={x:670,y:343,w:245};
    let renders=0; page.renderGraph=()=>renders++;
    const source={resolved_choices:{density:'A304'}};
    const advised=()=>{
        const result={...completeHour(),sources:source,density_label:'A304',
            suggested_density:{value:1.472,status:'calculated',reason:'按上一小时工况建议整体下调'},guidance:{direction:'down'}};
        result.cells.suggested_density=result.suggested_density;
        return result;
    };
    pending=h.update(page,true); const shared=h.inFlight;
    await h.update(page,true); assert.equal(h.inFlight,shared,'Instant/hour views share one request');
    const htmlPending=h.summary(page,root);
    assert(!htmlPending.includes('1.472') && htmlPending.includes('读取中'));
    resolve(advised()); await pending;
    assert.equal(renders,1,'Hourly completion redraws the instant page');
    let summary=h.summary(page,root);
    assert(summary.includes('本小时建议密度') && summary.includes('1.472') && summary.includes('11:00–12:00'));
    assert(summary.includes('top:118px') && !summary.includes('data-decision-node='),'Summary sits above the root without adding a calculation node');
    assert.equal(h.display(page,{id:'density'}).value,'1.472','Summary and cumulative view use the same value');
    page.hourMode=()=>true; assert.equal(h.summary(page,root),'','No duplicate summary in cumulative mode');
    await h.update(page); assert.equal(h.inFlight,null,'Switching modes respects the shared refresh throttle');
    page.hourMode=()=>false;
    context.DecisionSources.config={resolved_choices:{density:'A303'}};
    assert.equal(h.presentation(),null,'Another client changing the density source hides stale finite advice');
    assert(!h.summary(page,root).includes('1.472'));
    h.result.suggested_density={value:null,reason:'当前来源变化，等待完整小时'};
    assert(h.summary(page,root).includes('当前来源变化'),'Server reasons for a withheld hour remain visible');
    page.hourMode=()=>true;
    pending=h.update(page,true); resolve(advised()); await pending;
    assert(h.presentation(),'Fresh server-validated advice overrides a stale local config in cumulative mode');
    page.hourMode=()=>false;
    context.DecisionSources.config=null;
    pending=h.update(page,true); reject(new Error('小时统计离线')); await pending;
    assert(h.summary(page,root).includes('小时统计离线') && !h.summary(page,root).includes('1.472'));
    context.DecisionSources.saving=true;
    await h.update(page,true); assert.equal(h.inFlight,null,'Do not query hourly advice while source selection is being saved');
    context.DecisionSources.saving=false;
    // A response generated before the boundary but delivered after it must expire.
    clock=0; pending=h.update(page,true); clock=8000;
    resolve({...advised(),checked_at:'2026-10-09T03:59:56Z'}); await pending;
    assert.equal(h.presentation(),null,'Slow replies cannot prolong the previous hour');
    assert(!h.summary(page,root).includes('1.472'));
    page.view='production'; await h.update(page,true);
    assert.equal(h.inFlight,null); assert.equal(h.summary(page,root),'','DS does not request or display the hourly summary');
    console.log('Hourly UI: gap severity, instant summary, shared requests, source changes, failures and rollover passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
