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
recent.result={generated_at:'2026-10-14T08:31:00+08:00',publication_delay_seconds:7200,
    items:[{start:'2026-10-14T05:00:00+08:00',end:'2026-10-14T06:00:00+08:00',
        status:'partial',value:8.56,coverage:.95}]};
assert.equal(recent.latestEnd(),stamp('06:00'));
assert.equal(recent.rows().length,10);
assert.equal(recent.rows()[0].start,stamp('05:00'));
assert.equal(recent.rows()[0].value,8.56);
elapsed=29*60*1000;
assert.equal(recent.latestEnd(),stamp('07:00'));
assert.equal(recent.rows()[0].start,stamp('06:00'));
assert.equal(recent.rows()[0].value,null,'newly due hour cannot borrow an older value');
assert.equal(recent.rows()[1].value,8.56);
context.DecisionHourly={clockBaseAt:elapsed,result:{checked_at:'2026-10-15T00:01:00+08:00'}};
assert.equal(recent.parts(recent.rows()[0].start).join(' '),'2026-10-14 21:00');
const html=recent.markup({instantLayout:()=>true,escape:x=>x,number:x=>x===null?'—':x.toFixed(2)},
    {x:1,y:400,w:200});
assert(html.includes('延迟两小时结算') && html.includes('21:00–22:00'));
console.log('Recent ash: server clock, two-hour delay, exact-hour rollover, missing values and midnight passed.');
