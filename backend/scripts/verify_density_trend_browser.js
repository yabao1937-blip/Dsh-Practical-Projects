// Dedicated density-trend page: full-page chart, dialog restored to chart-free.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'docs/verification/density-trend-20261026');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';

(async () => {
    const trend = await (await fetch(url+'/api/v1/decision/evidence/density-trend?limit=168')).json();
    assert.equal(trend.policy,'density-trend-v1');
    assert.equal(trend.items.length,168);
    const withSetpoint = trend.items.filter(i => i.setpoint.value !== null).length;
    assert.ok(withSetpoint > 0, 'expected setpoint hours from live data');

    const browser = await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context = await browser.newContext({viewport:{width:1500,height:1000}});
        await context.route('**/api/**',route => ['GET','HEAD','OPTIONS'].includes(route.request().method()) ? route.continue() : route.abort());
        const page = await context.newPage(), errors = [];
        page.on('pageerror',error => errors.push(error.message));
        await page.goto(url+'/');
        await page.waitForFunction(() => App.__ready);
        // Navigate to the dedicated page via the main nav.
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForSelector('#page-density-trend.active');
        await page.waitForFunction(() => (document.getElementById('density-trend-canvas').width || 0) > 300
            && !document.getElementById('density-trend-status').textContent);
        const painted = await page.evaluate(() => {
            const canvas = document.getElementById('density-trend-canvas');
            const data = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
            let opaque = 0;
            for (let i = 3; i < data.length; i += 40) if (data[i]) opaque++;
            return {width:canvas.width,height:canvas.height,opaque};
        });
        assert.ok(painted.width > 800 && painted.height >= 400, JSON.stringify(painted));
        assert.ok(painted.opaque > 400, 'full-page canvas looks blank: '+JSON.stringify(painted));
        const caption = await page.locator('#density-trend-caption').innerText();
        assert.ok(/近7天 · 168 小时/.test(caption), caption);
        assert.ok(/人工设定 \d+ 小时/.test(caption) && /建议密度 \d+ 小时/.test(caption), caption);
        const pageText = await page.locator('#page-density-trend').innerText();
        assert.ok(pageText.includes('人工调密 与 建议密度 趋势'));
        assert.ok(pageText.includes('缺数据小时留空不外推'));
        const actionRows = await page.locator('#density-trend-actions tr').count();
        assert.ok(actionRows >= 1, 'expected recent action rows');
        // Hover a point: tooltip shows both series and the delta.
        const hover = await page.evaluate(() => {
            const canvas = document.getElementById('density-trend-canvas');
            const hit = (canvas._hits || []).find(h => h.text.includes('人工设定 1.') || h.text.includes('人工设定 1.'));
            if (!hit) return null;
            canvas.dispatchEvent(new MouseEvent('mousemove',{clientX:canvas.getBoundingClientRect().left+hit.x,
                clientY:canvas.getBoundingClientRect().top+hit.y,bubbles:true}));
            const tip = canvas.parentElement.querySelector('.density-trend-tooltip');
            return {hidden:tip.hidden,text:tip.textContent};
        });
        assert.ok(hover && !hover.hidden, 'tooltip did not open');
        assert.ok(hover.text.includes('建议−设定'), hover.text);
        // 30-day window switch refetches 720 hourly points.
        await page.locator('[data-t-range="720"]').click();
        await page.waitForFunction(() => (document.getElementById('density-trend-canvas')._hits || []).length === 720);
        assert.match(await page.locator('#density-trend-caption').innerText(), /近30天 · 720 小时/);
        await page.screenshot({path:path.join(output,'density-trend-page.png'),fullPage:false});
        // The tracking dialog must be restored: no chart inside it.
        await page.locator('.nav-item[data-page="page-decision"]').click();
        await page.waitForSelector('#page-decision.active');
        await page.locator('[data-evidence-open]').click();
        await page.waitForSelector('#decision-evidence-dialog[open]');
        await page.locator('[data-e-tab="actions"]').click();
        await page.waitForTimeout(400);
        assert.equal(await page.locator('#decision-evidence-dialog #evidence-density-trend').count(),0,
            'the dialog must stay chart-free after the move');
        assert.ok((await page.locator('#evidence-content').innerText()).includes('自动记录 A303/A304 设定值变化'));
        assert.deepEqual(errors,[]);
        const result = {passed:true,hours:168,setpointHours:withSetpoint,canvas:painted,
            caption,actionRows,tooltipDelta:true,range720:true,dialogChartFree:true,
            apiMutationsBlocked:true,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result-page.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
