// GET-only UI acceptance against the laptop test server; never target production.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const base='http://127.0.0.1:8000',output=path.resolve(__dirname,'../../output/legacy-cleanup');
(async()=>{
    assert.equal((await (await fetch(base+'/api/v1/runtime-mode')).json()).mode,'readonly-test');
    fs.mkdirSync(output,{recursive:true});
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        const page=await browser.newPage({viewport:{width:1600,height:1050}}),errors=[],writes=[],requests=[];
        page.on('pageerror',e=>errors.push(e.message));
        await page.route('**/api/**',route=>{
            const request=route.request();requests.push(new URL(request.url()).pathname);
            if(!['GET','HEAD','OPTIONS'].includes(request.method())) {writes.push(request.url());return route.abort();}
            return route.continue();
        });
        await page.goto(base);await page.waitForFunction(()=>App.__ready && DecisionRealtime.result && DecisionHourly.result);
        assert.equal(await page.locator('.page.active').getAttribute('id'),'page-decision');
        assert.equal(await page.locator('#page-overview,#page-collect,#page-import,#page-brief,#page-history').count(),0);
        const ids=await page.locator('.nav-item').evaluateAll(nodes=>nodes.map(n=>n.dataset.page));
        assert.deepEqual(ids.sort(),['page-coarse','page-decision','page-float','page-maintenance','page-telemetry']);
        await page.screenshot({path:path.join(output,'decision-overview.png'),fullPage:false});
        await page.locator('#decision-manual-open').click();
        await page.waitForFunction(()=>ManualMeasurements.ready);
        assert.equal(await page.locator('#decision-measurement-submit').isDisabled(),true);
        await page.locator('#decision-manual-dialog header button').click();
        await page.locator('[data-ash-history]:visible').click();
        await page.locator('#decision-ash-dialog').waitFor({state:'visible'});
        await page.locator('[data-ash-close]').click();
        await page.locator('[data-evidence-open]:visible').click();
        await page.locator('#decision-evidence-dialog').waitFor({state:'visible'});
        await page.locator('[data-e-close]').click();
        await page.locator('#maintenance-nav').click();
        await page.locator('[data-page="page-telemetry"]').click();
        await page.waitForFunction(()=>document.querySelectorAll('#telemetry-tbody tr[data-point]').length>0);
        assert.deepEqual(await page.locator('#page-telemetry th').allTextContents(),['测点名称','实时值','状态','采样时间']);
        const points=await page.locator('#telemetry-tbody tr[data-point]').evaluateAll(rows=>rows.map(row=>[row.dataset.source,row.dataset.point]));
        assert.equal(new Set(points.map(x=>JSON.stringify(x))).size,points.length);
        const names=await page.locator('#telemetry-tbody tr[data-point] td:first-child').allTextContents();
        assert.equal(new Set(names).size,names.length,'Same-description points remain distinguishable');
        assert.equal(await page.locator('#telemetry-reconnect').isDisabled(),true);
        assert.equal(requests.includes('/api/v1/state'),false,'KEP status needs no full-history download');
        await page.screenshot({path:path.join(output,'kep-telemetry.png'),fullPage:false});
        await page.locator('#maintenance-nav').click();
        await page.locator('[data-page="page-coarse"]').click();
        await page.waitForFunction(()=>document.getElementById('page-coarse').classList.contains('active'),null,{timeout:60000});
        await page.locator('#maintenance-nav').click();
        await page.locator('[data-page="page-float"]').click();
        await page.waitForFunction(()=>document.getElementById('page-float').classList.contains('active'));
        await page.locator('[data-page="page-decision"]').click();
        assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
        const result={passed:true,navigation:ids,points:points.length,manualReadOnly:true,intervalAndEvidence:true,analysisPages:true,errors,writes};
        fs.writeFileSync(path.join(output,'browser-navigation.json'),JSON.stringify(result,null,2));
        console.log(JSON.stringify(result));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
