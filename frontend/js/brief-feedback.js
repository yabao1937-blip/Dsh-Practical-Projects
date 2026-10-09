/* Hour-linked, append-only feedback. Drafts live outside the refreshed report DOM. */
const BriefFeedback = {
    drafts: new Map(),
    editor: null,
    endpoint: () => (window.Api?.base || '/api/v1') + '/hourly-brief/feedback',
    draft(hour) {
        if (!this.drafts.has(hour)) this.drafts.set(hour, {text:'', clientId:null, submitted:null, saving:false, message:'', failed:false});
        return this.drafts.get(hour);
    },
    uuid() {
        // randomUUID is unavailable on plain HTTP LAN addresses.
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
        const hex = [...bytes].map(b => b.toString(16).padStart(2, '0')).join('');
        return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
    },
    cell(row, index) {
        const e = v => BriefPage.escape(v), feedback = row.feedback, latest = feedback?.latest;
        return `<td class="brief-feedback-cell" data-brief-column="feedback">
            ${latest ? `<p class="brief-feedback-preview" title="${e(latest.content)}">${e(latest.content)}</p><small class="brief-muted">${e(BriefPage.localTime(latest.created_at))} · ${feedback.total} 条</small>` : '<span class="brief-muted">暂无反馈</span>'}
            <button class="btn btn-sm" onclick="BriefFeedback.open(${index})" aria-label="${e(BriefPage.localTime(row.hour).slice(0,16))} 反馈">${latest ? '查看 / 补充' : '填写反馈'}</button>
        </td>`;
    },
    active(editor = this.editor) {
        return editor && this.editor === editor && document.getElementById('brief-feedback-editor')?.dataset.hour === editor.hour
            && !document.getElementById('modal-overlay')?.classList.contains('hidden');
    },
    open(index) {
        const row = BriefPage.result?.rows[index];
        if (!row) return;
        const e = v => BriefPage.escape(v), draft = this.draft(row.hour);
        const editor = this.editor = {hour:row.hour, items:[], cursor:null, loading:false, historySequence:0};
        const context = [['coarse_ash','粗精煤泥灰分','%'], ['total_ash','总灰分','%'],
            ['density','当前分选密度','g/cm³'], ['suggested_density','建议密度','g/cm³']];
        App.openModal('人工经验反馈 · ' + BriefPage.localTime(row.hour).slice(0,16),
            `<div id="brief-feedback-editor" data-hour="${e(row.hour)}">
                <div class="brief-feedback-context">${context.map(([key,label,unit]) => `<div><small>${label}</small><strong>${e(BriefPage.value(row.cells[key],key))} <small>${unit}</small></strong></div>`).join('')}</div>
                <label class="brief-feedback-label" for="brief-feedback-text">填写反馈</label>
                <textarea id="brief-feedback-text" maxlength="2000" rows="5" oninput="BriefFeedback.input(this.value)" placeholder="例如：预测灰分偏高；现场建议维持密度，并说明判断依据或调整后的效果。">${e(draft.text)}</textarea>
                <div class="brief-feedback-hint"><span>可多次补充，保留每次内容和录入时间。</span><span id="brief-feedback-count"></span></div>
                <p id="brief-feedback-message" role="status" aria-live="polite"></p>
                <h4 class="brief-feedback-label">反馈记录</h4><div id="brief-feedback-history" class="brief-feedback-history"></div>
            </div>`,
            '<button class="btn" onclick="App.closeModal()">关闭</button><button id="brief-feedback-save" class="btn btn-primary" onclick="BriefFeedback.save()">保存反馈</button>');
        this.syncDraft();
        this.loadHistory(editor);
        document.getElementById('brief-feedback-text')?.focus();
    },
    input(value) {
        if (!this.active()) return;
        const draft = this.draft(this.editor.hour);
        if (draft.saving) return;
        draft.text = value;
        draft.message = ''; draft.failed = false;
        this.syncDraft();
    },
    syncDraft() {
        if (!this.active()) return;
        const draft = this.draft(this.editor.hour), textarea = document.getElementById('brief-feedback-text');
        textarea.disabled = draft.saving;
        if (textarea.value !== draft.text) textarea.value = draft.text;
        document.getElementById('brief-feedback-count').textContent = `${draft.text.length} / 2000`;
        const button = document.getElementById('brief-feedback-save');
        button.disabled = draft.saving || !draft.text.trim();
        button.textContent = draft.saving ? '保存中…' : '保存反馈';
        const message = document.getElementById('brief-feedback-message');
        message.textContent = draft.message;
        message.className = draft.failed ? 'brief-error' : 'brief-feedback-success';
    },
    async loadHistory(editor = this.editor, more = false) {
        if (!this.active(editor) || (more && (editor.loading || !editor.cursor))) return;
        const sequence = ++editor.historySequence;
        editor.loading = true; editor.error = '';
        this.renderHistory(editor);
        const params = new URLSearchParams({hour:editor.hour, limit:'20'});
        if (more) params.set('before_id', editor.cursor);
        try {
            const response = await fetch(this.endpoint() + '?' + params, {cache:'no-store', signal:AbortSignal.timeout(15000)});
            if (!response.ok) throw new Error('反馈记录读取失败，请重试。');
            const data = await response.json();
            if (!this.active(editor) || sequence !== editor.historySequence) return;
            editor.items = more ? [...editor.items, ...data.items] : data.items;
            editor.cursor = data.next_before_id; editor.total = data.total;
        } catch (error) {
            if (this.active(editor) && sequence === editor.historySequence) editor.error = '反馈记录读取失败，请重试。';
        } finally {
            if (this.active(editor) && sequence === editor.historySequence) { editor.loading = false; this.renderHistory(editor); }
        }
    },
    renderHistory(editor) {
        if (!this.active(editor)) return;
        const e = v => BriefPage.escape(v);
        document.getElementById('brief-feedback-history').innerHTML =
            editor.items.map(item => `<article class="brief-feedback-entry"><small class="brief-muted">${e(BriefPage.localTime(item.created_at))}</small><p>${e(item.content)}</p></article>`).join('') +
            (editor.error ? `<p class="brief-error">${e(editor.error)} <button class="btn btn-sm" onclick="BriefFeedback.loadHistory()">重试</button></p>` :
                editor.loading ? '<p class="brief-muted">正在读取…</p>' : !editor.items.length ? '<p class="brief-muted">暂无反馈，欢迎记录现场经验。</p>' :
                `<p class="brief-muted">共 ${editor.total} 条${editor.cursor ? ' <button class="btn btn-sm" onclick="BriefFeedback.loadHistory(BriefFeedback.editor, true)">查看更多</button>' : ''}</p>`);
    },
    async save() {
        if (!this.active()) return;
        const hour = this.editor.hour, draft = this.draft(hour), content = draft.text.trim();
        if (draft.saving || !content) return;
        if (!draft.clientId || draft.submitted !== content) { draft.clientId = this.uuid(); draft.submitted = content; }
        draft.saving = true; draft.message = ''; draft.failed = false;
        this.syncDraft();
        const headers = {'Content-Type':'application/json'}, token = window.Api?.writeToken?.();
        if (token) headers['X-DMCS-Token'] = token;
        try {
            const response = await fetch(this.endpoint(), {method:'POST', headers, signal:AbortSignal.timeout(15000),
                body:JSON.stringify({hour, content, client_id:draft.clientId})});
            if (!response.ok) {
                const messages = {401:'保存未授权，请刷新页面后重试。',404:'该小时简报不存在，无法保存。',
                    409:'提交标识冲突，请再次点击保存。',422:'请检查内容，最多 2000 字且不能包含控制字符。'};
                if (response.status === 409) draft.clientId = null;
                throw new Error(messages[response.status] || '保存失败，请重试。');
            }
            await response.json();
            draft.text = ''; draft.clientId = null; draft.submitted = null;
            draft.message = '反馈已保存。';
            // Invalidate a pre-save list request so it cannot restore a stale summary.
            BriefPage.generate();
            if (this.active() && this.editor.hour === hour) this.loadHistory(this.editor);
        } catch (error) {
            draft.failed = true;
            draft.message = (error.name === 'TimeoutError' || error instanceof TypeError ? '连接中断或超时，请重试。' : error.message) + ' 输入内容已保留。';
        } finally {
            draft.saving = false;
            if (this.active() && this.editor.hour === hour) this.syncDraft();
        }
    },
};
