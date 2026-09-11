/* ========================================
   brief.js - 推测简报（独立页）
   ----------------------------------------
   把原来只能"点一下、在 800px 宽弹窗里横向拉"的推测简报，做成导航里的独立整页：
   - 整页宽度（16 列在 1440px 以上基本一屏看全）
   - 表头吸顶（sticky）+ 双向滚动，纵向可直接下拉浏览全部行
   - 渲染后自动滚到最新一行（简报是时间升序，263 行不滚等于看不到当天数据）
   - 顶部摘要说明口径：行数/区间/建议密度版本/目标灰分/容差/重介灰分来源

   数据来源：App.buildHourlyBrief()（三表 1h 对齐 + 递归软测量，与后端 build_hourly_brief 逐值一致）。
   行存在规则：粗精煤泥 / 浮精 / 灰分密度三表在该小时结束前 24h 内均有记录才成行。
   ======================================== */
const BriefPage = {
    result: null,          // { headers, rows } —— 与导出 Excel 完全同一份数据
    generatedAt: null,

    init() {
        // 首次进入自动生成（数据已在 store 里，省一次点击）；之后切回来只重渲染
        if (!this.result) this.generate();
        else this.render();
    },

    // 三表各自最近一条记录的时间（0 行时用来告诉用户到底缺哪张表）
    _lastTs() {
        const last = arr => {
            let m = '';
            (arr || []).forEach(r => { const t = (r && (r.timestamp || r.ts)) || ''; if (t > m) m = t; });
            return m;
        };
        const ad = (App.store.calcLogs || []).filter(l => l.calc_type === 'ash_density');
        return { coarse: last(App.store.coarseCoal), float: last(App.store.floatCoal), ad: last(ad) };
    },

    generate() {
        const brief = App.buildHourlyBrief();
        this.result = brief;
        this.generatedAt = new Date();
        this.render();
        if (!brief.rows.length) this._showEmptyHint();
        return brief;
    },

    // 摘要文字（独立页与生成后的弹窗共用同一份口径说明）
    summaryHtml() {
        const b = this.result || { rows: [] };
        const rows = b.rows;
        const schemeName = (App.store.guideScheme === 'heavy') ? '重介精煤灰分版' : '总灰分版';
        const target = (App.store.ashTarget != null ? App.store.ashTarget : 8.50).toFixed(2);
        const tol = (App.store.ashTargetTol != null ? App.store.ashTargetTol : 0.1);
        const heavyAshVal = App.getHeavyAsh();
        const heavyAshLayer = App.heavyAshLayer();
        const range = rows.length ? `${rows[0][0]} ~ ${rows[rows.length - 1][0]}` : '—';
        return `
            <div style="color:var(--text-secondary);font-size:12px;line-height:1.9">
                共 <strong>${rows.length}</strong> 个 1h 间隔 ·
                区间 <strong>${range}</strong> ·
                建议密度口径：<strong>${schemeName}</strong> ·
                目标灰分 <strong>${target}%</strong> · 容差 ±<strong>${tol}%</strong><br>
                成行规则：粗精煤泥 / 浮精 / 灰分密度三表在该小时结束前 <strong>24h 内均有记录</strong>才成行，任一表超窗则整行跳过<br>
                重介精煤灰分：<strong>${heavyAshVal.toFixed(2)}%</strong>（${heavyAshLayer}）<br>
                <span style="color:var(--accent-orange)">三表无数据源项按恒值处理：粗精煤泥量 40 t/h · 501/502皮带秤 268.5/235.2 t/h
                · 501 灰分仪未接入，该列留空</span>
            </div>`;
    },

    _tableHtml() {
        const b = this.result || { headers: [], rows: [] };
        const thead = `<tr><th>#</th>${b.headers.map(h => `<th>${h}</th>`).join('')}</tr>`;
        const tbody = b.rows.map((row, i) =>
            `<tr><td>${i + 1}</td>${row.map(c => `<td>${c === '' ? '-' : c}</td>`).join('')}</tr>`).join('');
        return `<table class="data-table brief-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>`;
    },

    render() {
        const box = document.getElementById('brief-content');
        if (!box) return;
        if (!this.result) {
            box.innerHTML = '<div class="panel"><div class="panel-body">尚未生成简报：点右上角「生成/刷新简报」。</div></div>';
            return;
        }
        const rows = this.result.rows;
        if (!rows.length) {
            const t = this._lastTs();
            box.innerHTML = `
                <div class="panel"><div class="panel-body" style="line-height:2;font-size:13px">
                    <p><strong>未生成任何行</strong>，原因如下：</p>
                    <p>• 成行规则：某一小时要成行，<strong>粗精煤泥 / 浮精 / 灰分密度三张表都必须在该小时结束前 24 小时内各有一条记录</strong>；任一表超窗，该小时整行跳过（不再用陈旧数据续传凑行）。</p>
                    <p>• 本机各表最近一条记录：</p>
                    <table class="data-table" style="margin:4px 0 12px">
                        <tr><td>粗精煤泥（表1）</td><td>${t.coarse || '— 无数据'}</td></tr>
                        <tr><td>浮精（表2）</td><td>${t.float || '— 无数据'}</td></tr>
                        <tr><td>灰分密度（表3）</td><td>${t.ad || '— 无数据'}</td></tr>
                    </table>
                    <p>• 常见原因：三张表来自不同时间段（例如表1是 6-7 月、表2/表3 只有 5 月），时间窗不重叠；
                       浮精表是每天化验 1 次，因此只有每次浮精采样之后的 24 小时内才会成行。</p>
                </div></div>`;
            return;
        }
        box.innerHTML = `
            <div class="panel" style="margin-bottom:14px">
                <div class="panel-body" style="padding:14px 18px">${this.summaryHtml()}</div>
            </div>
            <div class="panel">
                <div class="panel-header">
                    <h3>三表 1h 对齐明细</h3>
                    <div class="panel-header-actions">
                        <span style="color:var(--text-muted);font-size:12px">共 ${rows.length} 行 · 表头吸顶，可直接下拉浏览</span>
                    </div>
                </div>
                <div class="panel-body" style="padding:0">
                    <div class="brief-scroll" id="brief-scroll">${this._tableHtml()}</div>
                </div>
            </div>`;
        // 简报按时间升序，263 行不滚到底等于看不到当天数据。
        // 用 rAF 等布局完成：页面若正处于隐藏状态（从导入页生成时就是这样），
        // 此时 scrollHeight/clientHeight 都是 0，直接设 scrollTop 会无效。
        this.scrollToLatest();
    },

    scrollToLatest() {
        requestAnimationFrame(() => {
            const sc = document.getElementById('brief-scroll');
            if (sc && sc.scrollHeight > sc.clientHeight) sc.scrollTop = sc.scrollHeight;
        });
    },

    // 0 行分诊（与独立页空状态同一套文案，供导入页生成后弹窗复用）
    emptyHintHtml() {
        const t = this._lastTs();
        return `
            <div style="line-height:2;font-size:13px">
                <p><strong>未生成任何行</strong>，原因如下：</p>
                <p>• 成行规则：某一小时要成行，<strong>粗精煤泥 / 浮精 / 灰分密度三张表都必须在该小时结束前 24 小时内各有一条记录</strong>；
                   任一表超窗，该小时整行跳过（不再用陈旧数据续传凑行）。</p>
                <p>• 本机各表最近一条记录：</p>
                <table class="data-table" style="margin:4px 0 12px">
                    <tr><td>粗精煤泥（表1）</td><td>${t.coarse || '— 无数据'}</td></tr>
                    <tr><td>浮精（表2）</td><td>${t.float || '— 无数据'}</td></tr>
                    <tr><td>灰分密度（表3）</td><td>${t.ad || '— 无数据'}</td></tr>
                </table>
                <p>• 常见原因：三张表来自不同时间段（例如表1是 6-7 月、表2/表3 只有 5 月），时间窗不重叠；
                   浮精表是每天化验 1 次，因此只有每次浮精采样之后的 24 小时内才会成行。</p>
            </div>`;
    },

    _showEmptyHint() {
        const t = this._lastTs();
        const empty = [t.coarse, t.float, t.ad].filter(x => !x).length;
        if (empty > 0) {
            App.showToast(`三表数据不齐（粗精煤泥 ${t.coarse || '无'} / 浮精 ${t.float || '无'} / 灰分密度 ${t.ad || '无'}），请先导入缺失的表`, 'warning');
        } else {
            App.showToast(`三表时间窗未落在同一天内，无法成行。各表最近：粗精煤泥 ${t.coarse} / 浮精 ${t.float} / 灰分密度 ${t.ad}`
                + `（成行规则：三表在该小时内 24h 内均有记录）`, 'warning');
        }
    },

    // 生成后弹窗里的预览（最近 N 行，可下拉）
    previewHtml(n) {
        const rows = (this.result && this.result.rows) || [];
        const headers = (this.result && this.result.headers) || [];
        if (!rows.length) return '';
        const tail = rows.slice(-Math.min(n || 20, rows.length));
        const startIdx = rows.length - tail.length + 1;
        const thead = `<tr><th>#</th>${headers.map(h => `<th>${h}</th>`).join('')}</tr>`;
        const tbody = tail.map((row, i) =>
            `<tr><td>${startIdx + i}</td>${row.map(c => `<td>${c === '' ? '-' : c}</td>`).join('')}</tr>`).join('');
        return `
            <div style="margin:12px 0 6px;color:var(--text-secondary);font-size:12px">
                预览：最近 ${tail.length} 行（共 ${rows.length} 行）
            </div>
            <div class="brief-scroll" style="max-height:38vh">
                <table class="data-table brief-table"><thead>${thead}</thead><tbody>${tbody}</tbody></table>
            </div>`;
    },

    exportExcel() {
        const brief = this.result;
        if (!brief || !brief.rows.length) { App.showToast('请先生成推测简报', 'warning'); return; }
        const aoa = [brief.headers, ...brief.rows];
        const ws = XLSX.utils.aoa_to_sheet(aoa);
        ws['!cols'] = brief.headers.map(() => ({ wch: 16 }));
        const wb = XLSX.utils.book_new();
        XLSX.utils.book_append_sheet(wb, ws, '推测简报');
        XLSX.writeFile(wb, '推测简报_1h对齐.xlsx');
        App.showToast(`推测简报已导出（${brief.rows.length} 行）`, 'success');
    },
};
