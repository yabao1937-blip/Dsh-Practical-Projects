// Fresh browser context; all API mutations are blocked before opening the live page.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'docs/verification/desliming-group-20261006');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
const identity = point => JSON.stringify([point.source_id,point.key]);

(async () => {
    const live = await (await fetch(url+'/api/v1/telemetry/latest?source_id=kepserver-main')).json();
    assert.equal(live.items.length,50);
    assert.ok(live.derived_items.length>=2);
    const group = live.derived_items.find(p => p.key === 'desliming_status');
    const four = live.derived_items.find(p => p.key === 'four_system_status');
    assert.ok(group.available && four.available);
    const browser = await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context = await browser.newContext({viewport:{width:1440,height:1100}});
        await context.route('**/api/**',route => ['GET','HEAD','OPTIONS'].includes(route.request().method()) ? route.continue() : route.abort());
        const page = await context.newPage(), errors = [];
        page.on('pageerror',error => errors.push(error.message));
        async function ready() {
            await page.waitForFunction(() => App.__ready && DecisionOverviewPage.snapshot);
            await page.evaluate(data => {
                clearInterval(Operations.timer); clearInterval(DecisionOverviewPage.timer);
                Operations.telemetry = data; Operations.telemetryFailed = false;
                DecisionOverviewPage.telemetryUpdated();
            },live);
        }
        await page.goto(url+'/#decision');
        await ready();
        let before = await page.evaluate(() => JSON.stringify(App.store));
        const card = page.locator('[data-decision-node="desliming"]');
        await card.click();
        await page.locator('#decision-source-point').selectOption(identity(group));
        assert.equal(await card.locator('.decision-state-grid > span').count(),2);
        const liveText = await card.locator('.decision-node-value').innerText();
        for (const component of group.sample.components) assert.ok(liveText.includes(component.label+' '+(component.value ? '开' : '关')));
        assert.equal(await card.getAttribute('title').then(t => t.includes('四路')),false);
        await page.keyboard.press('Escape');
        await page.screenshot({path:path.join(output,'desliming-card.png')});

        for (const component of group.sample.components) {
            await page.evaluate(({choice,data}) => {
                DecisionOverviewPage.pointChoices.desliming = choice;
                Operations.telemetry = data; DecisionOverviewPage.telemetryUpdated();
            },{choice:JSON.stringify([group.source_id,component.point_key]),data:live});
            assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices.desliming),identity(group));
        }
        const unrelated = live.items.find(p => !group.sample.components.some(c => c.point_key === p.key));
        await page.evaluate(choice => {
            DecisionOverviewPage.pointChoices.desliming = choice;
            DecisionOverviewPage.telemetryUpdated();
        },identity(unrelated));
        assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices.desliming),identity(unrelated));
        await page.evaluate(() => {
            delete DecisionOverviewPage.pointChoices.desliming;
            DecisionOverviewPage.telemetryUpdated();
        });
        assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices.desliming),undefined);
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),before);
        await card.click();
        await page.locator('#decision-source-point').selectOption(identity(group));
        await page.reload(); await ready();
        assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices.desliming),identity(group));
        before = await page.evaluate(() => JSON.stringify(App.store));

        async function inject(data) {
            await page.evaluate(data => {
                Operations.telemetry = data; Operations.telemetryFailed = false;
                DecisionOverviewPage.telemetryUpdated();
            },data);
        }
        const off = structuredClone(live);
        const offGroup = off.derived_items.find(p => p.key === 'desliming_status');
        offGroup.sample.components.forEach(c => { c.value = false; });
        offGroup.sample.value = {'473':false,'474':false};
        await inject(off);
        assert.match(await card.locator('.decision-node-value').innerText(),/473 关\s+474 关/);
        assert.equal(await card.evaluate(el => el.classList.contains('is-telemetry-unavailable')),false);
        const partial = structuredClone(off);
        const bad = partial.derived_items.find(p => p.key === 'desliming_status');
        bad.available = false; bad.status = 'member_unavailable';
        bad.sample.value = null; bad.sample.quality = 'bad'; bad.sample.issue = 'member_unavailable';
        bad.sample.components[1].available = false; bad.sample.components[1].quality = 'bad'; bad.sample.components[1].status = 'bad_quality';
        await inject(partial);
        assert.match(await card.locator('.decision-node-value').innerText(),/473 关\s+474 未知/);
        assert.equal(await card.evaluate(el => el.classList.contains('is-telemetry-unavailable')),true);
        await inject(live);
        await page.evaluate(choice => {
            DecisionOverviewPage.pointChoices['system-switches'] = choice;
            DecisionOverviewPage.telemetryUpdated();
        },identity(four));
        assert.equal(await page.locator('[data-decision-node="system-switches"] .decision-state-grid > span').count(),4);
        const geometry = [];
        for (const width of [1600,1440,1280,800]) {
            await page.setViewportSize({width,height:1100});
            await page.locator('#decision-fit').click();
            geometry.push(await card.evaluate(el => ({width:innerWidth,overflow:el.scrollWidth>el.clientWidth+2 || el.scrollHeight>el.clientHeight+2})));
        }
        assert.ok(geometry.every(g => !g.overflow));
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),before);
        assert.deepEqual(errors,[]);
        const result = {passed:true,physicalPoints:50,derivedPoints:live.derived_items.length,memberLabels:group.sample.components.map(c => c.label),liveText,
            migratedOld473:true,migratedOld474:true,unrelatedChoicePreserved:true,clearedChoicePreserved:true,reloadPreserved:true,
            allOffAvailable:true,partialFailureShowsUnknown:true,fourSystemCompatible:true,storeUnchanged:true,apiMutationsBlocked:true,geometry,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
