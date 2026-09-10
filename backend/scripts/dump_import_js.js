// 导出前端 ImportPage.parseCoarseFactors() 的解析结果到 JSON，供后端逐条 diff。
//
// 背景：brief / training / resolvers / seed 都有 dump_*_js.js 跨语言 golden，
// 唯独导入解析（parseCoarseFactors / importer.parse_coarse_factors）没有 ——
// 而这正是日期补偿、列定位、异常行跳过等最容易双轨分叉的地方。
//
// FIXTURE 覆盖的语义（改任一侧必须同步 tests/test_importer.py 的 GOLDEN_ROWS）：
//   · 文本两位小数 = 显式日：6.02→2 日、6.10→10 日、6.16→16 日
//   · 单小数位「省尾零」补偿：6.3 → 30 日
//   · 序列消歧 + 跨月回退：6.29 → 6.3(=6月30) → 6.4(=7月4日,不是 6月4日)
//   · 数值型取整按 Math.round：6.125 → 13 日
//   · 畸形日期单元格："6-16(早班)" 不抛异常、时间戳回退原始文本
//   · 多行工作面取首行、开关列、开关脱粉、坏灰分行跳过
//
// 用法：node scripts/dump_import_js.js [输出路径]   （默认 backend/data/import_js.json，需 Edge）
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const URL = 'file:///D:/dense-medium-density-control-system/frontend/index.html';
const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const PORT = 9378;
const UD = path.join(process.env.TEMP, 'dmcs-cdp-import-dump');
const OUT = process.argv[2] || path.join(__dirname, '..', 'data', 'import_js.json');
if (fs.existsSync(UD)) fs.rmSync(UD, { recursive: true, force: true });

// 与 tests/test_importer.py 的 GOLDEN_ROWS 逐字一致
const GOLDEN_ROWS = [
    ['粗精煤泥的灰分影响因素'],
    ['日期', '序号', '时间', '入洗工作面', '原煤灰分（%）', '小时带煤量（t/h）',
     '开启的系统', null, null, null, '脱粉', null, '315灰分（%）', '315全水分（%）', '精磁尾液位（%）'],
    [null, null, null, null, null, null, 'A', 'B', '401', '402', '473', '474'],
    ['6.16', 1, '09:31:00', '3309\n43下01', 31.82, 927, 1, 1, 0, 1, '开', '停', 13.86, 24.5, 55],
    ['6.29', 2, '08:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.0, 25.0, 55],
    ['6.3', 3, '09:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.1, 25.0, 55],
    ['6.4', 4, '10:00:00', '3309', 32.0, 900, 1, 1, 0, 1, '开', '开', 12.2, 25.0, 55],
    ['6.02', 5, '11:00:00', '3309', 32.0, 900, 1, 1, 0, 1, '开', '开', 12.3, 25.0, 55],
    ['6.10', 6, '12:00:00', '3309', 32.0, 900, 1, 1, 0, 1, '开', '开', 12.4, 25.0, 55],
    [6.125, 7, '13:00:00', '3309', 32.0, 900, 1, 1, 0, 1, '开', '开', 12.5, 25.0, 55],
    ['6-16(早班)', 8, '14:00:00', '3309', 32.0, 900, 1, 1, 0, 1, '开', '开', 12.6, 25.0, 55],
    ['8.23', 9, '10:36:00', '6303\n3309', 38.22, 785, 1, 1, 1, 1, '开', '开', 11.7, 28.3, 74],
    ['8.23', 10, '11:00:00', '6303\n3309', 38.22, 700, 1, 0, 1, 0, '停', '停', '？', 26.0, 60],
];

const edge = spawn(EDGE, ['--headless=new', '--disable-gpu', '--no-first-run', '--allow-file-access-from-files', `--user-data-dir=${UD}`, `--remote-debugging-port=${PORT}`, '--window-size=1680,1200', URL], { stdio: 'ignore' });
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// 年份 fixture：12 月 → 次年 1 月（跨年），再跟一条自带年份的 ISO 写法。
// 与 tests/test_importer.py 的 YEAR_ROWS 逐字一致。
const YEAR_ROWS = [
    ['粗精煤泥的灰分影响因素'],
    ['日期', '序号', '时间', '入洗工作面', '原煤灰分（%）', '小时带煤量（t/h）',
     '开启的系统', null, null, null, '脱粉', null, '315灰分（%）', '315全水分（%）', '精磁尾液位（%）'],
    [null, null, null, null, null, null, 'A', 'B', '401', '402', '473', '474'],
    ['12.30', 1, '08:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.0, 25.0, 55],
    ['1.1', 2, '09:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.1, 25.0, 55],
    ['1.2', 3, '10:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.2, 25.0, 55],
    ['2027-03-05', 4, '11:00:00', '3309', 31.8, 900, 1, 1, 0, 1, '开', '开', 12.3, 25.0, 55],
];

// 坏表头 fixture：既无「原煤灰分」也无「液位」→ 两侧都应报同一条错误、零记录。
// 与 tests/test_importer.py 的 BAD_HEADER_ROWS 逐字一致。
const BAD_HEADER_ROWS = [
    ['某某表'],
    ['日期', '灰分', '煤量'],
    ['6.16', 12.5, 900],
];

(async () => {
    try {
        let page;
        for (let i = 0; i < 30; i++) {
            try { const l = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); page = l.find(t => t.type === 'page' && t.url.includes('index.html')); if (page) break; } catch (e) {}
            await sleep(500);
        }
        if (!page) throw new Error('page not found');
        const ws = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((ok, fail) => { ws.onopen = ok; ws.onerror = fail; });
        await sleep(2000);
        let idc = 0; const pending = new Map();
        ws.onmessage = (ev) => { const m = JSON.parse(ev.data); if (pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
        const send = (method, params) => new Promise((ok) => { const id = ++idc; pending.set(id, ok); ws.send(JSON.stringify({ id, method, params })); });
        const evalJs = async (expr) => {
            const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
            if (r.result.exceptionDetails) {
                const d = r.result.exceptionDetails;
                throw new Error('page threw: ' + ((d.exception && d.exception.description) || d.text));
            }
            return r.result.result.value;
        };
        // 只取 records / errors：previewHeaders / previewRows 是纯 UI 结构，后端无对应物
        const dumped = JSON.parse(await evalJs(
            `JSON.stringify((() => {
                const run = rows => { const r = ImportPage.parseCoarseFactors(rows);
                    return { records: r.records, errors: r.errors }; };
                const main = run(${JSON.stringify(GOLDEN_ROWS)});
                const year = run(${JSON.stringify(YEAR_ROWS)});
                const bad = run(${JSON.stringify(BAD_HEADER_ROWS)});
                return { records: main.records, errors: main.errors,
                         yearRecords: year.records, yearErrors: year.errors,
                         badHeaderRecords: bad.records, badHeaderErrors: bad.errors };
            })())`));
        fs.writeFileSync(OUT, JSON.stringify(dumped, null, 1));
        console.log('records:', dumped.records.length, 'errors:', dumped.errors.length);
        dumped.records.forEach(r => console.log('  ', r.timestamp, '| ash', r.ash_content, '| face', JSON.stringify(r.mining_face)));
        console.log('yearRecords:', dumped.yearRecords.map(r => r.timestamp).join(' , '));
        console.log('badHeader: records=%d errors=%s', dumped.badHeaderRecords.length, JSON.stringify(dumped.badHeaderErrors));
        ws.close();
    } catch (e) {
        console.error('ERROR:', e.message);
        process.exitCode = 1;
    } finally {
        try { edge.kill(); } catch (e) {}
        setTimeout(() => process.exit(), 300);
    }
})();
