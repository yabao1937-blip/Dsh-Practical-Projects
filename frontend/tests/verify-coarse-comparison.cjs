// Browser acceptance against a static local frontend; every unrelated API is intercepted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname,'../..');
const output = process.env.DMCS_COMPARISON_UI_OUTPUT || path.join(root,'output/coarse-model-comparison-ui-20261103');
const endpoint = '/api/v1/training/coarse-model-comparison';
const result = {source:'Local static preview with intercepted API; never a production session',checks:[],screenshots:[],blockedUnrelatedWrites:[]};
const score = {mae:2.345,rmse:2.9,bias:-.1234,r2:-.765432,hit1:4,n:10,eligible_n:12,coverage:10/12,date_range:{start:'2026-06-01',end:'2026-10-31'},hit1_rate:.4,normal:{mae:1.2,hit1:3,n:5,hit1_rate:.6}};
const fixture = {
    schema_version:1,status:'research',reason:'交互验收专用状态夹具，不是真实模型指标',
    scope:{total_n:81,evaluable_n:70,review_n:6,excluded_n:5,by_system:{'3':{total_n:60},'2':{total_n:21}},reasons:[{reason:'验收示例原因',n:11}]},
    models:Object.fromEntries(['current','candidate'].map(key=>[key,{name:key==='current'?'现用 EX8':'动态候选名称',version:'验收夹具',status:key==='current'?'frozen':'provisional',method:{summary:'方法由正式元数据响应决定',details:['<img src=x onerror=alert(1)>']},metrics:Object.fromEntries(['history_all','history_forward','future'].map(phase=>[phase,{status:phase==='future'?'pending':'available',note:'验收夹具',by_system:{'3':{...score,mae:key==='current'?2.345:1.876},'2':{...score,mae:key==='current'?4.567:3.456}}}]))}]))
};
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    const browser = await chromium.launch({channel:'chrome',headless:true});
    const context = await browser.newContext({viewport:{width:1440,height:1200}});
    const page = await context.newPage();
    const skipFixtures=process.env.DMCS_COMPARISON_REAL_ONLY==='1';
    let real=skipFixtures?JSON.parse(fs.readFileSync(process.env.DMCS_COMPARISON_JSON || path.join(root,'backend/data/gpt_coarse_model_comparison.json'),'utf8')):undefined;
    let mode = skipFixtures?'real':'fixture';
    const old = JSON.parse(fs.readFileSync(path.join(root,'backend/models/gpt_expert8/20261012/production.json'),'utf8'));
    const currentModel = {engine:'gpt',production:'expert8',expert8:old.model,trainedAt:old.trained_at,activated_at:null,revision:'本地冻结产物预览',artifact_id:'local-production-json-preview',n:old.model.n,report:old.report,source:old.source,rows:old.rows};
    const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await context.route('**/api/**',route=>{
        if(route.request().method()!=='GET') {
            result.blockedUnrelatedWrites.push({method:route.request().method(),path:new URL(route.request().url()).pathname});
            assert.notEqual(new URL(route.request().url()).pathname,endpoint,'Comparison itself must never write');
            return route.abort();
        }
        if(new URL(route.request().url()).pathname==='/api/v1/gpt-coarse/current') return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({model:currentModel})});
        if(new URL(route.request().url()).pathname===endpoint) return route.fulfill(mode==='error'?{status:503,body:'{}'}:{status:200,contentType:'application/json',body:JSON.stringify(mode==='real'?real:mode==='invalid'?{...fixture,status:'invalid'}:fixture)});
        return route.fulfill({status:503,contentType:'application/json',body:'{}'});
    });
    try {
        await page.goto(process.env.DMCS_VERIFY_URL || 'http://127.0.0.1:8766');
        await page.locator('[data-page="page-coarse"]').click();
        const panel = page.locator('#coarse-model-comparison');
        await panel.getByRole('heading',{name:skipFixtures?real.models.candidate.name:'动态候选名称',exact:true}).waitFor();
        await page.locator('.gpt-expert-chart svg').waitFor();
        assert.equal(await page.locator('.gpt-expert-legacy').getAttribute('open'),null,'Old training metrics must be collapsed');
        assert.equal(await panel.locator('.coarse-comparison-metric').count(),6);
        const current = panel.locator('[data-model="current"]');
        const candidate = panel.locator('[data-model="candidate"]');
        if(!skipFixtures) {
        assert.equal(await current.locator('[data-phase="history_all"] strong').first().textContent(),'2.35');
        assert.equal(await candidate.locator('[data-phase="history_all"] strong').first().textContent(),'1.88');
        assert.equal(await current.locator('[data-phase="history_all"] [data-score="RMSE"] dd').textContent(),'2.90 pp');
        assert.equal(await current.locator('[data-phase="history_all"] [data-score="Bias"] dd').textContent(),'-0.12 pp');
        assert.equal(await current.locator('[data-phase="history_all"] [data-score="R²"] dd').textContent(),'-0.765');
        assert.match(await current.locator('[data-phase="history_all"]').innerText(),/83\.3%.*10 \/ 12/,'Stage coverage must use its supplied denominator, not scope total');
        assert.match(await current.locator('[data-phase="history_all"]').innerText(),/2026-06-01 至 2026-10-31/);
        await page.evaluate(()=>{window.CoarseComparison.data.models.current.metrics.history_all.by_system['3'].r2=null;window.CoarseComparison.render();});
        assert.equal(await current.locator('[data-phase="history_all"] [data-score="R²"] dd').textContent(),'不可计算');
        assert.match(await current.locator('[data-phase="future"]').innerText(),/待验证/);
        assert.equal(await current.locator('[data-phase="future"] .coarse-comparison-secondary dd').allTextContents().then(values=>values.every(v=>v==='待验证')),true);
        assert.doesNotMatch(await current.locator('[data-phase="future"]').innerText(),/0\.00|0%/);
        await panel.getByLabel('查看常规样本补充').check();
        assert.equal(await current.locator('[data-phase="history_all"] strong').first().textContent(),'2.35','Normal supplement cannot replace all-sample MAE');
        assert.match(await current.locator('[data-phase="history_all"]').innerText(),/MAE 1.20 pp/);
        await panel.getByRole('button',{name:'二系统 · 单列'}).click();
        await current.getByRole('heading',{name:'现用 14/15 规则'}).waitFor();
        assert.equal(await current.locator('[data-phase="history_all"] strong').first().textContent(),'4.57');
        assert.match(await current.locator('.coarse-comparison-method').innerText(),/经验规则/);
        await panel.getByRole('button',{name:'三系统 · 主比较'}).click();
        await panel.getByLabel('查看常规样本补充').uncheck();
        await candidate.getByText('查看方法与参数').click();
        assert.equal(await panel.locator('img').count(),0,'API method text must be escaped');
        result.checks.push('Dynamic names, system scores and 14/15 labeling; all-sample primary with normal supplement; null future; escaped methods');
        result.checks.push('RMSE/Bias/R² format; signed Bias; negative/null R²; stage dates and independent coverage denominator; all future fields pending');
        mode='invalid'; await panel.getByRole('button',{name:'刷新对比'}).click();
        await panel.getByRole('alert').waitFor();
        assert.equal(await panel.locator('.coarse-comparison-value:not(.is-pending)').count(),0,'Invalid evidence must not leak numeric scores');
        mode='error';await panel.getByRole('button',{name:'刷新对比'}).click();
        await panel.getByText('暂时无法读取模型对比',{exact:true}).waitFor();
        await panel.screenshot({path:path.join(output,'error-desktop.png')});result.screenshots.push('error-desktop.png');
        mode='fixture';await panel.getByRole('button',{name:'重新读取'}).click();
        await panel.getByRole('heading',{name:'动态候选名称'}).waitFor();
        result.checks.push('Invalid evidence hides numbers; network failure displays recovery; retry works');
        await page.evaluate(()=>{
            window.CoarseComparison.data={...window.CoarseComparison.data,status:'ready',freeze:{status:'accepted',sealed_at:'2026-11-03T06:00:00Z',due_at:'2026-11-10T06:00:00Z'}};
            window.CoarseComparison.data.models.candidate.status='frozen';
            window.CoarseComparison.render();
        });
        const futureText=await current.locator('[data-phase="future"]').innerText();
        assert.match(futureText,/2026\/11\/03.*14:00/);
        assert.match(futureText,/2026\/11\/10.*14:00/);
        assert.match(futureText,/待验证/,'Accepted freeze must not imply completed future testing');
        assert.match(await candidate.locator('.coarse-comparison-badge').textContent(),/已冻结.*待未来验证/);
        result.checks.push('Accepted freeze shows actual sealed/due dates in Asia/Shanghai while future stays pending');
        await page.evaluate(()=>{
            const empty={status:'unavailable',mae:null,rmse:null,bias:null,r2:null,hit1:null,n:0,hit1_rate:null,eligible_n:2,coverage:0,reason_counts:{'输入不满足当前模型门槛':2}};
            const available={status:'available',mae:.75,rmse:1,bias:-.25,r2:-.2,hit1:1,n:2,hit1_rate:.5,eligible_n:2,coverage:1,date_range:{start:'2026-11-04',end:'2026-11-05'}};
            window.CoarseComparison.data.models.current.metrics.future={status:'unavailable',baseline_id:'EX8_PRODUCTION_RUNS',baseline_label:'现用EX8生产RUNS口径（参数冻结）',attempted_n:3,prediction_n:0,prediction_coverage:0,by_system:{'3':empty}};
            window.CoarseComparison.data.models.candidate.metrics.future={status:'available',attempted_n:3,prediction_n:3,prediction_coverage:1,by_system:{'3':available}};
            window.CoarseComparison.data.future_comparison={by_system:{'3':{status:'unavailable',n:0,eligible_n:2,coverage:0,current:empty,candidate:empty}}};
            window.CoarseComparison.render();
        });
        assert.equal(await current.locator('[data-phase="future"] .coarse-comparison-value strong').textContent(),'未能评分');
        assert.match(await current.locator('[data-phase="future"]').innerText(),/N = 0/);
        assert.match(await current.locator('[data-phase="future"]').innerText(),/可评分覆盖率 0\.0%/);
        assert.match(await current.locator('[data-phase="future"]').innerText(),/EX8_PRODUCTION_RUNS/);
        assert.equal(await candidate.locator('[data-phase="future"] .coarse-comparison-value strong').textContent(),'0.75');
        assert.match(await panel.locator('.coarse-comparison-future-common').innerText(),/暂无共同评分样本/);
        assert.match(await panel.locator('.coarse-comparison-all-future').innerText(),/现用 0\.0%.*候选 100\.0%/);
        await page.evaluate(()=>{
            const score={mae:.5,rmse:.5,bias:.5,r2:null,hit1:1,n:1,hit1_rate:1};
            window.CoarseComparison.data.future_comparison.by_system['3']={status:'available',n:1,eligible_n:2,coverage:.5,current:score,candidate:{...score,bias:-.5}};
            window.CoarseComparison.render();
        });
        assert.match(await panel.locator('.coarse-comparison-future-common').innerText(),/N = 1 \/ 2.*50\.0%/);
        assert.match(await panel.locator('.coarse-comparison-future-common').innerText(),/Bias -0\.50 pp/);
        assert.match(await panel.locator('.coarse-comparison-future-common').innerText(),/R² 不可计算/);
        result.checks.push('Future sides independently show available/rejected results; real zero coverage differs from unstarted pending; common-target fair comparison; RUNS provenance');
        await page.evaluate(()=>{
            const oldScore={status:'available',mae:.5,rmse:.5,bias:.5,r2:null,hit1:1,n:1,hit1_rate:1,eligible_n:1,coverage:1};
            const refused={status:'unavailable',mae:null,rmse:null,bias:null,r2:null,hit1:null,n:0,hit1_rate:null,eligible_n:1,coverage:0};
            const current=window.CoarseComparison.data.models.current.metrics.future;
            const candidate=window.CoarseComparison.data.models.candidate.metrics.future;
            current.labelled_n=candidate.labelled_n=3;
            current.attempted_n=candidate.attempted_n=3;
            current.prediction_n=3; current.prediction_coverage=1;
            candidate.prediction_n=0; candidate.prediction_coverage=0;
            current.by_system=Object.fromEntries(['0','1','4'].map(key=>[key,{...oldScore}]));
            candidate.by_system=Object.fromEntries(['0','1','4'].map(key=>[key,{...refused}]));
            window.CoarseComparison.data.future_comparison={status:'unavailable',n:0,eligible_n:3,coverage:0,by_system:Object.fromEntries(['0','1','4'].map(key=>[key,{status:'unavailable',n:0,eligible_n:1,coverage:0,current:oldScore,candidate:refused}]))};
            window.CoarseComparison.render();
        });
        assert.match(await panel.locator('.coarse-comparison-future-other').innerText(),/其它工况已揭标签目标 3 条.*0 系统 1 条.*1 系统 1 条.*4 系统 1 条/);
        assert.match(await panel.locator('.coarse-comparison-all-future').innerText(),/现用 100\.0%.*候选 0\.0%/);
        assert.match(await current.locator('[data-phase="future"]').innerText(),/待验证/,'Other-system rows cannot masquerade as three-system scores');
        result.checks.push('Future 0/1/4 old-only rows are counted outside 2/3 tabs and retained in clearly labeled all-system coverage');
        await page.evaluate(data=>{window.CoarseComparison.data=data;window.CoarseComparison.render();},fixture);
        assert.match(await panel.locator('.coarse-comparison-future-other').innerText(),/范围：待验证/);
        }
        const realPath=process.env.DMCS_COMPARISON_JSON || path.join(root,'backend/data/gpt_coarse_model_comparison.json');
        if(fs.existsSync(realPath)) {
            real=JSON.parse(fs.readFileSync(realPath,'utf8')); mode='real';
            fs.copyFileSync(realPath,path.join(output,'browser-used-comparison.json'));
            await panel.getByRole('button',{name:'刷新对比'}).click();
            await page.waitForFunction(name=>window.CoarseComparison.data?.models.candidate.name===name,real.models.candidate.name);
            for(const system of ['3','2']) {
              await panel.locator(`[data-system="${system}"]`).click();
              for(const key of ['current','candidate']) for(const phase of ['history_all','history_forward','future']) {
                const m=real.models[key].metrics[phase];
                const systemScore=m.by_system?.[system];
                const text=await panel.locator(`[data-model="${key}"] [data-phase="${phase}"] .coarse-comparison-value strong`).textContent();
                if((systemScore?.status||m.status)==='available'&&systemScore?.n>0) assert.equal(text,systemScore.mae.toFixed(2));
                else assert.ok(['待验证','暂无结果'].includes(text));
                const metric=panel.locator(`[data-model="${key}"] [data-phase="${phase}"]`);
                for(const [field,label]of [['rmse','RMSE'],['bias','Bias'],['r2','R²']]) {
                    const actual=await metric.locator(`[data-score="${label}"] dd`).textContent();
                    const value=systemScore?.[field];
                    if((systemScore?.status||m.status)==='available'&&systemScore?.n>0) {
                        const expected=field==='r2'?(Number.isFinite(value)&&systemScore.n>=2?value.toFixed(3):'不可计算'):Number.isFinite(value)?(field==='bias'?`${value<0?'-':'+'}${Math.abs(value).toFixed(2)}`:value.toFixed(2))+' pp':'暂无结果';
                        assert.equal(actual,expected,`${system}/${key}/${phase}/${field}`);
                    } else assert.ok(['待验证','暂无结果'].includes(actual));
                }
                if(systemScore?.date_range) assert.match(await metric.locator('.coarse-comparison-time').textContent(),new RegExp(systemScore.date_range.start));
                if(Number.isFinite(systemScore?.coverage)) assert.ok((await metric.innerText()).includes(`预测覆盖率 ${(systemScore.coverage*100).toFixed(1)}%`));
                if(Number.isInteger(systemScore?.eligible_n)&&systemScore.n>0) assert.ok((await metric.innerText()).includes(`已评分 ${systemScore.n} / ${systemScore.eligible_n} 条`));
              }
            }
            await panel.locator('[data-system="3"]').click();
            result.realEvidence=realPath;result.checks.push('Real export values match desktop DOM for both models and all three phases');
        } else result.realEvidence='NOT YET AVAILABLE; screenshots below use explicitly labeled state fixture';
        const heights=await panel.locator('[data-phase]').evaluateAll(els=>els.map(e=>({phase:e.dataset.phase,top:e.getBoundingClientRect().top,height:e.getBoundingClientRect().height})));
        for(let i=0;i<3;i++){assert.ok(Math.abs(heights[i].top-heights[i+3].top)<2);assert.ok(Math.abs(heights[i].height-heights[i+3].height)<2);}
        // Static-preview startup sync is unrelated to this read-only panel; its requests are blocked above.
        await page.locator('.toast-container').evaluate(el=>el.replaceChildren());
        const desktopPanelHeight=await panel.evaluate(el=>Math.ceil(el.getBoundingClientRect().height));
        await page.setViewportSize({width:1440,height:desktopPanelHeight+220});
        await panel.scrollIntoViewIfNeeded();
        await panel.screenshot({path:path.join(output,'comparison-desktop.png')});result.screenshots.push('comparison-desktop.png');
        const desktopClip=await panel.evaluate(el=>{const r=el.getBoundingClientRect();return {x:r.x-24,y:r.y-24,width:r.width+48,height:r.height+48};});
        await page.screenshot({path:path.join(output,'comparison-presentation.png'),clip:desktopClip});result.screenshots.push('comparison-presentation.png');
        await page.setViewportSize({width:1440,height:desktopPanelHeight+550});
        await page.locator('.main-content').evaluate(el=>{el.scrollTop=0;});
        await page.screenshot({path:path.join(output,'page-desktop.png')});result.screenshots.push('page-desktop.png');
        await panel.getByRole('button',{name:'二系统 · 单列'}).click();
        await panel.screenshot({path:path.join(output,'comparison-system2.png')});result.screenshots.push('comparison-system2.png');
        await panel.getByRole('button',{name:'三系统 · 主比较'}).click();
        await page.setViewportSize({width:390,height:1000});
        await panel.scrollIntoViewIfNeeded();
        const bounds=await panel.evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth,left:el.getBoundingClientRect().left,right:el.getBoundingClientRect().right}));
        assert.ok(bounds.scroll<=bounds.width+1,JSON.stringify(bounds));
        assert.ok(bounds.right<=390&&bounds.left>=0,JSON.stringify(bounds));
        const mobileHeight = await panel.evaluate(el=>Math.ceil(el.getBoundingClientRect().height)+220);
        await page.setViewportSize({width:390,height:mobileHeight});
        await panel.scrollIntoViewIfNeeded();
        await panel.screenshot({path:path.join(output,'comparison-mobile.png')});result.screenshots.push('comparison-mobile.png');
        const mobileClip=await panel.evaluate(el=>{const r=el.getBoundingClientRect();return {x:r.x-24,y:r.y-24,width:r.width+48,height:r.height+48};});
        await page.screenshot({path:path.join(output,'comparison-mobile-presentation.png'),clip:mobileClip});result.screenshots.push('comparison-mobile-presentation.png');
        await page.locator('.main-content').evaluate(el=>{el.scrollTop=0;});
        await page.screenshot({path:path.join(output,'page-mobile.png')});result.screenshots.push('page-mobile.png');
        await panel.getByLabel('查看常规样本补充').check();
        await panel.getByRole('button',{name:'二系统 · 单列'}).click();
        assert.match(await current.innerText(),/14\/15/);
        result.checks.push('1440px paired row alignment; 390px stacked models without panel overflow; narrow-screen controls work');
        assert.deepEqual(errors,[]);
        result.pageErrors=errors; result.passed=true;
    } finally {
        fs.writeFileSync(path.join(output,'browser-verification.json'),JSON.stringify(result,null,2));
        await browser.close();
    }
    console.log(JSON.stringify(result,null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
