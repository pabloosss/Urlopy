const poprawnyLogin = "admin";
const poprawneHaslo = "admin3@1";

// logowanie
if (document.getElementById("loginForm")) {
  document.getElementById("loginForm").addEventListener("submit", function(e) {
    e.preventDefault();
    const login = document.getElementById("login").value;
    const haslo = document.getElementById("haslo").value;

    if (login === poprawnyLogin && haslo === poprawneHaslo) {
      localStorage.setItem("zalogowany", "true");
      window.location.href = "kadry.html";
    } else {
      document.getElementById("komunikat").textContent = "❌ Niepoprawny login lub hasło";
    }
  });
}

// panel kadrowy
if (window.location.pathname.includes("kadry.html")) {
  const zalogowany = localStorage.getItem("zalogowany");
  const panel = document.getElementById("panelKadrowy");
  const listaDiv = document.getElementById("listaPracownikow");

  if (zalogowany === "true") {
    panel.style.display = "block";
    pokazListe();
  } else {
    alert("Dostęp tylko dla kadry.");
    window.location.href = "login.html";
  }

  // Dodawanie pracownika
  window.dodajPracownika = function () {
    const imie = document.getElementById("noweImie").value.trim();
    const urlopy = parseInt(document.getElementById("urlopy").value);
    if (!imie || isNaN(urlopy)) {
      alert("Wpisz poprawne dane.");
      return;
    }

    const pracownicy = JSON.parse(localStorage.getItem("pracownicy") || "[]");
    pracownicy.push({ imie, dniUrlopu: urlopy });
    localStorage.setItem("pracownicy", JSON.stringify(pracownicy));
    document.getElementById("noweImie").value = "";
    document.getElementById("urlopy").value = "";
    pokazListe();
  };

  // Wyświetlanie listy
  function pokazListe() {
    const pracownicy = JSON.parse(localStorage.getItem("pracownicy") || "[]");
    listaDiv.innerHTML = "";
    pracownicy.forEach((p, index) => {
      const div = document.createElement("div");
      div.className = "pracownik";
      div.innerHTML = `
        <span>${p.imie} – ${p.dniUrlopu} dni</span>
        <button onclick="usunPracownika(${index})">❌ Usuń</button>
      `;
      listaDiv.appendChild(div);
    });
  }

  // Usuwanie
  window.usunPracownika = function (index) {
    const pracownicy = JSON.parse(localStorage.getItem("pracownicy") || "[]");
    pracownicy.splice(index, 1);
    localStorage.setItem("pracownicy", JSON.stringify(pracownicy));
    pokazListe();
  };
}
