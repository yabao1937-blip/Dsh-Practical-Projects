/* ========================================
   AI 解读助手(只读)
   - 悬浮按钮 + 聊天面板,全页面可用
   - 只解释、不操作:后端无工具调用、接口零写入,历史只存 localStorage
   - 流式输出(fetch reader),纯文本渲染(textContent,无 HTML 注入)
   ======================================== */
const Assistant = {
    open: false,
    sending: false,
    HISTORY_KEY: 'dmcs_assistant_history',
    HISTORY_CAP: 100,
    elements: {},

    init() {
        this.buildUi();
        this.loadHistory();
    },

    buildUi() {
        if (document.getElementById('ai-assistant-btn')) return;

        const btn = document.createElement('button');
        btn.id = 'ai-assistant-btn';
        btn.title = 'AI 解读助手(只读,只解释不操作)';
        btn.textContent = 'AI';
        btn.addEventListener('click', () => this.toggle());

        const panel = document.createElement('div');
        panel.id = 'ai-assistant-panel';
        panel.innerHTML = `
            <div class="ai-header">
                <span class="ai-title">AI 解读助手</span>
                <span class="ai-ro-badge" title="后端零工具调用、接口零写入;对话只存本地">只读</span>
                <span class="ai-actions">
                    <button class="ai-icon-btn" id="ai-clear-btn" title="清空对话">⌫</button>
                    <button class="ai-icon-btn" id="ai-close-btn" title="收起">✕</button>
                </span>
            </div>
            <div class="ai-subtitle">可解释系统概念/算法/指标与当前数据;不能执行任何操作</div>
            <div class="ai-messages" id="ai-messages"></div>
            <div class="ai-input-row">
                <input id="ai-input" type="text" placeholder="例如:为什么建议密度是这个值?"
                       maxlength="2000" autocomplete="off">
                <button id="ai-send-btn" class="btn btn-primary btn-sm">发送</button>
            </div>`;
        document.body.appendChild(panel);
        document.body.appendChild(btn);

        this.elements = { btn, panel, messages: panel.querySelector('#ai-messages'),
                          input: panel.querySelector('#ai-input'),
                          send: panel.querySelector('#ai-send-btn') };
        panel.querySelector('#ai-close-btn').addEventListener('click', () => this.toggle(false));
        panel.querySelector('#ai-clear-btn').addEventListener('click', () => {
            this.history = [];
            this.saveHistory();
            this.renderMessages();
        });
        this.elements.send.addEventListener('click', () => this.send());
        this.elements.input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.isComposing) this.send();
        });
    },

    toggle(force) {
        this.open = (force !== undefined) ? force : !this.open;
        this.elements.panel.classList.toggle('shown', this.open);
        this.elements.btn.classList.toggle('active', this.open);
        if (this.open) setTimeout(() => this.elements.input.focus(), 50);
    },

    // ---------- 历史(仅本地) ----------
    history: [],
    loadHistory() {
        try {
            const raw = localStorage.getItem(this.HISTORY_KEY);
            if (raw) this.history = JSON.parse(raw) || [];
        } catch (e) { this.history = []; }
        this.renderMessages();
    },
    saveHistory() {
        try { localStorage.setItem(this.HISTORY_KEY, JSON.stringify(this.history.slice(-this.HISTORY_CAP))); }
        catch (e) { /* 配额不足时忽略 */ }
    },

    renderMessages() {
        const box = this.elements.messages;
        box.textContent = '';
        if (!this.history.length) {
            const hint = document.createElement('div');
            hint.className = 'ai-hint';
            hint.textContent = '试试问:R² 和 Q² 有什么区别?/ 为什么当前建议密度是这个值?/ 502 灰分仪测的是什么?';
            box.appendChild(hint);
            return;
        }
        this.history.forEach(m => box.appendChild(this.messageEl(m.role, m.content)));
        box.scrollTop = box.scrollHeight;
    },

    messageEl(role, text) {
        const wrap = document.createElement('div');
        wrap.className = 'ai-msg ' + (role === 'user' ? 'ai-user' : 'ai-bot');
        const bubble = document.createElement('div');
        bubble.className = 'ai-bubble';
        bubble.textContent = text;          // 纯文本渲染,杜绝 HTML 注入
        wrap.appendChild(bubble);
        return wrap;
    },

    appendLive(role) {
        const el = this.messageEl(role, role === 'user' ? '' : '…');
        this.elements.messages.appendChild(el);
        this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
        return el.querySelector('.ai-bubble');
    },

    // ---------- 发送(流式) ----------
    async send() {
        if (this.sending) return;
        const q = this.elements.input.value.trim();
        if (!q) return;
        this.elements.input.value = '';
        this.history.push({ role: 'user', content: q });
        const userEl = this.appendLive('user');
        userEl.textContent = q;
        const botBubble = this.appendLive('assistant');
        botBubble.textContent = '思考中…';
        this.sending = true;
        this.elements.send.disabled = true;

        try {
            const res = await fetch('/api/v1/assistant/ask', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: q,
                    history: this.history.slice(0, -1).slice(-12),
                }),
            });
            if (!res.ok) {
                let detail = 'HTTP ' + res.status;
                try { detail = (await res.json()).detail || detail; } catch (e) {}
                throw new Error(String(detail));
            }
            botBubble.textContent = '';
            const reader = res.body.getReader();
            const dec = new TextDecoder('utf-8');
            let acc = '';
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                acc += dec.decode(value, { stream: true });
                botBubble.textContent = acc;      // 仍是纯文本
                this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
            }
            acc += dec.decode();
            botBubble.textContent = acc || '(空回复)';
            this.history.push({ role: 'assistant', content: acc });
        } catch (e) {
            botBubble.textContent = '请求失败:' + e.message +
                '\n(后端未启动或未配置 ZAI_API_KEY 时会这样;详见接口返回的提示)';
            this.history.push({ role: 'assistant', content: botBubble.textContent });
        } finally {
            this.sending = false;
            this.elements.send.disabled = false;
            this.saveHistory();
            this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
        }
    },
};

window.Assistant = Assistant;
