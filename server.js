import express from "express";
import fetch from "node-fetch";
import cors from "cors";

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(cors());
app.use(express.static(".")); // serwuje index.html itd. z katalogu głównego

const GH_TOKEN = process.env.GH_TOKEN;
const GH_OWNER = process.env.GH_OWNER;
const GH_REPO = process.env.GH_REPO;
const GH_FILE_PATH = process.env.GH_FILE_PATH || "baza.json";
const GH_BRANCH = process.env.GH_BRANCH || "main";

if (!GH_TOKEN || !GH_OWNER || !GH_REPO) {
  console.error("Brak wymaganych zmiennych: GH_TOKEN, GH_OWNER, GH_REPO");
}

const GH_API_BASE = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${GH_FILE_PATH}`;

async function getDb() {
  const url = `${GH_API_BASE}?ref=${GH_BRANCH}`;
  const res = await fetch(url, {
    headers: { Authorization: `Bearer ${GH_TOKEN}`, "User-Agent": "urlopy-app" }
  });
  if (!res.ok) throw new Error(`GitHub GET failed: ${res.status} ${await res.text()}`);
  const json = await res.json();
  const content = Buffer.from(json.content, "base64").toString("utf-8");
  return { data: JSON.parse(content), sha: json.sha };
}

async function saveDb(newData, sha, message = "update baza.json") {
  const body = {
    message,
    content: Buffer.from(JSON.stringify(newData, null, 2), "utf-8").toString("base64"),
    sha,
    branch: GH_BRANCH
  };
  const res = await fetch(GH_API_BASE, {
    method: "PUT",
    headers: {
      Authorization: `Bearer ${GH_TOKEN}`,
      "User-Agent": "urlopy-app",
      "Content-Type": "application/json"
    },
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(`GitHub PUT failed: ${res.status} ${await res.text()}`);
  const saved = await res.json();
  return saved;
}

// Utils
function daysInclusive(od, do_) {
  const d1 = new Date(od);
  const d2 = new Date(do_);
  const diff = Math.floor((d2 - d1) / (1000 * 60 * 60 * 24)) + 1;
  return diff > 0 ? diff : 0;
}

// --- API ---
// Cała baza
app.get("/api/db", async (req, res) => {
  try {
    const { data } = await getDb();
    res.json(data);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Dodanie pracownika
app.post("/api/pracownik", async (req, res) => {
  try {
    const { name, menedzer, dni = 20 } = req.body;
    if (!name || !menedzer) return res.status(400).json({ error: "Brak name/menedzer" });

    const { data, sha } = await getDb();
    data.pracownicy = data.pracownicy || [];
    const newId = (data.pracownicy.reduce((m, p) => Math.max(m, p.id || 0), 0) || 0) + 1;
    data.pracownicy.push({ id: newId, imie_nazwisko: name, dni_urlopowe: dni, menedzer });

    await saveDb(data, sha, `Dodano pracownika: ${name}`);
    res.json({ ok: true, id: newId });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Usunięcie pracownika
app.delete("/api/pracownik/:id", async (req, res) => {
  try {
    const id = Number(req.params.id);
    const { data, sha } = await getDb();
    data.pracownicy = (data.pracownicy || []).filter(p => p.id !== id);
    await saveDb(data, sha, `Usunięto pracownika id=${id}`);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Zmiana dni urlopowych
app.patch("/api/pracownik/:id", async (req, res) => {
  try {
    const id = Number(req.params.id);
    const { dni } = req.body;
    const { data, sha } = await getDb();
    const p = (data.pracownicy || []).find(x => x.id === id);
    if (!p) return res.status(404).json({ error: "Pracownik nie znaleziony" });
    if (typeof dni === "number") p.dni_urlopowe = dni;
    await saveDb(data, sha, `Zmieniono dni urlopowych pracownika id=${id}`);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Dodanie wniosku
app.post("/api/wniosek", async (req, res) => {
  try {
    const { employee, od, do_ } = req.body;
    if (!employee || !od || !do_) return res.status(400).json({ error: "Brak employee/od/do" });
    const dni = daysInclusive(od, do_);

    const { data, sha } = await getDb();
    data.wnioski = data.wnioski || [];
    const newId = (data.wnioski.reduce((m, w) => Math.max(m, w.id || 0), 0) || 0) + 1;
    data.wnioski.push({
      id: newId,
      pracownik: employee,
      od,
      do: do_,
      liczba_dni: dni,
      status: "oczekuje",
      zatwierdzony_przez: null,
      rozliczony: false
    });

    await saveDb(data, sha, `Dodano wniosek dla: ${employee}`);
    res.json({ ok: true, id: newId });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Zmiana statusu wniosku (akcept/odrzuc)
app.post("/api/wniosek/:id/status", async (req, res) => {
  try {
    const id = Number(req.params.id);
    const { status, reviewer } = req.body; // "zaakceptowany" lub "odrzucony"
    const { data, sha } = await getDb();

    const w = (data.wnioski || []).find(x => x.id === id);
    if (!w) return res.status(404).json({ error: "Wniosek nie znaleziony" });

    // Aktualizacja statusu
    w.status = status;
    w.zatwierdzony_przez = reviewer || null;

    // Jeśli zaakceptowano i nie rozliczony -> odejmij dni pracownikowi
    if (status === "zaakceptowany" && !w.rozliczony) {
      const p = (data.pracownicy || []).find(x => x.imie_nazwisko === w.pracownik);
      if (p) {
        p.dni_urlopowe = Math.max(0, (p.dni_urlopowe || 0) - (w.liczba_dni || 0));
        w.rozliczony = true;
      }
    }

    await saveDb(data, sha, `Zmieniono status wniosku id=${id} -> ${status}`);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// Login – frontend statyczny, zaciąga menedżerów z bazy
const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Server running on :${PORT}`));
