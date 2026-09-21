// 无浏览器、无网络、无存储副作用的算法回归载体。
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
module.exports = function appVM() {
    const elements = {};
    const ctx = vm.createContext({console, setTimeout, clearTimeout, Blob,
        window: {location: {protocol: 'file:'}},
        document: {addEventListener() {}, getElementById(id) { return elements[id] || null; }},
        localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
    });
    vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/js/app.js'), 'utf8'), ctx);
    const App = vm.runInContext('App', ctx);
    App.saveStore = () => {};
    App.showToast = () => {};
    return {App, ctx, elements};
};
