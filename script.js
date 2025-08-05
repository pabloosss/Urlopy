let DB = { pracownicy:[], wnioski:[] };

async function loadDB() {
  const res = await fetch('/api/getData');
  DB = await res.json();
}

function daysBetween(a,b){
  return ((new Date(b)) - new Date(a))/86400000 +1;
}

function populateEmployeeSelect(){
  empSelect.innerHTML = '';
  DB.pracownicy.forEach(p=>{
    const o=document.createElement('option'); o.text=p.imie; empSelect.add(o);
  });
}

function renderBalances(){
  balanceList.innerHTML = '';
  DB.pracownicy.forEach(p=>{
    balanceList.insertAdjacentHTML('beforeend',
      `<li class="list-group-item d-flex justify-content-between">
         <span>${p.imie}</span><span>${p.dni_urlopowe} dni</span>
       </li>`);
  });
}

function loadEmployees(){
  empTable.tBodies[0].innerHTML = '';
  DB.pracownicy.forEach(p=>{
    empTable.tBodies[0].insertAdjacentHTML('beforeend',
      `<tr><td>${p.imie}</td><td>${p.dni_urlopowe}</td>
        <td><button class="btn btn-sm btn-danger">Usuń</button></td></tr>`);
  });
}

function loadRequests(){
  reqTable.tBodies[0].innerHTML = '';
  DB.wnioski.forEach(w=>{
    reqTable.tBodies[0].insertAdjacentHTML('beforeend',
      `<tr><td>${w.imie}</td><td>${w.od}</td><td>${w.do}</td>
        <td>${w.ile}</td><td>${w.status}</td></tr>`);
  });
}
