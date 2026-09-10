// 端到端验证:「从服务器恢复数据」+ K 展示 + 导入幂等(headless Edge + CDP)
// 场景 = 用户旧浏览器(全新 profile → 种子旧数据 113/5/124)在 http:// 模式下:
//   1. 按钮可见性
//   2. pullFromServer → 本地变 156/17/360
//   3. 重导同一粗精煤泥文件 → 计数不变(幂等)
//   4. 密度指导面板 K 来源展示
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9381;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-verify-pull');
const XLSX_DIR = 'C:\\Users\\25925\\Desktop\\web(2)导入数据\\新';
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('no page');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2500);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text || 'eval error');
            return r.result.result.value;
        };

        const storeCounts = () => evalJs(`JSON.stringify({
            coarse: App.store.coarseCoal.length, float: App.store.floatCoal.length,
            calcLogs: (App.store.calcLogs || []).length,
            model: App.store.coarseModel ? App.store.coarseModel.production + '/n=' + App.store.coarseModel.n : 'none',
        })`);

        console.log('[1] 初始(种子):', await storeCounts());
        const btnVisible = await evalJs(`(() => { const b = document.getElementById('btn-pull-server'); return !!b && b.style.display !== 'none'; })()`);
        console.log('[2] 按钮可见(http模式):', btnVisible ? 'PASS' : 'FAIL');

        // 拉取服务器数据(confirm 自动确认)
        await evalJs('window.confirm = () => true;');
        await evalJs('(async () => { await App.pullFromServer(); return true; })()', true);
        await sleep(800);
        const pulled = JSON.parse(await storeCounts());
        console.log('[3] pullFromServer 后:', JSON.stringify(pulled));
        const pullOk = pulled.coarse === 156 && pulled.float === 17 && pulled.calcLogs === 360;
        console.log('    数据恢复:', pullOk ? 'PASS (156/17/360)' : 'FAIL');
        console.log('    生产模型:', pulled.model);

        // K 来源展示(密度指导面板)
        const kShow = await evalJs(`(() => {
            const g = App.computeDensityGuidance(App.store.ashTarget != null ? App.store.ashTarget : 8.50);
            return JSON.stringify({ K: g.K, kSource: g.kSource, kInfo: g.kInfo });
        })()`);
        console.log('[4] 指导计算 K:', kShow);

        // 幂等:重导同一粗精煤泥文件
        const file = path.join(XLSX_DIR, '粗精煤泥灰分影响因素 9-4.xlsx');
        const b64 = fs.readFileSync(file).toString('base64');
        await evalJs(`(async () => {
            const bytes = Uint8Array.from(atob(${JSON.stringify(b64)}), c => c.charCodeAt(0));
            ImportPage.processFile(new File([bytes], '粗精煤泥灰分影响因素 9-4.xlsx'));
            return true;
        })()`, true);
        let parsed = false;
        for (let i = 0; i < 60; i++) { parsed = await evalJs('!!(ImportPage.parsedFactors && ImportPage.parsedFactors.length)'); if (parsed) break; await sleep(300); }
        if (!parsed) throw new Error('解析超时');
        await evalJs('(async () => { await ImportPage.confirmImportFactors(); return true; })()', true);
        await sleep(1000);
        const after = JSON.parse(await storeCounts());
        console.log('[5] 重导后:', JSON.stringify(after));
        console.log('    幂等性:', after.coarse === 156 ? 'PASS (仍 156)' : 'FAIL (' + after.coarse + ')');

        // localStorage 持久化确认
        const ls = await evalJs(`(() => { const d = JSON.parse(localStorage.getItem('dmcs_store')); return d.coarseCoal.length + '/' + d.floatCoal.length + '/' + d.calcLogs.length; })()`);
        console.log('[6] localStorage:', ls);

        const allPass = btnVisible && pullOk && after.coarse === 156;
        console.log(allPass ? 'ALL PASS' : 'HAS FAILURES');
        ws.close();
        process.exitCode = allPass ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
