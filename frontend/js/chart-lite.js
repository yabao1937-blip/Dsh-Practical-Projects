/* ========================================
   chart-lite.js - 纯Canvas 2D轻量图表库
   兼容 Chart.js 基本API：new Chart(ctx, config)
   支持类型：line, bar, doughnut
   ======================================== */

class Chart {
    constructor(ctxOrCanvas, config) {
        const canvas = ctxOrCanvas instanceof HTMLCanvasElement ? ctxOrCanvas : ctxOrCanvas.canvas;
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.config = config;
        this.type = config.type;
        this.data = config.data;
        this.options = config.options || {};
        this._retryCount = 0;
        this._pointPositions = [];
        this._tooltipActive = false;

        // Tooltip mouse events
        this._onMouseMove = (e) => this._handleMouseMove(e);
        this._onMouseLeave = () => this._handleMouseLeave();
        this.canvas.addEventListener('mousemove', this._onMouseMove);
        this.canvas.addEventListener('mouseleave', this._onMouseLeave);

        this._setupAndDraw();
    }

    _getSize() {
        // 优先从父容器获取尺寸
        const parent = this.canvas.parentElement;
        if (parent) {
            const style = getComputedStyle(parent);
            const pw = parseFloat(style.width) || parent.clientWidth;
            const ph = parseFloat(style.height) || parent.clientHeight;
            if (pw > 0 && ph > 0) return { width: pw, height: ph };
        }
        // 回退到 canvas 的 getBoundingClientRect
        const rect = this.canvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0) return { width: rect.width, height: rect.height };
        // 最终回退
        return { width: 800, height: 300 };
    }

    _setup() {
        const dpr = window.devicePixelRatio || 1;
        const size = this._getSize();

        this.width = size.width;
        this.height = size.height;

        // 设置 canvas CSS 尺寸（撑满容器）
        this.canvas.style.width = '100%';
        this.canvas.style.height = '100%';
        this.canvas.style.display = 'block';

        // 设置 canvas 实际像素尺寸
        this.canvas.width = Math.round(this.width * dpr);
        this.canvas.height = Math.round(this.height * dpr);
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    _setupAndDraw() {
        const size = this._getSize();
        if ((size.width < 10 || size.height < 10) && this._retryCount < 5) {
            this._retryCount++;
            setTimeout(() => this._setupAndDraw(), 150);
            return;
        }
        this._setup();
        this.draw();
    }

    update() {
        this._pointPositions = [];
        this._setup();
        this.draw();
    }

    resize() {
        this._pointPositions = [];
        this._setup();
        this.draw();
    }

    draw() {
        if (!this.width || !this.height || this.width < 10 || this.height < 10) return;
        this.ctx.clearRect(0, 0, this.width, this.height);
        this._pointPositions = [];

        if (this.type === 'line') this._drawLine();
        else if (this.type === 'bar') this._drawBar();
        else if (this.type === 'doughnut') this._drawDoughnut();
    }

    /* ---------- Tooltip ---------- */
    _getMousePos(e) {
        const rect = this.canvas.getBoundingClientRect();
        return { x: e.clientX - rect.left, y: e.clientY - rect.top };
    }

    _handleMouseMove(e) {
        // 画布实际尺寸与内部绘制尺寸不一致时（窗口/布局缩放后未重绘），
        // 先用当前尺寸重绘，保证“点/线位置”与鼠标坐标一致，避免命错点
        const rect = this.canvas.getBoundingClientRect();
        if (rect.width > 0 && rect.height > 0 &&
            (Math.abs(rect.width - this.width) > 1 || Math.abs(rect.height - this.height) > 1)) {
            this.resize();
        }

        const pos = this._getMousePos(e);
        const threshold = 15;
        const opts = this.options;
        const interaction = opts.interaction || {};
        const mode = interaction.mode || 'nearest';

        if (this._pointPositions.length === 0) return;

        // 光标只有在贴近某个数据点（横向偏移在此范围内）时才显示数据，其它位置不显示
        const hitRange = interaction.hitRange || 16;

        if (mode === 'index') {
            // 找横向最近的数据点
            let bestIdx = -1;
            let bestDist = Infinity;
            for (const p of this._pointPositions) {
                const d = Math.abs(pos.x - p.x);
                if (d < bestDist) {
                    bestDist = d;
                    bestIdx = p.index;
                }
            }
            // 超过命中范围则不显示（点与点之间、轴区等位置不弹提示）
            if (bestIdx >= 0 && bestDist <= hitRange) {
                const pts = this._pointPositions.filter(p => p.index === bestIdx);
                this._drawTooltip(pts, pos);
            } else {
                this._clearTooltip();
            }
        } else {
            // Find nearest single point
            let best = null;
            let bestDist = Infinity;
            this._pointPositions.forEach(p => {
                const d = Math.sqrt((pos.x - p.x) ** 2 + (pos.y - p.y) ** 2);
                if (d < bestDist && d < threshold) {
                    bestDist = d;
                    best = p;
                }
            });
            if (best) {
                this._drawTooltip([best], pos);
            } else {
                this._clearTooltip();
            }
        }
    }

    _handleMouseLeave() {
        this._clearTooltip();
    }

    _drawTooltip(points, mousePos) {
        // Redraw the chart first (to clear previous tooltip)
        this.ctx.clearRect(0, 0, this.width, this.height);
        this._pointPositions = [];
        if (this.type === 'line') this._drawLine();
        else if (this.type === 'bar') this._drawBar();
        else if (this.type === 'doughnut') this._drawDoughnut();

        const ctx = this.ctx;
        const tooltipOpts = (this.options.plugins && this.options.plugins.tooltip) || {};
        const bgColor = tooltipOpts.backgroundColor || 'rgba(30,51,85,0.95)';
        const titleColor = tooltipOpts.titleColor || '#f0f4fa';
        const bodyColor = tooltipOpts.bodyColor || '#b0bdd0';
        const borderColor = tooltipOpts.borderColor || '#2a4470';
        const borderWidth = tooltipOpts.borderWidth || 1;

        // Build tooltip content
        const label = points[0].label || '';
        const lines = points.map(p => `${p.datasetLabel}: ${p.value}`);

        ctx.font = '13px Microsoft YaHei, sans-serif';
        const titleWidth = ctx.measureText(label).width;
        const lineW = Math.max(...lines.map(l => ctx.measureText(l).width));
        const boxW = Math.max(titleWidth, lineW) + 20;
        const boxH = 28 + lines.length * 22;

        // Position tooltip avoiding canvas edges
        let tx = mousePos.x + 12;
        let ty = mousePos.y - boxH / 2;
        if (tx + boxW > this.width - 5) tx = mousePos.x - boxW - 12;
        if (ty < 5) ty = 5;
        if (ty + boxH > this.height - 5) ty = this.height - boxH - 5;

        // Draw background
        ctx.fillStyle = bgColor;
        ctx.strokeStyle = borderColor;
        ctx.lineWidth = borderWidth;
        const r = 6;
        ctx.beginPath();
        ctx.moveTo(tx + r, ty);
        ctx.lineTo(tx + boxW - r, ty);
        ctx.quadraticCurveTo(tx + boxW, ty, tx + boxW, ty + r);
        ctx.lineTo(tx + boxW, ty + boxH - r);
        ctx.quadraticCurveTo(tx + boxW, ty + boxH, tx + boxW - r, ty + boxH);
        ctx.lineTo(tx + r, ty + boxH);
        ctx.quadraticCurveTo(tx, ty + boxH, tx, ty + boxH - r);
        ctx.lineTo(tx, ty + r);
        ctx.quadraticCurveTo(tx, ty, tx + r, ty);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();

        // Draw title
        ctx.fillStyle = titleColor;
        ctx.font = 'bold 13px Microsoft YaHei, sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(label, tx + 10, ty + 18);

        // Draw body lines
        ctx.fillStyle = bodyColor;
        ctx.font = '12px Microsoft YaHei, sans-serif';
        points.forEach((p, i) => {
            ctx.fillStyle = p.color || bodyColor;
            ctx.fillText(lines[i], tx + 10, ty + 38 + i * 22);
        });

        this._tooltipActive = true;
    }

    _clearTooltip() {
        if (!this._tooltipActive) return;
        this._tooltipActive = false;
        this.ctx.clearRect(0, 0, this.width, this.height);
        this._pointPositions = [];
        if (this.type === 'line') this._drawLine();
        else if (this.type === 'bar') this._drawBar();
        else if (this.type === 'doughnut') this._drawDoughnut();
    }

    /* ---------- 折线图 ---------- */
    _drawLine() {
        const ctx = this.ctx;
        const w = this.width, h = this.height;
        const opts = this.options;
        const scales = opts.scales || {};
        const datasets = this.data.datasets;
        const labels = this.data.labels || [];

        const padding = { top: 36, right: 60, bottom: 50, left: 60 };
        if (scales.y1) padding.right = 70;
        const chartW = w - padding.left - padding.right;
        const chartH = h - padding.top - padding.bottom;

        if (chartW <= 0 || chartH <= 0) return;

        // 计算Y轴范围
        let yMin = Infinity, yMax = -Infinity;
        datasets.forEach(ds => {
            if (ds.yAxisID === 'y1') return;
            (ds.data || []).forEach(v => {
                if (v !== null && v !== undefined) {
                    yMin = Math.min(yMin, v);
                    yMax = Math.max(yMax, v);
                }
            });
        });
        if (!isFinite(yMin)) { yMin = 0; yMax = 1; }
        const yRange = yMax - yMin || 1;
        yMin -= yRange * 0.1;
        yMax += yRange * 0.1;

        // 右侧Y轴范围
        let y1Min = Infinity, y1Max = -Infinity;
        let hasY1 = false;
        datasets.forEach(ds => {
            if (ds.yAxisID === 'y1') {
                hasY1 = true;
                (ds.data || []).forEach(v => {
                    if (v !== null && v !== undefined) {
                        y1Min = Math.min(y1Min, v);
                        y1Max = Math.max(y1Max, v);
                    }
                });
            }
        });
        if (hasY1) {
            if (!isFinite(y1Min)) { y1Min = 0; y1Max = 1; }
            const y1Range = y1Max - y1Min || 1;
            y1Min -= y1Range * 0.15;
            y1Max += y1Range * 0.15;
        }

        const n = labels.length || Math.max(...datasets.map(ds => (ds.data || []).length));
        if (n === 0) return;

        // X轴位置：默认按数据序号等距分布；若提供各点时间戳(this.xTimes)，
        // 则按真实时间间隔等比例分布（时间跨度决定间距，与数据条数无关）
        let toX = i => padding.left + (i / (n - 1 || 1)) * chartW;
        const xTimes = this.xTimes;
        if (Array.isArray(xTimes) && xTimes.length >= n) {
            let t0 = null, t1 = null, ok = true;
            for (let i = 0; i < n; i++) {
                const t = xTimes[i];
                if (typeof t !== 'number' || !isFinite(t)) { ok = false; break; }
                if (t0 === null) t0 = t;
                t1 = t;
            }
            if (ok && t0 !== null && t1 > t0) {
                const span = t1 - t0;
                toX = i => padding.left + ((xTimes[i] - t0) / span) * chartW;
            }
        }
        const toY = v => padding.top + chartH - ((v - yMin) / (yMax - yMin)) * chartH;
        const toY1 = v => padding.top + chartH - ((v - y1Min) / (y1Max - y1Min)) * chartH;

        // 绘制网格和Y轴刻度
        const yTicks = scales.y || {};
        const yColor = (yTicks.title && yTicks.title.color) || '#7b8da6';
        const yTitle = (yTicks.title && yTicks.title.text) || '';
        const yTickConf = yTicks.ticks || {};

        ctx.strokeStyle = 'rgba(255,255,255,0.06)';
        ctx.lineWidth = 1;
        ctx.fillStyle = yColor;
        ctx.font = '12px Microsoft YaHei, sans-serif';
        ctx.textAlign = 'right';

        // 生成Y轴刻度值
        let tickVals = [];
        const gridLines = 5;
        const precision = yTickConf.precision;
        const wantInt = precision === 0 || yTickConf.integer === true;
        if (yTickConf.stepSize && yTickConf.stepSize > 0) {
            for (let v = Math.ceil(yMin / yTickConf.stepSize) * yTickConf.stepSize; v <= yMax + 1e-9; v += yTickConf.stepSize)
                tickVals.push(v);
        } else if (wantInt) {
            // 整数刻度：按跨度取整齐整数步长，避免小数/重复标签
            const raw = (yMax - yMin) / gridLines;
            const mag = Math.pow(10, Math.floor(Math.log10(raw)));
            const norm = raw / mag;
            const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
            const step = Math.max(1, Math.round(nice * mag));
            for (let v = Math.ceil(yMin / step) * step; v <= yMax + 1e-9; v += step)
                tickVals.push(v);
        } else {
            for (let i = 0; i <= gridLines; i++) tickVals.push(yMin + (yMax - yMin) * (i / gridLines));
        }

        // 绘制网格与刻度文字
        tickVals.forEach(val => {
            const y = toY(val);
            ctx.beginPath();
            ctx.moveTo(padding.left, y);
            ctx.lineTo(w - padding.right, y);
            ctx.stroke();
            const label = yTickConf.callback ? yTickConf.callback(val)
                : (typeof precision === 'number' ? val.toFixed(precision) : val.toFixed(2));
            ctx.fillText(label, padding.left - 8, y + 4);
        });

        // Y轴标题
        if (yTitle) {
            ctx.save();
            ctx.translate(16, padding.top + chartH / 2);
            ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center';
            ctx.font = '13px Microsoft YaHei, sans-serif';
            ctx.fillText(yTitle, 0, 0);
            ctx.restore();
        }

        // 右侧Y轴
        if (hasY1) {
            const y1Ticks = scales.y1 || {};
            const y1Color = (y1Ticks.title && y1Ticks.title.color) || '#f59e0b';
            const y1Title = (y1Ticks.title && y1Ticks.title.text) || '';
            ctx.fillStyle = y1Color;
            ctx.textAlign = 'left';
            ctx.font = '12px Microsoft YaHei, sans-serif';
            for (let i = 0; i <= gridLines; i++) {
                const val = y1Min + (y1Max - y1Min) * (i / gridLines);
                const y = toY1(val);
                ctx.fillText(val.toFixed(3), w - padding.right + 8, y + 4);
            }
            if (y1Title) {
                ctx.save();
                ctx.translate(w - 10, padding.top + chartH / 2);
                ctx.rotate(Math.PI / 2);
                ctx.textAlign = 'center';
                ctx.font = '13px Microsoft YaHei, sans-serif';
                ctx.fillText(y1Title, 0, 0);
                ctx.restore();
            }
        }

        // X轴刻度
        const xTicks = scales.x || {};
        const xColor = (xTicks.ticks && xTicks.ticks.color) || '#7b8da6';
        const maxLabels = (xTicks.ticks && xTicks.ticks.maxTicksLimit) || 12;
        const step = Math.max(1, Math.ceil(n / maxLabels));
        ctx.fillStyle = xColor;
        ctx.textAlign = 'center';
        ctx.font = '11px Microsoft YaHei, sans-serif';
        // 自定义刻度标签（如按天）：非空才绘制，相邻标签过近时跳过防重叠
        const customTicks = Array.isArray(this.xTickLabels) && this.xTickLabels.length === n ? this.xTickLabels : null;
        if (customTicks) {
            let lastLabelX = -Infinity;
            for (let i = 0; i < n; i++) {
                if (!customTicks[i]) continue;
                const x = toX(i);
                if (x - lastLabelX < 38) continue;
                ctx.fillText(customTicks[i], x, h - padding.bottom + 20);
                lastLabelX = x;
            }
        } else {
            for (let i = 0; i < n; i += step) {
                const x = toX(i);
                ctx.fillText(labels[i] || '', x, h - padding.bottom + 20);
            }
        }

        // 绘制数据线
        datasets.forEach(ds => {
            const data = ds.data || [];
            if (data.length === 0) return;
            // 跳过全部为 null 的数据集
            if (data.every(v => v === null || v === undefined)) return;
            const isY1 = ds.yAxisID === 'y1';
            const mapY = isY1 ? toY1 : toY;

            // 填充区域（仅在有连续非null段时绘制）
            if (ds.fill && ds.backgroundColor) {
                ctx.beginPath();
                let started = false;
                let startX = 0;
                for (let i = 0; i < data.length; i++) {
                    if (data[i] === null || data[i] === undefined) {
                        if (started) {
                            ctx.lineTo(toX(i - 1), padding.top + chartH);
                            ctx.lineTo(toX(startX), padding.top + chartH);
                            ctx.closePath();
                            ctx.fillStyle = ds.backgroundColor;
                            ctx.fill();
                            started = false;
                        }
                        continue;
                    }
                    const x = toX(i), y = mapY(data[i]);
                    if (!started) { ctx.beginPath(); ctx.moveTo(x, padding.top + chartH); ctx.lineTo(x, y); startX = i; started = true; }
                    else { ctx.lineTo(x, y); }
                }
                if (started) {
                    ctx.lineTo(toX(data.length - 1), padding.top + chartH);
                    ctx.lineTo(toX(startX), padding.top + chartH);
                    ctx.closePath();
                    ctx.fillStyle = ds.backgroundColor;
                    ctx.fill();
                }
            }

            // 线条（null 值处断线）
            ctx.beginPath();
            ctx.strokeStyle = ds.borderColor || '#3b82f6';
            ctx.lineWidth = ds.borderWidth || 2;
            if (ds.borderDash) ctx.setLineDash(ds.borderDash);
            else ctx.setLineDash([]);

            const tension = ds.tension || 0;
            let penDown = false;
            for (let i = 0; i < data.length; i++) {
                if (data[i] === null || data[i] === undefined) {
                    penDown = false;
                    continue;
                }
                const x = toX(i), y = mapY(data[i]);
                if (!penDown) {
                    ctx.moveTo(x, y);
                    penDown = true;
                } else if (tension > 0 && data[i - 1] !== null && data[i - 1] !== undefined) {
                    const prevX = toX(i - 1), prevY = mapY(data[i - 1]);
                    const cpx = (prevX + x) / 2;
                    ctx.bezierCurveTo(cpx, prevY, cpx, y, x, y);
                } else {
                    ctx.lineTo(x, y);
                }
            }
            ctx.stroke();
            ctx.setLineDash([]);

            // 数据点（跳过 null）
            const pr = ds.pointRadius;
            if (pr && pr > 0) {
                ctx.fillStyle = ds.borderColor || '#3b82f6';
                for (let i = 0; i < data.length; i++) {
                    if (data[i] === null || data[i] === undefined) continue;
                    const px = toX(i), py = mapY(data[i]);
                    ctx.beginPath();
                    ctx.arc(px, py, pr, 0, Math.PI * 2);
                    ctx.fill();
                    // Record point position for tooltip
                    this._pointPositions.push({
                        x: px, y: py,
                        index: i,
                        label: labels[i] || '',
                        value: data[i].toFixed(2),
                        datasetLabel: ds.label || '',
                        color: ds.borderColor || '#3b82f6'
                    });
                }
            } else {
                // Even without visible points, record positions for tooltip on line segments
                for (let i = 0; i < data.length; i++) {
                    if (data[i] === null || data[i] === undefined) continue;
                    this._pointPositions.push({
                        x: toX(i), y: mapY(data[i]),
                        index: i,
                        label: labels[i] || '',
                        value: data[i].toFixed(2),
                        datasetLabel: ds.label || '',
                        color: ds.borderColor || '#3b82f6'
                    });
                }
            }
        });

        // 散点叠加绘制（用于回归分析图的散点）
        if (this._scatterPoints && this._scatterPoints.length > 0) {
            const pts = this._scatterPoints;
            const xs = pts.map(p => p.x);
            const ys = pts.map(p => p.y);
            const pxMin = Math.min(...labels.map(Number).filter(v => !isNaN(v)));
            const pxMax = Math.max(...labels.map(Number).filter(v => !isNaN(v)));
            if (isFinite(pxMin) && isFinite(pxMax)) {
                const xRange = pxMax - pxMin || 1;
                const scatterToX = (xVal) => padding.left + ((xVal - pxMin) / xRange) * chartW;
                ctx.fillStyle = '#3b82f6';
                ctx.strokeStyle = 'rgba(59,130,246,0.3)';
                ctx.lineWidth = 1;
                pts.forEach(p => {
                    const sx = scatterToX(p.x);
                    const sy = toY(p.y);
                    ctx.beginPath();
                    ctx.arc(sx, sy, 5, 0, Math.PI * 2);
                    ctx.fill();
                    ctx.stroke();
                });
            }
        }

        // 图例
        this._drawLegend(datasets, padding);
    }

    /* ---------- 柱状图 ---------- */
    _drawBar() {
        const ctx = this.ctx;
        const w = this.width, h = this.height;
        const datasets = this.data.datasets;
        const labels = this.data.labels || [];

        const padding = { top: 36, right: 20, bottom: 50, left: 60 };
        const chartW = w - padding.left - padding.right;
        const chartH = h - padding.top - padding.bottom;
        if (chartW <= 0 || chartH <= 0) return;

        const data = datasets[0].data || [];
        const colors = datasets[0].backgroundColor || '#3b82f6';
        const n = data.length;
        if (n === 0) return;

        let yMin = 0, yMax = Math.max(...data.map(Math.abs)) * 1.2;
        if (yMax === 0) yMax = 1;

        ctx.strokeStyle = 'rgba(255,255,255,0.06)';
        ctx.lineWidth = 1;
        ctx.fillStyle = '#7b8da6';
        ctx.font = '12px Microsoft YaHei, sans-serif';
        ctx.textAlign = 'right';
        const gridLines = 5;
        for (let i = 0; i <= gridLines; i++) {
            const val = yMin + (yMax - yMin) * (i / gridLines);
            const y = padding.top + chartH - (i / gridLines) * chartH;
            ctx.beginPath();
            ctx.moveTo(padding.left, y);
            ctx.lineTo(w - padding.right, y);
            ctx.stroke();
            ctx.fillText(val.toFixed(2), padding.left - 8, y + 4);
        }

        const gap = chartW / n;
        const barW = Math.min(36, gap * 0.65);
        for (let i = 0; i < n; i++) {
            const x = padding.left + gap * i + (gap - barW) / 2;
            const barH = (Math.abs(data[i]) / yMax) * chartH;
            const y = padding.top + chartH - barH;
            const color = Array.isArray(colors) ? colors[i] || '#3b82f6' : colors;

            const r = Math.min(4, barW / 2);
            ctx.beginPath();
            ctx.moveTo(x + r, y);
            ctx.lineTo(x + barW - r, y);
            ctx.quadraticCurveTo(x + barW, y, x + barW, y + r);
            ctx.lineTo(x + barW, y + barH);
            ctx.lineTo(x, y + barH);
            ctx.lineTo(x, y + r);
            ctx.quadraticCurveTo(x, y, x + r, y);
            ctx.closePath();
            ctx.fillStyle = color;
            ctx.fill();

            ctx.fillStyle = '#7b8da6';
            ctx.textAlign = 'center';
            ctx.font = '11px Microsoft YaHei, sans-serif';
            ctx.fillText(labels[i] || '', x + barW / 2, h - padding.bottom + 20);

            // Record bar position for tooltip
            this._pointPositions.push({
                x: x + barW / 2, y: y,
                index: i,
                label: labels[i] || '',
                value: data[i].toFixed(2),
                datasetLabel: datasets[0].label || '',
                color: Array.isArray(colors) ? (colors[i] || '#3b82f6') : colors
            });
        }

        this._drawLegend(datasets, padding);
    }

    /* ---------- 饼图(甜甜圈) ---------- */
    _drawDoughnut() {
        const ctx = this.ctx;
        const w = this.width, h = this.height;
        const datasets = this.data.datasets;
        const labels = this.data.labels || [];

        const ds = datasets[0];
        const data = ds.data || [];
        const colors = ds.backgroundColor || [];
        const total = data.reduce((s, v) => s + v, 0);
        if (total === 0) return;

        const centerX = w / 2;
        const centerY = h * 0.42;
        const outerR = Math.min(w * 0.4, h * 0.38);
        const innerR = outerR * 0.55;

        let angle = -Math.PI / 2;
        for (let i = 0; i < data.length; i++) {
            const sliceAngle = (data[i] / total) * Math.PI * 2;
            ctx.beginPath();
            ctx.arc(centerX, centerY, outerR, angle, angle + sliceAngle);
            ctx.arc(centerX, centerY, innerR, angle + sliceAngle, angle, true);
            ctx.closePath();
            ctx.fillStyle = colors[i] || '#3b82f6';
            ctx.fill();
            ctx.strokeStyle = ds.borderColor || '#131f36';
            ctx.lineWidth = ds.borderWidth || 2;
            ctx.stroke();

            const midAngle = angle + sliceAngle / 2;
            const labelR = outerR + 16;
            const lx = centerX + Math.cos(midAngle) * labelR;
            const ly = centerY + Math.sin(midAngle) * labelR;
            ctx.fillStyle = '#b0bdd0';
            ctx.font = '12px Microsoft YaHei, sans-serif';
            ctx.textAlign = midAngle > Math.PI / 2 && midAngle < Math.PI * 1.5 ? 'right' : 'left';
            ctx.fillText(`${labels[i]} ${((data[i]/total)*100).toFixed(0)}%`, lx, ly + 4);

            angle += sliceAngle;
        }

        // 底部图例
        const legendY = h - 24;
        const legendStartX = w / 2 - data.length * 55;
        for (let i = 0; i < data.length; i++) {
            const x = legendStartX + i * 110;
            ctx.fillStyle = colors[i] || '#3b82f6';
            ctx.fillRect(x, legendY, 12, 12);
            ctx.fillStyle = '#b0bdd0';
            ctx.font = '12px Microsoft YaHei, sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(labels[i], x + 16, legendY + 10);
        }
    }

    /* ---------- 图例 ---------- */
    _drawLegend(datasets, padding) {
        const ctx = this.ctx;
        const legendOpts = (this.options.plugins && this.options.plugins.legend) || {};
        const labels = legendOpts.labels || {};
        const color = labels.color || '#b0bdd0';
        const fontSize = (labels.font && labels.font.size) || 12;

        const items = datasets.filter(ds => ds.label);
        if (items.length === 0) return;

        ctx.font = `${fontSize}px Microsoft YaHei, sans-serif`;

        // 测量每个图例项宽度
        const itemWidths = items.map(ds => ctx.measureText(ds.label).width + 28);
        const totalW = itemWidths.reduce((s, w) => s + w, 0);
        let startX = this.width / 2 - totalW / 2;
        const y = 16;

        items.forEach((ds, i) => {
            ctx.fillStyle = ds.borderColor || '#3b82f6';
            ctx.fillRect(startX, y - 5, 18, 3);
            ctx.fillStyle = color;
            ctx.textAlign = 'left';
            ctx.fillText(ds.label, startX + 22, y);
            startX += itemWidths[i];
        });
    }
}
