// A normal browser session: no request interception, routing, mock API or injected data.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require(process.env.DMCS_PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'../..');
const output=process.env.DMCS_COMPARISON_UI_OUTPUT || path.join(root,'output/coarse-model-comparison-ui-20261103');
const url=process.env.DMCS_PREVIEW_URL || 'http://127.0.0.1:8767/';
(async()=>{
    fs.mkdirSync(output,{recursive:true});
    const response=await fetch(new URL('/api/v1/training/coarse-model-comparison',url));
    assert.equal(response.status,200);
    const evidence=await response.json();
    assert.ok(['ready','research'].includes(evidence.status));
    const browser=await chromium.launch({channel:'chrome',headless:true});
    const page=await browser.newPage({viewport:{width:1440,height:1200}});
    const requests=[],errors=[];
    page.on('request',request=>requests.push({method:request.method(),url:request.url()}));
    page.on('pageerror',error=>errors.push(error.message));
    try {
        await page.goto(url);
        const panel=page.locator('#coarse-model-comparison');
        await panel.getByRole('heading',{name:evidence.models.candidate.name,exact:true}).waitFor();
        assert.match(await page.locator('body').innerText(),/本地只读预览.*不连接现场/);
        let displayedFuturePeriod;
        if(evidence.freeze.status==='accepted') {
            displayedFuturePeriod=await panel.locator('[data-model="candidate"] [data-phase="future"] .coarse-comparison-time').textContent();
            const shanghaiTime=value=>new Date(Date.parse(value)+8*3600000).toISOString().slice(0,19).replace('T',' ').replaceAll('-','/');
            assert.ok(displayedFuturePeriod.includes(shanghaiTime(evidence.freeze.sealed_at)));
            assert.ok(displayedFuturePeriod.includes(shanghaiTime(evidence.freeze.due_at)));
            assert.match(displayedFuturePeriod,/北京时间/);
            assert.match(await panel.locator('[data-model="candidate"] header').innerText(),/尚未替换生产/);
        }
        for(const sys of ['3','2']) {
            await panel.locator(`[data-system="${sys}"]`).click();
            for(const model of ['current','candidate']) for(const phase of ['history_all','history_forward','future']) {
                const m=evidence.models[model].metrics[phase];
                const score=m.by_system[sys];
                const block=panel.locator(`[data-model="${model}"] [data-phase="${phase}"]`);
                const measured=(score?.status||m.status)==='available'&&score?.n>0;
                assert.equal(await block.locator('.coarse-comparison-value strong').textContent(),measured?score.mae.toFixed(2):phase==='future'?'待验证':'暂无结果');
                if(measured) {
                    assert.ok((await block.innerText()).includes(`已评分 ${score.n} / ${score.eligible_n} 条`));
                    for(const [field,label] of [['rmse','RMSE'],['bias','Bias'],['r2','R²']]) {
                        const value=score[field];
                        const expected=field==='r2'?(Number.isFinite(value)?value.toFixed(3):'不可计算'):(field==='bias'?`${value<0?'-':'+'}${Math.abs(value).toFixed(2)}`:value.toFixed(2))+' pp';
                        assert.equal(await block.locator(`[data-score="${label}"] dd`).textContent(),expected);
                    }
                } else assert.ok((await block.locator('.coarse-comparison-secondary dd').allTextContents()).every(text=>text==='待验证'));
            }
            if(sys==='2') {
                assert.match(await panel.locator('[data-model="candidate"] .coarse-comparison-method').innerText(),/14\/15/);
                await page.screenshot({path:path.join(output,'preview-system2.png'),fullPage:true});
            }
        }
        await panel.locator('[data-system="3"]').click();
        const desktopHeight=await panel.evaluate(el=>Math.ceil(el.getBoundingClientRect().height)+180);
        await page.setViewportSize({width:1440,height:desktopHeight});
        await page.screenshot({path:path.join(output,'preview-desktop.png'),fullPage:true});
        const desktopClip=await panel.evaluate(el=>{const r=el.getBoundingClientRect();return{x:r.x-24,y:r.y-24,width:r.width+48,height:r.height+48};});
        await page.screenshot({path:path.join(output,'preview-presentation.png'),clip:desktopClip});
        await panel.getByRole('button',{name:'刷新对比'}).click();
        await panel.getByRole('button',{name:'刷新对比'}).waitFor();
        await page.setViewportSize({width:390,height:1000});
        const noOverflow=await panel.evaluate(el=>el.scrollWidth<=el.clientWidth+1);
        assert.equal(noOverflow,true);
        await page.screenshot({path:path.join(output,'preview-mobile.png'),fullPage:true});
        assert.ok(requests.every(request=>request.method==='GET'&&new URL(request.url).origin===new URL(url).origin));
        assert.deepEqual(errors,[]);
        const result={passed:true,url,noRouteInterception:true,status:evidence.status,version:evidence.models.candidate.version,freeze:evidence.freeze,displayedFuturePeriod,artifact_sha256:evidence.artifact_sha256,requests,errors};
        fs.writeFileSync(path.join(output,'preview-verification.json'),JSON.stringify(result,null,2));
        fs.writeFileSync(path.join(output,'preview-served-comparison.json'),JSON.stringify(evidence,null,2));
        console.log(JSON.stringify(result,null,2));
    } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
