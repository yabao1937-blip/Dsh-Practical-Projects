// 验证 AI 助手前端组件:按钮存在/面板开关/一次真实问答
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9384;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-ai');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
(async () => {
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('127.0.0.1')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(3000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };
        const hasBtn = await evalJs('!!document.getElementById("ai-assistant-btn")');
        console.log('悬浮按钮存在:', hasBtn ? 'PASS' : 'FAIL');
        await evalJs('Assistant.toggle(true); true');
        const shown = await evalJs('document.getElementById("ai-assistant-panel").classList.contains("shown")');
        console.log('面板打开:', shown ? 'PASS' : 'FAIL');
        // 一次真实问答(流式)
        const out = await evalJs(`(async () => {
            Assistant.elements.input.value = '用一句话说明你能做什么';
            await Assistant.send();
            return (Assistant.history[Assistant.history.length - 1] || {}).content || '';
        })()`, true);
        console.log('问答返回:', String(out).slice(0, 160));
        console.log(String(out).includes('解释') && String(out).length > 20 ? 'AI问答: PASS' : 'AI问答: FAIL');
        ws.close();
        process.exitCode = (hasBtn && shown) ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
