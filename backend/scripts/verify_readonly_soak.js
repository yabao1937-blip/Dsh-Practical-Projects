// Bounded, real-browser acceptance. All requests to the configured laptop are GET-only.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {performance}=require('node:perf_hooks');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const options=Object.fromEntries(process.argv.slice(2).map(arg=>arg.replace(/^--/,'').split('=')));
const base=(options.url || 'http://127.0.0.1:8000').replace(/\/$/,'');
assert.ok(['127.0.0.1','localhost'].includes(new URL(base).hostname),'Only the laptop loopback server is allowed');
const duration=Number(options.seconds || 1800),probeEvery=Number(options['probe-seconds'] || 30);
assert.ok(duration>=30 && duration<=7200 && probeEvery>=10);
const reportPath=path.resolve(options.report || 'output/acceptance-20261017/readonly-soak.json');
fs.mkdirSync(path.dirname(reportPath),{recursive:true});
const routes=['/readonly-runtime/operations','/decision/current','/decision/hourly','/decision/total-ash-hours'];
const result={schema_version:1,started_at:new Date().toISOString(),duration_seconds:duration,mode:'running',
    browser_count:3,request_counts:{},errors:[],page_checks:[],probes:{},write_attempts:0,forbidden_full_reads:0};
const percentile=(values,q)=>{const sorted=values.slice().sort((a,b)=>a-b);return sorted.length?sorted[Math.ceil(q*sorted.length)-1]:null;};
const elapsed=()=>Math.round((performance.now()-started)/1000);
let started,browser,closing=false;
const samples={};
function error(kind,details){if(result.errors.length<200)result.errors.push({at_seconds:elapsed(),kind,...details});}
function snapshot(){
    result.elapsed_seconds=elapsed();
    for(const [route,rows] of Object.entries(samples)) {
        const hot=rows.filter(row=>row.confirmed_reuse).map(row=>row.ms),all=rows.map(row=>row.ms);
        result.probes[route]={requests:all.length,confirmed_reuse:hot.length,
            overall_p95_ms:percentile(all,.95),reuse_p95_ms:percentile(hot,.95),reuse_max_ms:hot.length?Math.max(...hot):null};
    }
    fs.writeFileSync(reportPath,JSON.stringify(result,null,2)+'\n');
}
async function read(route){
    const start=performance.now(),response=await fetch(base+'/api/v1'+route,{signal:AbortSignal.timeout(30000)});
    if(!response.ok)throw new Error(route+' HTTP '+response.status);
    const value=await response.json();
    const identity=value.generated_at || value.checked_at || crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
    return {ms:Math.round((performance.now()-start)*100)/100,identity,value};
}
async function probes(){
    for(const route of routes) {
        try {
            const first=await read(route),second=await read(route);
            (samples[route] ||= []).push({ms:first.ms,confirmed_reuse:false},{ms:second.ms,confirmed_reuse:first.identity===second.identity});
        }catch(e){error('probe',{route,message:e.message});}
    }
}
async function check(pages){
    for(const [index,page] of pages.entries()) {
        try {
            const state=await page.evaluate(()=>({page:document.querySelector('.page.active')?.id,
                ready:App.__ready,nodes:DecisionOverviewPage.nodes.length,
                rows:document.querySelectorAll('#decision-recent-ash-layer tbody tr').length,
                backend:Operations.serverState,telemetryFailed:Operations.telemetryFailed,
                manualFailed:Operations.manualMeasurementsFailed,fullHistory:!!App._readOnlyFullStateLoaded,
                modelAnalysis:GptExpert9.loaded,heap:performance.memory?.usedJSHeapSize || null}));
            result.page_checks.push({at_seconds:elapsed(),browser:index,...state});
            if(state.page!=='page-decision'||!state.ready||state.nodes!==18||state.rows!==10||state.fullHistory||state.modelAnalysis)
                error('page_invariant',{browser:index,state});
            if(state.backend!=='online'||state.telemetryFailed||state.manualFailed)
                error('page_health',{browser:index,state});
        }catch(e){error('page_check',{browser:index,message:e.message});}
    }
}
(async()=>{
    started=performance.now();
    const mode=await read('/runtime-mode');
    assert.equal(mode.value.mode,'readonly-test','A production server cannot be a soak target');
    result.data_server=mode.value.data_server;
    browser=await chromium.launch({channel:'chrome',headless:true});
    const pages=await Promise.all([0,1,2].map(async(index)=>{
        const context=await browser.newContext({viewport:{width:1500,height:1050}});
        await context.route('**/api/**',route=>{
            if(!['GET','HEAD','OPTIONS'].includes(route.request().method())) {
                result.write_attempts++;error('write_attempt',{browser:index,path:new URL(route.request().url()).pathname});return route.abort();
            }
            return route.continue();
        });
        const page=await context.newPage();
        page.on('pageerror',e=>error('javascript',{browser:index,message:e.message}));
        page.on('requestfailed',request=>{if(!closing)error('request_failed',{browser:index,path:new URL(request.url()).pathname,message:request.failure()?.errorText});});
        page.on('response',response=>{
            const pathname=new URL(response.url()).pathname;
            if(!pathname.startsWith('/api/'))return;
            result.request_counts[pathname]=(result.request_counts[pathname] || 0)+1;
            if(response.status()>=400)error('http',{browser:index,path:pathname,status:response.status()});
            if(['/api/v1/state','/api/v1/gpt-coarse/current'].includes(pathname))result.forbidden_full_reads++;
        });
        await page.goto(base+'/',{waitUntil:'domcontentloaded'});
        await page.waitForFunction(()=>App.__ready && DecisionOverviewPage.nodes.length===18 &&
            !!DecisionRealtime.result && !!DecisionHourly.result && document.querySelectorAll('#decision-recent-ash-layer tbody tr').length===10,null,{timeout:60000});
        return page;
    }));
    let nextProbe=0,nextCheck=0;
    while(elapsed()<duration){
        if(elapsed()>=nextProbe){await probes();nextProbe=elapsed()+probeEvery;}
        if(elapsed()>=nextCheck){await check(pages);snapshot();nextCheck=elapsed()+30;}
        await new Promise(resolve=>setTimeout(resolve,1000));
    }
    await check(pages);snapshot();
    const enough=duration>=600?20:1;
    const hotPass=routes.every(route=>result.probes[route]?.confirmed_reuse>=enough && result.probes[route].reuse_p95_ms<=500);
    result.passed=result.errors.length===0 && result.write_attempts===0 && result.forbidden_full_reads===0 && hotPass;
    result.mode='complete';result.ended_at=new Date().toISOString();
    result.criteria={confirmed_reuse_p95_max_ms:500,minimum_reuse_samples_per_route:enough};
    snapshot();console.log(JSON.stringify({passed:result.passed,report:reportPath,elapsed_seconds:elapsed(),errors:result.errors.length,probes:result.probes}));
    if(!result.passed)process.exitCode=1;
})().catch(e=>{result.passed=false;result.mode='failed';error('runner',{message:e.message});snapshot();console.error(e.message);process.exitCode=1;})
    .finally(async()=>{closing=true;if(browser)await browser.close();});
