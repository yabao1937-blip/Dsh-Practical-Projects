// GET only against the real API; synthetic values stay in a fresh browser context.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000',output=path.resolve(__dirname,'../../docs/verification/density-average-20261007');
(async()=> {
    const live=await (await fetch(url+'/api/v1/telemetry/latest?source_id=kepserver-main')).json();
    const average=live.derived_items.find(p=>p.key==='a303_a304_density_average');assert.ok(average);
    assert.equal(average.kind,'numeric_average');assert.equal(average.sample.components.length,2);
    assert.deepEqual(average.sample.components.map(c=>c.item_id),['TIA_4_1.TIA_4_1.A303/MD','TIA_4_1.TIA_4_1.A304/MD']);
    if(average.available) assert.equal(average.sample.value,average.sample.components[0].value/2+average.sample.components[1].value/2);
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context=await browser.newContext({viewport:{width:1440,height:1300}});
        await context.route('**/api/**',r=>['GET','HEAD','OPTIONS'].includes(r.request().method())?r.continue():r.abort());
        const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(url+'/#decision');await page.waitForFunction(()=>App.__ready && DecisionOverviewPage.snapshot);
        await page.evaluate(data=> {clearInterval(Operations.timer);clearInterval(DecisionOverviewPage.timer);Operations.telemetry=data;Operations.telemetryFailed=false;DecisionOverviewPage.telemetryUpdated();},live);
        const before=await page.evaluate(()=>JSON.stringify(App.store));
        const card=page.locator('[data-decision-node="density-current"]');await card.click();
        await page.locator('#decision-source-point').selectOption(JSON.stringify([average.source_id,average.key]));
        // Leave the selection control so its brief interaction hold does not suppress detail refresh.
        await card.focus();
        assert.ok((await page.locator('#decision-inspector').innerText()).includes('(A303/MD + A304/MD) ÷ 2'));
        assert.ok((await page.locator('#decision-inspector').innerText()).includes('A303 当前密度'));
        assert.ok((await page.locator('#decision-inspector').innerText()).includes('A304 当前密度'));
        assert.ok((await card.getAttribute('title')).includes('本系统派生'));
        const liveText=await card.locator('.decision-node-value').innerText();
        if(!average.available) assert.equal(liveText,'—');
        const simulated=structuredClone(live),item=simulated.derived_items.find(p=>p.key===average.key);
        item.available=true;item.status='good';item.sample.value=1.5;item.sample.quality='good';item.sample.issue=null;
        item.sample.components.forEach((c,i)=>{c.value=i?1.6:1.4;c.available=true;c.status='good';});
        await page.evaluate(data=>{Operations.telemetry=data;DecisionOverviewPage.telemetryUpdated();},simulated);
        assert.equal(await card.locator('.decision-node-value').innerText(),'1.5');
        assert.ok((await card.innerText()).includes('组合计算'));
        item.available=false;item.status='mixed_batches';item.sample.value=null;item.sample.issue='mixed_batches';
        await page.evaluate(data=>{Operations.telemetry=data;DecisionOverviewPage.telemetryUpdated();},simulated);
        assert.equal(await card.locator('.decision-node-value').innerText(),'—');
        assert.ok((await page.locator('#decision-inspector').innerText()).includes('成员接收批次不一致'));
        await page.evaluate(data=>{Operations.telemetry=data;DecisionOverviewPage.telemetryUpdated();},live);
        await page.locator('[data-decision-close]').click();
        await page.evaluate(()=>{document.querySelector('.main-content').scrollTop=0;});
        await page.screenshot({path:path.join(output,'average-card.png')});
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before));assert.deepEqual(errors,[]);
        const result={passed:true,physicalPoints:live.items.length,derivedPoints:live.derived_items.length,
            liveAvailable:average.available,liveValue:average.sample.value,liveText,unit:average.unit,
            formula:average.sample.formula,members:average.sample.components.map(c=>({label:c.label,value:c.value,available:c.available,item_id:c.item_id})),
            selectable:true,simulatedMeanShown:true,mixedBatchUnavailable:true,storeUnchanged:true,apiMutationsBlocked:true,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
