/* Controller-only tests: no browser, no production requests or persistent writes. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const context={console,Date,Map,Number,String,Object,Math,Array,JSON,URLSearchParams,AbortSignal,
    document:{addEventListener(){}},FormData:class {constructor(form){this.form=form;}[Symbol.iterator](){return Object.entries(this.form.values)[Symbol.iterator]();}},
    Api:{},DecisionAshHistory:{escape:s=>String(s??'').replaceAll('<','&lt;')},crypto:{randomUUID:()=> 'test-id'}};
context.window=context;
vm.createContext(context);
vm.runInContext(fs.readFileSync('frontend/js/decision-evidence.js','utf8')+';this.e=DecisionEvidence;',context);
const e=context.e;
async function main(){
    e.selected='action';e.result={revision:0};let status='',resolve,reject;
    const form={dataset:{eForm:'confirmation'},values:{note:'操作员的草稿',operator:'test'}};
    e.dialog={open:true,querySelectorAll:selector=>selector==='[data-e-form]'?[form]:[],querySelector:()=>({set textContent(v){status=v;}})};
    context.Api.decisionEvidence=()=>new Promise((a,b)=>{resolve=a;reject=b;});
    const p=e.submit(form);
    assert(e.busy);
    e.dialog.open=false;form.values={};e.saveDraft();
    reject(new Error('timeout'));await p;
    assert.equal(e.drafts.get('action')[0][1].note,'操作员的草稿','closing a pending save cannot overwrite a draft with disabled form data');
    assert.equal(e.requests.get('action:confirmation').client_id,'test-id');
    assert(status.includes('输入已保留'));
    e.serverStart='2026-10-10T02:00:00Z';assert.equal(e.hourStart(),e.serverStart);
    // API must not treat HTTP 200 with invalid or unrelated JSON as confirmed persistence.
    vm.runInContext(fs.readFileSync('frontend/js/api.js','utf8')+';this.api=Api;',context);
    const api=context.api;api.writeToken=()=>'';
    context.fetch=async()=>({ok:true,json:async()=>null});
    await assert.rejects(api.decisionEvidence('/actions/x/entries',{client_id:'x'}),/返回异常/);
    context.fetch=async()=>({ok:true,json:async()=>({entries:[]})});
    await assert.rejects(api.decisionEvidence('/actions/x/entries',{client_id:'x'}),/尚未确认/);
    context.fetch=async()=>({ok:true,json:async()=>({entries:[{client_id:'x'}]})});
    assert.equal((await api.decisionEvidence('/actions/x/entries',{client_id:'x'})).entries[0].client_id,'x');
    console.log('Evidence UI: pending-close draft retention, stable retry identifier, server hour and confirmed save response passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
