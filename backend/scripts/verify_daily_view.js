// 端到端验证:粗精煤泥「按日」聚合视图 + 日级模型(切按日→图表/卡片/表格/因子→切回)
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

// 与 verify_decision_log.js 同一套环境变量惯例:DMCS_URL 切目标(临时库后端),
// DMCS_EDGE 指定浏览器;候选列表覆盖 Windows/Windows-x64/Linux 常见路径
const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
// 注意:本文件的 `const URL` 是字符串,会**遮蔽**全局 URL 类,不能写 new URL(URL) —— 直接剥协议前缀
const HOST = BASE.replace(/^https?:\/\//, '');   // 形如 127.0.0.1:8000
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge', '/usr/bin/chromium',
    '/usr/bin/google-chrome', '/usr/bin/chromium-browser',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);   // 随机端口:避免连到遗留的调试浏览器
const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-daily-' + Date.now());
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
    if (!EDGE) {
        console.error('ERROR: 未找到浏览器可执行文件,请用环境变量 DMCS_EDGE 指定');
        process.exit(2);
    }
    console.log('浏览器:', EDGE, '| 目标:', URL);
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes(HOST)); if (page) break; } catch (e) {}
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

        // 页面测试只改当前一次性浏览器的内存，不向现场或临时数据库播种。
        // 默认使用版本管理夹具，允许指定只读记录文件复验现场数据。
        const fixturePath = process.env.DMCS_COARSE_FIXTURE || path.join(__dirname, '../data/seed_store.json');
        const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
        const fixtureRows = Array.isArray(fixture) ? fixture : fixture.coarseCoal;
        await evalJs(`(() => {
            App.saveStore = () => {};
            App.store.coarseCoal = ${JSON.stringify(fixtureRows)};
            App.store.coarseTrainRange = 'all';
            App.trainCoarseModel('all', 'gpt');
            App.trainCoarseModel('all', 'ds');
            return true;
        })()`);

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

        let allOk = true;
        const samplePredictions = {};
        for (const engine of ['ds', 'gpt', 'ds']) {
        await evalJs(`document.getElementById('coarse-engine-${engine}').click()`);
        for (let i = 0; i < 30; i++) {
            if (await evalJs(`!CoarsePage._training && App.coarseEngine() === '${engine}'`)) break;
            await sleep(200);
        }
        const selected = await evalJs(`document.getElementById('coarse-engine-${engine}').getAttribute('aria-pressed')`);
        if (selected !== 'true') throw new Error(engine + ' 按钮未选中');
        const prediction = await evalJs('CoarsePage._pred(App.store.coarseCoal[0])');
        if (samplePredictions[engine] !== undefined && samplePredictions[engine] !== prediction) throw new Error('来回切换改写了预测');
        samplePredictions[engine] = prediction;
        console.log('模型版本:', engine.toUpperCase());
        // 1. 采样级(默认)
        const hourly = JSON.parse(await evalJs(`JSON.stringify({
            n: CoarsePage.trendChart.data.labels.length,
            actualN: CoarsePage.trendChart.data.datasets[0].data.filter(v => v != null).length
        })`));
        console.log('采样级:', JSON.stringify(hourly));

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
                validationN: dm && dm[dm.production].metrics.pipelineValidation
                    ? dm[dm.production].metrics.pipelineValidation.n : 0,
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
        const groupedNote = await evalJs(`document.getElementById('coarse-model-summary').textContent.includes('GPT 留整日选参参考')`);
        if (groupedNote !== (engine === 'gpt')) throw new Error('GPT 选参说明与所选版本不一致');
        const coverageNote = await evalJs(`document.getElementById('coarse-model-summary').textContent.includes('GPT 工况覆盖')`);
        if (coverageNote !== (engine === 'gpt')) throw new Error('工况覆盖说明与所选版本不一致');

        // 6. 切回按采样
        await evalJs(`(() => {
            document.getElementById('coarse-view-mode').value = 'shift';
            CoarsePage.switchView();
            return true;
        })()`);
        await sleep(1000);
        const backN = await evalJs('CoarsePage.trendChart.data.labels.length');
        const backSub = await evalJs(`document.getElementById('coarse-fit-sub').innerHTML`);
        console.log('切回: n=' + backN, '(恢复采样级:', !backSub.includes('日级'), ')');

        // 验证页面口径和独立验证存在，不把训练集 R² 越高当作正确性。
        allOk = allOk && daily.n > 0 && daily.n < hourly.n && Number.isFinite(daily.r2) && (engine === 'ds' || daily.validationN > 0)
                     && daily.actualN === daily.n && daily.predN === daily.n && backN === hourly.n
                     && fitSub.includes('日级') && tableRows > 0 && !backSub.includes('日级');
        }
        allOk = allOk && Math.abs(samplePredictions.ds - samplePredictions.gpt) > 1e-6;
        if (process.env.DMCS_SCREENSHOT) {
            const shot = await send('Page.captureScreenshot', {format: 'png'});
            fs.writeFileSync(process.env.DMCS_SCREENSHOT, Buffer.from(shot.result.data, 'base64'));
        }
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
