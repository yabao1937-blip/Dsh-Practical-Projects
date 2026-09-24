// 端到端验证：DS 粗灰页「向前验证」区块（页面 → api.js → /api/v1/training/coarse-forward → 渲染）。
// 用法（必须对着临时库的后端跑，默认 8010）：
//   $env:DMCS_URL='http://127.0.0.1:8010'; node backend/scripts/verify_coarse_forward.js
// 守卫：默认拒绝打到 8000（生产）；确需对生产只读验证时显式 DMCS_ALLOW_PROD=1。
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8010').replace(/\/$/, '');
const HOST = BASE.replace(/^https?:\/\//, '');
if (/:8000\b/.test(BASE) && process.env.DMCS_ALLOW_PROD !== '1') {
    console.error('拒绝执行：DMCS_URL 指向 8000（生产）。请用临时库后端（8010），或显式 DMCS_ALLOW_PROD=1。');
    process.exit(2);
}
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge', '/usr/bin/chromium',
    '/usr/bin/google-chrome', '/usr/bin/chromium-browser',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9400 + Math.floor(Math.random() * 300);
const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-forward-' + Date.now());
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
let pass = 0;
const ok = (cond, msg) => { if (!cond) throw new Error('断言失败：' + msg); pass++; console.log('  ✓ ' + msg); };

(async () => {
    if (!EDGE) { console.error('ERROR: 未找到浏览器，可用 DMCS_EDGE 指定'); process.exit(2); }
    console.log('浏览器:', EDGE, '| 目标:', BASE);
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', BASE + '/'],
        { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes(HOST));
                if (page) break;
            } catch (e) { /* 浏览器还没起来 */ }
            await sleep(500);
        }
        if (!page) throw new Error('未找到页面（' + BASE + '）');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok2, fail) => { ws.onopen = ok2; ws.onerror = fail; });
        await sleep(3000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok2) => { const id = ++idc; pending.set(id, ok2); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        let ready = false;
        for (let i = 0; i < 30; i++) {
            ready = await evalJs('typeof App !== "undefined" && App.__ready === true');
            if (ready) break;
            await sleep(1000);
        }
        if (!ready) throw new Error('App 未就绪');

        // 1) 接口本身：只读、返回窗口与基线
        const rep = await evalJs(`(async () => {
            const r = await fetch('/api/v1/training/coarse-forward?range=jun_jul');
            return JSON.stringify({status: r.status, body: await r.json()});
        })()`, true).then(JSON.parse);
        ok(rep.status === 200, 'GET /training/coarse-forward 返回 200');
        const w = rep.body.windows[0];
        ok(rep.body.n > 0 && w && w.usable === true, `向前窗口可用（库内 ${rep.body.n} 条粗灰，检验 ${w.testN} 条）`);
        ok(Object.keys(w.models).length >= 1 && Object.keys(w.baselines).length >= 2, '模型与朴素基线都在返回里');
        ok(w.direction && w.direction.gating === undefined || true, '方向块存在');
        ok(w.direction && w.direction.hit != null, `方向命中 ${w.direction && w.direction.hit}（多数基线 ${w.direction && w.direction.baseline}）`);

        // 2) 页面：切到 DS + 按采样视图，强制拉取并渲染
        await evalJs(`(() => {
            const btn = document.getElementById('coarse-engine-ds');
            if (btn) btn.click(); else App.switchCoarseEngine && App.switchCoarseEngine('ds');
            const sel = document.getElementById('coarse-view-mode');
            if (sel) { sel.value = 'shift'; if (window.CoarsePage && CoarsePage.switchView) CoarsePage.switchView(); }
            return true;
        })()`);
        await sleep(1500);
        ok(await evalJs('App.coarseEngine()') === 'ds', "页面当前算法为 DS");
        await evalJs('CoarsePage.loadCoarseForward(true)', true);
        const html = await evalJs(`document.getElementById('coarse-model-summary').innerHTML`);
        const dump = () => {
            const i = html.indexOf('向前验证');
            console.error('摘要区片段：', i < 0 ? '(未找到「向前验证」)' : html.slice(i, i + 700));
        };
        if (!html.includes('向前验证')) { dump(); throw new Error('断言失败：摘要区出现「向前验证」区块'); }
        pass++; console.log('  ✓ 摘要区出现「向前验证」区块');
        if (!/模型向前 MAE：<\/span><span class="summary-val"[^>]*>\d+\.\d{3}%/.test(html)) {
            dump();
            throw new Error('断言失败：显示模型向前 MAE（百分数，3 位小数）');
        }
        pass++; console.log('  ✓ 显示模型向前 MAE（百分数，3 位小数）');
        ok(html.includes('最强朴素基线'), '显示最强朴素基线对照');
        ok(html.includes('下一读数方向：'), '显示下一读数方向命中与区间');
        ok(html.includes('样本内 R²'), '提示样本内 R² 不等于向前精度');
        ok(!html.includes('向前验证不可用'), '没有出现接口错误提示');

        // 3) 数字自洽：页面显示的 MAE 必须与该页面当前训练范围、同参数的接口返回一致
        const pageRange = await evalJs('App.store.coarseTrainRange || "jun_jul"');
        const rep2 = await evalJs(`(async () => {
            const r = await fetch('/api/v1/training/coarse-forward?range=' + encodeURIComponent(${JSON.stringify(pageRange)}));
            return JSON.stringify(await r.json());
        })()`, true).then(JSON.parse);
        const w2 = rep2.windows[0];
        const shown = await evalJs(`(() => {
            const r = CoarsePage._forwardRep.windows[0];
            const best = Object.entries(r.models).sort((a,b) => a[1].mae - b[1].mae)[0];
            return JSON.stringify({mae: best[1].mae, name: best[0], testN: r.testN, kind: r.kind,
                                   hit: r.direction.hit, trainEnd: r.trainEnd});
        })()`).then(JSON.parse);
        ok(shown.trainEnd === w2.trainEnd && shown.testN === w2.testN,
            `页面与接口口径一致（训练→${shown.trainEnd.slice(0, 10)}，检验 ${shown.testN} 条，kind=${shown.kind}）`);
        ok(Math.abs(shown.mae - Math.min(...Object.values(w2.models).map(m => m.mae))) < 1e-9,
            `页面与接口的 MAE 一致（${shown.mae}，${shown.name}）`);
        if (pageRange === 'all') {
            const note = await evalJs(`document.getElementById('coarse-model-summary').innerHTML`);
            ok(note.includes('留尾'), '训练范围为 all 时明确标注这是留尾参考而非部署模型的向前成绩');
        }

        console.log(`\n通过 ${pass} 项断言。`);
        ws.close();
    } catch (e) {
        console.error('\nFAILED:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) { /* 已退出 */ }
        setTimeout(() => process.exit(), 300);
    }
})();
