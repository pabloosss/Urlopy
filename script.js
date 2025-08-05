// Lista menedżerów (login = imię i nazwisko, password = hasło)
const MANAGERS = [
  { login: "Pisarczyk Paweł",    password: "hasloP1", name: "Pisarczyk Paweł" },
  { login: "Wroblewski Hubert",  password: "hasloP2", name: "Wroblewski Hubert" },
  { login: "Nowikow Dariusz",    password: "hasloP3", name: "Nowikow Dariusz" },
  { login: "Szmulik Damian",     password: "hasloP4", name: "Szmulik Damian" },
  { login: "Nurzyński Paweł",    password: "hasloP5", name: "Nurzyński Paweł" },
  { login: "Ewa Dusińska",       password: "hasloP6", name: "Ewa Dusińska" }
];

let DB = { pracownicy: [], wnioski: [] };

async function loadDB() {
  const res = await fetch('/api/getData');
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
