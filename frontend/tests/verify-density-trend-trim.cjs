const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = vm.runInNewContext(
    fs.readFileSync('frontend/js/decision-density-trend.js', 'utf8') + '\nDensityTrendPage;'
);
const iso = minutes => new Date(Date.UTC(2026, 10, 6, 0, minutes)).toISOString();
const ms = minutes => Date.parse(iso(minutes));
const hour = (start, value, reason) => ({
    at:iso(start+59), hour_start:iso(start), hour_end:iso(start+60), complete:true,
    final_valid:value!==null, value, reason
});
const data = {
    period:{start:iso(0),end:iso(180)}, summary:{total_samples:9},
    trend_exclusions:[
        {start:iso(0),end:iso(20),reason:'stopped'},
        {start:iso(40),end:iso(60),reason:'invalid_actual'},
        {start:iso(120),end:iso(180),reason:'stopped'}
    ],
    channels:{
        A304:[0,20,40,60,110,120].map(at=>({at:iso(at),value:1.5,break_before:at===40})),
        actual:[20,39,60,110].map(at=>({at:iso(at),value:1.49}))
    },
    hourly_suggestions:[hour(0,1.51),hour(60,null,'灰分缺失'),hour(120,1.52)],
    ash:[{at:iso(0),value:8.5,provisional:true},{at:iso(60),value:null},{at:iso(120),value:9.5}]
};
page.series=page.points(data);
const asJSON = x => JSON.parse(JSON.stringify(x));
assert.deepEqual(asJSON(page.series.blocks),[{startT:ms(20),endT:ms(40)},{startT:ms(60),endT:ms(120)}]);
assert.deepEqual(asJSON(page.series.advice.map(x=>[x.t,x.anchorT,x.value])),
    [[ms(20),ms(0),1.51],[ms(60),ms(60),null]]);
assert.deepEqual(asJSON(page.series.ash.map(x=>[x.t,x.anchorT,x.value,x.provisional ?? false])),
    [[ms(20),ms(0),8.5,true],[ms(60),ms(60),null,false]]);
assert.deepEqual(asJSON(page.series.groups),[{start:ms(60),end:ms(120),reason:'灰分缺失'}]);
assert.deepEqual(asJSON(page.series.A304.map(x=>[x.t,x.break_before])),
    [[ms(20),false],[ms(60),true],[ms(110),false]]);
assert.equal(page.stoppedStretches.length,2);
assert(!page.stoppedStretches.some(p=>p.start===ms(40)));
page.laid=page.layout(268);
const [left,right]=page.laid;
assert.equal(page.xAt(ms(40)),left.x1);
assert.equal(page.xAt(ms(45)),null);
assert.equal(page.tAt((left.x1+right.x0)/2),null);
assert.equal(page.tAt(left.x1),ms(40));
assert.equal(page.tAt(right.x0),ms(60));
assert.equal(page.connected(page.series.actual[1],page.series.actual[2]),false);
assert.equal(page.connected(page.series.A304[0],page.series.A304[1]),false);
assert.equal(page.connected({...page.series.A304[0]}, {...page.series.A304[1],break_before:false}),true);
page.series.blocks=Array.from({length:78},(_,i)=>({startT:i*2000,endT:i*2000+1000}));
page.laid=page.layout(70);
assert.equal(page.laid.at(-1).x1,70);
const gap=page.laid.slice(1).reduce((sum,b,i)=>sum+b.x0-page.laid[i].x1,0);
assert(Math.abs(gap-70*.12)<1e-8);
for (const block of page.laid) {
    assert(Number.isFinite(block.x0) && block.x0<block.x1 && block.x1<=70);
    assert(Math.abs(page.tAt(page.xAt(block.startT+500))-(block.startT+500))<1e-6);
}
const empty = page.points({...data,trend_exclusions:[{start:iso(0),end:iso(180),reason:'stopped'}]});
assert.equal(empty.blocks.length+empty.advice.length+empty.ash.length+empty.A304.length,0);
console.log('density trend trim fixture passed');

