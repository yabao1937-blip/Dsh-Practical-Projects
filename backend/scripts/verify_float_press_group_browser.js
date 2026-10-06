// Live GET snapshots in a fresh context; all API writes are blocked.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'docs/verification/float-press-group-20261006');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
const identity = point => JSON.stringify([point.source_id,point.key]);

(async () => {
    const live = await (await fetch(url+'/api/v1/telemetry/latest?source_id=kepserver-main')).json();
    assert.equal(live.items.length,50);
    const groups = new Map(live.derived_items.map(p => [p.key,p]));
    assert.deepEqual([...groups.keys()].sort(),['desliming_status','float_press_status','four_system_status']);
    const group = groups.get('float_press_status');
    assert.equal(group.member_count,4); assert.ok(group.available);
    const browser = await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context = await browser.newContext({viewport:{width:1440,height:1400}});
        await context.route('**/api/**',route => ['GET','HEAD','OPTIONS'].includes(route.request().method()) ? route.continue() : route.abort());
        const page = await context.newPage(), errors = [];
        page.on('pageerror',error => errors.push(error.message));
        async function inject(data=live) {
            await page.evaluate(data => {
                clearInterval(Operations.timer); clearInterval(DecisionOverviewPage.timer);
                Operations.telemetry = data; Operations.telemetryFailed = false;
                DecisionOverviewPage.telemetryUpdated();
            },data);
        }
        async function ready() {
            await page.waitForFunction(() => App.__ready && DecisionOverviewPage.snapshot);
            await inject();
        }
        await page.goto(url+'/#decision'); await ready();
        let before = await page.evaluate(() => JSON.stringify(App.store));
        for (const [nodeId,key] of [['system-switches','four_system_status'],['desliming','desliming_status'],['float-press','float_press_status']]) {
            await page.locator(`[data-decision-node="${nodeId}"]`).click();
            await page.locator('#decision-source-point').selectOption(identity(groups.get(key)));
        }
        const card = page.locator('[data-decision-node="float-press"]');
        const liveText = await card.locator('.decision-node-value').innerText();
        assert.equal(await card.locator('.decision-state-grid > span').count(),4);
        for (const c of group.sample.components) assert.ok(liveText.includes(c.label+' '+(c.value?'开':'关')));
        assert.ok((await page.locator('#decision-inspector').innerText()).includes('A415 压滤机'));
        await page.locator('[data-decision-close]').click();
        await page.evaluate(() => {
            document.querySelector('.main-content').scrollTop=0;
            const canvas=document.getElementById('decision-canvas-scroll');
            canvas.scrollTop=0; canvas.scrollLeft=0;
        });
        await page.screenshot({path:path.join(output,'float-press-card.png')});
        const migrated = [];
        for (const c of group.sample.components) {
            await page.evaluate(choice => {
                DecisionOverviewPage.pointChoices['float-press'] = choice;
                DecisionOverviewPage.telemetryUpdated();
            },JSON.stringify([group.source_id,c.point_key]));
            assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices['float-press']),identity(group));
            migrated.push(c.label);
        }
        const plates = live.items.filter(p => /^A41[5-8]\/PLATE_CNT$/.test(p.tag_name));
        assert.equal(plates.length,4);
        for (const p of plates) {
            await page.evaluate(choice => {
                DecisionOverviewPage.pointChoices['float-press'] = choice;
                DecisionOverviewPage.telemetryUpdated();
            },identity(p));
            assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices['float-press']),identity(p));
        }
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),before);
        await card.click(); await page.locator('#decision-source-point').selectOption(identity(group));
        await page.reload(); await ready();
        assert.equal(await page.evaluate(() => DecisionOverviewPage.pointChoices['float-press']),identity(group));
        before = await page.evaluate(() => JSON.stringify(App.store));
        const off = structuredClone(live), offGroup = off.derived_items.find(p => p.key==='float_press_status');
        offGroup.sample.components.forEach(c => { c.value=false; });
        offGroup.sample.value = Object.fromEntries(offGroup.sample.components.map(c => [c.label,false]));
        await inject(off);
        for (const c of offGroup.sample.components) assert.ok((await card.innerText()).includes(c.label+' 关'));
        assert.equal(await card.evaluate(el => el.classList.contains('is-telemetry-unavailable')),false);
        const partial=structuredClone(off), bad=partial.derived_items.find(p => p.key==='float_press_status');
        bad.available=false; bad.status='member_unavailable'; bad.sample.value=null;
        bad.sample.quality='bad'; bad.sample.issue='member_unavailable';
        bad.sample.components[2].available=false; bad.sample.components[2].quality='bad'; bad.sample.components[2].status='bad_quality';
        await inject(partial);
        assert.ok((await card.innerText()).includes('A417 未知'));
        assert.ok((await card.innerText()).includes('A415 关'));
        assert.equal(await card.evaluate(el => el.classList.contains('is-telemetry-unavailable')),true);
        await inject();
        assert.equal(await page.locator('[data-decision-node="system-switches"] .decision-state-grid > span').count(),4);
        assert.equal(await page.locator('[data-decision-node="desliming"] .decision-state-grid > span').count(),2);
        const geometry=[];
        for (const width of [1600,1440,1280,800]) {
            await page.setViewportSize({width,height:1400}); await page.locator('#decision-fit').click();
            geometry.push(await card.evaluate(el => {
                const rect=el.getBoundingClientRect(), next=document.querySelector('[data-decision-node="float-unmodeled"]').getBoundingClientRect();
                return {width:innerWidth,overflow:el.scrollWidth>el.clientWidth+2 || el.scrollHeight>el.clientHeight+2,overlap:rect.bottom>next.top+1};
            }));
        }
        assert.ok(geometry.every(g => !g.overflow && !g.overlap));
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),before);
        assert.deepEqual(errors,[]);
        const result={passed:true,physicalPoints:50,derivedPoints:3,liveText,migratedMachineChoices:migrated,plateCountChoicesPreserved:true,
            reloadPreserved:true,allOffAvailable:true,partialFailureShowsUnknown:true,existingGroupsCompatible:true,storeUnchanged:true,apiMutationsBlocked:true,geometry,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
