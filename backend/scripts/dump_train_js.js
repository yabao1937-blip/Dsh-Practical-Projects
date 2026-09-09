// 导出前端 App.trainCoarseModel('jun_jul') 全量结果到 JSON，供后端训练移植逐值对拍。
// 用法：node dump_train_js.js <out.json>
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'file:///D:/dense-medium-density-control-system/frontend/index.html';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9377;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-train-dump');
const OUT = process.argv[2] || path.join(__dirname, '..', 'data', 'train_js.json');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });

const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('index.html')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('no page');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;

        // 训练前基线：确认 coarseCoal 行数与范围
        const base = JSON.parse(await evalJs(`JSON.stringify({n: App.store.coarseCoal.length, range: App.store.coarseTrainRange, tol: App.store.coarseTolerance})`));

        // 运行训练并抓取全量结果（trainedAt 时间戳非确定，Python 侧忽略）
        const expr = `(() => {
            const ok = App.trainCoarseModel('jun_jul');
            return JSON.stringify({
                ok,
                features: App.MLR_FEATURES,
                coarseModel: App.store.coarseModel,
                history: App.store.coarseModelHistory,
                coarseCoal: App.store.coarseCoal
            });
        })()`;
        const out = JSON.parse(await evalJs(expr));
        out._base = base;
        fs.writeFileSync(OUT, JSON.stringify(out));
        console.log('base:', JSON.stringify(base));
        console.log('ok:', out.ok, 'production:', out.coarseModel && out.coarseModel.production,
            'mlr.lambda:', out.coarseModel.mlr.lambda, 'pls.A:', out.coarseModel.pls.A,
            'mlr.drop:', JSON.stringify(out.coarseModel.mlr.drop), 'pls.drop:', JSON.stringify(out.coarseModel.pls.drop),
            'mlr.q2Time:', out.coarseModel.mlr.metrics.q2Time, 'pls.q2Time:', out.coarseModel.pls.metrics.q2Time);
        console.log('mlr.intercept:', out.coarseModel.mlr.intercept, 'pls.intercept:', out.coarseModel.pls.intercept);
        console.log('predicted[0]:', out.coarseCoal[0].predicted_ash, 'predicted[last]:', out.coarseCoal[out.coarseCoal.length - 1].predicted_ash);
        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
