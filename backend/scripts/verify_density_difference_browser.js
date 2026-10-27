// Read-only UI checks. Synthetic differences below are explicitly labeled.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const output=path.resolve(__dirname,'../../output/playwright/density-difference-20261027');
const url=process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8000';
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    const browser=await chromium.launch({channel:'chrome',headless:true});
    const report={passed:false,mutations:0,errors:[]};
    try {
        const context=await browser.newContext({viewport:{width:1500,height:1000}});
        await context.route('**/api/**',route=>{
            if(!['GET','HEAD','OPTIONS'].includes(route.request().method())){report.mutations++;return route.abort();}
            return route.continue();
        });
        const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message));
        await page.goto(url);await page.waitForFunction(()=>App.__ready,{},{timeout:60000});
        await page.locator('.nav-item[data-page="page-density-trend"]').click();
        await page.waitForFunction(()=>DensityTrendPage.meta && !DensityTrendPage.loading);
        report.live=await page.evaluate(()=>({adviceCount:DensityTrendPage.series.advice.length,
            shadedIntervals:DensityTrendPage.differenceAreas().length,
            actualAdvice:DensityTrendPage.series.advice.map(p=>({at:p.at,value:p.value}))}));
        await page.screenshot({path:path.join(output,'live.png')});
        report.cases=await page.evaluate(()=>{
            const chart=DensityTrendPage,t=Date.parse('2026-10-27T00:00:00Z'),hour=3600000;
            const saved={series:chart.series,meta:chart.meta,visible:chart.visible,range:chart.range};
            window._restoreDifference=()=>{Object.assign(chart,saved);chart.draw();};
            chart.range={synthetic:true}; // No automatic live replacement during fixture checks.
            const point=(minute,value,key)=>({t:t+minute*60000,at:new Date(t+minute*60000).toISOString(),
                apply_start:new Date(t+Math.floor(minute/60)*hour).toISOString(),
                value,key,origin:'change',basis:{channels:['A304']}});
            const set=(a,b)=>{chart.visible={A304:true,advice:true};chart.series={
                A304:a.map(([m,v])=>point(m,v,'A304')),advice:b.map(([m,v])=>point(m,v,'advice'))};};
            const compact=()=>chart.differenceAreas().map(a=>({...a,start:(a.start-t)/60000,end:(a.end-t)/60000}));
            const result={};
            set([[0,1.48],[30,1.5],[60,1.49]],[[10,1.49],[50,1.49]]);
            const original=JSON.stringify(chart.series);result.offset=compact();
            result.sourceUnchanged=original===JSON.stringify(chart.series);
            set([[0,1.48],[60,1.48]],[[0,1.47],[60,1.49]]);result.crossing=compact();
            set([[0,1.51999998],[60,1.51999998]],[[0,1.52],[60,1.52]]);result.equal=compact();
            set([[0,1.48],[61,1.48]],[[0,1.5],[60,1.5]]);result.longA304Gap=compact();
            set([[0,1.48],[60,1.48]],[[0,1.5],[30,null],[60,1.5]]);result.missingAdvice=compact();
            set([[0,1.48],[30,null],[60,1.48]],[[0,1.5],[60,1.5]]);result.shortA304Gap=compact();
            chart.visible.A304=false;result.hiddenA304=compact();
            chart.visible={A304:true,advice:false};result.hiddenAdvice=compact();
            set([[0,1.48]],[[0,1.5],[60,1.5]]);result.isolated=compact();
            set([[0,1.48],[30,1.48]],[[60,1.5],[120,1.5]]);result.noOverlap=compact();
            set([[0,1.48],[0,1.49],[60,1.49]],[[0,1.5],[0,1.52],[60,1.52]]);result.duplicateTime=compact();
            set([[0,1.48],[60,1.48],[120,1.48],[180,1.48]],[[0,1.5],[180,1.5]]);result.longAdviceGap=compact();
            set([[0,1.48],[60,1.48]],[[0,1.5],[60,1.5]]);
            chart.series.advice[1].basis.channels=['A303'];result.basisChange=compact();
            chart.meta={period:{start:new Date(t).toISOString(),end:new Date(t+4*hour).toISOString()}};
            set([[0,1.48],[30,1.49],[60,1.49],[90,1.51],[120,1.51],[150,1.5],[180,1.49],[210,1.49],[240,1.49]],
                [[0,1.49],[60,1.52],[120,1.5],[180,1.48],[240,1.5]]);
            document.getElementById('density-trend-caption').textContent='合成验证示例 · 阶梯设定 / 独立建议 / 交叉差值（非生产数据）';
            document.getElementById('density-trend-archive-note').textContent='此图仅验证有差值时的阴影效果，未写入数据库。';
            const canvas=document.getElementById('density-trend-canvas'),ctx=canvas.getContext('2d');
            const originalStroke=ctx.stroke, originalMove=ctx.moveTo, originalLine=ctx.lineTo, originalBegin=ctx.beginPath;
            let commands=[];const strokes=[];
            ctx.beginPath=function(...args){commands=[];return originalBegin.apply(this,args);};
            ctx.moveTo=function(...xy){commands.push(['M',...xy]);return originalMove.apply(this,xy);};
            ctx.lineTo=function(...xy){commands.push(['L',...xy]);return originalLine.apply(this,xy);};
            ctx.stroke=function(...args){if(this.strokeStyle===chart.palette().advice)strokes.push(commands.slice());return originalStroke.apply(this,args);};
            try{chart.draw();}finally{ctx.stroke=originalStroke;ctx.moveTo=originalMove;ctx.lineTo=originalLine;ctx.beginPath=originalBegin;}
            result.adviceStrokes=strokes.map(s=>s.map(c=>c[0]));
            result.renderedAreaCount=chart.differenceAreas().length;
            result.hitCount=canvas._hits.length;
            // The fill path has no stroke or artificial boundary lines.
            let fillCalls=0,strokeCalls=0,closeCalls=0;
            chart.shadeDifference({save(){},restore(){},beginPath(){},moveTo(){},lineTo(){},
                closePath(){closeCalls++;},fill(){fillCalls++;},stroke(){strokeCalls++;}},
                chart.differenceAreas(),x=>x,y=>y,'#579ee8');
            result.fill={fillCalls,strokeCalls,closeCalls};
            return result;
        });
        const c=report.cases;
        assert.deepEqual(c.offset,[{start:10,end:30,setpoint:1.48,from:1.49,to:1.49},
            {start:30,end:50,setpoint:1.5,from:1.49,to:1.49}]);
        assert.equal(c.sourceUnchanged,true);
        assert.equal(c.crossing.length,2);assert.ok(Math.abs(c.crossing[0].end-30)<1e-6);
        assert.equal(c.crossing[0].to,1.48);assert.equal(c.crossing[1].from,1.48);
        for(const name of ['equal','longA304Gap','missingAdvice','hiddenA304','hiddenAdvice','isolated','noOverlap','longAdviceGap','basisChange'])assert.deepEqual(c[name],[],name);
        assert.deepEqual(c.duplicateTime,[{start:0,end:60,setpoint:1.49,from:1.52,to:1.52}]);
        assert.equal(c.shortA304Gap.length,1);
        assert.deepEqual(c.adviceStrokes,[['M','L','L','L','L']],'independent advice path; no label leader');
        assert.equal(c.hitCount,14,'only original observation hit targets');
        assert.equal(c.fill.strokeCalls,0);assert.equal(c.fill.fillCalls,1);assert.equal(c.fill.closeCalls,c.renderedAreaCount);
        await page.screenshot({path:path.join(output,'synthetic-light.png')});
        await page.locator('#density-trend-theme').click();await page.waitForTimeout(250);
        await page.screenshot({path:path.join(output,'synthetic-dark.png')});
        await page.locator('[data-density-series="advice"]').click();
        assert.equal(await page.evaluate(()=>DensityTrendPage.differenceAreas().length),0);
        await page.locator('[data-density-series="advice"]').click();
        await page.setViewportSize({width:390,height:844});
        await page.screenshot({path:path.join(output,'synthetic-mobile.png')});
        const overflow=await page.locator('#page-density-trend').evaluate(el=>Math.max(el.scrollWidth-el.clientWidth,el.getBoundingClientRect().right-innerWidth));
        assert.ok(overflow<=2);assert.deepEqual(report.errors,[]);assert.equal(report.mutations,0);
        await page.evaluate(()=>window._restoreDifference());
        report.passed=true;
        console.log(JSON.stringify({passed:true,live:report.live,syntheticCases:Object.keys(c),mutations:0,errors:[]}));
    } finally {
        fs.writeFileSync(path.join(output,'verification.json'),JSON.stringify(report,null,2));
        await browser.close();
    }
})().catch(e=>{console.error(e);process.exitCode=1;});
