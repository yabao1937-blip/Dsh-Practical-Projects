// Isolated browser acceptance. Uses file://, a fresh browser context, and no server DB.
// Resolve Playwright from an installed or bundled module via DMCS_PLAYWRIGHT_MODULE.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = path.join(root,'docs/verification/decision-overview-20261005');

(async () => {
    fs.mkdirSync(output,{recursive:true});
    const browser = await chromium.launch({channel:'chrome',headless:true});
    try {
        const context = await browser.newContext({viewport:{width:1600,height:1100}});
        const page = await context.newPage(), errors = [], mutations = [];
        page.on('pageerror',error => errors.push(error.message));
        page.on('request',request => { if (!['GET','HEAD'].includes(request.method())) mutations.push(request.url()); });
        await page.goto(pathToFileURL(path.join(root,'frontend/index.html')).href + '#decision');
        await page.waitForFunction(() => typeof App !== 'undefined' && App.__ready);
        await page.waitForFunction(() => typeof DecisionOverviewPage !== 'undefined' && DecisionOverviewPage.snapshot);
        assert.equal(await page.locator('#page-decision.active').count(),1,'Hash entry opens new overview');
        const navigation = await page.locator('.main-nav').innerText();
        assert.ok(navigation.includes('决策总览') && navigation.includes('密度推荐总览'));
        await page.locator('[data-page="page-decision"]').click();
        await page.waitForFunction(() => typeof DecisionOverviewPage !== 'undefined' && DecisionOverviewPage.snapshot);
        assert.equal(await page.locator('.decision-node').count(),19);
        const storeBefore = await page.evaluate(() => JSON.stringify(App.store));
        await page.locator('[data-decision-node="raw-ash"]').click();
        assert.match(await page.locator('#decision-inspector').innerText(),/原煤灰分/);
        assert.equal(await page.locator('#decision-inspector.is-open').count(),1);
        await page.keyboard.press('Escape');
        assert.equal(await page.locator('#decision-inspector.is-open').count(),0);
        await page.locator('[data-decision-model="production"]').click();
        await page.locator('[data-decision-model="gpt"]').click();
        await page.locator('#decision-refresh').click();
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),storeBefore);
        assert.equal(await page.locator('.decision-workspace.is-light').count(),1,'Paper theme is the default');
        await page.locator('#decision-theme').click();
        assert.equal(await page.locator('.decision-workspace.is-light').count(),0);
        await page.locator('#decision-theme').click();
        await page.locator('#decision-zoom-in').click();
        await page.locator('#decision-fit').click();
        await page.locator('[data-decision-node="density"]').click();
        await page.locator('[data-decision-close]').click();
        await page.setViewportSize({width:1600,height:1850});
        await page.locator('#decision-fit').click();
        await page.evaluate(() => { document.querySelector('.main-content').scrollTop = 0; });
        await page.locator('#decision-theme').click();
        await page.waitForTimeout(250); // Let the existing control/theme transitions settle for the artifact.
        await page.screenshot({path:path.join(output,'decision-overview-dark.png')});
        await page.locator('#decision-theme').click();
        await page.waitForTimeout(250);
        await page.screenshot({path:path.join(output,'decision-overview-light.png')});

        const layout = [];
        for (const width of [1600,1440,1280,800]) {
            await page.setViewportSize({width,height:1100});
            await page.locator('#decision-fit').click();
            layout.push(await page.evaluate(() => {
                const nodes = [...document.querySelectorAll('.decision-node')];
                const overflow = nodes.filter(n => n.scrollHeight > n.clientHeight+2 || n.scrollWidth > n.clientWidth+2).map(n => n.dataset.decisionNode);
                const overlap = [];
                nodes.forEach((a,i) => nodes.slice(i+1).forEach(b => {
                    const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
                    if (Math.min(x.right,y.right)-Math.max(x.left,y.left)>1 && Math.min(x.bottom,y.bottom)-Math.max(x.top,y.top)>1) overlap.push([a.dataset.decisionNode,b.dataset.decisionNode]);
                }));
                const canvas = document.getElementById('decision-canvas-scroll');
                return {width:innerWidth,documentOverflow:document.documentElement.scrollWidth>innerWidth,canvasClientWidth:canvas.clientWidth,canvasScrollWidth:canvas.scrollWidth,overflow,overlap};
            }));
        }
        for (const result of layout) {
            assert.equal(result.documentOverflow,false,JSON.stringify(result));
            assert.deepEqual(result.overflow,[],JSON.stringify(result));
            assert.deepEqual(result.overlap,[],JSON.stringify(result));
            if (result.width >= 1280) assert.ok(result.canvasScrollWidth <= result.canvasClientWidth+2,'Fit must not retain unscaled scroll overflow');
        }
        await page.setViewportSize({width:1440,height:1100});
        await page.evaluate(() => {
            window.Operations.telemetry = {sources:[{name:'KEP',state:'connected'}],items:[{source_id:'kep',key:'raw_ash',name:'原煤灰分参考点',unit:'%',available:true,status:'good',sample:{value:0,quality:'good',source_at:'2026-10-05T01:00:00Z',server_at:'2026-10-05T01:00:01Z',received_at:'2026-10-05T01:00:02Z'}}]};
            window.Operations.telemetryFailed = false;
            DecisionOverviewPage.refresh();
        });
        await page.locator('[data-decision-node="raw-ash"]').click();
        await page.locator('#decision-source-point').selectOption({label:'原煤灰分参考点 · kep/raw_ash'});
        assert.match(await page.locator('#decision-inspector').innerText(),/0 %/);
        assert.match(await page.locator('#decision-inspector').innerText(),/尚未参与本页计算/);
        assert.equal(await page.evaluate(() => JSON.stringify(App.store)),storeBefore,'Good telemetry never enters production store');
        await page.screenshot({path:path.join(output,'decision-overview-source.png')});
        await page.evaluate(() => { Operations.telemetryFailed = true; DecisionOverviewPage.renderInspector(); });
        assert.match(await page.locator('#decision-inspector').innerText(),/0 %（不可用）/);
        await page.keyboard.press('Escape');
        await page.locator('[data-page="page-overview"]').click();
        assert.equal(await page.locator('#page-overview.active').count(),1);
        await page.locator('[data-page="page-decision"]').click();
        await page.waitForFunction(() => document.querySelector('#page-decision.active'));
        const revealLayout = [];
        for (const viewport of [{width:1440,height:900},{width:390,height:1000},{width:1600,height:1850}]) {
            await page.setViewportSize(viewport);
            await page.evaluate(() => { DecisionOverviewPage.fit(); });
            await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
            await page.evaluate(() => { document.querySelector('.main-content').scrollTop = 1e6; });
            await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
            revealLayout.push(await page.evaluate(() => {
                const title = document.querySelector('.decision-underlay h3').getBoundingClientRect();
                const hit = document.elementFromPoint(title.left+10,title.top+10);
                return {width:innerWidth,height:innerHeight,titleTop:title.top,titleBottom:title.bottom,visible:!!hit?.closest('.decision-underlay'),documentOverflow:document.documentElement.scrollWidth>innerWidth};
            }));
        }
        for (const result of revealLayout) {
            assert.ok(result.visible && result.titleTop>=56 && result.titleBottom<=result.height,JSON.stringify(result));
            assert.equal(result.documentOverflow,false,JSON.stringify(result));
        }
        assert.deepEqual(errors,[]);
        assert.deepEqual(mutations,[]);
        fs.writeFileSync(path.join(output,'browser-result.json'),JSON.stringify({passed:true,isolatedFileMode:true,layout,revealLayout,pageErrors:errors,mutatingRequests:mutations,checks:['new and old navigation','node inspection','view isolation','default paper and dark/light themes','zoom and fit','quality/zero/failed telemetry','page switching','footer title visible at 3 viewport sizes']},null,2)+'\n');
        console.log('Decision overview browser acceptance passed: 4 widths, themes, sources, isolation, navigation and footer reveal.');
    } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
