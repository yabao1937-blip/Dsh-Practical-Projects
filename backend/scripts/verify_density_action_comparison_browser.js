// Read-only live table contract and explicit synthetic display examples.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const output=path.resolve(__dirname,'../../output/playwright/density-actions-20261027');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    const browser=await chromium.launch({channel:'chrome',headless:true});
    const report={passed:false,mutations:0,errors:[]};
    try {
        const context=await browser.newContext({viewport:{width:1500,height:1000}});
        await context.route('**/api/**',r=>{
            if(!['GET','HEAD','OPTIONS'].includes(r.request().method())){report.mutations++;return r.abort();}
            return r.continue();
        });
        const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message));
        await page.goto(url);await page.waitForFunction(()=>App.__ready,{},{timeout:60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(()=>DensityTrendPage.meta && !DensityTrendPage.loading);
        report.live=await page.evaluate(()=>({total:DensityTrendPage.meta.actions.length,
            contract:DensityTrendPage.meta.actions.every(a=>'hourly_advice' in a && 'difference' in a && 'comparison_at' in a),
            missing:DensityTrendPage.meta.actions.filter(a=>!Number.isFinite(a.hourly_advice?.value)).length}));
        assert.equal(report.live.contract,true);
        const table=page.locator('#density-trend-actions').locator('..');
        assert.deepEqual(await table.locator('th').allTextContents(),
            ['检测时间','设备','设定变化 (g/cm³)','小时建议密度 (g/cm³)','差值 (g/cm³)']);
        await page.locator('.density-trend-table-title').scrollIntoViewIfNeeded();
        await table.locator('..').locator('..').screenshot({path:path.join(output,'live-table.png')});
        await page.evaluate(()=>{
            const chart=DensityTrendPage;
            window._realDensityMeta=chart.meta;chart.range={synthetic:true};chart.page=0;
            const at='2026-10-27T02:20:00Z';
            const advice={value:1.50,generated_at:'2026-10-27T02:00:08Z',apply_start:'2026-10-27T02:00:00Z'};
            const base={at,comparison_at:at,channel:'A304',before:1.48,continuous:true};
            chart.meta={...chart.meta,actions:[
                {...base,after:1.52,hourly_advice:null,difference:null},
                {...base,after:1.49999998,hourly_advice:advice,difference:-.00000002},
                {...base,after:1.49,hourly_advice:advice,difference:-.01},
                {...base,after:1.51,hourly_advice:advice,difference:.01}]};
            document.querySelector('.density-trend-table-title').textContent='调密记录合成验证示例（非生产数据）';
            chart.renderActions();
        });
        const rows=await page.locator('#density-trend-actions tr').evaluateAll(rows=>rows.map(r=>
            [...r.cells].map(c=>c.textContent.trim())));
        report.synthetic=rows.map(r=>r.slice(2));
        assert.deepEqual(report.synthetic,[['1.480 → 1.510','1.500','+0.010'],
            ['1.480 → 1.490','1.500','-0.010'],['1.480 → 1.500','1.500','0.000'],
            ['1.480 → 1.520','—','—']]);
        assert.match(await page.locator('#density-trend-actions tr').first().locator('td').nth(3).getAttribute('title'),/调后观测.*建议存档于/);
        await table.locator('..').locator('..').screenshot({path:path.join(output,'synthetic-table.png')});
        await page.evaluate(()=>{
            const chart=DensityTrendPage,base=chart.meta.actions[0];
            chart.meta={...chart.meta,actions:Array.from({length:25},(_,id)=>({...base,id}))};chart.renderActions();
        });
        assert.equal(await page.locator('#density-trend-actions tr').count(),20);
        await page.locator('#density-trend-next').click();assert.equal(await page.locator('#density-trend-actions tr').count(),5);
        await page.setViewportSize({width:390,height:844});
        await page.screenshot({path:path.join(output,'mobile-table.png')});
        const overflow=await page.locator('#page-density-trend').evaluate(el=>Math.max(el.scrollWidth-el.clientWidth,el.getBoundingClientRect().right-innerWidth));
        assert.ok(overflow<=2);
        await page.evaluate(()=>{DensityTrendPage.meta={...DensityTrendPage.meta,actions:[]};DensityTrendPage.renderActions();});
        assert.equal(await page.locator('#density-trend-actions td').getAttribute('colspan'),'5');
        assert.deepEqual(report.errors,[]);assert.equal(report.mutations,0);
        report.passed=true;console.log(JSON.stringify(report));
    } finally {
        fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));await browser.close();
    }
})().catch(e=>{console.error(e);process.exitCode=1;});
