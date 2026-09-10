// 验证前端 JS filterTrainRows 的 30d/all 计数与后端一致
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9379;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-range-verify');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2500);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;
        const out = JSON.parse(await evalJs(`JSON.stringify({jun: App.filterTrainRows('jun_jul').length, d30: App.filterTrainRows('30d').length, all: App.filterTrainRows('all').length})`));
        console.log('JS filterTrainRows:', JSON.stringify(out));
        // 2026-09-10 重基线:7.10 重复行清除后 jun=113(与源文件有效行一致),
        // d30=39(8.23~9.3 真实窗口,8.30 灰分列损坏被正确拒绝), all=152
        const EXPECT = { jun: 113, d30: 39, all: 152 };
        console.log('expect:', JSON.stringify(EXPECT));
        process.exitCode = (out.jun === EXPECT.jun && out.d30 === EXPECT.d30 && out.all === EXPECT.all) ? 0 : 1;
        ws.close();
    } catch (e) { console.error('ERROR:', e.message); process.exitCode = 1; }
    finally { try { edge.kill(); } catch (e) {} setTimeout(() => process.exit(), 300); }
})();
