// 验证「浮精影响分析」整页已排除 2026-05 及以前的数据（现场要求 2026-09-17）。
//
// 背景：浮精只有 17 条数据，其中 5 月 5 条（05-21~05-25，煤量 26~27 t/h、灰分 8.2~10.4）
// 与 8 月以后（19.5~22.6 t/h、9.4~14.0）不是同一工况。联动趋势图 X 轴按真实时间比例分布，
// 混在一起会把轴拉到 3 个多月、中间 6~8 月留一大段空白，并把"动态波动范围"的标准差算大。
//
// 断言：
//   STORE_KEEPS_MAY            页面过滤**不改** store.floatCoal（总灰分公式/密度建议照旧用全部数据）
//   CHART_EXCLUDES_MAY         联动趋势图不含 05- 标签，且点数 = 页面口径条数
//   CHART_KEEPS_LATER          注入的 8/9 月记录出现在图上（证明不是"全滤掉了"）
//   DIST_CHART_EXCLUDES_MAY    分布图（最近12个时段）同样不含 05-
//   TABLE_EXCLUDES_MAY         明细表不含 2026-05，且行数 = 页面口径条数
//   RANGE_CARD_EXCLUDES_MAY    ±标准差 = 按页面口径算出的值，且与"含5月"的值明显不同（证明断言有牙）
//   RANGE_LABEL_HONEST         卡片副标题不再写"近24h标准差"，写明数据起点
//   MODAL_SCOPE_AND_COUNT      卡片详情写明数据范围，且"共N条" = 页面口径条数
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = (process.env.DMCS_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const URL = BASE + '/';
const HOST = BASE.replace(/^https?:\/\//, '');
const EDGE = ['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    '/usr/bin/microsoft-edge-stable', '/usr/bin/google-chrome'].find(p => p && fs.existsSync(p));
const PORT = 9300 + Math.floor(Math.random() * 400);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const fails = [];
function check(name, ok, detail) {
    console.log(`${name}: ${ok ? 'PASS' : 'FAIL'}${detail ? ' | ' + detail : ''}`);
    if (!ok) fails.push(name);
}
const api = async (p) => {
    const opts = {};
    if (process.env.DMCS_WRITE_TOKEN) opts.headers = { 'X-DMCS-Token': process.env.DMCS_WRITE_TOKEN };
    const r = await fetch(BASE + p, opts);
    if (!r.ok) throw new Error(`${p} → HTTP ${r.status}`);
    return r.json();
};

(async () => {
    if (!EDGE) { console.error('ERROR: 未找到浏览器'); process.exit(2); }
    console.log('浏览器:', EDGE, '| 目标:', URL, '| Node', process.version);
    const st = await api('/api/v1/state');
    const nonEmpty = ['coarseCoal', 'floatCoal', 'calcLogs'].some(k => (st[k] || []).length > 0);
    if (nonEmpty && process.env.DMCS_ALLOW_REAL_DB !== '1') {
        console.error('ERROR: 目标库非空，请指向临时库（或显式 DMCS_ALLOW_REAL_DB=1）');
        process.exit(2);
    }

    const UD = path.join(process.env.TEMP || '/tmp', 'dmcs-cdp-float-' + Date.now());
    const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--hide-scrollbars',
        `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
    try {
        let page;
        for (let i = 0; i < 40; i++) {
            try {
                const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
                page = l.find(t => t.type === 'page' && t.url.includes(HOST));
                if (page) break;
            } catch (e) { }
            await sleep(500);
        }
        if (!page) throw new Error('未能连上浏览器调试端口');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        let idc = 0; const pending = new Map();
        ws.onmessage = ev => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (m, p) => new Promise(ok => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method: m, params: p })); });
        const evalJs = async (expr, awaitPromise = false) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise });
            if (r.result.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || r.result.exceptionDetails.text);
            return r.result.result.value;
        };
        let ready = false, lastErr = null;
        for (let i = 0; i < 60; i++) {
            try { if (await evalJs("typeof App !== 'undefined' && App.__ready === true && !!App.store"
                    + " && typeof FloatPage !== 'undefined'")) { ready = true; break; } }
            catch (e) { lastErr = e.message; }
            await sleep(500);
        }
        if (!ready) throw new Error('页面 30 秒内未完成初始化' + (lastErr ? '；' + lastErr : ''));

        // 先走真实的导航路径（goToPage 内部 setTimeout 50ms 才建图），再等图表就绪
        await evalJs(`(() => { App.goToPage('page-float'); return true; })()`);
        await sleep(800);
        const navOk = await evalJs(`!!(typeof FloatPage !== 'undefined' && FloatPage.linkageChart && FloatPage.distChart)`);
        check('NAV_BUILDS_CHARTS', navOk === true, `点击导航后图表已建：linkageChart+distChart=${navOk}`);

        // ---------- 1) 注入一条 8 月、一条 9 月记录（5 月那批由种子数据自带）----------
        const r = JSON.parse(await evalJs(`(() => {
            // 首次进入该页会自动 init()（建图）；万一没建成就在这里显式补一次，并把异常带出来
            let initErr = null;
            if (!FloatPage.linkageChart) { try { FloatPage.init(); } catch (e) { initErr = String((e && e.message) || e); } }
            if (!FloatPage.linkageChart) return JSON.stringify({ initErr: initErr, hasChart: false });
            const start = new Date(FloatPage.FLOAT_DATA_START + 'T00:00:00').getTime();
            const tsOf = d => new Date(String((d && d.timestamp) || '').replace(' ', 'T')).getTime();
            const baseN = (App.store.floatCoal || []).length;
            // 注入两条"6 月以后"的记录；id 用字符串前缀避免与真实自增 id 冲突
            App.store.floatCoal.push({ id: 'E2E-F1', timestamp: '2026-08-25 07:00:00', ash_content: 11.0,
                                      coal_amount: 21.0, system: '合并', filter_press_running: false, annotation: '正常' });
            App.store.floatCoal.push({ id: 'E2E-F2', timestamp: '2026-09-01 07:00:00', ash_content: 10.0,
                                      coal_amount: 22.0, system: '合并', filter_press_running: false, annotation: '正常' });
            FloatPage.updateCharts(); FloatPage.updateCards(); FloatPage.renderTable();
            const all = App.store.floatCoal || [];
            const seen = new Set(); const scope = [];
            all.forEach(d => { const k = String(d.timestamp); if (seen.has(k)) return; seen.add(k);
                               if (!(isFinite(tsOf(d)) && tsOf(d) < start)) scope.push(d); });
            // 卡片口径的标准差（与 updateCards 同一公式，含/不含 5 月各算一次）
            const amt = App.INFLUENCE_HEAVY_AMT;
            const infl = arr => arr.map(d => {
                const h = App.getAshByTime(d.timestamp) ?? 8.50;
                return App.calcTotalAsh(h, amt, d.ash_content, d.coal_amount, 0, 0)
                     - App.calcTotalAsh(h, amt, 0, 0, 0, 0);
            });
            const stdOf = a => { const m = a.reduce((s, v) => s + v, 0) / a.length;
                                 return Math.sqrt(a.reduce((s, v) => s + (v - m) ** 2, 0) / a.length); };
            const uniqAll = (() => { const s = new Set(); return all.filter(d => { const k = String(d.timestamp);
                                 if (s.has(k)) return false; s.add(k); return true; }); })();
            const labels = (FloatPage.linkageChart.data.labels) || [];
            const distLabels = (FloatPage.distChart.data.labels) || [];
            const tbody = (document.getElementById('float-tbody') || {}).innerHTML || '';
            const rangeTxt = (document.getElementById('float-dynamic-range') || {}).textContent || '';
            const subTxt = (document.getElementById('float-range-sub') || {}).textContent || '';
            FloatPage.showCardDetail('range');
            const modal = (document.getElementById('modal-body') || {}).innerHTML || '';
            App.closeModal();
            return JSON.stringify({
                initErr: initErr, hasChart: true,
                start: FloatPage.FLOAT_DATA_START,
                baseN: baseN, storeN: all.length,
                mayInStore: all.filter(d => String(d.timestamp || '').slice(0, 7) === '2026-05').length,
                scopeN: scope.length, allN: uniqAll.length,
                labels: labels, distLabels: distLabels,
                rowCount: (tbody.match(/<tr>/g) || []).length,
                tbodyHasMay: tbody.includes('2026-05'),
                rangeTxt: rangeTxt.replace(/\\s+/g, ''),
                subTxt: subTxt,
                stdScope: +stdOf(infl(scope)).toFixed(3),
                stdAll: +stdOf(infl(uniqAll)).toFixed(3),
                modal: modal,
            });
        })()`));
        console.log('  数据: store', r.storeN, '条（其中 5 月', r.mayInStore, '条）→ 页面口径', r.scopeN, '条；'
            + '全量', r.allN, '条。σ(页面)=' + r.stdScope + ' σ(含5月)=' + r.stdAll);
        console.log('  图标签:', JSON.stringify(r.labels), '分布图:', JSON.stringify(r.distLabels));

        check('STORE_KEEPS_MAY', r.mayInStore > 0 && r.allN > r.scopeN,
            `store 内 5 月记录仍为 ${r.mayInStore} 条（页面口径 ${r.scopeN} 条 < 全量 ${r.allN} 条）`);
        check('CHART_EXCLUDES_MAY',
            r.labels.length === r.scopeN && !r.labels.some(l => String(l).startsWith('05-')),
            `图点数=${r.labels.length} 期望=${r.scopeN}；含 05- 标签=${r.labels.some(l => String(l).startsWith('05-'))}`);
        check('CHART_KEEPS_LATER',
            r.labels.some(l => String(l).startsWith('08-25')) && r.labels.some(l => String(l).startsWith('09-01')),
            `标签=${JSON.stringify(r.labels)}`);
        check('DIST_CHART_EXCLUDES_MAY', !r.distLabels.some(l => String(l).startsWith('05-')),
            `分布图标签=${JSON.stringify(r.distLabels)}`);
        check('TABLE_EXCLUDES_MAY',
            r.tbodyHasMay === false && r.rowCount === Math.min(30, r.scopeN),
            `表内出现 2026-05=${r.tbodyHasMay} 行数=${r.rowCount} 期望=${Math.min(30, r.scopeN)}`);
        check('RANGE_CARD_EXCLUDES_MAY',
            r.rangeTxt === `±${r.stdScope.toFixed(3)}%` && Math.abs(r.stdAll - r.stdScope) > 1e-6,
            `卡片=${r.rangeTxt} 期望=±${r.stdScope.toFixed(3)}%（含5月会是 ±${r.stdAll.toFixed(3)}%，两者不同=${Math.abs(r.stdAll - r.stdScope) > 1e-6}）`);
        check('RANGE_LABEL_HONEST',
            r.subTxt.includes('2026-06') && !r.subTxt.includes('近24h'),
            `副标题="${r.subTxt}"`);
        check('MODAL_SCOPE_AND_COUNT',
            r.modal.includes('2026-06-01 起') && r.modal.includes(`共${r.scopeN}条`),
            `弹窗含数据范围=${r.modal.includes('2026-06-01 起')} 共N条=${r.modal.includes(`共${r.scopeN}条`)}`);

        // ---------- 2) 收尾：移除注入的记录并同步回服务器 ----------
        const cleaned = JSON.parse(await evalJs(`(() => {
            App.store.floatCoal = (App.store.floatCoal || []).filter(d => String(d.id).indexOf('E2E-F') !== 0);
            FloatPage.updateCharts(); FloatPage.updateCards(); FloatPage.renderTable();
            App.saveStore();
            return JSON.stringify({ storeN: App.store.floatCoal.length,
                                    labels: (FloatPage.linkageChart.data.labels || []).length });
        })()`));
        check('CLEANUP_OK', cleaned.storeN === r.baseN,
            `注入已移除：store ${cleaned.storeN} 条（原 ${r.baseN} 条），图点数 ${cleaned.labels}`);

        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        fails.push('EXCEPTION');
    } finally {
        try { edge.kill(); } catch (e) { }
        console.log(fails.length ? `FLOAT_MAY_EXCLUDED: FAIL (${fails.join(', ')})` : 'FLOAT_MAY_EXCLUDED: PASS');
        process.exitCode = fails.length ? 1 : 0;
        setTimeout(() => process.exit(), 300);
    }
})();
