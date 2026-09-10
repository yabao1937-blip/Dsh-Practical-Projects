// CDP 验证：推测简报——三表1h对齐、16列表头、常量列、建议密度/模型推测列、弹窗按钮
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'http://127.0.0.1:8000/';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9375;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-profile-brief');
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
        const check = (name, ok, detail) => { console.log(`${ok ? '✓' : '✗'} ${name}: ${detail}`); if (!ok) okAll = false; };

        const info = JSON.parse(await evalJs(`(() => {
            const b = App.buildHourlyBrief();
            const rows = b.rows;
            const uniq = i => Array.from(new Set(rows.map(r => r[i])));
            const rhoFilled = rows.filter(r => r[12] !== '').length;
            const inRange = rows.filter(r => r[12] !== '' && +r[12] >= 1.35 && +r[12] <= 1.60).length;
            const densityFilled = rows.filter(r => r[13] !== '').length;
            const modelFilled = rows.filter(r => r[14] !== '').length;
            const measuredFilled = rows.filter(r => r[15] !== '').length;
            const firstMeasuredIdx = rows.findIndex(r => r[15] !== '');
            const firstMeasured = firstMeasuredIdx >= 0 ? rows[firstMeasuredIdx] : null;
            // 2026-09 行存在规则(三表齐全才成行)后,行允许时间缺口(如某表断档>24h的时段):
            // 检查改为"时间严格递增且为整点",不再要求无缝连续
            const monotonicOk = rows.length >= 2 && (() => {
                const toH = s => Math.floor(new Date(s.replace(' ', 'T')).getTime() / 3600000);
                for (let i = 1; i < rows.length; i++) if (toH(rows[i][0]) <= toH(rows[i - 1][0])) return false;
                return true;
            })();
            return JSON.stringify({
                n: rows.length, hLen: b.headers.length,
                rowLenOk: rows.every(r => r.length === 16),
                timeOk: rows.every(r => r[0].endsWith(':00')),
                monotonicOk,
                h0: b.headers[0], hRho: b.headers[12], hDensity: b.headers[13], hModel: b.headers[14], hMeasured: b.headers[15],
                coarseAmtUniq: uniq(3), scale501Uniq: uniq(1), scale502Uniq: uniq(2),
                heavyUniq: uniq(4), totalAmtUniq: uniq(5),
                rhoFilled, inRange, densityFilled, modelFilled, measuredFilled,
                firstMeasured, baseOk: firstMeasured ? (firstMeasured[14] === firstMeasured[15]) : false,
                first: rows[0], last: rows[rows.length - 1]
            });
        })()`));

        check('表头16列', info.hLen === 16, `hLen=${info.hLen}`);
        check('存在对齐行', info.n > 0, `n=${info.n}`);
        check('每行16列', info.rowLenOk, 'rowLenOk=' + info.rowLenOk);
        check('时间列整点(:00)', info.timeOk, 'timeOk=' + info.timeOk);
        check('时间严格递增(规则允许缺口)', info.monotonicOk, `n=${info.n}, first=${info.first[0]}, last=${info.last[0]}`);
        check('首列=时间', info.h0 === '时间', info.h0);
        check('建议密度列', info.hRho === '建议密度(g/cm³)', info.hRho);
        check('实测密度列', info.hDensity === '实测密度(g/cm³)', info.hDensity);
        check('预测粗灰列', info.hModel === '预测粗精煤泥灰分(%)', info.hModel);
        check('实测粗灰列', info.hMeasured === '实测粗精煤泥灰分(%)', info.hMeasured);

        check('粗精煤泥量恒值40', JSON.stringify(info.coarseAmtUniq) === '["40.0"]', JSON.stringify(info.coarseAmtUniq));
        check('501皮带秤恒值268.5', JSON.stringify(info.scale501Uniq) === '["268.5"]', JSON.stringify(info.scale501Uniq));
        check('502皮带秤恒值235.2', JSON.stringify(info.scale502Uniq) === '["235.2"]', JSON.stringify(info.scale502Uniq));
        check('重介灰分恒值8.50', JSON.stringify(info.heavyUniq) === '["8.50"]', JSON.stringify(info.heavyUniq));
        check('总精煤量恒值503.7', JSON.stringify(info.totalAmtUniq) === '["503.7"]', JSON.stringify(info.totalAmtUniq));

        check('每行都有建议密度且在[1.35,1.60]', info.rhoFilled === info.n && info.inRange === info.n, `filled=${info.rhoFilled}/${info.n}`);
        check('每行都有预测粗灰', info.modelFilled === info.n, `modelFilled=${info.modelFilled}/${info.n}`);
        check('实测粗灰稀疏(有采样才填)', info.measuredFilled > 0 && info.measuredFilled < info.n, `measuredFilled=${info.measuredFilled}/${info.n}`);
        check('第一次采样=预测基值', info.baseOk, `firstMeasured=${JSON.stringify(info.firstMeasured)}`);

        console.log('首行:', JSON.stringify(info.first));
        console.log('末行:', JSON.stringify(info.last));

        // 按钮 + 弹窗
        const modal = JSON.parse(await evalJs(`(() => {
            ImportPage.generateBrief();
            const ov = document.getElementById('modal-overlay');
            const title = document.getElementById('modal-title').textContent;
            const btn = document.getElementById('page-import').querySelector('button[onclick*="generateBrief"]');
            return JSON.stringify({ overlayShown: !ov.classList.contains('hidden'), title, hasBtn: !!btn });
        })()`));
        check('弹窗打开', modal.overlayShown, JSON.stringify(modal.title));
        check('弹窗标题=推测简报', modal.title.includes('推测简报'), modal.title);
        check('导入页有生成按钮', modal.hasBtn, 'hasBtn=' + modal.hasBtn);

        console.log(okAll ? 'RESULT: PASS - 推测简报功能生效' : 'RESULT: FAIL');
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
