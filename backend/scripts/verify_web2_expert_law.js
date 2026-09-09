// CDP 验证：专家经验调整算法——0.15%→0.01、0.25%→0.02、分段插值/外推、容差0.1
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9374;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-profile42');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });

const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1:8000')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('page not found');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2000);
        let idc = 0;
        const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;

        let okAll = true;
        const check = (name, ok, detail) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${name}: ${detail}`); if (!ok) okAll = false; };

        await evalJs(`App.toggleTotalAshManual(); 'ok'`);
        const guideAt = async (manual) => JSON.parse(await evalJs(`(() => {
            App.setAshInput('totalAsh', { manual: ${manual} });
            const g = App.computeDensityGuidance(App.store.ashTarget ?? 8.5);
            return JSON.stringify({ dA: g.deltaA, dRho: g.deltaRho, rhoNew: g.rhoNew, rhoCur: g.rhoCur, dir: g.direction, tol: App.store.ashTargetTol });
        })()`));

        const tol = JSON.parse(await evalJs(`JSON.stringify(App.store.ashTargetTol)`));
        check('默认容差迁移为0.1', tol === 0.1, `tol=${tol}`);

        let g = await guideAt(8.65);
        check('偏差0.15% → 下调0.01', Math.abs(g.dRho + 0.01) < 0.0005 && g.dir === 'down', JSON.stringify(g));

        g = await guideAt(8.75);
        check('偏差0.25% → 下调0.02', Math.abs(g.dRho + 0.02) < 0.0005 && g.dir === 'down', JSON.stringify(g));

        g = await guideAt(8.58);
        check('偏差0.08% ≤ 容差 → 保持', g.dRho === 0 && g.dir === 'stable', JSON.stringify(g));

        g = await guideAt(8.20);
        check('偏差0.30%(外推) → 上调0.025', Math.abs(g.dRho - 0.025) < 0.0005 && g.dir === 'up', JSON.stringify(g));

        g = await guideAt(9.0);
        check('偏差0.50% → 下调0.045', Math.abs(g.dRho + 0.045) < 0.0005 && g.dir === 'down', JSON.stringify(g));

        console.log(okAll ? 'RESULT: PASS - 专家经验调整算法生效' : 'RESULT: FAIL');
        process.exitCode = okAll ? 0 : 1;
        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
