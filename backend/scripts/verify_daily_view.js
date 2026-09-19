// 修复版:等 App 完全就绪后再操作
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9391;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-daily2');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(5000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        // 等 App 就绪
        let ready = false;
        for (let i = 0; i < 30; i++) {
            ready = await evalJs('typeof App !== "undefined" && App.__ready === true');
            if (ready) break;
            await sleep(1000);
        }
        if (!ready) throw new Error('App 未就绪');

        // 导航到粗精煤泥分析页(默认在总览页,CoarsePage 未初始化)
        await evalJs(`(() => {
            document.querySelector('[data-page="page-coarse"]').click();
            return true;
        })()`);
        for (let i = 0; i < 15; i++) {
            ready = await evalJs('typeof CoarsePage !== "undefined" && CoarsePage.trendChart !== null');
            if (ready) break;
            await sleep(1000);
        }
        if (!ready) throw new Error('CoarsePage/trendChart 未就绪');
        console.log('粗精煤泥页就绪 ✓');

        // 1. 小时级(默认)
        const hourly = JSON.parse(await evalJs(`JSON.stringify({
            n: CoarsePage.trendChart.data.labels.length,
            actualN: CoarsePage.trendChart.data.datasets[0].data.filter(v => v != null).length
        })`));
        console.log('小时级:', JSON.stringify(hourly));

        // 2. 切换到按日
        await evalJs(`(() => {
            document.getElementById('coarse-view-mode').value = 'day';
            CoarsePage.switchView();
            return true;
        })()`);
        await sleep(2000);

        const daily = JSON.parse(await evalJs(`(() => {
            const dm = CoarsePage._ensureDailyModel();
            const labels = CoarsePage.trendChart.data.labels;
            const ds = CoarsePage.trendChart.data.datasets;
            const m = dm ? CoarsePage._dailyMetrics(dm) : null;
            return JSON.stringify({
                n: labels.length,
                actualN: ds[0].data.filter(v => v != null).length,
                predN: ds[1].data.filter(v => v != null).length,
                production: dm ? dm.production : null,
                modelN: dm ? dm.n : 0,
                r2: m ? +m.r2.toFixed(3) : null,
                passRate: m ? +m.passRate.toFixed(1) : null,
            });
        })()`));
        console.log('日级:', JSON.stringify(daily));

        // 3. 卡片
        const fitR2 = await evalJs(`document.getElementById('coarse-fit-r2').textContent`);
        const fitSub = await evalJs(`document.getElementById('coarse-fit-sub').innerHTML`);
        console.log('卡片: R²=' + fitR2, '| 副标题含日级:', fitSub.includes('日级'));

        // 4. 表格
        const tableRows = await evalJs(`document.getElementById('coarse-tbody').rows.length`);
        const firstCell = await evalJs(`(document.getElementById('coarse-tbody').rows[0] || {cells:[{textContent:''}]}).cells[0].textContent`);
        console.log('表格: ' + tableRows + ' 行, 首行=' + firstCell);

        // 5. 因子权重
        const factorLabels = await evalJs(`JSON.stringify(CoarsePage.factorChart.data.labels.slice(0,3))`);
        console.log('因子前3:', factorLabels);

        // 6. 切回按班次
        await evalJs(`(() => {
            document.getElementById('coarse-view-mode').value = 'shift';
            CoarsePage.switchView();
            return true;
        })()`);
        await sleep(1000);
        const backN = await evalJs('CoarsePage.trendChart.data.labels.length');
        const backSub = await evalJs(`document.getElementById('coarse-fit-sub').innerHTML`);
        console.log('切回: n=' + backN, '(恢复小时级:', !backSub.includes('日级'), ')');

        const allOk = daily.n > 0 && daily.n < hourly.n && daily.r2 && daily.r2 > 0.5
                     && fitSub.includes('日级') && tableRows > 0 && !backSub.includes('日级');
        console.log(allOk ? '\nALL PASS' : '\nHAS FAILURES');
        ws.close();
        process.exitCode = allOk ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
