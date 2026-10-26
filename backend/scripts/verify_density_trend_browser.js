// Live read-only UI verification; synthetic checks are explicitly labeled.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..'), output = path.join(root,'docs/verification/density-trend-20261026');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    const started=Date.now(), response=await fetch(url+'/api/v1/decision/evidence/density-trend?limit=168');
    assert.equal(response.status,200);const data=await response.json();
    assert.equal(data.policy,'density-trend-v2');
    assert.equal(Date.parse(data.period.end)-Date.parse(data.period.start),7*86400000);
    const counts=Object.fromEntries(Object.entries(data.channels).map(([k,v])=>[k,v.length]));
    assert.ok(Object.values(counts).some(n=>n>0));const apiMs=Date.now()-started;
    const browser=await chromium.launch({channel:'chrome',headless:true});
    try {
        const context=await browser.newContext({viewport:{width:1500,height:1000}});let mutationAttempts=0;
        await context.route('**/api/**',r=>{
            if(!['GET','HEAD','OPTIONS'].includes(r.request().method())){mutationAttempts++;return r.abort();}return r.continue();
        });
        const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
        await page.goto(url);await page.waitForFunction(()=>App.__ready,{},{timeout:60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(()=>DensityTrendPage.meta?.policy==='density-trend-v2');
        await page.waitForFunction(()=>document.getElementById('density-trend-canvas')._hits.length>0);
        const markers=await page.evaluate(()=>{
            const canvas=document.getElementById('density-trend-canvas'),ctx=canvas.getContext('2d'),original=ctx.arc;
            let drawn=0;ctx.arc=function(...args){drawn++;return original.apply(this,args);};
            try{DensityTrendPage.draw();}finally{ctx.arc=original;}
            return {drawn,changes:DensityTrendPage.series.A304.filter(p=>p.origin==='change'&&p.value!==null).length,
                observations:DensityTrendPage.series.A304.length,
                suggested:DensityTrendPage.series.advice.filter((p,i,all)=>DensityTrendPage.showPoint(all[i-1],p)).length};
        });
        assert.equal(markers.drawn,markers.changes+markers.suggested,'only density changes have markers');
        assert.ok(markers.changes<markers.observations,'repeated observations retain lines without dots');
        await page.locator('#density-trend-canvas').focus();await page.keyboard.press('End');
        assert.equal(await page.evaluate(()=>{const c=document.getElementById('density-trend-canvas');return c._index===c._hits.length-1;}),true);
        await page.keyboard.press('Home');assert.equal(await page.evaluate(()=>document.getElementById('density-trend-canvas')._index),0);
        assert.equal(await page.locator('.density-trend-tooltip').isVisible(),true);await page.keyboard.press('Escape');
        await page.locator('#density-trend-title').click(); // Remove the keyboard focus ring from the preview.
        await page.waitForTimeout(250); // Let the existing CSS theme transition settle.
        await page.screenshot({path:path.join(output,'live-7-days.png')});
        await page.locator('[data-t-range="720"]').click();
        await page.waitForFunction(()=>DensityTrendPage.meta && Date.parse(DensityTrendPage.meta.period.end)-Date.parse(DensityTrendPage.meta.period.start)===30*86400000);
        const monthCount=await page.evaluate(()=>DensityTrendPage.meta.total_actions);
        await page.locator('#density-trend-custom').click();
        const local=offset=>new Date(Date.parse(data.period.end)+offset+8*3600000).toISOString().slice(0,16);
        const start=local(-50*3600000),end=local(-2*3600000);
        await page.locator('#density-trend-start').fill(start);await page.locator('#density-trend-end').fill(end);
        await page.locator('#density-trend-query').click();
        await page.waitForFunction(s=>Date.parse(DensityTrendPage.meta.period.start)===Date.parse(s+':00+08:00'),start);
        assert.ok((await page.locator('#density-trend-caption').innerText()).startsWith('自选时间'));
        const edge=await page.evaluate(()=>{
            const t=Date.now(),at=ms=>new Date(t+ms).toISOString();
            const a={at:at(-7200000),value:1.48,key:'advice',t:t-7200000,apply_start:at(-7200000),basis:{channels:['A304']}};
            return {gap:!DensityTrendPage.connected(a,{...a,at:at(0),t,apply_start:at(0)}),
                route:!DensityTrendPage.connected(a,{...a,t:a.t+3600000,basis:{channels:['A303']}}),
                missing:!DensityTrendPage.connected({...a,value:null},a)};
        });assert.deepEqual(edge,{gap:true,route:true,missing:true});
        assert.equal(await page.locator('[data-density-series="A303"]').count(),0);
        await page.locator('[data-density-series="advice"]').click();
        assert.equal(await page.locator('[data-density-series="advice"]').getAttribute('aria-pressed'),'false');
        await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(output,'live-mobile.png')});
        const overflow=await page.locator('#page-density-trend').evaluate(el=>Math.max(el.scrollWidth-el.clientWidth,el.getBoundingClientRect().right-innerWidth));
        assert.ok(overflow<=2,'horizontal overflow: '+overflow);
        await page.setViewportSize({width:1500,height:1000});await page.locator('#density-trend-theme').click();
        await page.screenshot({path:path.join(output,'live-dark.png')});
        assert.deepEqual(errors,[]);assert.equal(mutationAttempts,0);
        const report={passed:true,liveData:true,apiMs,counts,monthCount,archiveAvailable:data.archive_available,
            trueAdviceCount:data.suggestions.length,exactRanges:true,keyboard:true,changeMarkers:markers,edgeCases:edge,mutationAttempts,pageErrors:errors};
        fs.writeFileSync(path.join(output,'result-page.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report));
    } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
