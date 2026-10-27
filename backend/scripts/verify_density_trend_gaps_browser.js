// Read-only live UI acceptance plus explicitly synthetic chart boundary cases.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const output = path.resolve(__dirname, '../../output/playwright/density-trend-20261027');
const url = process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
(async () => {
    fs.mkdirSync(output, {recursive:true});
    const browser = await chromium.launch({channel:'chrome', headless:true});
    const report = {passed:false, url, mutations:0, errors:[]};
    try {
        const context = await browser.newContext({viewport:{width:1500,height:1000}});
        await context.route('**/api/**', route => {
            if (!['GET','HEAD','OPTIONS'].includes(route.request().method())) {
                report.mutations++; return route.abort();
            }
            return route.continue();
        });
        const page = await context.newPage();
        await page.addInitScript(() => {
            const original = window.setInterval;
            window._intervalChecks = new Map();
            window.setInterval = function(callback, milliseconds, ...args) {
                const id = original.call(this, callback, milliseconds, ...args);
                window._intervalChecks.set(id, {callback, milliseconds});
                return id;
            };
        });
        page.on('pageerror', error => report.errors.push(error.message));
        await page.goto(url);
        await page.waitForFunction(() => App.__ready, {}, {timeout:60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(() => DensityTrendPage.meta && !DensityTrendPage.loading);
        report.live = await page.evaluate(() => {
            const canvas = document.getElementById('density-trend-canvas');
            const hits = canvas._hits.filter(p => p.key === 'advice');
            return {advice:hits.map(p => ({at:p.at, apply_start:p.apply_start, value:p.value})),
                spanPixels:hits.at(-1).x-hits[0].x, label:canvas._adviceLabel.text,
                note:document.getElementById('density-trend-archive-note').textContent};
        });
        assert.ok(report.live.advice.length >= 2);
        assert.match(report.live.label, /1\.520/);
        report.refresh = await page.evaluate(() => {
            const chart=DensityTrendPage, timer=window._intervalChecks.get(chart.timer);
            const saved={refresh:chart.refresh,active:chart.active,range:chart.range,loading:chart.loading};
            const hidden=Object.getOwnPropertyDescriptor(document,'hidden'), calls=[];
            try {
                chart.refresh=options=>calls.push(options);chart.active=()=>true;chart.range=null;chart.loading=false;
                timer.callback();
                chart.loading=true;timer.callback();chart.loading=false;
                chart.range={};timer.callback();chart.range=null;
                chart.active=()=>false;timer.callback();chart.active=()=>true;
                Object.defineProperty(document,'hidden',{configurable:true,value:true});timer.callback();
                return {milliseconds:timer.milliseconds,calls};
            } finally {
                Object.assign(chart,saved);
                if(hidden)Object.defineProperty(document,'hidden',hidden);else delete document.hidden;
            }
        });
        assert.deepEqual(report.refresh,{milliseconds:60000,calls:[{keepPage:true}]});
        await page.screenshot({path:path.join(output,'live-7-days.png')});
        const label = await page.evaluate(() => document.getElementById('density-trend-canvas')._adviceLabel);
        const box = await page.locator('#density-trend-canvas').boundingBox();
        await page.mouse.move(box.x+label.left+10, box.y+label.top+10);
        assert.match(await page.locator('.density-trend-tooltip').innerText(), /存档于/);
        await page.locator('#density-trend-focus-advice').click();
        await page.waitForFunction(() => !DensityTrendPage.loading && DensityTrendPage.meta &&
            Date.parse(DensityTrendPage.meta.period.end)-Date.parse(DensityTrendPage.meta.period.start)<86400000);
        report.zoom = await page.evaluate(() => ({period:DensityTrendPage.meta.period,
            advice:DensityTrendPage.series.advice.map(p => ({at:p.at,value:p.value})),
            label:document.getElementById('density-trend-canvas')._adviceLabel.text}));
        assert.deepEqual(report.zoom.advice, report.live.advice.map(p => ({at:p.at,value:p.value})));
        await page.mouse.move(0,0);
        await page.screenshot({path:path.join(output,'live-advice-zoom.png')});
        await page.locator('[data-density-series="advice"]').click();
        assert.equal(await page.evaluate(() => document.getElementById('density-trend-canvas')._adviceLabel), null);
        await page.locator('#density-trend-focus-advice').click();
        await page.waitForFunction(() => !DensityTrendPage.loading && DensityTrendPage.visible.advice);
        await page.locator('[data-t-range="720"]').click();
        await page.waitForFunction(() => !DensityTrendPage.loading && !DensityTrendPage.range && DensityTrendPage.hours===720);
        assert.ok(await page.evaluate(() => document.getElementById('density-trend-canvas')._adviceLabel));
        if(await page.locator('#density-trend-next').isEnabled()) {
            await page.locator('#density-trend-next').click();
            await page.evaluate(() => DensityTrendPage.refresh({keepPage:true}));
            assert.equal(await page.evaluate(() => DensityTrendPage.page),1);
        }
        await page.setViewportSize({width:390,height:844});
        await page.waitForFunction(() => document.getElementById('density-trend-canvas').clientWidth < 500);
        await page.screenshot({path:path.join(output,'live-mobile.png')});
        const overflow = await page.locator('#page-density-trend').evaluate(el =>
            Math.max(el.scrollWidth-el.clientWidth, el.getBoundingClientRect().right-innerWidth));
        assert.ok(overflow<=2);
        await page.setViewportSize({width:1500,height:1000});
        await page.locator('#density-trend-theme').click();
        await page.waitForTimeout(250);
        await page.screenshot({path:path.join(output,'live-dark.png')});
        await page.locator('#density-trend-theme').click();
        report.synthetic = await page.evaluate(() => {
            const chart=DensityTrendPage, saved={series:chart.series,meta:chart.meta,visible:chart.visible};
            const t=Date.parse('2026-10-24T01:00:00Z'), hour=3600000;
            const point=(minutes,value,extra={})=>({at:new Date(t+minutes*60000).toISOString(),
                t:t+minutes*60000,value,key:'A304',origin:'anchor',...extra});
            const paths=points=>{
                const result=[];
                chart.trace({moveTo:(...xy)=>result.push(['M',...xy]),lineTo:(...xy)=>result.push(['L',...xy])},
                    points,'A304',x=>(x-t)/60000,y=>y);
                return result;
            };
            const short=[point(0,1.48),point(5,1.50,{origin:'change',break_before:true}),point(10,1.48,{origin:'change'})];
            const result={short:paths(short), exactHour:paths([point(0,1.48),point(60,1.50)]),
                longGap:paths([point(0,1.48),point(60+1/60000,1.50,{break_before:true})]),
                reverse:chart.connected(point(5,1.48),point(0,1.50)),
                standalone:paths([point(5,1.48)])};
            chart.meta={period:{start:new Date(t-hour).toISOString(),end:new Date(t+3*hour).toISOString()}};
            chart.series={A304:[point(0,1.48),point(2,null),...short.slice(1),point(80,1.51)], advice:[]};
            chart.visible={A304:true,advice:true};
            const canvas=document.getElementById('density-trend-canvas'), ctx=canvas.getContext('2d');
            const move=ctx.moveTo,line=ctx.lineTo, records=[];
            ctx.moveTo=function(...xy){if(this.strokeStyle===chart.palette().A304)records.push(['M',...xy]);return move.apply(this,xy);};
            ctx.lineTo=function(...xy){if(this.strokeStyle===chart.palette().A304)records.push(['L',...xy]);return line.apply(this,xy);};
            try{chart.draw();}finally{ctx.moveTo=move;ctx.lineTo=line;}
            result.renderOps=records.map(r=>r[0]);
            result.hitCount=canvas._hits.length; result.labelAbsent=canvas._adviceLabel===null;
            const advice={...point(0,1.52),key:'advice',apply_start:point(0,1.52).at,basis:{channels:['A304']}};
            result.adviceGap=chart.connected(advice,{...advice,t:t+2*hour,at:point(120,1.52).at,apply_start:point(120,1.52).at});
            result.adviceMissing=chart.connected({...advice,value:null},advice);
            result.changedChannel=chart.connected(advice,{...advice,t:t+hour,basis:{channels:['A303']}});
            const anchor=point(0,1.48,{apply_start:point(0,1.48).at});
            result.anchorJitter=chart.connected(anchor,point(60.01,1.48,{apply_start:point(60,1.48).at}));
            result.lateAnchor=chart.connected(anchor,point(70,1.48,{apply_start:point(60,1.48).at}));
            result.explicitAnchorGap=chart.connected(anchor,point(60.01,1.48,{apply_start:point(60,1.48).at,break_before:true}));
            result.missingHourAnchor=chart.connected(anchor,point(120,1.48,{apply_start:point(120,1.48).at}));
            result.changeOnly=short.map((p,i)=>chart.showPoint(short[i-1],p));
            window._restoreDensityTrend=()=>{Object.assign(chart,saved);chart.draw();};
            return result;
        });
        assert.deepEqual(report.synthetic.short, [['M',0,1.48],['L',5,1.48],['L',5,1.5],['L',10,1.5],['L',10,1.48]]);
        assert.deepEqual(report.synthetic.exactHour, [['M',0,1.48],['L',60,1.48],['L',60,1.5]]);
        assert.deepEqual(report.synthetic.longGap.map(p=>p[0]), ['M','M']);
        assert.deepEqual(report.synthetic.standalone, [['M',5,1.48]]);
        assert.equal(report.synthetic.reverse,false);
        assert.equal(report.synthetic.hitCount,4,'no invented samples/hits');
        assert.deepEqual(report.synthetic.renderOps.slice(0,6),['M','L','L','L','L','M']);
        for(const flag of ['adviceGap','adviceMissing','changedChannel'])assert.equal(report.synthetic[flag],false);
        assert.equal(report.synthetic.anchorJitter,true);
        assert.equal(report.synthetic.missingHourAnchor,false);
        assert.equal(report.synthetic.lateAnchor,false);
        assert.equal(report.synthetic.explicitAnchorGap,false);
        assert.equal(report.synthetic.labelAbsent,true);
        assert.deepEqual(report.synthetic.changeOnly,[false,true,true]);
        await page.screenshot({path:path.join(output,'synthetic-short-and-long-gap.png')});
        await page.evaluate(()=>window._restoreDensityTrend());
        assert.equal(report.mutations,0);assert.deepEqual(report.errors,[]);
        report.passed=true;
        console.log(JSON.stringify({passed:true,liveAdvice:report.live.advice.length,
            originalSpanPixels:report.live.spanPixels,synthetic:report.synthetic,mutations:0,errors:[]}));
    } finally {
        fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));
        await browser.close();
    }
})().catch(error=>{console.error(error);process.exitCode=1;});
