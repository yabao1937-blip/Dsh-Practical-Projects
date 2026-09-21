// 前端决策对拍入口：stdin 接收 {store, now}，不启动浏览器、不读写 localStorage。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class FixedDate extends Date {
    static now() { return input.now; }
}
const ctx = vm.createContext({
    document: { addEventListener() {} }, Date: FixedDate, console,
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../../frontend/js/app.js'), 'utf8'), ctx);
ctx.input = input;
const output = vm.runInContext(`(() => {
    App.store = input.store;
    App.saveStore = () => {};
    const guidance = App.computeDensityGuidance();
    return {guidance, ash501: App.resolveInstrument('ash_501'),
            ash502: App.resolveInstrument('ash_502'), density: App.resolveDensity()};
})()`, ctx);
process.stdout.write(JSON.stringify(output));
