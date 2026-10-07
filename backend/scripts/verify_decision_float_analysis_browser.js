// Fresh Chrome contexts; all API mutations are blocked before navigation.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000',output=path.resolve(__dirname,'../../docs/verification/decision-float-white-20261007');
(async()=> {
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context=await browser.newContext({viewport:{width:1440,height:1800}});
        await context.route('**/api/**',r=>['GET','HEAD','OPTIONS'].includes(r.request().method())?r.continue():r.abort());
        const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(url+'/#decision');await page.waitForFunction(()=>App.__ready && DecisionFloatAnalysis.snapshot?.rows.length);
        await page.evaluate(()=>{clearInterval(Operations.timer);clearInterval(DecisionOverviewPage.timer);document.querySelector('.toast-container').textContent='';});
        const before=await page.evaluate(()=>JSON.stringify(App.store));
        const section=page.locator('#decision-float-analysis');await section.scrollIntoViewIfNeeded();
        assert.equal(await section.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
        const coarseColors=await page.locator('#decision-coarse-analysis').evaluate(el=>({background:getComputedStyle(el).backgroundColor,ink:getComputedStyle(el).getPropertyValue('--ds-ink').trim()}));
        assert.equal(coarseColors.background,'rgb(28, 10, 235)');assert.equal(coarseColors.ink,'#ffffff');
        async function capture(name) {
            const height=await section.evaluate(el=>Math.ceil(el.getBoundingClientRect().height)+180);
            await page.setViewportSize({width:page.viewportSize().width,height});await section.scrollIntoViewIfNeeded();
            await page.locator('#decision-float-meta').hover();await section.screenshot({path:path.join(output,name)});
        }
        const count=await page.evaluate(()=>DecisionFloatAnalysis.snapshot.rows.length);
        assert.equal(await section.locator('canvas').count(),2);assert.equal(await section.locator('.decision-float-stats strong').count(),4);
        assert.equal(await page.locator('#decision-float-table tr').count(),count);
        const parity=await page.evaluate(()=>DecisionFloatAnalysis.snapshot.rows.every(r=> {
            const record=App.store.floatCoal.find(d=>d.timestamp===r.timestamp);return r.influence===App.influenceOfFloat(record);
        }));assert.ok(parity);
        await capture('float-light.png');
        await page.evaluate(()=> {
            window.__floatBuild=DecisionFloatAnalysis.build;DecisionFloatAnalysis.build=()=>{throw Error('Hover must not recalculate float history');};
        });
        for(const id of ['linkage','impact']) {
            const canvas=page.locator('#decision-float-'+id),hit=await page.evaluate(id=>DecisionFloatAnalysis.hits[id][0],id);
            assert.ok(hit);await canvas.hover({position:{x:hit.x,y:hit.y}});
            const card=canvas.locator('..').locator('..');assert.equal(await card.evaluate(el=>el.classList.contains('is-engaged')),true);
            assert.equal(await card.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(28, 10, 235)');
            assert.equal(await canvas.evaluate(c=>DecisionFloatAnalysis.palette(c.closest('.decision-chart-card')).blue),'#ffffff');
            assert.equal(await canvas.locator('..').locator('.decision-chart-tooltip').isVisible(),true);
            assert.ok((await canvas.locator('..').locator('.decision-chart-tooltip').innerText()).includes('参考'));
            await page.locator('#decision-float-meta').hover();assert.equal(await card.evaluate(el=>el.classList.contains('is-engaged')),false);
            assert.equal(await card.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
        }
        const hit=await page.evaluate(()=>DecisionFloatAnalysis.hits.linkage[0]);
        await page.locator('#decision-float-linkage').hover({position:{x:hit.x,y:hit.y}});
        await section.screenshot({path:path.join(output,'float-hover-blue.png')});
        await page.locator('#decision-float-meta').hover();
        await page.locator('#decision-float-open').focus();await page.keyboard.press('Tab');
        assert.equal(await page.evaluate(()=>document.activeElement.id),'decision-float-linkage');
        await page.keyboard.press('End');assert.ok((await page.locator('#decision-float-linkage-tooltip').innerText()).includes(await page.evaluate(()=>DecisionFloatAnalysis.snapshot.latest.timestamp)));
        await page.keyboard.press('Escape');assert.equal(await page.locator('#decision-float-linkage-tooltip').isVisible(),false);
        await page.locator('#decision-float-open').focus();await page.evaluate(()=>{DecisionFloatAnalysis.build=window.__floatBuild;});
        await page.locator('#decision-theme').click();await capture('float-dark.png');
        assert.equal(await section.evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)','Float remains white in the dark page theme');
        const geometry=[];
        for(const width of [1600,1440,1024,800,390]) {
            await page.setViewportSize({width,height:1600});await section.scrollIntoViewIfNeeded();
            await page.waitForFunction(()=>DecisionFloatAnalysis.chartIds.every(id=> {
                const c=DecisionFloatAnalysis.canvas(id);return Math.abs(c.width-c.getBoundingClientRect().width*Math.min(devicePixelRatio || 1,2))<2;
            }));
            geometry.push(await section.evaluate(el=>({width:innerWidth,overflow:el.scrollWidth>el.clientWidth+2,
                cards:[...el.querySelectorAll('.decision-chart-card')].map(c=>({overflow:c.scrollWidth>c.clientWidth+2,plot:c.querySelector('canvas').clientWidth}))})));
        }
        assert.ok(geometry.every(g=>!g.overflow && g.cards.every(c=>!c.overflow && c.plot>200)));await capture('float-mobile.png');
        await page.evaluate(()=> {
            window.__floatStore=JSON.parse(JSON.stringify(App.store));
            App.store.floatCoal.find(r=>r.timestamp===DecisionFloatAnalysis.snapshot.rows[0].timestamp).annotation='<img onerror=bad>';DecisionFloatAnalysis.refresh();
        });
        assert.equal(await section.locator('img').count(),0);assert.ok((await page.locator('#decision-float-table').innerHTML()).includes('&lt;img'));
        await page.evaluate(()=>{App.store.floatCoal=[];DecisionFloatAnalysis.refresh();});
        assert.equal(await page.locator('#decision-float-stat-ash').innerText(),'— %');assert.equal(await page.evaluate(()=>DecisionFloatAnalysis.hits.linkage.length),0);
        await page.evaluate(()=>{App.store=window.__floatStore;DecisionFloatAnalysis.refresh();});
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before));
        await page.locator('[data-decision-period="sample"]').click();await section.scrollIntoViewIfNeeded();
        assert.equal(await page.evaluate(()=>DecisionFloatAnalysis.snapshot.rows.length),count,'Coarse period switch must not filter float data');
        assert.equal(await section.locator('[data-decision-period]').count(),0);
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before));assert.deepEqual(errors,[]);
        const result={passed:true,records:count,charts:2,metrics:4,originalFormulaParity:true,hoverKeyboard:true,themes:true,emptyState:true,
            defaultWhite:true,hoverBlue:true,whiteInBothThemes:true,coarsePalettePreserved:true,
            coarseSwitchIsolated:true,annotationsEscaped:true,storeUnchanged:true,apiMutationsBlocked:true,geometry,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
