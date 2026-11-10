// Narrow UI state regression. No browser, serial, network or movement.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const text=fs.readFileSync('tars_companion/gait_web/app.js','utf8');
const fn=text.slice(text.indexOf('function controls()'),text.indexOf('function updateAngles()'));
const elements=new Proxy({}, {get:(o,k)=>o[k]??(o[k]={value:'device',checked:false,disabled:false,textContent:''})});
const c={owner:'',snapshot:{connected:true,busy:false},metadata:{},frames:Array(6),end:2,busy:false,token:'a'.repeat(64),actionLabels:{walk_forward:'前进'},
 $:id=>elements[id],valid:()=>true,document:{querySelectorAll:()=>[]}};
vm.createContext(c);vm.runInContext(fn+';controls();',c);
assert.equal(elements.confirm.disabled,false);
assert.equal(elements.run.disabled,true);
assert.match(elements['run-reason'].textContent,/控制权/);
c.owner='manual-owner';elements.confirm.checked=true;vm.runInContext('controls()',c);
assert.equal(elements.run.disabled,false);
c.snapshot.connected=false;vm.runInContext('controls()',c);
assert.equal(elements.confirm.disabled,false);assert.equal(elements.run.disabled,true);
c.snapshot.busy=true;vm.runInContext('controls()',c);
assert.equal(elements.confirm.disabled,true);assert.equal(elements.run.disabled,true);
console.log('Checkbox usability and execution safety gates passed. No hardware accessed.');
