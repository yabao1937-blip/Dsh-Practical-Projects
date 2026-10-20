// Companion to the disposable Python host fixture. No forced polling or fake clock.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {performance}=require('node:perf_hooks');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const [manifestPath,reportPath]=process.argv.slice(2),m=JSON.parse(fs.readFileSync(manifestPath,'utf8'));
for(const url of [m.host_url,m.laptop_url])assert.equal(new URL(url).hostname,'127.0.0.1');
const report={passed:false,synthetic_only:true,production_requests:0,browser_count:3,sample_count:m.sample_count,
    slots_per_hour:m.slots_per_hour,coverage:m.coverage,model:m.model,stages:[],errors:[],laptop_write_requests:0,external_requests:0};
const save=()=>fs.writeFileSync(reportPath,JSON.stringify(report,null,2)+'\n');
let browser,closing=false;
async function state(page){return page.evaluate(()=>({
    rows:[...document.querySelectorAll('#decision-recent-ash-layer tbody tr')].map(row=>({
        start:Number(row.dataset.decisionKey?.replace('recent-ash-','')),value:Number(row.cells[1]?.textContent)})),
    hourly:typeof DecisionHourly==='undefined'?null:DecisionHourly.result,
    densityText:document.querySelector('.decision-hourly-summary .decision-node-value')?.textContent.trim(),
    currentPending:typeof DecisionRealtime==='undefined'?true:!!DecisionRealtime.inFlight,
    hourlyPending:typeof DecisionHourly==='undefined'?true:!!DecisionHourly.inFlight
}));}
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function until(action,timeout=45000){const began=performance.now();let last;
    while(performance.now()-began<timeout){last=await action();if(last)return last;await wait(100);}throw Error('Condition timeout');}
async function assay(observed,value,id){const response=await fetch(m.host_url+'/api/v1/manual-measurements',{
    method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({metric:'heavy_clean_coal_ash',value,observed_at:observed,client_id:id})});
    assert.equal(response.status,201,await response.clone().text());const row=await response.json();
    assert.equal(row.time_basis,'production-time-v1');assert.equal(row.value,value);
    assert.equal(Date.parse(row.observed_at),Date.parse(observed));return row;}
function observed(index,minute){return new Date(Date.parse(m.starts[index])+minute*60000).toISOString();}
async function stage(pages,name,index,minute,value,expected,expectedDensity){
    const previous=await Promise.all(pages.map(state));
    // Start immediately after current/hourly baseline requests have completed.
    // Normal browser timers alone must notice this external host mutation.
    await until(async()=>{const s=await Promise.all(pages.map(state));return s.every(x=>!x.currentPending&&!x.hourlyPending);});
    const started=performance.now(),row=await assay(observed(index,minute),value,'e2e-'+name);
    const entry={name,observed_at:row.observed_at,value,expected_density:expectedDensity,page_updates:[]};
    report.stages.push(entry);save();
    await Promise.all(pages.map(async(page,pageIndex)=>{
        let last;
        try{await until(async()=>{last=await state(page);
            const table=expected.every((number,i)=>last.rows.find(row=>row.start===Date.parse(m.starts[i]))?.value===number);
            const hour=last.hourly,ash=hour?.advice_inputs?.ash;
            const matching=hour?.suggested_density?.value===expectedDensity&&
                Math.abs(ash?.value-expected[9])<1e-7&&
                hour?.advice_inputs?.density?.value===m.anchor.value&&
                String(last.densityText).startsWith(expectedDensity.toFixed(3));
            return table&&matching;
        });const elapsed=(performance.now()-started)/1000;
            entry.page_updates.push({page:pageIndex+1,seconds:elapsed,density:last.hourly.suggested_density.value,
                ash_revision:last.hourly.advice_inputs.ash.revision,origin:last.hourly.advice_inputs.ash.calculation_origin,
                all_ten_rows:true,within_30_seconds:elapsed<=30});
        }catch(error){entry.page_updates.push({page:pageIndex+1,error:String(error),last});}
        save();
    }));
    assert.equal(entry.page_updates.length,3);
    assert(entry.page_updates.every(x=>x.within_30_seconds),name+' exceeded 30 seconds or did not update');
    assert(entry.page_updates.every(x=>x.origin==='readonly-local-replay'));
    assert.equal(new Set(entry.page_updates.map(x=>x.ash_revision)).size,1);
    return previous;
}
(async()=>{
    browser=await chromium.launch({channel:'chrome',headless:true});
    const context=await browser.newContext({viewport:{width:1500,height:1100}});
    await context.route('**/*',async route=>{const url=new URL(route.request().url());
        if(url.hostname!=='127.0.0.1'){report.external_requests++;return route.abort();}return route.continue();});
    const pages=await Promise.all(Array.from({length:3},()=>context.newPage()));
    for(const page of pages){page.on('pageerror',error=>{report.errors.push(String(error));save();});
        page.on('requestfailed',request=>{if(!closing){report.errors.push(request.url()+' '+request.failure()?.errorText);save();}});
        page.on('response',response=>{if(response.status()>=400){report.errors.push(response.url()+' HTTP '+response.status());save();}});
        page.on('request',request=>{if(new URL(request.url()).origin===m.laptop_url&& !['GET','HEAD','OPTIONS'].includes(request.method()))report.laptop_write_requests++;});}
    await Promise.all(pages.map(page=>page.goto(m.laptop_url,{waitUntil:'domcontentloaded'})));
    await until(async()=>{const states=await Promise.all(pages.map(state));return states.every(s=>
        s.rows.length===10&&s.rows.every(row=>row.value===m.initial.ash)&&s.hourly?.suggested_density?.value===m.initial.density);},90000);
    report.baseline=await Promise.all(pages.map(state));save();
    await stage(pages,'ten-hours-first-late-assay',0,47,8,Array(10).fill(m.after_first.ash),m.after_first.density);
    await stage(pages,'same-hour-newer-sample',0,50,9,Array(10).fill(m.after_latest.ash),m.after_latest.density);
    await stage(pages,'next-actual-sample',5,20,8.6,[...Array(5).fill(m.after_latest.ash),...Array(5).fill(m.after_next.ash)],m.after_next.density);
    await stage(pages,'backfill-stops-at-next-actual',2,20,8.4,[...Array(2).fill(m.after_latest.ash),...Array(3).fill(m.after_backfill.ash),...Array(5).fill(m.after_next.ash)],m.after_next.density);
    await pages[0].screenshot({path:path.join(path.dirname(reportPath),'readonly-settlement-e2e.png'),fullPage:true});
    assert.equal(report.laptop_write_requests,0);assert.equal(report.external_requests,0);assert.deepEqual(report.errors,[]);
    report.passed=true;
})().catch(error=>{report.error=String(error.stack||error);process.exitCode=1;}).finally(async()=>{
    closing=true;if(browser)await browser.close();save();console.log(JSON.stringify({passed:report.passed,stages:report.stages.map(s=>({name:s.name,pages:s.page_updates.map(p=>({page:p.page,seconds:p.seconds,error:p.error}))})),error:report.error}));
});
