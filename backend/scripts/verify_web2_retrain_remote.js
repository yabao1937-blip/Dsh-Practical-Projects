// 验证：前端（后端托管 http://127.0.0.1:8000）重训练走后端接口并正确落本地
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9378;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-retrain-verify');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });

const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
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
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.text || 'eval error');
            return r.result.result.value;
        };

        const base = JSON.parse(await evalJs(`JSON.stringify({n: App.store.coarseCoal.length, factoryPls: App.store.coarseModel && App.store.coarseModel.pls.intercept})`));
        console.log('base:', JSON.stringify(base));

        const expr = `(async () => {
            const ok = await App.retrainCoarseModelAsync('jun_jul');
            return JSON.stringify({
                ok,
                production: App.store.coarseModel && App.store.coarseModel.production,
                plsIntercept: App.store.coarseModel && App.store.coarseModel.pls && App.store.coarseModel.pls.intercept,
                mlrLambda: App.store.coarseModel && App.store.coarseModel.mlr && App.store.coarseModel.mlr.lambda,
                plsA: App.store.coarseModel && App.store.coarseModel.pls && App.store.coarseModel.pls.A,
                predicted0: App.store.coarseCoal[0] && App.store.coarseCoal[0].predicted_ash,
                historyLen: (App.store.coarseModelHistory || []).length,
            });
        })()`;
        const out = JSON.parse(await evalJs(expr, true));
        console.log('result:', JSON.stringify(out, null, 2));

        // 判定：2026-09-09 重基线(日期消歧修复+156条数据+jun_jul n=117)。
        // 当前后端数据训练的期望值(sklearn 版,range=jun_jul):
        //   production=mlr, mlr.lambda≈11.6, pls.A=2, pls.intercept≈22.7339, predicted[0]≈14.1708
        const pls = out.plsIntercept;
        const okRemote = Math.abs(pls - 22.733940594) < 1e-3;
        console.log('REMOTE_TRAINED:', okRemote ? 'PASS' : 'FAIL', '(pls.intercept=' + pls + ')');
        console.log('production:', out.production, '| mlr.lambda:', out.mlrLambda, '| pls.A:', out.plsA);
        console.log('predicted[0]:', out.predicted0, '(期望 14.1708)');
        ws.close();
        process.exitCode = (okRemote && out.production === 'mlr'
            && Math.abs(out.mlrLambda - 11.6) < 1e-6
            && out.plsA === 2
            && Math.abs(out.predicted0 - 14.1708) < 1e-9) ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
