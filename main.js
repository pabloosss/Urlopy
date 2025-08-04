// Zapis danych logowania
const poprawnyLogin = "admin";
const poprawneHaslo = "admin3@1";

// Obsługa formularza logowania
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

// Sprawdzenie dostępu do kadry
if (window.location.pathname.includes("kadry.html")) {
  const zalogowany = localStorage.getItem("zalogowany");
  if (zalogowany === "true") {
    document.getElementById("panelKadrowy").style.display = "block";
  } else {
    alert("Dostęp tylko dla zalogowanych.");
    window.location.href = "login.html";
  }
}
