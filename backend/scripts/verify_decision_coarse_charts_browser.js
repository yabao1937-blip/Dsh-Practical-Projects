// Fresh browser, live GET state; every HTTP API mutation is blocked before navigation.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
const output=path.resolve(__dirname,'../../docs/verification/decision-gpt-charts-style-20261006');
(async()=> {
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        fs.mkdirSync(output,{recursive:true});
        const context=await browser.newContext({viewport:{width:1440,height:1300}});
        const blocked=[];
        await context.route('**/api/**',route=> {
            if(['GET','HEAD','OPTIONS'].includes(route.request().method())) return route.continue();
            blocked.push({method:route.request().method(),url:route.request().url()});return route.abort();
        });
        const page=await context.newPage(),errors=[];
        page.on('pageerror',e=>errors.push(e.message));
        await page.goto(url+'/#decision');
        await page.waitForFunction(()=>App.__ready && DecisionCoarseCharts.snapshot?.model && DecisionCoarseCharts.snapshot.rows.length);
        await page.evaluate(()=> {
            clearInterval(DecisionOverviewPage.timer);clearInterval(Operations.timer);
            App.saveStore=App.switchCoarseEngine=App.retrainCoarseModelAsync=App._trainCoarseRows=()=>{throw Error('Read-only chart test attempted mutation');};
        });
        const before=await page.evaluate(()=>JSON.stringify(App.store));
        // Abort of the app's initial auto-sync can create an unrelated warning toast in this isolated context.
        await page.evaluate(()=>{document.querySelector('.toast-container').textContent='';});
        const section=page.locator('#decision-coarse-analysis');
        const lightBackground=await section.evaluate(el=>getComputedStyle(el).backgroundColor);
        async function capture(name) {
            // Fit the entire section inside the app's scrolling main panel for an unclipped artifact.
            const height=await section.evaluate(el=>Math.ceil(el.getBoundingClientRect().height)+180);
            await page.setViewportSize({width:page.viewportSize().width,height:Math.max(1300,height)});
            await section.scrollIntoViewIfNeeded();
            await section.screenshot({path:path.join(output,name)});
        }
        await section.scrollIntoViewIfNeeded();
        const day=await page.evaluate(()=>({mode:DecisionCoarseCharts.mode,count:DecisionCoarseCharts.snapshot.count,groups:DecisionCoarseCharts.snapshot.rows.length,
            factors:DecisionCoarseCharts.snapshot.factors,model:DecisionCoarseCharts.snapshot.which,hits:Object.fromEntries(Object.entries(DecisionCoarseCharts.hits).map(([k,v])=>[k,v.length]))}));
        assert.equal(day.mode,'day');assert.ok(day.groups>0);assert.equal(day.hits.trend,day.groups);assert.equal(day.factors.length,10);
        await capture('gpt-day-light.png');
        await page.locator('[data-decision-period="shift"]').click();
        const shift=await page.evaluate(()=>({mode:DecisionCoarseCharts.mode,groups:DecisionCoarseCharts.snapshot.rows.length,factors:DecisionCoarseCharts.snapshot.factors,rows:DecisionCoarseCharts.snapshot.rows}));
        assert.equal(shift.mode,'shift');assert.ok(shift.groups>=day.groups);assert.deepEqual(shift.factors,day.factors);
        assert.equal(await page.locator('[data-decision-period="shift"]').getAttribute('aria-pressed'),'true');
        assert.ok(shift.rows.every(r=>r.label.endsWith('白班') || r.label.endsWith('夜班')));
        await capture('gpt-shift-light.png');
        await page.locator('.decision-analysis-data summary').click();
        assert.equal(await page.locator('#decision-coarse-table tr').count(),shift.groups);
        await page.locator('.decision-analysis-data summary').click();
        for(const id of ['trend','factors','level']) {
            const canvas=page.locator('#decision-coarse-'+id),hit=await page.evaluate(id=>DecisionCoarseCharts.hits[id][0],id);
            assert.ok(hit);
            await canvas.hover({position:{x:hit.x,y:hit.y}});
            await page.waitForFunction(id=>!document.getElementById('decision-coarse-'+id).parentElement.querySelector('.decision-chart-tooltip').hidden,id);
            assert.ok((await canvas.locator('..').locator('.decision-chart-tooltip').innerText()).length>0);
        }
        await page.locator('#decision-theme').click();await section.scrollIntoViewIfNeeded();
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.palette().blue),'#ffffff');
        assert.notEqual(await section.evaluate(el=>getComputedStyle(el).backgroundColor),lightBackground);
        await capture('gpt-shift-dark.png');
        const geometry=[];
        for(const width of [1600,1440,1024,801,800,390]) {
            await page.setViewportSize({width,height:1300});await section.scrollIntoViewIfNeeded();
            await page.waitForFunction(()=>['trend','factors','level'].every(id=> {
                const c=document.getElementById('decision-coarse-'+id),r=c.getBoundingClientRect();
                return Math.abs(c.width-r.width*Math.min(devicePixelRatio || 1,2))<2;
            }));
            geometry.push(await section.evaluate(el=>({width:innerWidth,overflow:el.scrollWidth>el.clientWidth+2,
                cards:[...el.querySelectorAll('.decision-chart-card')].map(c=> {
                    const copy=c.querySelector('.decision-chart-copy').getBoundingClientRect(),plot=c.querySelector('.decision-chart-plot').getBoundingClientRect();
                    return {width:c.clientWidth,plotWidth:plot.width,overflow:c.scrollWidth>c.clientWidth+2,
                        layout:plot.top>=copy.bottom?'stacked':plot.left<copy.left?'plot-left':'plot-right'};
                })})));
        }
        assert.ok(geometry.every(g=>!g.overflow && g.cards.every(c=>!c.overflow && c.width>200)));
        assert.ok(geometry.filter(g=>g.width>800).every(g=>g.cards.map(c=>c.layout).join(',')==='plot-right,plot-left,plot-right'));
        assert.ok(geometry.filter(g=>g.width<=800).every(g=>g.cards.every(c=>c.layout==='stacked')));
        await page.setViewportSize({width:390,height:2400});
        await section.scrollIntoViewIfNeeded();
        await capture('gpt-shift-mobile.png');
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before),'Period, hover, resize and theme must not mutate store');
        // Current production is changed only in this isolated browser memory to exercise GPT-bank selection.
        await page.evaluate(()=> {
            window.__chartsSaved=JSON.parse(JSON.stringify(App.store));
            const gpt=DecisionCoarseCharts.modelGroup(App.store);
            App.store.coarseModelVariants.gpt=JSON.parse(JSON.stringify(gpt));
            const ds=JSON.parse(JSON.stringify(gpt));ds.mlr.metrics.version=1;ds.mlr.intercept=999;
            App.store.coarseModel=ds;DecisionCoarseCharts.refresh();
        });
        assert.ok(await page.evaluate(()=>DecisionCoarseCharts.snapshot.model.intercept!==999));
        await page.evaluate(()=>{App.store.coarseCoal=[];DecisionCoarseCharts.refresh();});
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.hits.factors.length),10,'Saved factor chart remains with no history');
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.hits.trend.length),0);
        await page.evaluate(()=>{delete App.store.coarseModelVariants.gpt;DecisionCoarseCharts.refresh();});
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.snapshot.model),null);
        assert.ok((await page.locator('#decision-coarse-meta').innerText()).includes('尚未保存'));
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.hits.factors.length),0);
        await page.evaluate(()=>{App.store=window.__chartsSaved;DecisionCoarseCharts.refresh();});
        await page.locator('[data-decision-period="day"]').click();
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before),'Empty/model-state cases must restore store');
        // Exercise re-entry without invoking unrelated pages' existing stateful calculators.
        await page.evaluate(()=>{document.getElementById('page-decision').classList.remove('active');});
        await page.evaluate(()=>{document.getElementById('page-decision').classList.add('active');DecisionOverviewPage.init();});
        assert.equal(await page.evaluate(()=>DecisionCoarseCharts.mode),'day');
        assert.ok(await page.evaluate(before=>JSON.stringify(App.store)===before,before),'Decision re-entry must not mutate store');
        assert.deepEqual(errors,[]);
        const result={passed:true,day:{...day,factors:day.factors.length},shift:{groups:shift.groups},geometry,
            tooltips:true,themes:true,unifiedBlueSection:true,alternatingDesktopLayout:true,stackedNarrowLayout:true,
            gptWhileDsSelected:true,emptyHistoryKeepsFactors:true,noDsFallback:true,storeUnchanged:true,apiMutationsBlocked:true,blockedRequests:blocked,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');
        console.log(JSON.stringify(result));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
