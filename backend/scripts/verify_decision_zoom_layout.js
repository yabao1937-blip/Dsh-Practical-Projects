// Equivalent browser-zoom viewport/DPR pairs, not a CSS transform applied to the app.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000',output=path.resolve(__dirname,'../../docs/verification/decision-zoom-layout-20261007');
(async()=> {
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});const layouts=[],errors=[];
        for(const zoom of [1,.5,.25]) {
            // A 2560x1600 display at base DPR 1.25; zoom scales DPR and CSS viewport inversely.
            const dpr=1.25*zoom,width=Math.round(2560/dpr),height=Math.round(1600/dpr);
            const context=await browser.newContext({viewport:{width,height},deviceScaleFactor:dpr});
            await context.route('**/api/**',r=>['GET','HEAD','OPTIONS'].includes(r.request().method())?r.continue():r.abort());
            const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
            await page.goto(url+'/#decision');await page.waitForFunction(()=>App.__ready && DecisionCoarseCharts.snapshot && DecisionFloatAnalysis.snapshot);
            await page.evaluate(()=> {clearInterval(DecisionOverviewPage.timer);clearInterval(Operations.timer);document.querySelector('.toast-container').textContent='';});
            const before=await page.evaluate(()=>JSON.stringify(App.store));
            const layout=await page.evaluate(()=> {
                const p=document.getElementById('page-decision'),r=p.getBoundingClientRect(),main=document.querySelector('.main-content'),nav=document.querySelector('.top-bar');
                return {cssWidth:innerWidth,dpr:devicePixelRatio,pageWidth:r.width,left:r.left,right:innerWidth-r.right,navWidth:nav.getBoundingClientRect().width,
                    plots:[...p.querySelectorAll('.decision-chart-plot')].map(el=>({width:el.clientWidth,height:el.clientHeight})),
                    stageHeight:document.querySelector('.decision-underlay').clientHeight,
                    mainOverflow:main.scrollWidth>main.clientWidth+1,pageOverflow:p.scrollWidth>p.clientWidth+2,
                    background:getComputedStyle(main).backgroundColor,transform:getComputedStyle(p).transform};
            });
            assert.equal(layout.pageWidth,1760);assert.equal(layout.navWidth,1760);assert.ok(Math.abs(layout.left-layout.right)<=8);
            assert.ok(!layout.mainOverflow && !layout.pageOverflow);assert.equal(layout.transform,'none');assert.ok(layout.stageHeight<=900);
            assert.equal(layout.background,'rgb(28, 10, 235)');
            if(layouts.length) assert.deepEqual(layout.plots,layouts[0].plots,'Plots must stop widening after the page reaches its width cap');
            const panels=[];
            for(const id of ['float-amount','raw-ash','density']) {
                await page.locator(`[data-decision-node="${id}"]`).click();
                const bounds=await page.locator('#decision-inspector').evaluate(el=> {
                    const r=el.getBoundingClientRect(),p=document.getElementById('page-decision').getBoundingClientRect();
                    return {placement:el.dataset.placement,left:r.left,right:r.right,pageLeft:p.left,pageRight:p.right};
                });
                assert.ok(bounds.left>=bounds.pageLeft && bounds.right<=bounds.pageRight,'Inspector must remain beside the centered content');
                panels.push({node:id,...bounds});await page.locator('[data-decision-close]').click();
            }
            await page.evaluate(()=>{document.querySelector('.main-content').scrollTop=0;});
            await page.screenshot({path:path.join(output,'zoom-'+Math.round(zoom*100)+'.png')});
            assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before));
            layouts.push({zoom,...layout,panels});await context.close();
        }
        const context=await browser.newContext({viewport:{width:390,height:1000}});
        await context.route('**/api/**',r=>['GET','HEAD','OPTIONS'].includes(r.request().method())?r.continue():r.abort());
        const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
        await page.goto(url+'/#decision');await page.waitForFunction(()=>App.__ready && DecisionFloatAnalysis.snapshot);
        const narrow=await page.evaluate(()=> {
            const p=document.getElementById('page-decision'),m=document.querySelector('.main-content');
            return {pageWidth:p.clientWidth,overflow:m.scrollWidth>m.clientWidth+1};
        });assert.ok(narrow.pageWidth<390 && !narrow.overflow);
        await page.setViewportSize({width:1920,height:1200});await page.locator('#decision-theme').click();
        assert.equal(await page.locator('.main-content').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(21, 19, 148)');
        await page.evaluate(()=> {document.getElementById('page-decision').classList.remove('active');document.getElementById('page-import').classList.add('active');});
        assert.equal(await page.locator('.top-bar').evaluate(el=>getComputedStyle(el).maxWidth),'none','Other pages retain their existing layout');
        await context.close();assert.deepEqual(errors,[]);
        const result={passed:true,method:'Equivalent CSS viewport and DPR for browser zoom; no app page scaling',maxPageWidth:1760,
            layouts,narrow,blueBackdrop:true,inspectorInsidePage:true,chartsKeepRatio:true,otherPagesUnaffected:true,storeUnchanged:true,apiMutationsBlocked:true,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
