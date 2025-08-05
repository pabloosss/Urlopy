// Wspólna logika front-endu

const API_URL = '/api';
const MANAGERS = [
  { login: 'menadzer1', password: 'haslo1', name: 'Jan Menadżer' },
  { login: 'menadzer2', password: 'haslo2', name: 'Anna Menadżer' }
];
let DB = { pracownicy: [], wnioski: [] };

async function loadDB() {
  const res = await fetch(`${API_URL}/getData`);
  DB = await res.json();
}

function daysBetween(a, b) {
  return ((new Date(b)) - (new Date(a))) / 86400000 + 1;
}

function populateEmployeeSelect() {
  empSelect.innerHTML = '';
  DB.pracownicy.forEach(p => {
    const o = document.createElement('option');
    o.textContent = p.imie;
    o.value = p.imie;
    empSelect.appendChild(o);
  });
}

function renderBalances() {
  balanceList.innerHTML = '';
  DB.pracownicy.forEach(p => {
    balanceList.insertAdjacentHTML('beforeend',
      `<li class="list-group-item d-flex justify-content-between">
         <span>${p.imie}</span><span>${p.dni_urlopowe} dni</span>
       </li>`);
  });
}

function populateManagerSelect() {
  empManager.innerHTML = '';
  MANAGERS.forEach(m => {
    const o = document.createElement('option');
    o.value = m.login;
    o.textContent = m.name;
    empManager.appendChild(o);
  });
}
