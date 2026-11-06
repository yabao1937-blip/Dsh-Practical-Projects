/* Shared GPT objective editor. Drafts never drive calculations before Apply. */
const DecisionTarget = {
    opened:false, busy:false, revision:null,
    init(page) {
        if(this.initialized) return;
        this.initialized=true;
        this.page=page;
        // 2026-11-06：入口从页头按钮改为卡片内目标灰分（span，卡片本身是 button 不能嵌套）
        document.addEventListener('click',event=>{
            if(!event.target.closest('[data-target-open]')) return;
            if(event.target.closest('#decision-target-form')) return;
            this.opened?this.close():this.open(this.page);
        });
        document.getElementById('decision-target-cancel').addEventListener('click',()=>this.close());
        document.getElementById('decision-target-form').addEventListener('submit',event=>{event.preventDefault();this.submit(page);});
        document.getElementById('decision-target-form').addEventListener('keydown',event=>{
            if(event.key==='Escape') {event.stopPropagation();this.close();}
        });
    },
    sync(page) {
        // 触发按钮已并入建议密度卡片（由 renderGraph 渲染最新范围）；此处仅保留表单可见性兼容。
    },
    message(text) { document.getElementById('decision-target-status').textContent=text; },
    async open(page) {
        if(this.busy) return;
        this.busy=true;
        try {
            await DecisionSources.ensure(page);
            const config=await Api.decisionSources();
            DecisionSources.apply(page,config);
            this.revision=DecisionSources.config.revision;
            document.getElementById('decision-target-min').value=DecisionSources.config.target_min;
            document.getElementById('decision-target-max').value=DecisionSources.config.target_max;
            document.getElementById('decision-target-form').hidden=false;
            document.getElementById('decision-target-open')?.setAttribute('aria-expanded','true');
            const readOnly=!!Api.isReadOnlyTest?.();
            const fields=document.getElementById('decision-target-fields');
            fields.disabled=false;
            fields.querySelectorAll('input,button[type="submit"]').forEach(control=>{control.disabled=readOnly;});
            this.opened=true; this.message(readOnly ? Api.readOnlyMessage : '点击应用后，即时与小时建议采用新范围。');
            document.getElementById('decision-target-min').focus();
        } catch(error) { App.showToast('目标范围读取失败：'+error.message,'error'); }
        finally {this.busy=false;}
    },
    close() {
        if(this.busy) return;
        this.opened=false;
        document.getElementById('decision-target-form').hidden=true;
        const trigger=document.getElementById('decision-target-open');
        if(trigger){trigger.setAttribute('aria-expanded','false');trigger.focus();}
    },
    async submit(page) {
        if(this.busy) return;
        if(Api.isReadOnlyTest?.()) {this.message(Api.readOnlyMessage);return;}
        const low=document.getElementById('decision-target-min').value.trim();
        const high=document.getElementById('decision-target-max').value.trim();
        const min=Number(low),max=Number(high);
        if(!low || !high || !Number.isFinite(min) || !Number.isFinite(max) || min<0 || max>100 || min>max) {
            this.message('请输入 0–100% 的范围，下限不能大于上限。'); return;
        }
        this.busy=true;
        const fields=document.getElementById('decision-target-fields'); fields.disabled=true;
        this.message('正在保存…');
        let saved=false;
        try {
            await DecisionSources.changeTarget(page,this.revision,min,max);
            saved=true; App.showToast('目标灰分已应用，即时与小时建议正在更新','success');
        } catch(error) {
            this.message('未应用：'+error.message+'。请关闭后重新打开，核对服务器范围。');
        } finally {
            fields.disabled=false; this.busy=false;
            if(saved) this.close();
        }
    },
};
