/* Local decorative flow field. No telemetry, model or store dependencies. */
class DecisionBackground {
    constructor(canvas) {
        this.canvas = canvas;
        this.context = canvas?.getContext('2d');
        this.running = false;
        this.frame = null;
        this.time = 0;
        this.width = 0;
        this.height = 0;
        this.lastFrame = 0;
        this.lastPaint = 0;
        this.tick = this.tick.bind(this);
        this.resize();
    }

    resize() {
        if (!this.context) return; // The original SVG remains visible without Canvas support.
        const rect = this.canvas.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const scale = Math.min(window.devicePixelRatio || 1, 1.25, 1440 / Math.max(rect.width, rect.height));
        const width = Math.round(rect.width * scale), height = Math.round(rect.height * scale);
        if (width === this.canvas.width && height === this.canvas.height && rect.width === this.width && rect.height === this.height) return;
        this.width = rect.width;
        this.height = rect.height;
        this.canvas.width = width;
        this.canvas.height = height;
        this.context.setTransform(width / rect.width, 0, 0, height / rect.height, 0, 0);
        this.paint();
    }

    setRunning(running) {
        running = !!running && !!this.context;
        if (running === this.running) return;
        this.running = running;
        if (running) {
            this.resize();
            this.lastFrame = performance.now();
            this.lastPaint = this.lastFrame;
            this.frame = requestAnimationFrame(this.tick);
        } else {
            cancelAnimationFrame(this.frame);
            this.frame = null;
        }
    }

    tick(now) {
        if (!this.running) return;
        this.time += Math.min(.1, Math.max(0, now - this.lastFrame) / 1000);
        this.lastFrame = now;
        if (now - this.lastPaint >= 1000 / 30) {
            this.lastPaint = now;
            this.paint();
        }
        this.frame = requestAnimationFrame(this.tick);
    }

    paint() {
        const ctx = this.context, w = this.width, h = this.height, time = this.time;
        if (!ctx || !w || !h) return;
        ctx.clearRect(0, 0, w, h);
        const amplitude = Math.min(110, h * .12), gap = 32, descent = time * 18 % gap;
        const wave = x => amplitude * (Math.sin(x / w * 6.3 - time * .18) + .6 * Math.sin(x / w * 2.8 + time * .1));
        ctx.lineWidth = .75;
        ctx.strokeStyle = 'rgba(211,230,255,.14)';
        // Identical rows wrap by one pitch, so the downward flow has no loop seam.
        for (let y = -amplitude * 2 + descent; y < h + amplitude * 2; y += gap) {
            ctx.beginPath();
            for (let step = 0; step <= 40; step++) {
                const x = step / 40 * w, yy = y + wave(x);
                if (step) ctx.lineTo(x, yy); else ctx.moveTo(x, yy);
            }
            ctx.stroke();
        }
        // Fine descending glints give direction without flashing or moving any text.
        const count = Math.max(32, Math.min(100, Math.round(w / 15)));
        for (let i = 0; i < count; i++) {
            const spread = (i * .61803398875 + .17) % 1;
            const depth = (i * .41421356237 + .31) % 1;
            const y = (depth * (h + 120) + time * (32 + spread * 42)) % (h + 120) - 60;
            const x = spread * w + Math.sin(y * .004 + time * .2 + i) * 12;
            const length = 12 + depth * 24;
            const light = ctx.createLinearGradient(x, y - length, x, y);
            light.addColorStop(0, 'rgba(220,240,255,0)');
            light.addColorStop(1, `rgba(220,240,255,${.12 + depth * .2})`);
            ctx.strokeStyle = light;
            ctx.lineWidth = .7 + depth * .5;
            ctx.beginPath();
            ctx.moveTo(x - 2, y - length);
            ctx.quadraticCurveTo(x - 1, y - length / 2, x, y);
            ctx.stroke();
        }
    }
}
