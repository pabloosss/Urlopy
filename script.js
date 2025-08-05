/* ======= INICJALIZACJA BAZY ======= */
const DEFAULT_DB = {
  pracownicy: [
    { imie: 'Jan Kowalski', dni_urlopowe: 20 },
    { imie: 'Anna Nowak',   dni_urlopowe: 20 }
  ],
  wnioski: []
};

// Jeśli nie ma jeszcze bazy w localStorage, ustawiamy domyślną
if (!localStorage.getItem('baza')) {
  localStorage.setItem('baza', JSON.stringify(DEFAULT_DB));
}

// Odczyt / zapis
function readDB()  {
  return JSON.parse(localStorage.getItem('baza'));
}
function writeDB(d) {
  localStorage.setItem('baza', JSON.stringify(d));
}

/* ======= NARZĘDZIA ======= */
// Ilość dni włącznie
function daysBetween(a, b) {
  return ((new Date(b)) - (new Date(a))) / 86400000 + 1;
}

// Uzupełnienie selecta i bilansu
function populateEmployeeSelect() {
  const sel = document.getElementById('empSelect');
  if (!sel) return;
  sel.innerHTML = '';
  readDB().pracownicy.forEach(p => {
    const o = document.createElement('option');
    o.textContent = p.imie;
    sel.appendChild(o);
  });
}

function renderBalances() {
  const ul = document.getElementById('balanceList');
  if (!ul) return;
  ul.innerHTML = '';
  readDB().pracownicy.forEach(p => {
    ul.insertAdjacentHTML('beforeend', `
      <li class="list-group-item d-flex justify-content-between">
        <span>${p.imie}</span><span>${p.dni_urlopowe} dni</span>
      </li>`);
  });
}
