// Pure VM, no browser/network/database: independent views and shared save state.
const assert=require('node:assert/strict'), fs=require('node:fs'), path=require('node:path'), vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../../frontend/js/manual-measurements.js'),'utf8');

function fixture({collection=true,readOnly=false}={}) {
    const moves=[], observers=[];
    let document;
    class Element {
        constructor(tag='div') {this.tagName=tag.toLowerCase();this.children=[];this.listeners={};this.dataset={};this.style={};this.value='';this.disabled=false;this.hidden=false;this.open=false;this.className='';this.id='';this.parentElement=null;}
        get classList() {
            const current=()=>new Set(this.className.split(/\s+/).filter(Boolean));
            return {contains:name=>current().has(name),add:name=>{const set=current();set.add(name);this.className=[...set].join(' ');},remove:name=>{const set=current();set.delete(name);this.className=[...set].join(' ');},toggle:(name,enabled)=>{const set=current();if(enabled ?? !set.has(name))set.add(name);else set.delete(name);this.className=[...set].join(' ');}};
        }
        set innerHTML(html) {
            this._html=html;this.children=[];
            const stack=[this];
            for (const token of html.matchAll(/<\/?[a-z][^>]*>/gi)) {
                const text=token[0], closing=text.startsWith('</'), tag=text.match(/^<\/?([a-z0-9-]+)/i)[1].toLowerCase();
                if (closing) {if(stack.length>1)stack.pop();continue;}
                const child=new Element(tag);
                for (const attr of text.matchAll(/([a-z][a-z0-9-]*)="([^"]*)"/gi)) {
                    if(attr[1]==='id')child.id=attr[2];else if(attr[1]==='class')child.className=attr[2];else child[attr[1]]=attr[2];
                }
                child.hidden=/\shidden(?:\s|>)/.test(text);child.disabled=/\sdisabled(?:\s|>)/.test(text);
                stack.at(-1).append(child);
                if(!['input','br','hr','img','meta','link'].includes(tag))stack.push(child);
            }
        }
        get innerHTML(){return this._html || '';}
        append(child) {
            if(child.parentElement){moves.push(child.id);child.parentElement.children=child.parentElement.children.filter(item=>item!==child);}
            this.children.push(child);child.parentElement=this;
        }
        setAttribute(name,value){this[name]=value;}
        removeAttribute(name){delete this[name];}
        addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}
        async dispatch(type,extra={}){await Promise.all((this.listeners[type] || []).map(fn=>fn({target:this,preventDefault(){},stopPropagation(){},...extra})));}
        matches(selector){return selector.startsWith('#')?this.id===selector.slice(1):selector.startsWith('.')?this.classList.contains(selector.slice(1)):this.tagName===selector;}
        querySelector(selector){const parts=selector.split(/\s+/);const walk=node=>{for(const child of node.children){if(child.matches(parts[0])){if(parts.length===1)return child;const nested=child.querySelector(parts.slice(1).join(' '));if(nested)return nested;}const found=walk(child);if(found)return found;}return null;};return walk(this);}
        closest(selector){return this.matches(selector)?this:this.parentElement?.closest(selector);}
        focus(){document.activeElement=this;}
        show(){this.open=true;}
        close(){this.open=false;void this.dispatch('close');}
        getBoundingClientRect(){return {left:100,top:80,width:620,height:790};}
        setPointerCapture(){} hasPointerCapture(){return false;} releasePointerCapture(){}
    }
    const body=new Element('body');
    document={body,hidden:false,activeElement:null,listeners:{},createElement:tag=>new Element(tag),
        getElementById:id=>body.querySelector('#'+id),addEventListener(type,fn){(this.listeners[type] ||= []).push(fn);}};
    const add=(id,parent=body,classes='')=>{const el=new Element();el.id=id;el.className=classes;parent.append(el);return el;};
    const decision=add('page-decision',body,'active'), workspace=add('decision-workspace',decision,'is-light');
    add('decision-manual-open',workspace);
    let collect, panel;
    if(collection){collect=add('page-collect');panel=add('manual-measurements-panel',collect,'panel');}
    const calls={metrics:0,history:0,saves:[],operations:0,recent:0,interval:0}, control={readOnly,saveMode:'ok',saveResolve:null,metricsResolve:null};
    const definitions=[{key:'heavy_clean_coal_ash',name:'重介精煤灰分',unit:'%',maximum:100},
        {key:'raw_coal_slime_content',name:'原煤煤泥含量',unit:'%',maximum:100},
        {key:'coarse_slime_ash',name:'粗精煤泥灰分',unit:'%',maximum:100,archive_only:true},
        {key:'coarse_slime_amount',name:'旧粗精煤泥量',unit:'t/h',maximum:null}];
    const record=body=>({id:1,name:definitions.find(item=>item.key===body.metric).name,unit:'%',...body,
        observed_at:body.observed_at || '2026-10-17T04:00:00.000+00:00',created_at:'2026-10-17T04:00:00.000+00:00'});
    let counter=0;
    const context=vm.createContext({document,location:{protocol:'http:'},console,Date,Intl,Promise,Math,setTimeout,clearTimeout,
        innerWidth:1280,innerHeight:1000,addEventListener(){},crypto:{randomUUID:()=>`isolated-${++counter}`},
        MutationObserver:class {constructor(callback){this.callback=callback;observers.push(this);}observe(){}},
        Api:{isReadOnlyTest:()=>control.readOnly,readOnlyMessage:'测试服只读，请在主机保存。',
            async getManualMeasurementMetrics(){calls.metrics++;if(control.metricsGate)await new Promise(resolve=>{control.metricsResolve=resolve;});return definitions;},
            async getManualMeasurements(){calls.history++;return {total:0,items:[]};},
            async saveManualMeasurement(body,definition){calls.saves.push({body:{...body},definition:{...definition}});
                if(control.saveMode==='hold')return new Promise(resolve=>{control.saveResolve=()=>resolve(record(body));});
                if(control.saveMode==='lost')throw new Error('未收到保存确认');return record(body);}},
        Operations:{manualMeasurementSaved(){calls.operations++;}},DecisionRecentAsh:{measurementSaved(){calls.recent++;}},
        DecisionAshHistory:{measurementSaved(){calls.interval++;}},DecisionOverviewPage:{},App:{store:{original:'unchanged'}}});
    context.window=context;
    vm.runInContext(source,context);
    const manual=vm.runInContext('ManualMeasurements',context), entry=vm.runInContext('DecisionManualEntry',context);
    manual.init();
    if (panel) manual.mount(panel,'secondary-measurement');
    const input=async(view,field,value)=>{view.el(field).value=value;await view.el(field).dispatch(field==='metric'?'change':'input');};
    return {context,document,manual,entry,calls,control,collect,panel,moves,input,observers};
}

(async()=>{
    const absent=fixture({collection:false});
    absent.entry.open();await absent.manual.refresh();
    assert.ok(absent.entry.view,'Overview mounts without the collection page or panel');
    assert.equal(absent.manual.views.length,1);
    assert.equal(absent.entry.panel.parentElement.className,'manual-dialog-body');
    assert.ok(absent.entry.view.el('form'));
    absent.entry.close();

    const f=fixture();
    f.control.metricsGate=true;
    const first=f.manual.refresh(), second=f.manual.refresh();
    assert.equal(f.calls.metrics,1);f.control.metricsResolve();await Promise.all([first,second]);
    f.control.metricsGate=false;
    const collectView=f.manual.views.find(view=>view.root===f.panel), originalParent=f.panel.parentElement;
    await f.input(collectView,'metric','heavy_clean_coal_ash');
    await f.input(collectView,'time','2026-10-17T08:47');await f.input(collectView,'value','8.35');
    f.entry.open();await f.manual.refresh();
    const dialogView=f.entry.view;
    assert.notEqual(dialogView.root,collectView.root);
    assert.notEqual(dialogView.el('form'),collectView.el('form'));
    assert.equal(f.panel.parentElement,originalParent,'Collection DOM never leaves its page');
    assert.deepEqual(f.moves,[]);
    assert.equal(dialogView.el('value').value,'8.35');assert.equal(dialogView.el('time').value,'2026-10-17T08:47');
    assert.match(dialogView.el('purpose').textContent,/实际生产所属时间/);
    assert.doesNotMatch(dialogView.el('metric').innerHTML,/coarse_slime_amount/);
    await f.input(dialogView,'value','8.4');assert.equal(collectView.el('value').value,'8.4');
    f.entry.close();f.entry.open();assert.equal(f.entry.view,dialogView);assert.equal(dialogView.el('value').value,'8.4');

    f.control.saveMode='lost';await f.manual.submit({preventDefault(){}},dialogView);
    const id=f.manual.pending.id;
    assert.equal(dialogView.el('value').value,'8.4');assert.equal(f.manual.notice.status,'error');
    f.entry.close();f.control.saveMode='ok';await f.manual.submit({preventDefault(){}},collectView);
    assert.equal(f.calls.saves[0].body.client_id,id);assert.equal(f.calls.saves[1].body.client_id,id);
    assert.equal(f.calls.saves[1].body.observed_at,'2026-10-17T08:47+08:00','No two-hour subtraction');
    assert.equal(dialogView.el('value').value,'');assert.equal(collectView.el('time').value,'');assert.equal(f.manual.pending,null);
    assert.equal(f.calls.recent,1);assert.equal(f.calls.interval,1);

    f.entry.open();await f.input(dialogView,'value','8.7');f.control.saveMode='hold';
    const saving=f.manual.submit({preventDefault(){}},dialogView);
    assert.ok(f.manual.busy && collectView.el('fields').disabled && dialogView.el('fields').disabled);
    const count=f.calls.saves.length;await f.manual.submit({preventDefault(){}},collectView);assert.equal(f.calls.saves.length,count);
    f.document.getElementById('page-decision').classList.remove('active');f.entry.observer.callback();
    assert.equal(f.entry.dialog.open,false);assert.equal(collectView.el('value').value,'8.7','Closing and switching pages while saving preserves the draft');
    assert.equal(f.panel.parentElement,originalParent);
    f.document.getElementById('page-decision').classList.add('active');f.entry.open();
    assert.equal(dialogView.el('value').value,'8.7');
    f.control.saveResolve();await saving;
    assert.equal(dialogView.el('value').value,'');assert.equal(f.manual.busy,false);

    f.control.saveMode='ok';await f.input(dialogView,'metric','coarse_slime_ash');await f.input(dialogView,'value','0');
    assert.match(collectView.el('purpose').textContent,/仅保存记录，不参与实时计算/);
    const recent=f.calls.recent;await f.manual.submit({preventDefault(){}},dialogView);
    assert.equal(f.calls.saves.at(-1).body.value,0);assert.equal(f.calls.saves.at(-1).body.observed_at,null);
    assert.equal(f.calls.saves.at(-1).definition.archive_only,true);assert.equal(f.calls.recent,recent);
    assert.match(f.manual.notice.text,/仅保存记录/);assert.equal(JSON.stringify(f.context.App.store),'{"original":"unchanged"}');

    const ro=fixture({readOnly:true});await ro.manual.refresh();ro.entry.open();await ro.manual.refresh();
    assert.ok(ro.manual.views.find(view=>view.root===ro.panel).el('fields').disabled && ro.entry.view.el('submit').disabled);
    ro.entry.view.el('value').value='8.5';await ro.manual.submit({preventDefault(){}},ro.entry.view);
    assert.equal(ro.calls.saves.length,0,'Read-only mode issues zero save requests, including direct submit');
    assert.match(ro.manual.notice.text,/只读/);assert.equal(ro.entry.view.el('mode').hidden,false);
    console.log(JSON.stringify({passed:true,independentWithoutCollection:true,distinctDomSharedDraft:true,
        metadataSingleFlight:true,draftAcrossCloseAndPageChange:true,busyPreventsDuplicate:true,retryUsesSameClientId:true,
        actualProductionTime:true,archiveOnlyZeroPreserved:true,unsupportedAmountRemoved:true,readonlyWriteRequests:0,modelStoreUnchanged:true}));
})().catch(error=>{console.error(error);process.exitCode=1;});
