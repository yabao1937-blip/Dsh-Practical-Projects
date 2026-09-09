// 验证：局域网 IP + 全新浏览器(空 localStorage) 下，种子数据与推测简报是否正常
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const URL = 'http://10.92.25.147:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9381;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-lan-brief-check');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('10.92.25.147')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('no page');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(3000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true })).result.result.value;

        const out = JSON.parse(await evalJs(`JSON.stringify({
            protocol: window.location.protocol,
            merged: App.store.__merged,
            coarseCoal: (App.store.coarseCoal || []).length,
            floatCoal: (App.store.floatCoal || []).length,
            calcLogs: (App.store.calcLogs || []).length,
            seedLoaded: !!window.DMCS_SEED_STORE,
            briefRows: (() => { try { return App.buildHourlyBrief().rows.length; } catch (e) { return 'ERR:' + e.message; } })(),
        })`));
        console.log(JSON.stringify(out, null, 2));
        ws.close();
    } catch (e) { console.error('ERROR:', e.message); process.exitCode = 1; }
    finally { try { edge.kill(); } catch (e) {} setTimeout(() => process.exit(), 300); }
})();
