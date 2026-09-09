/* ========================================
   xlsx-lite.js - 轻量Excel读写库（支持.xlsx和.csv）
   核心功能:
   - XLSX.read(data, opts) -> workbook (异步，支持真正的.xlsx)
   - XLSX.utils.sheet_to_json(ws, opts) -> array of arrays
   - XLSX.utils.aoa_to_sheet(aoa) -> worksheet
   - XLSX.writeFile(wb, filename) -> 导出CSV
   ======================================== */

// 如果已加载完整的 SheetJS 库，则跳过
if (typeof XLSX !== 'undefined' && XLSX.version) {
} else {
var XLSX = {

    // ========== 读取入口（异步） ==========
    async read(data, opts) {
        if (data instanceof ArrayBuffer) data = new Uint8Array(data);

        // 检测是否为ZIP格式（xlsx文件以PK开头）
        if (data && data[0] === 0x50 && data[1] === 0x4B) {
            return await this._readXLSX(data);
        }

        // 回退：CSV解析
        const text = new TextDecoder().decode(data);
        const rows = this._parseCSV(text);
        const ws = this._rowsToSheet(rows);
        return { SheetNames: ['Sheet1'], Sheets: { Sheet1: ws } };
    },

    // ========== xlsx 解析核心 ==========

    async _readXLSX(data) {
        // 1. 解析ZIP结构
        const entries = this._parseZip(data);

        // 2. 读取共享字符串表
        const sharedStrings = [];
        const ssEntry = entries.find(e => e.name === 'xl/sharedStrings.xml');
        if (ssEntry) {
            const xml = await this._inflate(data, ssEntry);
            const re = /<si>(?:<r>.*?<\/r>)+|<t[^>]*>(.*?)<\/t>/gs;
            let m;
            while ((m = re.exec(xml)) !== null) {
                if (m[1] !== undefined) {
                    sharedStrings.push(this._decodeXml(m[1]));
                } else {
                    const pieces = [];
                    const rt = /<t[^>]*>(.*?)<\/t>/g;
                    let rm;
                    while ((rm = rt.exec(m[0])) !== null) pieces.push(this._decodeXml(rm[1]));
                    sharedStrings.push(pieces.join(''));
                }
            }
        }

        // 3. 读取样式表，识别日期格式的样式索引
        const dateStyles = await this._readDateStyles(data, entries);

        // 4. 读取工作簿关系，获取实际Sheet名称
        let sheetNames = [];
        const wbEntry = entries.find(e => e.name === 'xl/workbook.xml');
        if (wbEntry) {
            const wbXml = await this._inflate(data, wbEntry);
            const sheetRe = /<sheet\s[^>]*name="([^"]*)"/g;
            let sm;
            while ((sm = sheetRe.exec(wbXml)) !== null) {
                sheetNames.push(this._decodeXml(sm[1]));
            }
        }

        // 5. 读取所有工作表
        const wb = { SheetNames: [], Sheets: {} };
        const sheetEntries = entries
            .filter(e => /xl\/worksheets\/sheet\d+\.xml$/i.test(e.name))
            .sort((a, b) => {
                const na = parseInt(a.name.match(/sheet(\d+)/i)[1]);
                const nb = parseInt(b.name.match(/sheet(\d+)/i)[1]);
                return na - nb;
            });

        for (let i = 0; i < sheetEntries.length; i++) {
            const xml = await this._inflate(data, sheetEntries[i]);
            const ws = this._parseSheetXml(xml, sharedStrings, dateStyles);
            const name = (i < sheetNames.length) ? sheetNames[i] : ('Sheet' + (i + 1));
            wb.SheetNames.push(name);
            wb.Sheets[name] = ws;
        }

        if (wb.SheetNames.length === 0) {
            wb.SheetNames.push('Sheet1');
            wb.Sheets['Sheet1'] = {};
        }
        return wb;
    },

    // ========== 日期样式识别 ==========

    async _readDateStyles(data, entries) {
        const dateStyles = new Set();
        const styleEntry = entries.find(e => e.name === 'xl/styles.xml');
        if (!styleEntry) return dateStyles;

        const xml = await this._inflate(data, styleEntry);
        const cellXfsMatch = xml.match(/<cellXfs[^>]*>([\s\S]*?)<\/cellXfs>/);
        if (!cellXfsMatch) return dateStyles;

        const allXf = cellXfsMatch[1].match(/<xf\s[^>]*>/g);
        if (!allXf) return dateStyles;

        allXf.forEach((xf, idx) => {
            const fmtMatch = xf.match(/numFmtId="(\d+)"/);
            const fmtId = fmtMatch ? parseInt(fmtMatch[1]) : 0;
            // Excel内置日期格式ID: 14-22, 27-36, 45-47, 50-58
            if ((fmtId >= 14 && fmtId <= 22) || (fmtId >= 27 && fmtId <= 36) ||
                (fmtId >= 45 && fmtId <= 47) || (fmtId >= 50 && fmtId <= 58)) {
                dateStyles.add(idx);
            }
        });
        return dateStyles;
    },

    // Excel序列号 → "YYYY-MM-DD HH:MM:SS"
    _excelDateToStr(serial) {
        const utcMs = Math.round((serial - 25569) * 86400000);
        const date = new Date(utcMs);
        const pad = n => String(n).padStart(2, '0');
        return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
    },

    // ========== ZIP 解析 ==========

    _parseZip(data) {
        const entries = [];
        const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
        let offset = 0;

        while (offset < data.length - 30) {
            const sig = view.getUint32(offset, true);
            if (sig !== 0x04034b50) break;

            const method = view.getUint16(offset + 8, true);
            const compSize = view.getUint32(offset + 18, true);
            const nameLen = view.getUint16(offset + 26, true);
            const extraLen = view.getUint16(offset + 28, true);
            const name = new TextDecoder().decode(data.slice(offset + 30, offset + 30 + nameLen));

            entries.push({
                name, method,
                dataOffset: offset + 30 + nameLen + extraLen,
                compSize
            });

            offset += 30 + nameLen + extraLen + compSize;
        }
        return entries;
    },

    // 解压单个ZIP条目
    async _inflate(data, entry) {
        const raw = data.slice(entry.dataOffset, entry.dataOffset + entry.compSize);

        if (entry.method === 0) {
            // STORE - 无压缩
            return new TextDecoder().decode(raw);
        }

        if (entry.method === 8) {
            // DEFLATE - 使用浏览器原生解压
            const ds = new DecompressionStream('deflate-raw');
            const writer = ds.writable.getWriter();
            writer.write(raw);
            writer.close();

            const reader = ds.readable.getReader();
            const chunks = [];
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                chunks.push(value);
            }

            const total = chunks.reduce((s, c) => s + c.length, 0);
            const result = new Uint8Array(total);
            let pos = 0;
            for (const ch of chunks) { result.set(ch, pos); pos += ch.length; }
            return new TextDecoder().decode(result);
        }

        return '';
    },

    // ========== Sheet XML 解析 ==========

    _parseSheetXml(xml, sharedStrings, dateStyles) {
        const ws = {};
        let maxR = 0, maxC = 0;

        // 逐行解析 <row r="1" ...>...</row>
        const rowRe = /<row\s[^>]*\br="(\d+)"[^>]*>([\s\S]*?)<\/row>/g;
        let rm;
        while ((rm = rowRe.exec(xml)) !== null) {
            const rowIdx = parseInt(rm[1]) - 1;
            const rowBody = rm[2];

            // 解析每个 <c r="A1" [t="s"] [s="0"]> [<v>...</v>] </c>
            // 只匹配有 </c> 闭合标签的单元格，自闭合空单元格（<c .../>）由 sheet_to_json 返回 null
            // [^>/]* 确保不匹配自闭合标签的 '/'，避免跨越单元格边界
            const cellRe = /<c\s+r="([A-Z]+)(\d+)"([^>/]*)>([\s\S]*?)<\/c>/g;
            let cm;
            while ((cm = cellRe.exec(rowBody)) !== null) {
                const colStr = cm[1];
                const attrs = cm[3] || '';
                const cellBody = cm[4] || '';
                const vMatch = cellBody.match(/<v>([^<]*)<\/v>/);
                const rawVal = vMatch ? vMatch[1] : undefined;

                // 列字母转数字
                let col = 0;
                for (let k = 0; k < colStr.length; k++) {
                    col = col * 26 + (colStr.charCodeAt(k) - 64);
                }
                col -= 1;

                let value = null;
                let type = 'n';

                if (rawVal === undefined || rawVal === null) {
                    value = null;
                } else if (/\bt="s"/.test(attrs)) {
                    const idx = parseInt(rawVal);
                    value = (idx >= 0 && idx < sharedStrings.length) ? sharedStrings[idx] : '';
                    type = 's';
                } else if (/\bt="b"/.test(attrs)) {
                    value = rawVal === '1';
                    type = 'b';
                } else if (/\bt="str"/.test(attrs)) {
                    value = this._decodeXml(rawVal);
                    type = 's';
                } else {
                    const num = parseFloat(rawVal);
                    if (isNaN(num)) {
                        value = this._decodeXml(rawVal);
                        type = 's';
                    } else {
                        // 检查是否为日期格式样式
                        const styleMatch = attrs.match(/\bs="(\d+)"/);
                        const styleIdx = styleMatch ? parseInt(styleMatch[1]) : -1;
                        if (dateStyles.has(styleIdx)) {
                            value = this._excelDateToStr(num);
                            type = 's';
                        } else {
                            value = num;
                            type = 'n';
                        }
                    }
                }

                const ref = this.utils._encodeCell(rowIdx, col);
                ws[ref] = { v: value, t: type };
                if (rowIdx > maxR) maxR = rowIdx;
                if (col > maxC) maxC = col;
            }
        }

        if (maxR > 0 || maxC > 0) {
            ws['!ref'] = this.utils._encodeCell(0, 0) + ':' + this.utils._encodeCell(maxR, maxC);
        }
        return ws;
    },

    _decodeXml(s) {
        return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
               .replace(/&quot;/g, '"').replace(/&apos;/g, "'");
    },

    // ========== utils（保持原有API不变） ==========

    utils: {
        sheet_to_json(ws, opts) {
            if (!ws || !ws['!ref']) return [];
            const range = ws['!ref'].split(':');
            const start = XLSX.utils._decodeCell(range[0]);
            const end = XLSX.utils._decodeCell(range[1]);
            const result = [];
            for (let r = start.r; r <= end.r; r++) {
                const row = [];
                for (let c = start.c; c <= end.c; c++) {
                    const cell = ws[XLSX.utils._encodeCell(r, c)];
                    row.push(cell ? cell.v : null);
                }
                result.push(row);
            }
            if (opts && opts.header === 1) return result;
            return result;
        },

        aoa_to_sheet(aoa) {
            const ws = {};
            let maxC = 0;
            aoa.forEach((row, r) => {
                maxC = Math.max(maxC, row.length);
                row.forEach((val, c) => {
                    ws[XLSX.utils._encodeCell(r, c)] = { v: val !== null && val !== undefined ? val : '', t: 's' };
                });
            });
            ws['!ref'] = XLSX.utils._encodeCell(0, 0) + ':' + XLSX.utils._encodeCell(aoa.length - 1, maxC - 1);
            return ws;
        },

        book_new() { return { SheetNames: [], Sheets: {} }; },

        book_append_sheet(wb, ws, name) {
            wb.SheetNames.push(name);
            wb.Sheets[name] = ws;
        },

        _encodeCell(r, c) {
            let col = '';
            let cc = c;
            while (cc >= 0) {
                col = String.fromCharCode(65 + (cc % 26)) + col;
                cc = Math.floor(cc / 26) - 1;
            }
            return col + (r + 1);
        },

        _decodeCell(ref) {
            const match = ref.match(/([A-Z]+)(\d+)/);
            if (!match) return { r: 0, c: 0 };
            let c = 0;
            for (let i = 0; i < match[1].length; i++) {
                c = c * 26 + (match[1].charCodeAt(i) - 64);
            }
            return { r: parseInt(match[2]) - 1, c: c - 1 };
        }
    },

    // ========== 导出（CSV） ==========

    writeFile(wb, filename) {
        const wsName = wb.SheetNames[0];
        const ws = wb.Sheets[wsName];
        if (!ws || !ws['!ref']) return;

        const range = ws['!ref'].split(':');
        const start = XLSX.utils._decodeCell(range[0]);
        const end = XLSX.utils._decodeCell(range[1]);
        let csv = '';

        for (let r = start.r; r <= end.r; r++) {
            const row = [];
            for (let c = start.c; c <= end.c; c++) {
                const cell = ws[XLSX.utils._encodeCell(r, c)];
                let val = cell ? String(cell.v) : '';
                if (val.includes(',') || val.includes('"') || val.includes('\n')) {
                    val = '"' + val.replace(/"/g, '""') + '"';
                }
                row.push(val);
            }
            csv += row.join(',') + '\n';
        }

        const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename.replace(/\.(xlsx|xls)$/i, '.csv');
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    },

    // ========== CSV 解析（回退） ==========

    _parseCSV(text) {
        const rows = [];
        let current = [];
        let field = '';
        let inQuotes = false;

        for (let i = 0; i < text.length; i++) {
            const ch = text[i];
            if (inQuotes) {
                if (ch === '"') {
                    if (i + 1 < text.length && text[i + 1] === '"') { field += '"'; i++; }
                    else inQuotes = false;
                } else field += ch;
            } else {
                if (ch === '"') inQuotes = true;
                else if (ch === ',') { current.push(field); field = ''; }
                else if (ch === '\r') continue;
                else if (ch === '\n') {
                    current.push(field); field = '';
                    if (current.some(c => c !== '')) rows.push(current);
                    current = [];
                } else field += ch;
            }
        }
        if (field || current.length > 0) {
            current.push(field);
            if (current.some(c => c !== '')) rows.push(current);
        }
        return rows;
    },

    _rowsToSheet(rows) {
        const ws = {};
        let maxC = 0;
        rows.forEach((row, r) => {
            maxC = Math.max(maxC, row.length);
            row.forEach((val, c) => {
                let v = val, t = 's';
                if (val !== null && val !== undefined && val !== '') {
                    const num = Number(val);
                    if (!isNaN(num) && val.trim() !== '') { v = num; t = 'n'; }
                }
                ws[XLSX.utils._encodeCell(r, c)] = { v, t };
            });
        });
        if (rows.length > 0) {
            ws['!ref'] = XLSX.utils._encodeCell(0, 0) + ':' + XLSX.utils._encodeCell(rows.length - 1, maxC - 1);
        }
        return ws;
    }
};
} // end else
