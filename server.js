// server.js
// Emerlog Urlopy – backend (Supabase)
// wymagane ENV: SUPABASE_URL, SUPABASE_SECRET, JWT_SECRET

const express = require('express');
const path = require('path');
const cors = require('cors');
const jwt = require('jsonwebtoken');
const bodyParser = require('body-parser');
const { createClient } = require('@supabase/supabase-js');

const PORT = process.env.PORT || 10000;
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SECRET = process.env.SUPABASE_SECRET;
const JWT_SECRET = process.env.JWT_SECRET || 'dev_secret_change_me';

if (!SUPABASE_URL || !SUPABASE_SECRET) {
  console.error('❌ Brak SUPABASE_URL lub SUPABASE_SECRET w zmiennych środowiskowych.');
  process.exit(1);
}
const sb = createClient(SUPABASE_URL, SUPABASE_SECRET, { auth: { persistSession: false } });

const app = express();
app.use(cors());
app.use(bodyParser.json());
app.use(express.static(path.join(__dirname, 'public')));

// ===== Helpers =====
function startOfYear(d = new Date()) { return new Date(d.getFullYear(), 0, 1); }
function endOfYear(d = new Date()) { return new Date(d.getFullYear(), 11, 31); }

function parseDateISO(s) { return new Date(s + 'T00:00:00'); }

function businessDaysBetween(fromISO, toISO) {
  if (!fromISO || !toISO) return 0;
  let d = parseDateISO(fromISO), to = parseDateISO(toISO);
  if (d > to) [d, to] = [to, d];
  let count = 0;
  while (d <= to) {
    const day = d.getDay(); // 0..6
    if (day !== 0 && day !== 6) count++;
    d.setDate(d.getDate() + 1);
  }
  return count;
}

function signToken(user) {
  const payload = {
    id: user.id,
    role: user.role,
    email: user.email,
    name: user.name,
    manager_id: user.manager_id || null,
  };
  return jwt.sign(payload, JWT_SECRET, { expiresIn: '7d' });
}

function authRequired(req, res, next) {
  const hdr = req.headers.authorization || '';
  const tok = hdr.startsWith('Bearer ') ? hdr.slice(7) : null;
  if (!tok) return res.status(401).json({ error: 'unauthorized' });
  try {
    req.user = jwt.verify(tok, JWT_SECRET);
    next();
  } catch {
    return res.status(401).json({ error: 'unauthorized' });
  }
}

function canSeeUser(viewer, userRow) {
  if (!viewer) return false;
  if (viewer.role === 'admin') return true;
  if (viewer.role === 'manager') {
    return viewer.id === userRow.id || userRow.manager_id === viewer.id;
  }
  // employee
  return viewer.id === userRow.id;
}

// ===== Auth =====
app.post('/api/login', async (req, res) => {
  try {
    const { email, pass } = req.body || {};
    if (!email || !pass) return res.status(400).json({ error: 'missing credentials' });

    const { data: users, error } = await sb
      .from('users')
      .select('id,name,email,pass,role,manager_id,employment,start_date,vacation_days')
      .eq('email', email)
      .limit(1);

    if (error) return res.status(500).json({ error: 'db', details: error.message });
    const u = users && users[0];
    if (!u || String(u.pass) !== String(pass)) return res.status(401).json({ error: 'invalid' });

    const token = signToken(u);
    res.json({ token, user: { ...u, vacation_total: u.vacation_days ?? 20 } });
  } catch (e) {
    console.error('login error', e);
    res.status(500).json({ error: 'login_failed' });
  }
});

app.get('/api/me', authRequired, async (req, res) => {
  const { data, error } = await sb
    .from('users')
    .select('id,name,email,role,manager_id,employment,start_date,vacation_days')
    .eq('id', req.user.id)
    .limit(1);
  if (error) return res.status(500).json({ error: 'db' });
  const u = data?.[0];
  res.json(u || null);
});

// ===== Users =====

// GET /api/users?q=&manager_id=&sort=vac_left|name|manager|start_date
app.get('/api/users', authRequired, async (req, res) => {
  try {
    const viewer = req.user;
    // we fetch all, then filter in memory according to rights (simple and safe)
    const { data: list, error } = await sb
      .from('users')
      .select('id,name,email,role,manager_id,employment,start_date,vacation_days');
    if (error) return res.status(500).json({ error: 'db' });

    // visibility filter
    let visible = list.filter(u => canSeeUser(viewer, u));

    // search / filters
    const q = (req.query.q || '').toString().trim().toLowerCase();
    if (q) {
      visible = visible.filter(u =>
        (u.name || '').toLowerCase().includes(q) ||
        (u.email || '').toLowerCase().includes(q)
      );
    }
    const mgr = (req.query.manager_id || '').toString().trim();
    if (mgr) visible = visible.filter(u => (u.manager_id || '') === mgr);

    // prefetch approved leaves in current year
    const y0 = startOfYear(); const y1 = endOfYear();
    const fromISO = y0.toISOString().slice(0, 10);
    const toISO = y1.toISOString().slice(0, 10);

    const { data: leaves, error: lerr } = await sb
      .from('leaves')
      .select('id,user_id,type,from,to,status')
      .eq('status', 'approved')
      .gte('from', fromISO)
      .lte('to', toISO);

    if (lerr) return res.status(500).json({ error: 'db' });

    const VAC_TYPES = new Set([
      'urlop wypoczynkowy', 'wypoczynkowy',
      'urlop na żądanie', 'na żądanie', 'na zadanie'
    ]);

    const usedDaysByUser = {};
    for (const l of leaves || []) {
      const ty = (l.type || '').toLowerCase();
      if (!VAC_TYPES.has(ty)) continue;
      const days = businessDaysBetween(l.from, l.to);
      usedDaysByUser[l.user_id] = (usedDaysByUser[l.user_id] || 0) + days;
    }

    const enriched = visible.map(u => {
      const total = Number(u.vacation_days ?? 20);
      const used = Number(usedDaysByUser[u.id] || 0);
      const left = Math.max(0, total - used);
      return { ...u, vacation_total: total, vacation_left: left };
    });

    // sort
    const sort = (req.query.sort || '').toString();
    if (sort === 'vac_left') {
      enriched.sort((a, b) => b.vacation_left - a.vacation_left);
    } else if (sort === 'name') {
      enriched.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
    } else if (sort === 'start_date') {
      enriched.sort((a, b) => (a.start_date || '').localeCompare(b.start_date || ''));
    }

    res.json(enriched);
  } catch (e) {
    console.error('GET /api/users error', e);
    res.status(500).json({ error: 'server' });
  }
});

// create user
app.post('/api/users', authRequired, async (req, res) => {
  try {
    if (req.user.role !== 'admin' && req.user.role !== 'manager') {
      return res.status(403).json({ error: 'forbidden' });
    }
    const row = req.body || {};
    // defaults
    row.role = row.role || 'employee';
    if (row.vacation_days == null) row.vacation_days = 20;

    const { data, error } = await sb.from('users').insert(row).select().single();
    if (error) return res.status(400).json({ error: 'create user failed', details: error.message });
    res.json(data);
  } catch (e) {
    console.error('POST /api/users', e);
    res.status(500).json({ error: 'server' });
  }
});

// update user
app.put('/api/users/:id', authRequired, async (req, res) => {
  try {
    const id = req.params.id;
    if (!id) return res.status(400).json({ error: 'missing id' });

    // employee może edytować siebie (ograniczony zakres), menedżer/admin dowolnie
    if (req.user.role === 'employee' && req.user.id !== id) {
      return res.status(403).json({ error: 'forbidden' });
    }

    const patch = { ...req.body };
    // nie pozwól zwykłemu pracownikowi podnieść roli lub komuś zmienić
    if (req.user.role === 'employee') {
      delete patch.role; delete patch.manager_id; delete patch.employment;
      delete patch.start_date; // itp. minimalny bezpieczny patch
    }

    const { data, error } = await sb.from('users').update(patch).eq('id', id).select().single();
    if (error) return res.status(400).json({ error: 'update failed', details: error.message });
    res.json(data);
  } catch (e) {
    console.error('PUT /api/users/:id', e);
    res.status(500).json({ error: 'server' });
  }
});

// delete user
app.delete('/api/users/:id', authRequired, async (req, res) => {
  try {
    if (req.user.role !== 'admin' && req.user.role !== 'manager') {
      return res.status(403).json({ error: 'forbidden' });
    }
    const id = req.params.id;
    await sb.from('users').delete().eq('id', id);
    res.json({ ok: true });
  } catch (e) {
    console.error('DELETE /api/users/:id', e);
    res.status(500).json({ error: 'server' });
  }
});

// ===== Leaves =====

// widoczność listy wniosków + filtry: ?status=submitted|approved|rejected
app.get('/api/leaves', authRequired, async (req, res) => {
  try {
    const viewer = req.user;
    const { data: users } = await sb
      .from('users')
      .select('id,manager_id');

    // whitelista ID
    let allowedIds = new Set();
    if (viewer.role === 'admin') {
      for (const u of users || []) allowedIds.add(u.id);
    } else if (viewer.role === 'manager') {
      allowedIds.add(viewer.id);
      for (const u of users || []) if (u.manager_id === viewer.id) allowedIds.add(u.id);
    } else {
      allowedIds.add(viewer.id);
    }

    let query = sb.from('leaves').select('id,user_id,type,from,to,comment,status,decided_by_l,decided_by_i,decided_at_l,decided_at_i,created_at,updated_at');
    const status = (req.query.status || '').toString();
    if (status) query = query.eq('status', status);

    const { data, error } = await query.order('from', { ascending: true });
    if (error) return res.status(500).json({ error: 'db' });

    const filtered = (data || []).filter(l => allowedIds.has(l.user_id));
    res.json(filtered);
  } catch (e) {
    console.error('GET /api/leaves', e);
    res.status(500).json({ error: 'server' });
  }
});

// create leave – employee składa tylko za siebie
app.post('/api/leaves', authRequired, async (req, res) => {
  try {
    const viewer = req.user;
    const row = { ...req.body };

    if (viewer.role === 'employee') {
      row.user_id = viewer.id; // ignorujemy to co przyszło z frontu
    } else if (!row.user_id) {
      return res.status(400).json({ error: 'user_id required' });
    }
    row.status = row.status || 'submitted';

    const { data, error } = await sb.from('leaves').insert(row).select().single();
    if (error) return res.status(400).json({ error: 'create failed', details: error.message });

    res.json(data);
  } catch (e) {
    console.error('POST /api/leaves', e);
    res.status(500).json({ error: 'server' });
  }
});

// update leave (akceptacja/odrzucenie lub edycja wniosku)
app.put('/api/leaves/:id', authRequired, async (req, res) => {
  try {
    const id = req.params.id;
    const patch = { ...req.body };

    // odczytaj wniosek, by sprawdzić właściciela i status
    const { data: lrow, error: gerr } = await sb.from('leaves').select('*').eq('id', id).single();
    if (gerr || !lrow) return res.status(404).json({ error: 'not_found' });

    const viewer = req.user;

    const isOwner = lrow.user_id === viewer.id;

    // pracownik: może edytować TYLKO swój wniosek i tylko gdy submitted
    if (viewer.role === 'employee') {
      if (!isOwner) return res.status(403).json({ error: 'forbidden' });
      if (lrow.status !== 'submitted') return res.status(400).json({ error: 'locked' });
      // nie pozwalaj samemu zmieniać statusu
      delete patch.status;
      delete patch.decided_by_l;
      delete patch.decided_by_i;
      delete patch.decided_at_l;
      delete patch.decided_at_i;
    }

    // manager: może akceptować dla swoich podopiecznych (lub swoje)
    if (viewer.role === 'manager') {
      // sprawdź czy user jest podwładnym
      const { data: emp } = await sb.from('users').select('id,manager_id').eq('id', lrow.user_id).single();
      if (lrow.user_id !== viewer.id && emp?.manager_id !== viewer.id && viewer.role !== 'admin') {
        return res.status(403).json({ error: 'forbidden' });
      }
      if (patch.status) {
        patch.decided_by_l = viewer.id;
        patch.decided_at_l = new Date().toISOString();
      }
    }

    // admin: pełna władza
    if (viewer.role === 'admin' && patch.status) {
      patch.decided_by_i = viewer.id;
      patch.decided_at_i = new Date().toISOString();
    }

    const { data, error } = await sb.from('leaves').update(patch).eq('id', id).select().single();
    if (error) return res.status(400).json({ error: 'update failed', details: error.message });
    res.json(data);
  } catch (e) {
    console.error('PUT /api/leaves/:id', e);
    res.status(500).json({ error: 'server' });
  }
});

// delete leave
app.delete('/api/leaves/:id', authRequired, async (req, res) => {
  try {
    const id = req.params.id;
    const { data: lrow } = await sb.from('leaves').select('id,user_id,status').eq('id', id).single();
    if (!lrow) return res.status(404).json({ error: 'not_found' });

    const viewer = req.user;
    const isOwner = lrow.user_id === viewer.id;

    if (viewer.role === 'employee' && (!isOwner || lrow.status !== 'submitted')) {
      return res.status(403).json({ error: 'forbidden' });
    }
    if (viewer.role === 'manager') {
      // manager może skasować swój/podopiecznego (dowolny status w naszej logice)
    }
    await sb.from('leaves').delete().eq('id', id);
    res.json({ ok: true });
  } catch (e) {
    console.error('DELETE /api/leaves/:id', e);
    res.status(500).json({ error: 'server' });
  }
});

// ===== Start =====
app.listen(PORT, () => {
  console.log(`Emerlog Urlopy (Supabase) running on :${PORT}`);
});
