/* ======= BAZA DANYCH W localStorage ======= */
const DEFAULT_DB = {
  pracownicy: [
    { imie: 'Jan Kowalski', dni_urlopowe: 20 },
    { imie: 'Anna Nowak',   dni_urlopowe: 20 }
  ],
  wnioski: []
};
function readDB()  { return JSON.parse(localStorage.getItem('baza')) || DEFAULT_DB; }
function writeDB(d){ localStorage.setItem('baza', JSON.stringify(d)); }

/* ======= Narzędzia ======= */
function daysBetween(a,b){
  return ( (new Date(b)) - (new Date(a)) ) / 86400000 + 1;
}

/* ▸ Index.html: uzupełnij listę pracowników i bilans */
function populateEmployeeSelect(){
  const sel=document.getElementById('empSelect'); if(!sel)return;
  sel.innerHTML='';
  readDB().pracownicy.forEach(p=>{
    const o=document.createElement('option'); o.textContent=p.imie; sel.appendChild(o);
  });
}
function renderBalances(){
  const ul=document.getElementById('balanceList'); if(!ul)return;
  ul.innerHTML='';
  readDB().pracownicy.forEach(p=>{
    ul.insertAdjacentHTML('beforeend', `<li class="list-group-item d-flex justify-content-between">
      <span>${p.imie}</span><span>${p.dni_urlopowe} dni</span></li>`);
  });
}
