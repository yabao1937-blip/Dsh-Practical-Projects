/* Pure-controller regression. Does not launch browsers or contact production. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
let clock=0,resolve,reject;
const context={Date,Number,Math,Object,Array,String,performance:{now:()=>clock},DecisionSources:{epoch:0},
    Api:{getDecisionHourly:()=>new Promise((a,b)=>{resolve=a;reject=b;})}};
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/js/decision-hourly.js','utf8')+';this.hourly=DecisionHourly;',context);
const h=context.hourly;
const page={hourMode:()=>true,active:()=>true,renderGraph(){},renderContext(){},renderInspector(){},
    summaryChildren:new Map(),number:(v,d=2,u='')=>Number.isFinite(v)?v.toFixed(d)+(u?' '+u:''):'—',time:v=>v};
const response=()=>({period:{end:'2026-10-09T03:00:00Z',apply_end:'2026-10-09T04:00:00Z',apply_label:'11:00–12:00'},
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
    coverage:{ratio:1,required_ratio:.9,longest_gap_seconds:0,max_gap_seconds:60}});
(async()=>{
    assert.equal(h.dataGaps(null).size,0,'Loading or failed queries are not known data gaps');
    let hour=completeHour();
    assert.equal(h.dataGaps(hour).size,0,'Valid carry, zero flow, stopped switches and reference-only gaps are not blockers');
    hour.cells.heavy_ash={value:null,status:'missing',reason:'缺少重介化验'};
    let gaps=h.dataGaps(hour);
    assert(gaps.has('heavy-ash') && gaps.has('heavy') && gaps.has('density'));
    assert(!gaps.has('float') && !gaps.has('float-unmodeled'));
    hour.suggested_density.value=1.48;
    assert.equal(h.dataGaps(hour).size,0,'A valid suggestion removes gap styling');
    hour=completeHour(); delete hour.cells.systems.components.A;
    assert(h.dataGaps(hour).has('system-switches'),'One missing required switch must mark the grouped card');
    hour=completeHour(); hour.density_basis.observed_at='2026-10-09T02:59:29Z';
    assert(h.dataGaps(hour).has('density-current'),'Hourly mean does not replace the missing hour-end basis');
    hour.density_basis.observed_at='2026-10-09T02:59:30Z';
    assert(!h.dataGaps(hour).has('density-current'),'Exactly 30 seconds is a valid basis');
    hour=completeHour();
    hour.evidence={segments:[{duration_seconds:80,state:{quality:{coal_amount:'held',coarse_ash:'held',total_ash:'held',heavy_ash:'manual'}}}]};
    const before=JSON.stringify(hour);
    gaps=h.dataGaps(hour);
    assert(gaps.has('coal-rate') && gaps.has('coarse') && gaps.has('total-ash') && gaps.has('density'));
    assert(gaps.get('coal-rate').includes('80') && !gaps.has('heavy-ash'));
    assert.equal(JSON.stringify(hour),before,'Highlighting never mutates frozen values');
    hour=completeHour(); hour.coverage.longest_gap_seconds=80;
    assert.deepEqual([...h.dataGaps(hour).keys()],['density'],'Advice gaps alone must not blame healthy input cards');
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
    console.log('Hourly UI: gap attribution, carry/zero/reference isolation, end basis, segment quality, server clock, rollover and request races passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
