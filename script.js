// script.js – wspólna logika front-endu

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
  const sel = document.getElementById('empSelect');
  sel.innerHTML = '';
  DB.pracownicy.forEach(p => {
    const o = document.createElement('option');
    o.value = p.imie;
    o.textContent = p.imie;
    sel.appendChild(o);
  });
}

function renderBalances() {
  const ul = document.getElementById('balanceList');
  ul.innerHTML = '';
  DB.pracownicy.forEach(p => {
    ul.insertAdjacentHTML('beforeend',
      `<li class="list-group-item d-flex justify-content-between">
         <span>${p.imie}</span><span>${p.dni_urlopowe} dni</span>
       </li>`);
  });
}

function populateManagerSelect() {
  const sel = document.getElementById('empManager');
  sel.innerHTML = '';
  MANAGERS.forEach(m => {
    const o = document.createElement('option');
    o.value = m.login;
    o.textContent = m.name;
    sel.appendChild(o);
  });
}

// Po załadowaniu DOM
window.addEventListener('DOMContentLoaded', () => {
  // Formularz logowania
  const loginForm = document.getElementById('loginForm');
  const loginBox  = document.getElementById('loginBox');
  const panelBox  = document.getElementById('panelBox');
  const tabPracNav = document.getElementById('tabPracNav');
  const tabBar     = document.getElementById('tabBar');

  loginForm.addEventListener('submit', async e => {
    e.preventDefault();
    const user = document.getElementById('loginUser').value.trim();
    const pass = document.getElementById('loginPass').value.trim();
    let isAdmin = false;
    let isMgr   = false;
    let mgrName = '';

    if (user === 'admin' && pass === 'admin3@1') {
      isAdmin = true;
    } else {
      const m = MANAGERS.find(m => m.login === user && m.password === pass);
      if (m) { isMgr = true; mgrName = m.login; }
    }
    if (!isAdmin && !isMgr) { alert('Błędny login/hasło'); return; }

    loginBox.classList.add('d-none');
    panelBox.classList.remove('d-none');

    await loadDB();

    if (isAdmin) {
      populateManagerSelect();
      populateEmployeeSelect();
      renderBalances();
      loadEmployees();
      loadRequests();
    } else {
      tabPracNav.style.display = 'none';
      populateEmployeeSelect();
      renderBalances();
      loadRequests(mgrName);
    }
  });

  // Zakładki
  tabBar.addEventListener('click', e => {
    if (e.target.classList.contains('nav-link')) {
      tabBar.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
      e.target.classList.add('active');
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('show', 'active'));
      document.querySelector(e.target.dataset.bsTarget).classList.add('show', 'active');
    }
  });

  // Obsługa dodawania pracowników
  document.getElementById('addEmpForm').addEventListener('submit', async e => {
    e.preventDefault();
    const imie    = document.getElementById('empName').value.trim();
    const dni     = Number(document.getElementById('empLimit').value);
    const manager = document.getElementById('empManager').value;
    if (!imie) { alert('Imię jest wymagane'); return; }
    await fetch('/api/addEmployee', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ imie, dni_urlopowe: dni, manager })
    });
    await loadDB();
    populateEmployeeSelect(); renderBalances(); loadEmployees();
  });

  // Pozostałe funkcje loadEmployees, loadRequests, decide pozostają niezmienione
});
