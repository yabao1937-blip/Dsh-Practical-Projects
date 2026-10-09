// Deterministic browser-controller checks, including delayed responses and retries.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, {value:'', innerHTML:'', textContent:'', dataset:{},
        classList:{contains:()=>false}, querySelector:()=>null, focus(){}});
    return elements.get(id);
}
const requests = [];
let transport = async () => ({ok:true, status:200, json:async()=>({items:[],total:0,next_before_id:null})});
const context = vm.createContext({console, Intl, Date, Map, Set, Uint8Array, crypto:webcrypto, URLSearchParams,
    AbortSignal, TypeError, setTimeout, window:{Api:{base:'/api/v1',writeToken:()=> 'test-token'}},
    document:{getElementById:element}, location:{protocol:'http:'},
    fetch:async (url, options) => { requests.push({url, options}); return transport(url, options); },
    App:{openModal(title, html) {
        element('modal-body').innerHTML = html;
        element('brief-feedback-editor').dataset.hour = html.match(/data-hour="([^"]+)"/)[1];
        element('modal-overlay').classList.contains = () => false;
    }, closeModal(){ element('modal-overlay').classList.contains = () => true; }},
});
for (const file of ['brief.js', 'brief-feedback.js']) {
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js', file), 'utf8'), context);
}
const page = vm.runInContext('BriefPage', context), feedback = vm.runInContext('BriefFeedback', context);
const hours = ['2026-10-09T01:00:00.000+00:00','2026-10-09T02:00:00.000+00:00'];
page.result = {columns:[{key:'density',label:'当前分选密度',unit:'g/cm³'}], total:2,
    rows:hours.map(hour=>({hour,cells:{density:{value:1.48}}, feedback:{total:0,latest:null}}))};
page.day = '2026-10-09';
const flush = () => new Promise(resolve => setImmediate(resolve));

(async () => {
    feedback.open(0); await flush();
    assert(element('brief-feedback-save').disabled);
    const draft = '现场经验 <script>alert(1)</script>\n建议维持密度';
    feedback.input(draft);
    page.render(); // The periodic report refresh must not replace the editor/draft.
    assert.equal(element('brief-feedback-text').value, draft);
    assert(element('brief-content').innerHTML.includes('data-brief-column="feedback"'));
    assert(!element('brief-content').innerHTML.includes('data-brief-detail="feedback"'));
    context.App.closeModal(); feedback.open(0); await flush();
    assert.equal(element('brief-feedback-text').value, draft);

    // Lost response: retain text and retry exactly the same submission key.
    transport = async () => { throw new TypeError('network'); };
    await feedback.save();
    assert.equal(element('brief-feedback-text').value, draft);
    assert(element('brief-feedback-message').textContent.includes('输入内容已保留'));
    const first = JSON.parse(requests.at(-1).options.body);
    assert.match(first.client_id, /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
    assert.equal(requests.at(-1).options.headers['X-DMCS-Token'], 'test-token');

    let resolveSave;
    transport = async (url, options) => options?.method === 'POST' ? new Promise(resolve => resolveSave=resolve) :
        ({ok:true,json:async()=>({items:[],total:0,next_before_id:null})});
    const pending = feedback.save();
    const retry = JSON.parse(requests.at(-1).options.body);
    assert.equal(retry.client_id, first.client_id);
    const sent = requests.length;
    await feedback.save(); assert.equal(requests.length, sent); // no double submit
    feedback.open(1); feedback.input('另一个小时的草稿');
    let refreshed = 0; page.generate = () => refreshed++;
    resolveSave({ok:true,status:200,json:async()=>({id:1})});
    await pending; await flush();
    assert.equal(element('brief-feedback-text').value, '另一个小时的草稿');
    assert.equal(feedback.draft(hours[0]).text, '');
    assert.equal(refreshed, 1);

    // Old history responses must not replace another hour's history.
    let resolveHistory;
    transport = async () => new Promise(resolve => resolveHistory=resolve);
    feedback.open(0);
    const previous = resolveHistory;
    transport = async () => ({ok:true,json:async()=>({items:[{content:'本小时记录',created_at:hours[1]}],total:1,next_before_id:null})});
    feedback.open(1); await flush();
    previous({ok:true,json:async()=>({items:[{content:'旧小时记录'}],total:1,next_before_id:null})});
    await flush();
    assert(element('brief-feedback-history').innerHTML.includes('本小时记录'));
    assert(!element('brief-feedback-history').innerHTML.includes('旧小时记录'));
    const unsafe = '<img src=x onerror=alert(1)>"&';
    feedback.editor.items = [{content:unsafe,created_at:hours[1]}];
    feedback.renderHistory(feedback.editor);
    assert(!element('brief-feedback-history').innerHTML.includes('<img'));
    assert(feedback.cell({...page.result.rows[0],feedback:{total:1,latest:{content:unsafe}}},0).includes('&lt;img'));
    console.log('PASS: refresh and close preserve drafts; retries idempotent; double submit blocked; hours isolated; stale history ignored; text escaped; feedback stays outside folded groups.');
})().catch(error => { console.error(error); process.exitCode=1; });
