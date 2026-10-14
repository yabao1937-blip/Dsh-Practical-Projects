/* Read-only delayed publication clock and latest-ten-hour display checks. */
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
let elapsed=0;
const context={performance:{now:()=>elapsed},window:{location:{protocol:'http:'}},
    DecisionHourly:{result:null,clockBaseAt:0}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../frontend/js/decision-recent-ash.js'),'utf8')+
    '\nthis.recent=DecisionRecentAsh;',context);
const recent=context.recent;
const stamp=value=>Date.parse(`2026-10-14T${value}:00+08:00`);
recent.result={generated_at:'2026-10-14T10:45:00+08:00',publication_delay_seconds:3600,
    items:[{start:'2026-10-14T08:00:00+08:00',end:'2026-10-14T09:00:00+08:00',
        status:'partial',value:8.56,coverage:.95,provisional:true,reason:'暂估：对应化验待补录'}]};
assert.equal(recent.latestEnd(),stamp('09:00'));
assert.equal(recent.rows().length,10);
assert.equal(recent.rows()[0].start,stamp('08:00'));
assert.equal(recent.rows()[0].value,8.56);
assert(recent.rows()[0].reason.includes('暂估'));
elapsed=15*60*1000;
assert.equal(recent.latestEnd(),stamp('10:00'));
assert.equal(recent.rows()[0].start,stamp('09:00'));
assert.equal(recent.rows()[0].value,null,'newly due hour cannot borrow an older value');
assert.equal(recent.rows()[1].value,8.56);
context.DecisionHourly={clockBaseAt:elapsed,result:{checked_at:'2026-10-15T00:01:00+08:00'}};
assert.equal(recent.parts(recent.rows()[0].start).join(' '),'2026-10-14 22:00');
const html=recent.markup({instantLayout:()=>true,escape:x=>x,number:x=>x===null?'—':x.toFixed(2)},
    {x:1,y:400,w:200});
assert(html.includes('按重介采样归属') && html.includes('22:00–23:00'));
async function main(){
    let requests=0, resolveOld;
    context.Api={getTotalAshHours:()=>new Promise(resolve=>{requests++;resolveOld=resolve;})};
    const page={instantLayout:()=>true,active:()=>true,renderGraph(){}};
    const old=recent.update(page,true), saved=recent.measurementSaved(page), savedAgain=recent.measurementSaved(page);
    assert.equal(requests,1,'save waits for pre-save request');
    resolveOld({generated_at:'2026-10-14T10:45:00+08:00',latest_end:'2026-10-14T09:00:00+08:00',items:[]});
    await old;
    assert.equal(requests,2,'save forces a new fetch after pre-save request');
    resolveOld({generated_at:'2026-10-14T10:45:00+08:00',latest_end:'2026-10-14T09:00:00+08:00',
        items:[{start:'2026-10-14T08:00:00+08:00',end:'2026-10-14T09:00:00+08:00',status:'partial',value:8.8}]});
    await saved;
    assert.equal(requests,3,'a second save waits for the first save refresh to finish');
    resolveOld({generated_at:'2026-10-14T10:45:00+08:00',latest_end:'2026-10-14T09:00:00+08:00',
        items:[{start:'2026-10-14T08:00:00+08:00',end:'2026-10-14T09:00:00+08:00',status:'partial',value:8.9}]});
    await savedAgain;
    assert.equal(recent.result.items[0].value,8.9);
    console.log('Recent ash: attribution clock, estimates, rollover, midnight and in-flight save refresh passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
