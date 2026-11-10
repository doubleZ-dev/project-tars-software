// Targeted page-auth refresh regression only. No browser, server, USB or motion.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('tars_companion/gait_web/app.js','utf8').split('const $=')[0];
const store=new Map(),storage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v)};
function load(hash,sessionStorage=storage){return vm.runInNewContext(source+'\ntoken;',{location:{hash},sessionStorage,history:{replaceState(){}}});}
assert.equal(load('#'+'a'.repeat(64)),'a'.repeat(64));
assert.equal(load(''),'a'.repeat(64)); // same-tab refresh keeps authorization
assert.equal(load('#'+'b'.repeat(64)),'b'.repeat(64)); // new launcher replaces old token
assert.equal(load(''),'b'.repeat(64));
assert.equal(load('#invalid'),'');
assert.equal(load('#'+'c'.repeat(64),{getItem(){throw Error('blocked')},setItem(){throw Error('blocked')}}),'c'.repeat(64));
console.log('Page authorization: refresh, renewal and blocked-storage checks passed. No hardware accessed.');
