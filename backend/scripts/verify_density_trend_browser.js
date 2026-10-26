// Density trend chart (manual setpoint vs suggested) rendered in the tracking dialog.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'docs/verification/density-trend-20261026');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8767';

(async () => {
    const trend = await (await fetch(url+'/api/v1/decision/evidence/density-trend?limit=168')).json();
    assert.equal(trend.policy,'density-trend-v1');
    assert.equal(trend.items.length,168);
    const withSetpoint = trend.items.filter(i => i.setpoint.value !== null);
    const withSuggested = trend.items.filter(i => i.suggested.value !== null);
    const withActions = trend.items.filter(i => i.actions.length);
    assert.ok(withSetpoint.length >= 40, 'expected seeded setpoint hours, got '+withSetpoint.length);
    assert.ok(withSuggested.length >= 40, 'expected seeded advice hours, got '+withSuggested.length);
    assert.ok(withActions.length >= 2, 'expected action hours, got '+withActions.length);
    assert.ok(withActions.every(i => i.setpoint.origin === 'action' && i.actions.at(-1).after === i.setpoint.value));
    assert.ok(trend.items.some(i => i.setpoint.origin === 'anchor'));
    assert.ok(trend.items.some(i => i.setpoint.value === null && i.suggested.value === null));

    const browser = await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context = await browser.newContext({viewport:{width:1440,height:1000}});
        await context.route('**/api/**',route => ['GET','HEAD','OPTIONS'].includes(route.request().method()) ? route.continue() : route.abort());
        const page = await context.newPage(), errors = [];
        page.on('pageerror',error => errors.push(error.message));
        await page.goto(url+'/#decision');
        await page.waitForFunction(() => App.__ready && DecisionOverviewPage.snapshot);
        const storeBefore = await page.evaluate(() => JSON.stringify(App.store));
        await page.locator('[data-evidence-open]').click();
        await page.waitForSelector('#decision-evidence-dialog[open]');
        await page.locator('[data-e-tab="actions"]').click();
        await page.waitForSelector('#evidence-density-trend');
        await page.waitForFunction(() => (document.getElementById('evidence-density-trend').width || 0) > 100);
        const painted = await page.evaluate(() => {
            const canvas = document.getElementById('evidence-density-trend');
            const data = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
            let opaque = 0;
            for (let i = 3; i < data.length; i += 40) if (data[i]) opaque++;
            return {width:canvas.width,height:canvas.height,opaque};
        });
        assert.ok(painted.width > 400 && painted.height >= 230, JSON.stringify(painted));
        assert.ok(painted.opaque > 200, 'canvas looks blank: '+JSON.stringify(painted));
        const cardText = await page.locator('.evidence-trend-card').innerText();
        assert.ok(cardText.includes('人工调密 与 建议密度 趋势'));
        assert.ok(cardText.includes('MD_SET A304'));
        assert.ok(cardText.includes('建议密度（小时均值）'));
        const actionRows = await page.locator('#evidence-content table tbody tr').count();
        assert.ok(actionRows >= 2, 'expected action table rows, got '+actionRows);
        // Hover the first data point: the tooltip must show both series values.
        const hover = await page.evaluate(() => {
            const canvas = document.getElementById('evidence-density-trend');
            const hit = canvas._hits.find(h => h.text.includes('人工设定') && !h.text.includes('人工设定 —'));
            if (!hit) return null;
            canvas.dispatchEvent(new MouseEvent('mousemove',{clientX:canvas.getBoundingClientRect().left+hit.x,
                clientY:canvas.getBoundingClientRect().top+hit.y,bubbles:true}));
            const tip = canvas.parentElement.querySelector('.evidence-trend-tooltip');
            return {hidden:tip.hidden,text:tip.textContent};
        });
        assert.ok(hover && !hover.hidden, 'tooltip did not open');
        assert.ok(hover.text.includes('人工设定 1.') && hover.text.includes('建议密度 1.'), hover.text);
        assert.ok(hover.text.includes('建议−设定'), hover.text);
        // Range switch to the 30-day window refetches 720 hourly points.
        await page.locator('[data-e-range="720"]').click();
        await page.waitForFunction(() => document.getElementById('evidence-density-trend')?._hits?.length === 720);
        assert.equal(await page.evaluate(() => DecisionEvidence.trendHours),720);
        await page.screenshot({path:path.join(output,'density-trend.png')});
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),storeBefore);
        assert.deepEqual(errors,[]);
        const result = {passed:true,hours:trend.items.length,setpointHours:withSetpoint.length,
            suggestedHours:withSuggested.length,actionHours:withActions.length,channel:trend.channel,
            canvas:painted,tooltipSeries:true,rangeSwitch:true,storeUnchanged:true,apiMutationsBlocked:true,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
