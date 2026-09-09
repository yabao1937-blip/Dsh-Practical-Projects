// 导出前端 App.buildHourlyBrief() 全量结果到 JSON，供后端逐行 diff
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'file:///D:/dense-medium-density-control-system/frontend/index.html';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9376;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-brief-dump');
const OUT = process.argv[2];
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
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;
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
