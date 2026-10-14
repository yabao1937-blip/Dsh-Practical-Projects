/* Shared GPT objective editor. Drafts never drive calculations before Apply. */
const DecisionTarget = {
    opened:false, busy:false, revision:null,
    init(page) {
        if(this.initialized) return;
        this.initialized=true;
        this.page=page;
        document.getElementById('decision-target-open').addEventListener('click',()=>this.opened?this.close():this.open(page));
        document.getElementById('decision-target-cancel').addEventListener('click',()=>this.close());
        document.getElementById('decision-target-form').addEventListener('submit',event=>{event.preventDefault();this.submit(page);});
        document.getElementById('decision-target-form').addEventListener('keydown',event=>{
            if(event.key==='Escape') {event.stopPropagation();this.close();}
        });
    },
    sync(page) {
        const button=document.getElementById('decision-target-open');
        if(!button) return;
        button.hidden=!DecisionSources.enabled(page);
        const c=DecisionSources.config;
        button.textContent=c?`目标灰分 ${c.target_min}–${c.target_max}%`:'目标灰分范围';
        // Polls and another workstation's changes must not overwrite a draft.
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
            document.getElementById('decision-target-open').setAttribute('aria-expanded','true');
            this.opened=true; this.message('点击应用后，即时与小时建议采用新范围。');
            document.getElementById('decision-target-min').focus();
        } catch(error) { App.showToast('目标范围读取失败：'+error.message,'error'); }
        finally {this.busy=false;}
    },
    close() {
        if(this.busy) return;
        this.opened=false;
        document.getElementById('decision-target-form').hidden=true;
        document.getElementById('decision-target-open').setAttribute('aria-expanded','false');
        document.getElementById('decision-target-open').focus();
    },
    async submit(page) {
        if(this.busy) return;
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
