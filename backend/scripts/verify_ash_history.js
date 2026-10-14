/* Verify modal async isolation and Beijing formatting without touching live data. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const path=require('path');
const context={AbortController,setTimeout,clearTimeout,Api:{}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../../frontend/js/decision-ash-history.js'),'utf8')+'\nthis.history=DecisionAshHistory;',context);
const h=context.history, elements=new Map();
h.dialog={open:true,querySelector(selector){if(!elements.has(selector)) elements.set(selector,{innerHTML:'',value:'',classList:{toggle(){}}});return elements.get(selector);}};
const result=value=>({summary:{value,valid_segments:1,total_segments:2,coverage:.25},
    period:{start:'2026-10-08T23:15:00+00:00',end:'2026-10-09T01:23:00+00:00'},latest_end:'2026-10-09T02:24:00+00:00',method:'<unsafe>',
    items:[{start:'2026-10-08T23:00:00+00:00',end:'2026-10-09T00:00:00+00:00',value,sample_count:360,expected_count:720,coverage:.5,status:'partial',reason:'<unsafe>'}]});
async function main(){
    assert.equal(h.label('2026-10-08T23:00:00Z'),'2026-10-09 07:00');
    const pending=[];
    context.Api.getTotalAshInterval=()=>new Promise((resolve,reject)=>pending.push({resolve,reject}));
    const first=h.load(),second=h.load({start:'new',end:'new'});
    pending[1].resolve(result(8.5));await second;
    pending[0].resolve(result(99));await first;
    assert.equal(h.result.summary.value,8.5,'late response must not overwrite the newer range');
    const html=elements.get('#ash-history-result').innerHTML;
    assert(html.includes('8.50') && html.includes('&lt;unsafe&gt;') && !html.includes('<unsafe>'));
    assert.equal(h.selectedRange().start,'2026-10-09T07:15:00+08:00');
    assert.equal(h.selectedRange().end,'2026-10-09T09:23:00+08:00');
    assert(html.includes('分时段平均灰分') && html.includes('有值时段') && !html.includes('已结束小时'));
    let refreshRange=null;
    const load=h.load;
    h.load=async range=>{refreshRange=range;};
    await h.measurementSaved();
    assert.equal(refreshRange.start,'2026-10-09T07:15:00+08:00','save preserves queried range');
    refreshRange=null;elements.get('#ash-start-time').value='08:00';
    await h.measurementSaved();
    assert.equal(refreshRange,null,'save does not submit unsaved range edits');
    h.load=load;
    const querying=h.load();
    h.measurementSaved();
    pending[2].resolve(result(8.6));await querying;
    assert.equal(pending.length,4,'save during query queues a fresh request');
    pending[3].resolve(result(8.8));await new Promise(resolve=>setTimeout(resolve,0));
    assert.equal(h.result.summary.value,8.8,'query completed with post-save result');
    const failed=h.load({start:'bad',end:'bad'});
    assert.equal(elements.get('#ash-history-result').innerHTML,'','old results must clear on query');
    pending[4].reject(new Error('invalid range'));await failed;
    assert.equal(h.result,null);assert.equal(elements.get('#ash-history-status').textContent,'invalid range');
    const closed=h.load();h.dialog.open=false;h.requestId++;
    pending[5].resolve(result(88));await closed;
    assert.equal(h.result,null,'closed modal must ignore pending response');
    console.log('Ash history: Beijing boundaries, weighted-result display, escaping, stale response, errors and close isolation passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
