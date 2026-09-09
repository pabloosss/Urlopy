const assert=require('node:assert/strict');
const vm=require('node:vm');
const {context,script}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
function element(){return {value:'',checked:false,disabled:false,textContent:'',innerHTML:'',style:{},append(){},appendChild(){},addEventListener(){}};}
const elements=new Map();
const el=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
el('timesheetContext').textContent=JSON.stringify(context);
el('hoursTotal').value='160';el('randomHolidays').checked=true;
const alerts=[];
const sandbox={document:{getElementById:el,querySelector:()=>null,querySelectorAll:()=>[],createElement:element,createTextNode:()=>({})},window:{UrlopyCSRFToken:'fixture'},alert:message=>alerts.push(message),console};
// Eksportujemy funkcje istniejącego skryptu bez uruchamiania obsługi przeglądarki.
const source=script.split("document.getElementById('monthPickerForm').addEventListener")[0]+`
this.generator={generate,parametersChanged,onOvertimeEdit,syncSavedRows,restoreCustomFromSaved,ctx,rows:()=>dayData,setRows:r=>{dayData=r;},setClean:()=>{needsRegenerate=false;}};
})();`;
vm.createContext(sandbox);vm.runInContext(source,sandbox);
const g=sandbox.generator;
for(let i=0;i<80;i++){
  g.generate();
  const rows=g.rows();assert.equal(rows.length,30);assert.equal(rows.reduce((n,r)=>n+r.hours,0),160);
  const random=rows.filter(r=>r.off_source==='random');assert.ok(random.length>=3&&random.length<=6);
  for(const r of rows){
    if(r.off){assert.equal(r.hours,0);continue;}
    assert.ok(r.hours>=4&&r.hours<=16);
    const minute=t=>{const [h,m]=t.split(':').map(Number);return h*60+m;};
    assert.equal(minute(r.end)-minute(r.start),r.hours*60);
  }
  const restored=g.syncSavedRows(rows);
  for(const r of restored.filter(r=>r.off_source==='random'))assert.equal(r.off,true);
}
assert.equal(alerts.length,0);
g.parametersChanged();assert.equal(el('submitToHr').disabled,true);assert.equal(el('saveDraft').disabled,true);
el('hoursTotal').value='160.5';g.generate();assert.equal(alerts.length,1);
// Nadgodziny nie mogą odblokować wysyłki po zmianie parametrów.
g.ctx.employee.contract_type='Umowa o pracę';
g.ctx.employee.fte_percent=75;g.generate();
let row=g.rows().find(r=>!r.off);assert.equal(row.hours,6);assert.equal(row.end,'14:00');
g.parametersChanged();
const cells={};const tr={dataset:{day:String(row.day)},querySelector:s=>cells[s]||(cells[s]={})};
g.onOvertimeEdit({target:{textContent:'2',closest:()=>tr}});
assert.equal(row.overtime,2);assert.equal(row.end,'16:00');assert.equal(el('submitToHr').disabled,true);
// Przy wczytaniu zapisu pozostaje wybrany wariant bez losowych dni wolnych.
g.ctx.saved={rows:g.rows()};el('randomHolidays').checked=true;g.restoreCustomFromSaved();assert.equal(el('randomHolidays').checked,false);
console.log('Generator regression checks passed');
