/* ========================================
   页面五：批量数据导入
   - Excel(.xlsx/.csv) 导入三表数据，导入后自动训练 MLR/PLS 模型
   ======================================== */

const ImportPage = {
    parsedData: null,
    rawData: null,
    fileName: '',
    briefResult: null,

    init() {
        this.bindDropZone();
        this.loadLogs();
    },

    bindDropZone() {
        const zone = document.getElementById('file-drop-zone');
        zone.addEventListener('click', () => document.getElementById('import-file').click());
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            zone.classList.add('dragover');
        });
        zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
        zone.addEventListener('drop', (e) => {
            e.preventDefault();
            zone.classList.remove('dragover');
            const file = e.dataTransfer.files[0];
            if (file) this.processFile(file);
        });
    },

    onFileSelected(input) {
        const file = input.files[0];
        if (file) this.processFile(file);
    },

    processFile(file) {
        if (!file.name.match(/\.(xlsx|xls|csv)$/i)) {
            App.showToast('请选择Excel或CSV文件', 'error');
            return;
        }
        this.fileName = file.name;
        document.getElementById('import-file-info').textContent = `已选择：${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
        document.getElementById('btn-import').disabled = false;

        const reader = new FileReader();
        reader.onload = async (e) => {
            try {
                const wb = await XLSX.read(e.target.result, { type: 'array' });

                // 表1 粗精煤泥多因素：支持多Sheet（新表按Sheet分月存放），
                // 每个Sheet跳过 标题/表头/子表头 行，只取数据行合并解析
                const hasCoarseSheet = wb.SheetNames.some(name => {
                    const sd = XLSX.utils.sheet_to_json(wb.Sheets[name], { header: 1 }) || [];
                    return sd.slice(0, 12).some(r => (r || []).some(c => c && String(c).includes('原煤灰分')));
                });
                if (hasCoarseSheet) {
                    let allRows = [];
                    wb.SheetNames.forEach(name => {
                        const sd = XLSX.utils.sheet_to_json(wb.Sheets[name], { header: 1 }) || [];
                        const hIdx = sd.slice(0, 12).findIndex(r => (r || []).some(c => c && String(c).includes('原煤灰分')));
                        if (hIdx < 0) return;
                        const dataRows = sd.slice(hIdx + 2).filter(r => r.length > 0 && r.some(c => c !== null && c !== '' && c !== undefined));
                        if (allRows.length === 0) {
                            allRows = [sd[hIdx], sd[hIdx + 1], ...dataRows];   // 表头 + 子表头 + 数据
                        } else {
                            allRows = allRows.concat(dataRows);
                        }
                    });
                    this.rawData = allRows;
                    const catSel = document.getElementById('import-category');
                    if (catSel.value !== 'coarse_factors') {
                        catSel.value = 'coarse_factors';
                        App.showToast('已自动识别数据类别：粗精煤泥多因素（合并 ' + wb.SheetNames.length + ' 个Sheet）', 'info');
                    }
                    const r = this.parseCoarseFactors(this.rawData);
                    this.parsedFactors = r.records;
                    this.parsedData = { headers: r.previewHeaders, rows: r.previewRows, errors: r.errors, duplicates: [] };
                    if (r.records.length === 0) {
                        App.showToast('未识别到多因素数据，请确认表头含"原煤灰分/315灰分/精磁尾液位"', 'warning');
                    }
                    this.showPreview();
                    return;
                }

                // 收集所有有效Sheet的数据，合并为统一数据集
                let allRaw = null;
                let headers = null;
                let sheetCount = 0;
                wb.SheetNames.forEach(name => {
                    const ws = wb.Sheets[name];
                    const sheetData = XLSX.utils.sheet_to_json(ws, { header: 1 });
                    if (sheetData.length < 2) return;
                    // 检测是否有有效表头（包含"时间"关键字）
                    const firstRow = sheetData[0] || [];
                    const hasHeader = firstRow.some(c => c && String(c).includes('时间'));
                    if (!hasHeader) return;
                    // 找到"系统"列的索引
                    const sysIdx = firstRow.findIndex(h => h && String(h).includes('系统'));
                    // 提取数据行
                    const dataRows = sheetData.slice(1).filter(r => r.length > 0 && r.some(c => c !== null && c !== '' && c !== undefined));
                    // 从同一Sheet已有数据中推断系统名（取第一个非空非'-'的系统列值）
                    let sheetSystem = name;
                    if (sysIdx >= 0) {
                        for (const r of dataRows) {
                            const v = r[sysIdx];
                            if (v !== null && v !== undefined && v !== '' && v !== '-') {
                                sheetSystem = v;
                                break;
                            }
                        }
                        // 用推断的系统名填充缺失行
                        dataRows.forEach(r => {
                            const v = r[sysIdx];
                            if (v === null || v === undefined || v === '' || v === '-') {
                                r[sysIdx] = sheetSystem;
                            }
                        });
                    }
                    if (!allRaw) {
                        allRaw = [firstRow, ...dataRows];
                        headers = firstRow;
                    } else {
                        allRaw = allRaw.concat(dataRows);
                    }
                    sheetCount++;
                });

                if (!allRaw || allRaw.length < 2) {
                    this.rawData = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { header: 1 });
                    this.parsedData = this.parseRawData(this.rawData);
                } else {
                    this.rawData = allRaw;
                    this.parsedData = this.parseRawData(this.rawData);
                    if (sheetCount > 1) {
                        document.getElementById('import-file-info').textContent +=
                            ` （已读取 ${sheetCount} 个Sheet）`;
                    }
                }
                // 按表头自动识别数据类别（避免选错类别导致数据进错表）
                const detectCategory = hdr => {
                    const h = (hdr || []).map(x => String(x || ''));
                    if (h.some(x => x.includes('原煤灰分') || x.includes('315'))) return 'coarse_factors';
                    if (h.some(x => x.includes('压滤机'))) return 'float_ash';
                    if (h.some(x => x.includes('密度'))) return 'ash_density';
                    if (h.some(x => x.includes('液位'))) return 'coarse_magnetic';
                    return null;
                };
                const headRow = (this.rawData && this.rawData[0]) || [];
                const autoCat = detectCategory(headRow);
                const catNames = { coarse_factors: '粗精煤泥多因素', float_ash: '浮精', ash_density: '灰分、密度', coarse_magnetic: '粗精煤泥、精磁尾' };
                if (autoCat) {
                    const sel = document.getElementById('import-category');
                    if (sel.value !== autoCat) {
                        sel.value = autoCat;
                        App.showToast(`已自动识别数据类别：${catNames[autoCat] || autoCat}`, 'info');
                    }
                }
                // 粗精煤泥多因素历史表：用专用解析器（按列位/关键字识别全部因子）
                if (document.getElementById('import-category').value === 'coarse_factors') {
                    const r = this.parseCoarseFactors(this.rawData);
                    this.parsedFactors = r.records;
                    this.parsedData = { headers: r.previewHeaders, rows: r.previewRows, errors: r.errors, duplicates: [] };
                    if (r.records.length === 0) {
                        App.showToast('未识别到多因素数据，请确认表头含“原煤灰分/315灰分/精磁尾液位”', 'warning');
                    }
                }
                this.showPreview();
            } catch (err) {
                App.showToast('文件解析失败：' + err.message, 'error');
            }
        };
        reader.readAsArrayBuffer(file);
    },

    parseRawData(raw) {
        if (raw.length < 2) return { headers: [], rows: [], errors: [], duplicates: [] };

        // 检测标题行：如果第一行只有1-2个非空单元格且不包含"时间"关键字，视为标题行跳过
        let startIdx = 0;
        const firstRow = raw[0] || [];
        const nonEmpty = firstRow.filter(c => c !== null && c !== '' && c !== undefined);
        if (nonEmpty.length <= 2 && !firstRow.some(c => c && String(c).includes('时间'))) {
            startIdx = 1;
        }

        const headers = raw[startIdx] || [];
        const rows = raw.slice(startIdx + 1).filter(r => r.length > 0 && r.some(c => c !== null && c !== ''));
        const errors = [];
        const duplicates = [];

        const category = document.getElementById('import-category').value;
        const validate = document.getElementById('import-validate').checked;
        const checkAbnormal = document.getElementById('import-abnormal-check').checked;

        rows.forEach((row, i) => {
            if (validate) {
                // 基本格式校验
                row.forEach((cell, j) => {
                    if (headers[j] && headers[j].includes('时间') && cell) {
                        if (isNaN(Date.parse(cell)) && typeof cell !== 'number') {
                            errors.push({ row: i + 2, col: j + 1, msg: `时间格式异常: ${cell}` });
                        }
                    }
                    if (headers[j] && (headers[j].includes('灰分') || headers[j].includes('密度') ||
                        headers[j].includes('煤量') || headers[j].includes('液位')) && cell !== null && cell !== '') {
                        const cellStr = String(cell);
                        // 跳过占位符(？)和说明性文字(长度超过15的文本)
                        if (cellStr === '？' || cellStr === '?' || cellStr === '-' || cellStr.length > 15) return;
                        if (isNaN(parseFloat(cell))) {
                            errors.push({ row: i + 2, col: j + 1, msg: `数值格式异常: ${cell}` });
                        }
                    }
                });
            }

            if (checkAbnormal) {
                row.forEach((cell, j) => {
                    const val = parseFloat(cell);
                    if (isNaN(val)) return;
                    if (headers[j] && headers[j].includes('灰分') && (val < 5 || val > 35)) {
                        errors.push({ row: i + 2, col: j + 1, msg: `灰分值异常(${val}%)，合理范围5-35%` });
                    }
                    if (headers[j] && headers[j].includes('密度') && (val < 1.0 || val > 2.0)) {
                        errors.push({ row: i + 2, col: j + 1, msg: `密度值异常(${val})，合理范围1.0-2.0` });
                    }
                    if (headers[j] && headers[j].includes('液位') && (val < 0 || val > 100)) {
                        errors.push({ row: i + 2, col: j + 1, msg: `液位值异常(${val}%)，合理范围0-100%` });
                    }
                });
            }
        });

        // 去重检测
        if (document.getElementById('import-dedup').checked) {
            const rule = document.getElementById('import-dedup-rule').value;
            const seen = new Map();
            rows.forEach((row, i) => {
                let key;
                if (rule === 'timestamp') {
                    const tsIdx = headers.findIndex(h => h && h.includes('时间'));
                    const sysIdx = headers.findIndex(h => h && h.includes('系统'));
                    const fmtTs = (v) => String(v);
                    if (tsIdx >= 0 && sysIdx >= 0) {
                        key = fmtTs(row[tsIdx]) + '|' + String(row[sysIdx]);
                    } else if (tsIdx >= 0) {
                        key = fmtTs(row[tsIdx]);
                    } else {
                        key = row.join('|');
                    }
                } else {
                    key = row.join('|');
                }
                if (seen.has(key)) {
                    duplicates.push(i);
                } else {
                    seen.set(key, i);
                }
            });
        }

        return { headers, rows, errors, duplicates };
    },

    // 解析粗精煤泥多因素历史表（自动识别表头列位，提取全部扰动因子）
    parseCoarseFactors(raw) {
        const errors = [], records = [];
        if (!raw || raw.length < 3) return { records, errors, previewHeaders: [], previewRows: [] };

        // 1. 定位主表头行（含“原煤灰分”，回退“液位”）
        let hIdx = -1;
        for (let i = 0; i < Math.min(raw.length, 12); i++) {
            if ((raw[i] || []).some(c => c && String(c).includes('原煤灰分'))) { hIdx = i; break; }
        }
        if (hIdx < 0) {
            for (let i = 0; i < Math.min(raw.length, 12); i++) {
                if ((raw[i] || []).some(c => c && String(c).includes('液位'))) { hIdx = i; break; }
            }
        }
        if (hIdx < 0) {
            errors.push({ row: 0, msg: '未识别到多因素表头（需含“原煤灰分/315灰分/精磁尾液位”）' });
            return { records, errors, previewHeaders: [], previewRows: [] };
        }
        const hRow = raw[hIdx] || [];
        const subRow = raw[hIdx + 1] || [];

        // 2. 列索引：主表头按关键字
        const findCol = (kw, not) => {
            for (let j = 0; j < hRow.length; j++) {
                const c = String(hRow[j] || '');
                if (c.includes(kw) && !(not && c.includes(not))) return j;
            }
            return -1;
        };
        const col = {
            raw_ash: findCol('原煤灰分'), coal_amount: findCol('煤量'),
            ash: findCol('灰分', '原'), moisture: findCol('水分'),
            level: findCol('液位'), time: findCol('时间'), face: findCol('入洗工作面')
        };
        if (col.ash < 0) { for (let j = 0; j < hRow.length; j++) if (String(hRow[j] || '').includes('315')) col.ash = j; }
        // 系统/脱粉：在子表头行找 A/B/401/402/473/474
        const findSub = kw => { for (let j = 0; j < subRow.length; j++) if (String(subRow[j] || '').includes(kw)) return j; return -1; };
        col.sysA = findSub('A'); col.sysB = findSub('B');
        col.sys401 = findSub('401'); col.sys402 = findSub('402');
        col.des473 = findSub('473'); col.des474 = findSub('474');

        const dateCol = 0, timeCol = col.time >= 0 ? col.time : 2;
        const toNum = v => {
            if (v === null || v === undefined || v === '') return NaN;
            const n = parseFloat(String(v).trim());
            return isNaN(n) ? NaN : n;
        };
        const parseDes = v => {
            const s = String(v || '').trim();
            if (s === '开' || s === '1') return 1;
            if (s === '停' || s === '关' || s === '0') return 0;
            const n = toNum(v); return isNaN(n) ? 0 : n;
        };
        const pad = n => String(n).padStart(2, '0');
        const extractTime = tc => {
            if (tc === null || tc === undefined) return '00:00:00';
            if (typeof tc === 'number') {                 // Excel 时间序列（小数 = 占当天的比例）
                if (tc < 0) return '00:00:00';
                const frac = tc - Math.floor(tc);         // 纯时间<1；含日期的序列取时间分量
                const tot = Math.round(frac * 86400);
                return `${pad(Math.floor(tot / 3600))}:${pad(Math.floor((tot % 3600) / 60))}:${pad(tot % 60)}`;
            }
            const s = (tc instanceof Date) ? tc.toTimeString().slice(0, 8) : String(tc);
            const mt = s.match(/(\d{1,2}):(\d{2})(?::(\d{2}))?/);   // 兼容 "09:31:00" / "1899-12-30 09:31:00"
            return mt ? `${pad(+mt[1])}:${pad(+mt[2])}:${pad(+(mt[3] || 0))}` : '00:00:00';
        };
        // 任务四：手写表日期 M.DD 语义补偿（个位天数省略尾零：6.3=6月30日、7.4=7月4日、6.1=6月1日）
        // 规则与种子生成脚本一致：dd100==10→1；1~31→原值；否则÷10
        const fixDay = (m, dd100) => {
            if (dd100 === 10) return { m, d: 1 };
            if (dd100 >= 1 && dd100 <= 31) return { m, d: dd100 };
            return { m, d: Math.floor(dd100 / 10) };
        };
        const extractDate = dc => {
            if (dc === null || dc === undefined) return null;
            if (typeof dc === 'number') {
                if (dc >= 1000) {   // Excel 日期序列（1900纪元）
                    const d = new Date(Math.round((dc - 25569) * 86400 * 1000));
                    return isNaN(d) ? null : { m: d.getMonth() + 1, d: d.getDate() };
                }
                const m = Math.floor(dc);
                const dd100 = Math.round((dc - m) * 100);   // 6.16→16；6.3→30；7.4→40
                if (dd100 <= 0) return null;                // 纯月份数字无日期
                return fixDay(m, dd100);
            }
            const s = String(dc);
            const iso = s.match(/(\d{4})[-\/.](\d{1,2})[-\/.](\d{1,2})/);   // "2026-06-16"
            if (iso) return { m: +iso[2], d: +iso[3] };
            const dot = s.match(/^(\d{1,2})\.(\d{1,2})$/);                  // "6.16" / "6.3"
            if (dot) {
                const m = +dot[1], frac = +dot[2];
                return fixDay(m, frac < 10 ? frac * 10 : frac);
            }
            const p = s.split(/[\/\-]/);                                    // "6/16" / "6-16"
            if (p.length >= 2 && +p[0] >= 1 && +p[0] <= 12) return { m: +p[0], d: +p[1] };
            return null;
        };
        // 「M.f 单个小数位」歧义识别:6.3 可能是 6月30(省尾零)也可能是 6月3;
        // 数值型单个小数位必然 %10==0(6.3→30),字符串型直接匹配一位小数
        const ambiguousDate = dc => {
            if (dc === null || dc === undefined || dc instanceof Date) return null;
            if (typeof dc === 'number') {
                if (dc >= 1000) return null;
                const m = Math.floor(dc);
                const fracDd = Math.round((dc - m) * 100);
                if (fracDd % 10 === 0 && fracDd >= 10 && fracDd <= 90) return [m, fracDd / 10];
                return null;
            }
            const mt = String(dc).trim().match(/^(\d{1,2})\.(\d)$/);
            return mt ? [+mt[1], +mt[2]] : null;
        };
        // 带序列上下文的日期解析(与后端 importer.extract_date_seq 一致):
        // 候选={f, f*10},取「不早于前一行日期的最小候选」——
        // 6.29 后的 6.3→30;9.1 后的 9.2→2(旧规则会错解析成 9.20)
        const extractDateSeq = (dc, prev) => {
            const legacy = extractDate(dc);
            if (!legacy) return null;
            const amb = ambiguousDate(dc);
            if (!amb || !prev) return legacy;
            const [m, f] = amb;
            const cands = [...new Set([f, f * 10])].sort((a, b) => a - b);
            for (const d of cands) {
                if (d >= 1 && d <= 31 && (m > prev.m || (m === prev.m && d >= prev.d))) return { m, d };
            }
            return legacy;
        };
        const parseTs = (dc, tc, prevMd) => {
            const md = extractDateSeq(dc, prevMd);
            const t = extractTime(tc);
            return md ? `2026-${pad(md.m)}-${pad(md.d)} ${t}` : `${String(dc || '').trim()} ${t}`;
        };
        const systemInfo = row => {
            const on = [];
            if (col.sys401 >= 0 && toNum(row[col.sys401])) on.push('401');
            if (col.sys402 >= 0 && toNum(row[col.sys402])) on.push('402');
            if (col.sysA >= 0 && toNum(row[col.sysA])) on.push('A');
            if (col.sysB >= 0 && toNum(row[col.sysB])) on.push('B');
            return on;
        };

        let lastRawAsh = null;
        let lastMd = null;   // 上一行解析出的 {m,d},供 extractDateSeq 消歧
        const dataRows = raw.slice(hIdx + 2).filter(r => r && r.some(c => c !== null && c !== '' && c !== undefined));
        const previewRows = [];
        dataRows.forEach((row) => {
            const md = extractDateSeq(row[dateCol], lastMd);
            if (md) lastMd = md;
            const ash = col.ash >= 0 ? toNum(row[col.ash]) : NaN;
            if (isNaN(ash)) return;                       // 无灰分行跳过
            if (ash < 1 || ash > 40) return;              // 异常灰分跳过（如时间误填成1899-12-30被读成1899）
            let rawAsh = col.raw_ash >= 0 ? toNum(row[col.raw_ash]) : NaN;
            if (isNaN(rawAsh)) { rawAsh = lastRawAsh; } else { lastRawAsh = rawAsh; }   // 原煤灰分前向填充
            const coal = col.coal_amount >= 0 ? toNum(row[col.coal_amount]) : NaN;
            const coalSafe = isNaN(coal) ? 0 : coal;
            const level = col.level >= 0 ? toNum(row[col.level]) : NaN;
            const moist = col.moisture >= 0 ? toNum(row[col.moisture]) : NaN;
            const onSys = systemInfo(row);
            const rec = {
                timestamp: parseTs(row[dateCol], row[timeCol], lastMd),
                system: '合并',   // 界面合并显示；系统开关内部保留在 sysA..sys402
                ash_content: +ash.toFixed(4),
                coal_amount: isNaN(coal) ? 0 : +coal,
                level: isNaN(level) ? null : +level,
                raw_ash: (rawAsh === null || isNaN(rawAsh)) ? null : +rawAsh,
                moisture: isNaN(moist) ? null : +moist,
                sysA: col.sysA >= 0 ? (toNum(row[col.sysA]) || 0) : 0,
                sysB: col.sysB >= 0 ? (toNum(row[col.sysB]) || 0) : 0,
                sys401: col.sys401 >= 0 ? (toNum(row[col.sys401]) || 0) : 0,
                sys402: col.sys402 >= 0 ? (toNum(row[col.sys402]) || 0) : 0,
                desliming473: col.des473 >= 0 ? parseDes(row[col.des473]) : 0,
                desliming474: col.des474 >= 0 ? parseDes(row[col.des474]) : 0,
                is_stoppage: (coalSafe <= 10) ? 1 : 0,
                mining_face: col.face >= 0 ? String(row[col.face] || '').replace(/&#10;/g, '\n').split(/\r?\n/)[0].trim() : ''
            };
            records.push(rec);
            previewRows.push([
                rec.timestamp, rec.system, rec.raw_ash === null ? '-' : rec.raw_ash.toFixed(2),
                rec.coal_amount.toFixed(0), rec.ash_content.toFixed(2),
                rec.moisture === null ? '-' : rec.moisture.toFixed(1),
                rec.level === null ? '-' : rec.level.toFixed(1),
                onSys.includes('A') ? 1 : 0, onSys.includes('B') ? 1 : 0,
                onSys.includes('401') ? 1 : 0, onSys.includes('402') ? 1 : 0,
                rec.desliming473, rec.desliming474, rec.mining_face
            ]);
        });

        const previewHeaders = ['时间', '系统', '原煤灰分', '带煤量', '灰分%', '全水分', '液位%', 'A', 'B', '401', '402', '473', '474', '工作面'];
        return { records, errors, previewHeaders, previewRows };
    },

    // 确认导入：粗精煤泥多因素（写完整 coarseCoal + 镜像 magneticTail，导入后自动训练）
    async confirmImportFactors() {
        const recs = this.parsedFactors || [];
        if (recs.length === 0) { App.showToast('无可导入的多因素数据', 'warning'); return; }
        const heavyAsh = 8.50, heavyAmt = 250, fAsh = 0, fAmt = 0;
        const baseTotal = App.calcTotalAsh(heavyAsh, heavyAmt, fAsh, fAmt, 0, 0);
        let imported = 0;
        recs.forEach(rec => {
            // 同时间戳覆盖（幂等重导入表1，避免重复记录）
            App.store.coarseCoal = App.store.coarseCoal.filter(c => c.timestamp !== rec.timestamp);
            App.store.magneticTail = App.store.magneticTail.filter(m => m.timestamp !== rec.timestamp);
            const curTotal = App.calcTotalAsh(heavyAsh, heavyAmt, fAsh, fAmt, rec.ash_content, rec.coal_amount);
            App.store.coarseCoal.push({
                id: App.store.coarseCoal.length + 1,
                timestamp: rec.timestamp, system: '合并', source: 'import',
                ash_content: rec.ash_content, coal_amount: rec.coal_amount, level: rec.level || 0,
                raw_ash: rec.raw_ash, moisture: rec.moisture,
                sysA: rec.sysA, sysB: rec.sysB, sys401: rec.sys401, sys402: rec.sys402,
                desliming473: rec.desliming473, desliming474: rec.desliming474, is_stoppage: rec.is_stoppage,
                mining_face: rec.mining_face,
                influence_value: +(curTotal - baseTotal).toFixed(3)
            });
            // 镜像到 magneticTail，供单因素密度模型向后兼容
            App.store.magneticTail.push({
                id: App.store.magneticTail.length + 1, timestamp: rec.timestamp, system: rec.system,
                level: rec.level || 0, ash_content: rec.ash_content, moisture: rec.moisture || 0,
                coal_amount: rec.coal_amount, source: 'import'
            });
            const disp = `多因素:原煤${rec.raw_ash === null ? '-' : rec.raw_ash.toFixed(2)}%,带煤${rec.coal_amount.toFixed(0)},灰分${rec.ash_content.toFixed(2)}%,液位${rec.level === null ? '-' : rec.level.toFixed(1)}%`;
            App.store.manualEntries.push({
                id: App.store.manualEntries.length + 1, timestamp: rec.timestamp, category: 'magnetic_tail',
                values: { display: disp }, remark: '批量导入', operator: '批量导入', status: '已导入'
            });
            imported++;
        });

        App.store.importLogs.push({
            id: App.store.importLogs.length + 1, timestamp: App.formatDate(new Date()),
            category: 'coarse_factors', fileName: this.fileName, total: recs.length,
            success: imported, failed: 0, skipped: 0, status: '成功', errors: []
        });

        // 一次性数据修复(2026-09):旧 fixDay 规则曾把 7.2/7.3 误解析成 7月20/30 日,
        // 种子/历史数据遗留的 2026-07-20/30 孤儿行在此清除(修正后的解析写回 07-02/03)。
        // 源表(6.16-7.14)不存在 7月20/30 采样,可安全按时间戳模式清除。
        const badTs = /^2026-07-(20|30) /;
        if (App.store.coarseCoal.some(c => badTs.test(c.timestamp))) {
            App.store.coarseCoal = App.store.coarseCoal.filter(c => !badTs.test(c.timestamp));
            App.store.magneticTail = App.store.magneticTail.filter(c => !badTs.test(c.timestamp));
        }

        App.saveStore();

        // 导入后自动训练多因素模型（http 下走后端 sklearn，file:// 回退本地）
        let trainMsg = '';
        try {
            const ok = await App.retrainCoarseModelAsync();
            if (ok) {
                const cm = App.store.coarseModel;
                trainMsg = `，已训练模型[${cm.production.toUpperCase()}] R²=${cm[cm.production].metrics.r2.toFixed(3)} 合格率=${cm[cm.production].metrics.passRate.toFixed(0)}%`;
            }
        } catch (e) { console.warn('训练失败:', e); }

        App._onExternalInput();   // 新数据 → 自动执行重算目标密度
        App.refreshAllPages();
        this.cancelImport();
        this.loadLogs();
        App.showToast(`多因素导入完成：${imported}条${trainMsg}`, 'success');
    },

    showPreview() {
        if (!this.parsedData) return;
        const panel = document.getElementById('import-preview-panel');
        panel.style.display = '';

        const p = this.parsedData;
        const validRows = p.rows.length - p.duplicates.length;
        document.getElementById('import-preview-stats').textContent =
            `共 ${p.rows.length} 行 | 有效 ${validRows} 行 | 错误 ${p.errors.length} 项 | 重复 ${p.duplicates.length} 行`;

        document.getElementById('import-summary').innerHTML =
            `<div class="summary-item"><span class="summary-label">总行数：</span><span class="summary-val">${p.rows.length}</span></div>` +
            `<div class="summary-item"><span class="summary-label">可导入：</span><span class="summary-val" style="color:var(--accent-green)">${validRows}</span></div>` +
            `<div class="summary-item"><span class="summary-label">格式错误：</span><span class="summary-val" style="color:var(--accent-red)">${p.errors.length}</span></div>` +
            `<div class="summary-item"><span class="summary-label">重复跳过：</span><span class="summary-val" style="color:var(--accent-orange)">${p.duplicates.length}</span></div>`;

        // 渲染预览表
        const thead = document.getElementById('import-preview-thead');
        const tbody = document.getElementById('import-preview-tbody');
        thead.innerHTML = '<tr>' +
            '<th>状态</th>' +
            p.headers.map(h => `<th>${h || '-'}</th>`).join('') +
            '</tr>';

        const maxPreview = Math.min(p.rows.length, 50);
        tbody.innerHTML = p.rows.slice(0, maxPreview).map((row, i) => {
            const isDup = p.duplicates.includes(i);
            const hasErr = p.errors.some(e => e.row === i + 2);
            const cls = hasErr ? 'preview-row-error' : isDup ? 'preview-row-dup' : '';
            const status = hasErr ? '<span class="status-error">错误</span>' :
                           isDup ? '<span class="status-warn">重复</span>' :
                           '<span class="status-good">正常</span>';
            return `<tr class="${cls}">
                <td>${status}</td>
                ${p.headers.map((_, j) => `<td>${row[j] !== null && row[j] !== undefined ? row[j] : ''}</td>`).join('')}
            </tr>`;
        }).join('');

        if (p.rows.length > 50) {
            tbody.innerHTML += `<tr><td colspan="${p.headers.length + 1}" style="text-align:center;color:var(--text-muted)">
                ... 仅显示前50行，共 ${p.rows.length} 行</td></tr>`;
        }
    },

    startImport() {
        if (!this.parsedData) {
            App.showToast('请先选择并解析文件', 'warning');
            return;
        }
        this.showPreview();
    },

    // 模板列映射定义
    // 粗精煤泥、精磁尾: 采样时间 | 系统 | 灰分% | 煤量(t/h) | 水分% | 液位%
    // 浮精: 采样时间 | 系统 | 浮精灰分% | 浮精煤量(t/h) | 压滤机运行
    // 灰分、密度: 采样时间 | 系统 | 皮带 | 灰分% | 密度值(g/cm³)

    // 任务四：统一时间解析（表2/表3 模板导入），非法时间返回 null
    // 支持："2026-06-16 08:23:00" / "2026/6/16 8:23" / "6.16 08:23"(M.DD补偿) / "6/16 08:23" / Excel序列
    normalizeTs(raw) {
        if (raw === null || raw === undefined) return null;
        const pad = n => String(n).padStart(2, '0');
        if (raw instanceof Date && !isNaN(raw)) {
            return `${raw.getFullYear()}-${pad(raw.getMonth() + 1)}-${pad(raw.getDate())} ${pad(raw.getHours())}:${pad(raw.getMinutes())}:${pad(raw.getSeconds())}`;
        }
        if (typeof raw === 'number' && isFinite(raw)) {
            const d = new Date(Math.round((raw - 25569) * 86400 * 1000));
            if (isNaN(d)) return null;
            return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
        }
        const s = String(raw).trim();
        if (!s) return null;
        let m = s.match(/^(\d{4})[-\/](\d{1,2})[-\/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/);
        if (m) return `${m[1]}-${pad(+m[2])}-${pad(+m[3])} ${pad(+m[4])}:${pad(+m[5])}:${pad(+(m[6] || 0))}`;
        // M.DD 点记法（个位天数省略尾零补偿）
        m = s.match(/^(\d{1,2})\.(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/);
        if (m) {
            const mm = +m[1], frac = +m[2];
            const dd100 = frac < 10 ? frac * 10 : frac;
            const d = dd100 === 10 ? 1 : (dd100 >= 1 && dd100 <= 31 ? dd100 : Math.floor(dd100 / 10));
            return `2026-${pad(mm)}-${pad(d)} ${pad(+m[3])}:${pad(+m[4])}:${pad(+(m[5] || 0))}`;
        }
        // 斜杠记法（无补偿，6/3=6月3日）
        m = s.match(/^(\d{1,2})\/(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/);
        if (m) return `2026-${pad(+m[1])}-${pad(+m[2])} ${pad(+m[3])}:${pad(+m[4])}:${pad(+(m[5] || 0))}`;
        return null;
    },

    confirmImport() {
        if (!this.parsedData) return;
        const p = this.parsedData;
        const category = document.getElementById('import-category').value;
        // 类别与表头不匹配保护（如浮精文件误选"粗精煤泥、精磁尾"）
        const hdrTxt = (p.headers || []).map(h => String(h || '')).join(',');
        const mismatch = (category === 'coarse_magnetic' && hdrTxt.includes('压滤机'))
                      || (category === 'float_ash' && !hdrTxt.includes('压滤机'))
                      || (category === 'ash_density' && !hdrTxt.includes('密度'));
        if (mismatch) {
            App.showToast('数据类别与文件表头不匹配（浮精文件应选"浮精"），已阻止导入', 'error');
            return;
        }
        if (category === 'coarse_factors') return this.confirmImportFactors();
        const skipDups = document.getElementById('import-dedup').checked;

        // 按表头关键字定位列位（兼容无"系统"列的4列表2文件），找不到回退模板默认列位
        const hdr = (p.headers || []).map(h => String(h || ''));
        const col = {
            time: hdr.findIndex(h => h.includes('时间')),
            system: hdr.findIndex(h => h.includes('系统')),
            ash: hdr.findIndex(h => h.includes('灰分')),
            amount: hdr.findIndex(h => h.includes('煤量')),
            water: hdr.findIndex(h => h.includes('水分')),
            level: hdr.findIndex(h => h.includes('液位')),
            press: hdr.findIndex(h => h.includes('压滤机')),
            belt: hdr.findIndex(h => h.includes('皮带')),
            density: hdr.findIndex(h => h.includes('密度')),
        };

        let imported = 0, failed = 0, skipped = 0;
        const errors = [];

        p.rows.forEach((row, i) => {
            if (skipDups && p.duplicates.includes(i)) { skipped++; return; }
            if (p.errors.some(e => e.row === i + 2)) { failed++; return; }
            try {
                const ts = this.normalizeTs(row[col.time >= 0 ? col.time : 0]);
                if (!ts) { failed++; errors.push({ row: i + 2, msg: `时间格式异常: ${row[col.time >= 0 ? col.time : 0]}` }); return; }

                // 系统字段：表3(灰分密度)保留系统列作内部参考；表2(浮精)无系统列按合并处理
                const system = (category === 'ash_density') ? String((col.system >= 0 ? row[col.system] : row[1]) || '').trim() : '合并';

                // 同时间戳覆盖：先移除目标数组中同时间的旧导入记录，避免重复与旧值残留
                if (category === 'coarse_magnetic') {
                    App.store.magneticTail = App.store.magneticTail.filter(m => m.timestamp !== ts);
                    App.store.coarseCoal = App.store.coarseCoal.filter(c => c.timestamp !== ts);
                } else if (category === 'float_ash') {
                    App.store.floatCoal = App.store.floatCoal.filter(f => f.timestamp !== ts);
                } else if (category === 'ash_density') {
                    // 同时间+同系统覆盖（表3含A/B两系统同时间记录，需各自保留一条）
                    App.store.calcLogs = App.store.calcLogs.filter(l => {
                        if (!(l.calc_type === 'ash_density' && l.timestamp === ts)) return true;
                        try { return (JSON.parse(l.input_json || '{}').system || '') !== system; } catch (e) { return true; }
                    });
                }

                // 类别到 manualEntries 类别名的映射
                const catMap = {
                    coarse_magnetic: 'magnetic_tail',
                    float_ash: 'float_ash',
                    ash_density: 'ash_meter'
                };

                switch (category) {
                    case 'coarse_magnetic':
                        // 按表头定位：灰分% / 煤量(t/h) / 水分% / 液位%
                        const cAsh = parseFloat(row[col.ash >= 0 ? col.ash : 2]) || 0;
                        const cAmt = parseFloat(row[col.amount >= 0 ? col.amount : 3]) || 0;
                        const cWater = parseFloat(row[col.water >= 0 ? col.water : 4]) || 0;
                        const cLevel = parseFloat(row[col.level >= 0 ? col.level : 5]) || 0;
                        App.store.magneticTail.push({
                            id: App.store.magneticTail.length + 1,
                            timestamp: ts,
                            system: system,
                            level: cLevel,
                            ash_content: cAsh,
                            moisture: cWater,
                            coal_amount: cAmt,
                            source: 'import'
                        });
                        // 同步写入 coarseCoal，供粗精煤泥分析页面使用
                        const heavyAsh = 8.50, heavyAmt = 250, fAsh = 0, fAmt = 0;
                        const baseTotal = App.calcTotalAsh(heavyAsh, heavyAmt, fAsh, fAmt, 0, 0);
                        const curTotal = App.calcTotalAsh(heavyAsh, heavyAmt, fAsh, fAmt, cAsh, cAmt);
                        App.store.coarseCoal.push({
                            id: App.store.coarseCoal.length + 1,
                            timestamp: ts,
                            system: system,
                            ash_content: cAsh,
                            coal_amount: cAmt,
                            level: cLevel,
                            influence_value: +(curTotal - baseTotal).toFixed(3),
                            source: 'import'
                        });
                        break;
                    case 'float_ash':
                        // 按表头定位：浮精灰分% / 浮精煤量(t/h) / 压滤机运行
                        const pressVal = String(row[col.press >= 0 ? col.press : 4] || '');
                        const flAsh = parseFloat(row[col.ash >= 0 ? col.ash : 2]) || 0;
                        const flAmt = parseFloat(row[col.amount >= 0 ? col.amount : 3]) || 0;
                        const hAsh = 8.50, hAmt = 250, csAsh = 13.0, csAmt = 15;
                        const flBaseTotal = App.calcTotalAsh(hAsh, hAmt, 0, 0, csAsh, csAmt);
                        const flCurTotal = App.calcTotalAsh(hAsh, hAmt, flAsh, flAmt, csAsh, csAmt);
                        App.store.floatCoal.push({
                            id: App.store.floatCoal.length + 1,
                            timestamp: ts,
                            system: system,
                            ash_content: flAsh,
                            coal_amount: flAmt,
                            filter_press_running: pressVal === '运行' ? 1 : 0,
                            influence_value: +(flCurTotal - flBaseTotal).toFixed(3),
                            annotation: '导入数据'
                        });
                        break;
                    case 'ash_density':
                        // 按表头定位：系统 / 皮带 / 灰分% / 密度值(g/cm³)
                        const densityVal = row[col.density >= 0 ? col.density : 4];
                        let density = (densityVal && densityVal !== '-' && densityVal !== '')
                            ? parseFloat(densityVal) : null;
                        // 缺测/坏值清洗：密度必须在 1.3~1.6 之间，否则按缺失处理
                        if (density != null && !(density >= 1.3 && density <= 1.6)) density = null;
                        App.store.calcLogs.push({
                            id: App.store.calcLogs.length + 1,
                            timestamp: ts,
                            calc_type: 'ash_density',
                            input_json: JSON.stringify({
                                system: system,
                                belt: String(row[col.belt >= 0 ? col.belt : 2] || ''),
                                ash_content: parseFloat(row[col.ash >= 0 ? col.ash : 3]) || 0,
                                density: density
                            }),
                            output_json: '{}'
                        });
                        break;
                }

                // 同步写入 manualEntries，使导入数据在补录历史中可见
                let displayStr = '';
                if (category === 'coarse_magnetic') {
                    displayStr = `系统:${system}, 灰分:${parseFloat(row[col.ash >= 0 ? col.ash : 2])||0}%, 煤量:${parseFloat(row[col.amount >= 0 ? col.amount : 3])||0}, 水分:${parseFloat(row[col.water >= 0 ? col.water : 4])||0}%, 液位:${parseFloat(row[col.level >= 0 ? col.level : 5])||0}%`;
                } else if (category === 'float_ash') {
                    const press = String(row[col.press >= 0 ? col.press : 4] || '');
                    displayStr = `系统:${system}, 灰分:${parseFloat(row[col.ash >= 0 ? col.ash : 2])||0}%, 煤量:${parseFloat(row[col.amount >= 0 ? col.amount : 3])||0}, 压滤机:${press || '-'}`;
                } else if (category === 'ash_density') {
                    const dv = row[col.density >= 0 ? col.density : 4];
                    displayStr = `系统:${system}, 皮带:${row[col.belt >= 0 ? col.belt : 2]||'-'}, 灰分:${parseFloat(row[col.ash >= 0 ? col.ash : 3])||0}%, 密度:${(dv && dv !== '-' && dv !== '') ? dv : '-'}`;
                }
                App.store.manualEntries.push({
                    id: App.store.manualEntries.length + 1,
                    timestamp: ts,
                    category: catMap[category] || category,
                    values: { display: displayStr },
                    remark: '批量导入',
                    operator: '批量导入',
                    status: '已导入'
                });

                imported++;
            } catch (e) {
                failed++;
                errors.push({ row: i + 2, msg: e.message });
            }
        });

        // 记录导入日志
        const log = {
            id: App.store.importLogs.length + 1,
            timestamp: App.formatDate(new Date()),
            category: category,
            fileName: this.fileName,
            total: p.rows.length,
            success: imported,
            failed: failed,
            skipped: skipped,
            status: failed > 0 ? '部分失败' : '成功',
            errors: errors
        };
        App.store.importLogs.push(log);

        // 持久化到 localStorage
        App.saveStore();

        // 新数据 → 自动执行重算目标密度；触发已初始化页面刷新
        App._onExternalInput();
        App.refreshAllPages();

        this.cancelImport();
        this.loadLogs();
        App.showToast(`导入完成：成功${imported}条，失败${failed}条，跳过${skipped}条`,
            failed > 0 ? 'warning' : 'success');
    },

    cancelImport() {
        this.parsedData = null;
        this.parsedFactors = null;
        this.rawData = null;
        document.getElementById('import-preview-panel').style.display = 'none';
        document.getElementById('import-file-info').textContent = '';
        document.getElementById('btn-import').disabled = true;
        document.getElementById('import-file').value = '';
    },

    loadLogs() {
        const tbody = document.getElementById('import-log-tbody');
        const catNames = {
            coarse_magnetic: '粗精煤泥、精磁尾',
            coarse_factors: '粗精煤泥多因素',
            float_ash: '浮精',
            ash_density: '灰分、密度'
        };
        if (App.store.importLogs.length === 0) {
            tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--text-muted)">暂无导入记录</td></tr>';
            return;
        }
        tbody.innerHTML = App.store.importLogs.slice().reverse().map(log => {
            const statusClass = log.status === '成功' ? 'status-good' :
                                log.status === '部分失败' ? 'status-warn' : 'status-error';
            return `<tr>
                <td style="font-size:12px">${log.timestamp}</td>
                <td>${catNames[log.category] || log.category}</td>
                <td>${log.fileName}</td>
                <td>${log.total}</td>
                <td style="color:var(--accent-green)">${log.success}</td>
                <td style="color:var(--accent-red)">${log.failed}</td>
                <td style="color:var(--accent-orange)">${log.skipped}</td>
                <td class="${statusClass}">${log.status}</td>
                <td><button class="link-btn" onclick="ImportPage.showLogDetail(${log.id})">查看明细</button></td>
            </tr>`;
        }).join('');
    },

    showLogDetail(id) {
        const log = App.store.importLogs.find(l => l.id === id);
        if (!log) return;
        let errHtml = '';
        if (log.errors && log.errors.length > 0) {
            errHtml = '<h4 style="margin:12px 0 8px;color:var(--accent-red)">错误明细：</h4>' +
                '<table class="data-table"><thead><tr><th>行号</th><th>错误信息</th></tr></thead><tbody>' +
                log.errors.map(e => `<tr><td>第${e.row}行</td><td>${e.msg}</td></tr>`).join('') +
                '</tbody></table>';
        }
        const html = `
            <div style="line-height:2">
                <p><strong>导入时间：</strong>${log.timestamp}</p>
                <p><strong>数据类别：</strong>${log.category}</p>
                <p><strong>文件名：</strong>${log.fileName}</p>
                <p><strong>总行数：</strong>${log.total}</p>
                <p><strong>成功：</strong><span style="color:var(--accent-green)">${log.success}</span></p>
                <p><strong>失败：</strong><span style="color:var(--accent-red)">${log.failed}</span></p>
                <p><strong>跳过(重复)：</strong><span style="color:var(--accent-orange)">${log.skipped}</span></p>
                ${errHtml}
            </div>
        `;
        App.openModal('导入日志详情', html, '<button class="btn" onclick="App.closeModal()">关闭</button>');
    },

    downloadTemplate() {
        const category = document.getElementById('import-category').value;
        const templates = {
            coarse_magnetic: {
                name: '粗精煤泥、精磁尾导入模板.xlsx',
                headers: ['采样时间', '系统', '灰分%', '煤量（t/h)', '水分%', '液位%'],
                sample: [['2026-05-01 08:25:00', '401、A、B', 15.82, 15.0, 28.7, 44]]
            },
            coarse_factors: {
                name: '粗精煤泥多因素导入模板.xlsx',
                headers: ['日期', '序号', '时间', '入洗工作面', '原煤灰分(%)', '小时带煤量(t/h)',
                          '开启的系统_A', '开启的系统_B', '401', '402', '脱粉_473', '脱粉_474',
                          '315灰分(%)', '315全水分(%)', '精磁尾液位(%)'],
                sample: [['6.16', 1, '09:31:00', '3309/43下01', 31.82, 927, 1, 1, 0, 1, '开', '开', 13.86, 24.5, 55]]
            },
            float_ash: {
                name: '浮精导入模板.xlsx',
                headers: ['采样时间', '系统', '浮精灰分%', '浮精煤量（t/h)', '压滤机运行'],
                sample: [['2026-05-01 07:27:00', '401、402、A、B', 8.71, 30.0, '运行']]
            },
            ash_density: {
                name: '灰分、密度导入模板.xlsx',
                headers: ['采样时间', '系统', '皮带', '灰分%', '密度值（g/cm³)'],
                sample: [['2026-05-01 08:33:00', 'A', '502', 8.42, 1.450]]
            }
        };

        const tpl = templates[category];
        if (!tpl) return;

        const wsData = [tpl.headers, ...tpl.sample];
        const ws = XLSX.utils.aoa_to_sheet(wsData);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '数据');

        // 列宽
        ws['!cols'] = tpl.headers.map(() => ({ wch: 25 }));

        XLSX.writeFile(wb, tpl.name);
        App.showToast('模板已下载：' + tpl.name, 'success');
    },

    // ========================================
    // 推测简报：三表 1h 对齐 + 建议密度 + 粗精煤泥灰分模型推测
    // ========================================
    generateBrief() {
        const brief = App.buildHourlyBrief();
        this.briefResult = brief;
        if (!brief.rows.length) {
            App.showToast('暂无可对齐的三表数据，请先导入三张表', 'warning');
            return;
        }
        const schemeName = (App.store.guideScheme === 'heavy') ? '重介精煤灰分版' : '总灰分版';
        const target = (App.store.ashTarget != null ? App.store.ashTarget : 8.50).toFixed(2);
        const tol = (App.store.ashTargetTol != null ? App.store.ashTargetTol : 0.1);
        const thead = `<tr><th>#</th>${brief.headers.map(h => `<th>${h}</th>`).join('')}</tr>`;
        const tbody = brief.rows.map((row, i) =>
            `<tr><td>${i + 1}</td>${row.map(c => `<td>${c === '' ? '-' : c}</td>`).join('')}</tr>`).join('');
        const body = `
            <div style="margin-bottom:10px;color:var(--text-secondary);font-size:12px;line-height:1.7">
                共 <strong>${brief.rows.length}</strong> 个 1h 间隔 ·
                建议密度口径：<strong>${schemeName}</strong> ·
                目标灰分 <strong>${target}%</strong> · 达标容差 ±<strong>${tol}%</strong><br>
                <span style="color:var(--accent-orange)">三表无数据源项按恒值处理：粗精煤泥量 40 t/h · 501/502皮带秤 268.5/235.2 t/h · 重介灰分 8.50%</span>
            </div>
            <div class="table-scroll" style="max-height:60vh">
                <table class="data-table">
                    <thead>${thead}</thead>
                    <tbody>${tbody}</tbody>
                </table>
            </div>`;
        App.openModal('推测简报（三表1h对齐）', body,
            '<button class="btn btn-primary" onclick="ImportPage.exportBrief()">导出Excel</button>' +
            '<button class="btn" onclick="App.closeModal()">关闭</button>');
    },

    exportBrief() {
        const brief = this.briefResult;
        if (!brief || !brief.rows.length) { App.showToast('请先生成推测简报', 'warning'); return; }
        const aoa = [brief.headers, ...brief.rows];
        const ws = XLSX.utils.aoa_to_sheet(aoa);
        ws['!cols'] = brief.headers.map(() => ({ wch: 16 }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '推测简报');
        XLSX.writeFile(wb, '推测简报_1h对齐.xlsx');
        App.showToast('推测简报已导出', 'success');
    },

    exportErrors() {
        const logs = App.store.importLogs.filter(l => l.errors && l.errors.length > 0);
        if (logs.length === 0) {
            App.showToast('暂无错误记录', 'info');
            return;
        }
        const rows = [['导入时间', '数据类别', '文件名', '行号', '错误信息']];
        logs.forEach(log => {
            log.errors.forEach(e => {
                rows.push([log.timestamp, log.category, log.fileName, '第' + e.row + '行', e.msg]);
            });
        });
        const ws = XLSX.utils.aoa_to_sheet(rows);
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '错误记录');
        XLSX.writeFile(wb, '导入错误记录.xlsx');
        App.showToast('错误记录已导出', 'success');
    }
};
