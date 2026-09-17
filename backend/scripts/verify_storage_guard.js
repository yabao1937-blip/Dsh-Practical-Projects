// 端到端验证:本地存储容量防线(app.js saveStore / storageHealth)
// 场景:
//  0) 正常态:状态区显示体积读数(good)
//  A) setItem 首次抛 QuotaExceededError 且存在待发镜像槽 → 释放镜像槽后写回成功(不报失败)
//  B) setItem 持续抛 → failCount>0、"写入失败"红字、横幅可见、应用数据完好
//  C) 恢复 setItem → 再保存回归正常(failCount 清零、体积读数恢复),且落盘内容可解析
// 依赖:后端处于运行中(DMCS_URL 指定,默认 127.0.0.1:8000)
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL0 = BASE + '/';
const HOST = BASE.replace(/^https?:\/\//, '');
const CANDIDATES = [
    process.env.DMCS_EDGE,
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/microsoft-edge',
    '/usr/bin/google-chrome', '/usr/bin/chromium',
];
const EDGE = CANDIDATES.find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);
const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-storageguard-' + Date.now());
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
    if (!EDGE) {
        console.error('ERROR: 未找到浏览器可执行文件，请用环境变量 DMCS_EDGE 指定');
        process.exit(2);
    }
    console.log('浏览器:', EDGE, '| 目标:', URL0);
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL0], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes(HOST)); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('未找到调试页面(浏览器未起来?)');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };

        // 等页面初始化完成
        let appReady = false;
        for (let i = 0; i < 60; i++) {
            try { if (await evalJs("typeof App !== 'undefined' && !!App.store && !!App.store.coarseCoal")) { appReady = true; break; } } catch (e) {}
            await sleep(500);
        }
        if (!appReady) throw new Error('页面 30 秒内未完成初始化');

        const results = [];
        const check = (name, ok, detail) => { results.push(ok); console.log((ok ? 'PASS' : 'FAIL'), name, detail || ''); };

        // ---------- 0) 正常态 ----------
        await evalJs("App.saveStore(); App._renderStorageHealth(); true");
        const s0 = JSON.parse(await evalJs(`JSON.stringify({
            txt: document.getElementById('status-storage').textContent,
            cls: document.getElementById('status-storage').className,
            fail: App.storageHealth.failCount })`));
        check('0 正常态显示体积', /KB|MB/.test(s0.txt) && /good/.test(s0.cls) && s0.fail === 0, JSON.stringify(s0));

        // ---------- A) 首次抛错 + 有镜像槽 → 释放写回 ----------
        await evalJs(`(function(){
            window.__origSetItem = localStorage.setItem.bind(localStorage);
            let throwsLeft = 1;                       // 只让第一次 dmcs_store 写入抛
            localStorage.setItem = function(k, v) {
                if (k === 'dmcs_store' && throwsLeft > 0) {
                    throwsLeft--;
                    const e = new Error('quota stress'); e.name = 'QuotaExceededError'; throw e;
                }
                return window.__origSetItem(k, v);
            };
            try { window.__origSetItem('dmcs_mirror_pending', JSON.stringify({ wall: Date.now() + 1e9, stamp: 'guard-test', body: '{"x":1}' })); } catch (e) {}
            return true; })()`);
        await evalJs("App.saveStore(); true");
        const a = JSON.parse(await evalJs(`JSON.stringify({
            fail: App.storageHealth.failCount,
            pending: localStorage.getItem('dmcs_mirror_pending'),
            stored: !!localStorage.getItem('dmcs_store') })`));
        check('A 释放镜像槽后写回成功', a.fail === 0 && a.pending === null && a.stored, JSON.stringify(a));

        // ---------- B) 持续抛错 ----------
        await evalJs(`localStorage.setItem = function(k, v) {
            if (k === 'dmcs_store') { const e = new Error('quota stress'); e.name = 'QuotaExceededError'; throw e; }
            return window.__origSetItem(k, v); }; true`);
        await evalJs("App.saveStore(); App._renderStorageHealth(); true");
        const b = JSON.parse(await evalJs(`JSON.stringify({
            fail: App.storageHealth.failCount,
            txt: document.getElementById('status-storage').textContent,
            cls: document.getElementById('status-storage').className,
            banner: !document.getElementById('alert-banner').classList.contains('hidden'),
            dataAlive: (App.store.coarseCoal || []).length >= 1 })`));
        check('B 持续失败:计数/红字/横幅/应用存活',
            b.fail >= 1 && /写入失败/.test(b.txt) && /error/.test(b.cls) && b.banner && b.dataAlive,
            JSON.stringify(b));

        // ---------- C) 恢复 ----------
        await evalJs("localStorage.setItem = window.__origSetItem; App.saveStore(); App._renderStorageHealth(); true");
        const c = JSON.parse(await evalJs(`JSON.stringify({
            fail: App.storageHealth.failCount,
            txt: document.getElementById('status-storage').textContent,
            cls: document.getElementById('status-storage').className })`));
        const d = await evalJs("(JSON.parse(localStorage.getItem('dmcs_store')).coarseCoal || []).length");
        check('C 恢复后回归正常且落盘可解析', c.fail === 0 && /KB|MB/.test(c.txt) && /good/.test(c.cls) && d >= 1,
            JSON.stringify({ ...c, storedCoarse: d }));

        const allOk = results.every(Boolean);
        console.log(allOk ? 'STORAGE_GUARD: PASS' : 'STORAGE_GUARD: FAIL');
        ws.close();
        process.exitCode = allOk ? 0 : 1;
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
