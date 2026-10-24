// Isolated backend/database and fresh browsers. Only the new manual POST is allowed.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), net = require('node:net');
const {spawn,spawnSync} = require('node:child_process');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..'), backend = path.join(root,'backend');
const output = path.join(root,'output/legacy-cleanup/manual-browser');
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));
async function freePort() {
    const server = net.createServer();
    await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
    const port = server.address().port;
    await new Promise(resolve => server.close(resolve));
    return port;
}

(async () => {
    fs.mkdirSync(output,{recursive:true});
    const database = path.join(output,'browser-test-'+Date.now()+'.db');
    const port = await freePort(), base = 'http://127.0.0.1:'+port;
    const isolatedEnv={...process.env,DMCS_DATABASE_URL:'sqlite:///'+database.replaceAll('\\','/'),
        DMCS_ISOLATED_TEST_DB:'1',DMCS_WRITE_AUTH:'off'};
    delete isolatedEnv.DMCS_REMOTE_READONLY_CONFIG;
    const server = spawn(path.join(backend,'.venv/Scripts/python.exe'), ['-m','uvicorn','app.main:app','--host','127.0.0.1','--port',String(port)], {
        cwd:backend, windowsHide:true, env:isolatedEnv,
        stdio:['ignore','ignore','pipe'],
    });
    let stderr = '', browser;
    server.stderr.on('data',chunk=>{stderr=(stderr+chunk).slice(-16000);});
    try {
        let healthy=false;
        for(let i=0;i<60;i++) {
            try {healthy=(await fetch(base+'/api/v1/health')).ok;} catch (_) {}
            if(healthy) break;
            if(server.exitCode!==null) throw Error('Isolated backend failed: '+stderr);
            await sleep(200);
        }
        assert.ok(healthy,'Isolated backend must start');
        browser=await chromium.launch({channel:'chrome',headless:true});
        const errors=[],blocked=[];
        async function newClient() {
            const context=await browser.newContext({viewport:{width:1500,height:1100}});
            await context.route('**/api/**',route=>{
                const request=route.request(),url=new URL(request.url());
                if(['GET','HEAD','OPTIONS'].includes(request.method()) ||
                    (url.origin===base && url.pathname==='/api/v1/manual-measurements' && request.method()==='POST')) return route.continue();
                blocked.push(url.pathname);return route.abort();
            });
            const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
            return {context,page};
        }
        const a=await newClient();
        let releaseMetrics, metricsCalls=0;
        const metricsGate=new Promise(resolve=>{releaseMetrics=resolve;});
        await a.page.route('**/manual-measurements/metrics',async route=>{metricsCalls++;await metricsGate;await route.continue();});
        await a.page.goto(base+'/#decision');
        await a.page.waitForFunction(()=>App.__ready);
        await a.page.locator('#decision-manual-open').click();
        await a.page.waitForFunction(()=>!!ManualMeasurements.definitionsPromise);
        await a.page.evaluate(()=>{ManualMeasurements.refresh();ManualMeasurements.refresh();});
        releaseMetrics();
        await a.page.waitForFunction(()=>ManualMeasurements.ready);
        assert.equal(metricsCalls,1,'Concurrent initial refresh must share one metadata request');
        await a.page.evaluate(()=>{clearInterval(Operations.timer);Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true});});
        const storeBefore=await a.page.evaluate(()=>JSON.stringify(App.store));
        assert.equal(await a.page.locator('#decision-measurement-time').inputValue(),'');
        async function submit(metric,value,time='') {
            await a.page.locator('#decision-measurement-metric').selectOption(metric);
            await a.page.locator('#decision-measurement-value').fill(String(value));
            await a.page.locator('#decision-measurement-time').fill(time);
            await a.page.locator('#decision-measurement-submit').click();
            await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='success');
        }
        await submit('heavy_clean_coal_ash',8.63);
        let records=await (await fetch(base+'/api/v1/manual-measurements')).json();
        assert.equal(records.total,1);assert.equal(records.items[0].value,8.63);
        assert.equal(records.items[0].observed_at,records.items[0].created_at);
        await submit('coarse_slime_ash',16.2,'2026-10-07T09:12');
        records=await (await fetch(base+'/api/v1/manual-measurements')).json();
        assert.equal(records.items[0].observed_at,'2026-10-07T01:12:00.000+00:00');
        await submit('raw_coal_slime_content',7.25);
        const b=await newClient();
        await b.page.goto(base+'/#decision');await b.page.waitForFunction(()=>App.__ready);
        await b.page.locator('#decision-manual-open').click();
        await b.page.waitForFunction(()=>ManualMeasurements.total===3);
        assert.match(await b.page.locator('#decision-measurement-history').innerText(),/7\.25/);
        await b.page.reload();await b.page.waitForFunction(()=>App.__ready);
        await b.page.locator('#decision-manual-open').click();
        await b.page.waitForFunction(()=>ManualMeasurements.total===3);

        // The server commits but its response is lost: preserve inputs and reuse the request ID.
        await a.page.route('**/api/v1/manual-measurements',async route=>{
            if(route.request().method()!=='POST') return route.continue();
            await route.fetch();await route.abort();
        });
        await a.page.locator('#decision-measurement-metric').selectOption('heavy_clean_coal_ash');
        await a.page.locator('#decision-measurement-value').fill('8.7');
        await a.page.locator('#decision-measurement-submit').click();
        await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='error');
        assert.equal(await a.page.locator('#decision-measurement-value').inputValue(),'8.7');
        assert.equal((await (await fetch(base+'/api/v1/manual-measurements')).json()).total,4);
        await a.page.unroute('**/api/v1/manual-measurements');
        await a.page.locator('#decision-measurement-submit').click();
        await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='success');
        assert.equal((await (await fetch(base+'/api/v1/manual-measurements')).json()).total,4);

        // Malformed confirmation cannot clear the form or report a successful save.
        await a.page.route('**/api/v1/manual-measurements',async route=>{
            if(route.request().method()!=='POST') return route.continue();
            const response=await route.fetch(),record=await response.json();
            await route.fulfill({response,json:{...record,value:99.9,observed_at:'invalid'}});
        });
        await a.page.locator('#decision-measurement-value').fill('8.75');
        await a.page.locator('#decision-measurement-submit').click();
        await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='error');
        assert.equal(await a.page.locator('#decision-measurement-value').inputValue(),'8.75');
        await a.page.unroute('**/api/v1/manual-measurements');
        await a.page.locator('#decision-measurement-submit').click();
        await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='success');
        assert.equal((await (await fetch(base+'/api/v1/manual-measurements')).json()).total,5);
        await submit('raw_coal_slime_content',0);
        assert.equal(await a.page.evaluate(()=>JSON.stringify(App.store)),storeBefore,'The manual form must not change business model inputs');
        await a.page.locator('#decision-manual-dialog').screenshot({path:path.join(output,'manual-form-desktop.png')});
        await a.page.locator('#decision-measurement-filter').selectOption('coarse_slime_ash');
        await a.page.waitForFunction(()=>ManualMeasurements.total===1);
        assert.match(await a.page.locator('#decision-measurement-history').innerText(),/09:12:00/);
        await a.page.setViewportSize({width:390,height:1000});
        const bounds=await a.page.locator('#decision-manual-dialog').evaluate(panel=>({width:panel.clientWidth,scroll:panel.scrollWidth,left:panel.getBoundingClientRect().left,right:panel.getBoundingClientRect().right}));
        assert.ok(bounds.scroll<=bounds.width+1,'The dialog contains horizontal table scrolling');
        assert.ok(bounds.left>=0 && bounds.right<=391,'The manual dialog fits a narrow viewport');
        await a.page.locator('#decision-manual-dialog').screenshot({path:path.join(output,'manual-form-mobile.png')});
        await a.page.setViewportSize({width:1500,height:1100});
        assert.equal(await a.page.locator('#page-collect').count(),0,'Retired collection page is absent');
        await a.page.locator('#decision-measurement-metric').selectOption('heavy_clean_coal_ash');
        await a.page.locator('#decision-measurement-value').fill('8.66');
        await a.page.locator('#decision-measurement-time').fill('2026-10-17T08:47');
        await a.page.locator('#decision-manual-dialog header button').click();
        await a.page.locator('#decision-manual-open').click();
        assert.equal(await a.page.locator('#decision-measurement-value').inputValue(),'8.66');
        await a.page.locator('#decision-measurement-submit').click();
        await a.page.waitForFunction(()=>!ManualMeasurements.busy && document.getElementById('decision-measurement-message').dataset.status==='success');
        records=await (await fetch(base+'/api/v1/manual-measurements')).json();
        assert.equal(records.total,7);assert.equal(records.items[0].value,8.66);
        assert.equal(records.items[0].observed_at,'2026-10-17T00:47:00.000+00:00','actual production time is saved without a two-hour shift');
        assert.deepEqual(errors,[]);
        const result={passed:true,database,rows:7,metrics:3,defaultServerTime:true,beijingTimeRoundtrip:true,
            sharedAcrossBrowsersAndReload:true,concurrentMetadataSingleRequest:true,lanRequestIdFallback:true,
            lostResponseRetryNoDuplicate:true,malformedResponsePreservesInputs:true,zeroValuePreserved:true,
            independentDialogSave:true,actualProductionTime:true,
            modelStoreUnchanged:true,mobileOverflow:false,pageErrors:errors,blockedOtherWritePaths:[...new Set(blocked)]};
        fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally {
        if(browser) await browser.close();
        if(server.exitCode===null && process.platform==='win32')
            spawnSync('taskkill',['/PID',String(server.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
        else server.kill();
        await new Promise(resolve=>{if(server.exitCode!==null)return resolve();server.once('exit',resolve);});
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
