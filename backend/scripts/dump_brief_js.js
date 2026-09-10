// 导出前端 App.buildHourlyBrief() 全量结果到 JSON，供后端逐行 diff。
// 2026-09 起:向页面注入与 tests/test_brief.py fixture_store() 完全一致的合成 store
// (种子数据三表时间窗不重叠,新规则下产出 0 行,无法作为对拍载体)。
// 改 FIXTURE 必须同步 tests/test_brief.py 的 fixture_store()。
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'file:///D:/dense-medium-density-control-system/frontend/index.html';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9376;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-brief-dump');
const OUT = process.argv[2];
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });

const ZERO_MODEL_NODE = {
    type: 'pls', intercept: 10.0,
    coefs: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0], means: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    stds: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1], stdCoef: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    imputeMeans: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    A: 1, metrics: {}, n: 3, tolerance: 0.8, range: 'fixture',
};
const FIXTURE = {
    coarseCoal: [
        { timestamp: '2026-08-01 08:10:00', ash_content: 12.5, coal_amount: 800, level: 55, raw_ash: 40.0 },
        { timestamp: '2026-08-01 12:00:00', ash_content: 13.1, coal_amount: 810, level: 57, raw_ash: 40.0 },
        { timestamp: '2026-08-02 08:00:00', ash_content: 12.8, coal_amount: 790, level: 54, raw_ash: 40.0 },
    ],
    floatCoal: [
        { timestamp: '2026-08-01 07:30:00', ash_content: 9.5, coal_amount: 30.0 },
        { timestamp: '2026-08-02 12:00:00', ash_content: 9.8, coal_amount: 32.0 },
    ],
    calcLogs: [
        { timestamp: '2026-08-01 08:05:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.5, density: 1.45 }) },
        { timestamp: '2026-08-01 22:00:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.6, density: 1.46 }) },
        { timestamp: '2026-08-02 09:00:00', calc_type: 'ash_density',
          input_json: JSON.stringify({ system: 'A', belt: '502', ash_content: 8.4, density: 1.47 }) },
    ],
    coarseModel: {
        production: 'pls',
        pls: Object.assign({}, ZERO_MODEL_NODE),
        mlr: Object.assign({}, ZERO_MODEL_NODE, { type: 'mlr' }),
        trainedAt: '2026-08-01 00:00:00', n: 3, tolerance: 0.8, range: 'fixture',
    },
};

const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('index.html')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;
        // 注入合成 store(仅内存,不落盘),再导出简报
        await evalJs(`App.store = ${JSON.stringify(FIXTURE)}; true`);
        const brief = JSON.parse(await evalJs(`JSON.stringify(App.buildHourlyBrief())`));
        fs.writeFileSync(OUT, JSON.stringify(brief));
        console.log('rows:', brief.rows.length, 'first:', JSON.stringify(brief.rows[0]), 'last:', JSON.stringify(brief.rows[brief.rows.length - 1]));
        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
