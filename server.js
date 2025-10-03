// =============================================================
// Emerlog Urlopy — BACKEND (Express + Supabase)
// Endpointy BEZ prefiksu /api (frontend woła np. POST /login)
// =============================================================

const express = require('express');
const path = require('path');
const cookieParser = require('cookie-parser');
const { createClient } = require('@supabase/supabase-js');

const app = express();
app.use(express.json());
app.use(cookieParser());

// --------- ENV / Supabase client ---------
const SUPABASE_URL =
  process.env.SUPABASE_URL || process.env.NEXT_PUBLIC_SUPABASE_URL;
const SUPABASE_SERVICE_KEY =
  process.env.SUPABASE_SERVICE_ROLE ||
  process.env.SUPABASE_SECRET ||
  process.env.SUPABASE_SERVICE_KEY;

if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
  console.error('❌ Brak SUPABASE_URL lub SUPABASE_SECRET w zmiennych środowiskowych.');
  process.exit(1);
}

const sb = createClient(SUPABASE_URL, SUPABASE_SERVICE_KEY, {
  db: { schema: 'public' },
});

// --------- Helpers ---------
const PROD = process.env.NODE_ENV === 'production';
const COOKIE_NAME = 'uid';
const cookieOpts = {
  httpOnly: true,
  sameSite: 'lax',
  secure: PROD,
  maxAge: 1000 * 60 * 60 * 24 * 30, // 30 dni
};

function sanitizeUser(u) {
  if (!u) return null;
  const {
    id, email, name, role, manager_id, employment,
    start_date, vacation_days, used_days, created_at,
  } = u;
  return {
    id, email, name, role, manager_id, employment,
    start_date, vacation_days, used_days, created_at,
  };
}

async function getUserFromReq(req) {
  const uid = req.cookies[COOKIE_NAME];
  if (!uid) return null;
  const { data, error } = await sb
    .from('users')
    .select('*')
    .eq('id', uid)
    .maybeSingle();
  if (error) return null;
  return data || null;
}

async function requireAuth(req, res, next) {
  const u = await getUserFromReq(req);
  if (!u) return res.status(401).json({ error: 'unauthorized' });
  req.user = u;
  next();
}

function isAdmin(u)   { return u?.role === 'admin'; }
function isManager(u) { return u?.role === 'manager'; }

// ID list widocznych userów dla aktualnego zalogowanego
async function visibleUserIds(current) {
  if (isAdmin(current)) {
    const { data } = await sb.from('users').select('id');
    return (data || []).map(x => x.id);
  }
  if (isManager(current)) {
    const { data } = await sb.from('users').select('id').eq('manager_id', current.id);
    return [current.id, ...(data || []).map(x => x.id)];
  }
  return [current.id];
}

// map id->user (przydatne do nazw)
async function usersIndex(ids) {
  if (!ids?.length) return {};
  const { data } = await sb.from('users')
    .select('id,name,manager_id,role,email,employment,start_date,vacation_days,used_days')
    .in('id', ids);
  const idx = {};
  (data || []).forEach(u => { idx[u.id] = u; });
  return idx;
}

function daysInclusive(fromISO, toISO) {
  const a = new Date(fromISO);
  const b = new Date(toISO);
  if (isNaN(a) || isNaN(b)) return 0;
  const ms = (b.setHours(12,0,0,0) - a.setHours(12,0,0,0));
  return Math.max(0, Math.floor(ms / 86400000) + 1);
}

// =============================================================
//                         AUTH
// =============================================================
app.post('/login', async (req, res) => {
  try {
    const { email, pass } = req.body || {};
    if (!email || !pass) return res.status(400).json({ error: 'missing credentials' });

    // Pobierz usera po email (szukamy różnych nazw kolumn hasła)
    const { data: user, error } = await sb
      .from('users')
      .select('*')
      .eq('email', String(email).toLowerCase())
      .maybeSingle();

    if (error || !user) return res.status(401).json({ error: 'invalid' });

    // Prosty dev-check: kolumna 'pass' lub 'password' wprost (bez hash).
    // Jeżeli nie ma kolumny, logowanie się nie uda — wtedy popraw kolumny w DB.
    const plain = user.pass ?? user.password ?? '';
    if (String(plain) !== String(pass)) {
      return res.status(401).json({ error: 'invalid' });
    }

    res.cookie(COOKIE_NAME, user.id, cookieOpts);
    res.json({ ok: true, user: sanitizeUser(user) });
  } catch (e) {
    console.error('login error', e);
    res.status(500).json({ error: 'login failed' });
  }
});

app.post('/logout', (req, res) => {
  res.clearCookie(COOKIE_NAME, cookieOpts);
  res.json({ ok: true });
});

app.get('/me', requireAuth, (req, res) => {
  res.json({ user: sanitizeUser(req.user) });
});

// =============================================================
//                         USERS
// =============================================================
app.get('/users', requireAuth, async (req, res) => {
  try {
    let query = sb.from('users')
      .select('id,name,email,role,manager_id,employment,start_date,vacation_days,used_days')
      .order('manager_id', { ascending: true })
      .order('name', { ascending: true });

    if (!isAdmin(req.user)) {
      if (isManager(req.user)) {
        // manager: on + podopieczni
        const { data: team } = await sb.from('users').select('id').eq('manager_id', req.user.id);
        const ids = [req.user.id, ...(team || []).map(x => x.id)];
        query = query.in('id', ids);
      } else {
        // employee: tylko on
        query = query.eq('id', req.user.id);
      }
    }

    const { data, error } = await query;
    if (error) throw error;

    // dopisz manager_name
    const allIds = Array.from(new Set((data || []).flatMap(u => [u.id, u.manager_id].filter(Boolean))));
    const idx = await usersIndex(allIds);

    const out = (data || []).map(u => ({
      ...u,
      manager_name: u.manager_id ? (idx[u.manager_id]?.name || '—') : '—',
    }));

    res.json(out);
  } catch (e) {
    console.error('GET /users', e);
    res.status(500).json({ error: 'list users failed' });
  }
});

app.post('/users', requireAuth, async (req, res) => {
  try {
    if (!isAdmin(req.user)) return res.status(403).json({ error: 'forbidden' });

    const payload = req.body || {};
    const insert = {
      id: payload.id, // opcjonalnie
      name: payload.name ?? null,
      email: (payload.email || '').toLowerCase(),
      pass: payload.pass || 'test123',        // DEV: proste hasło jeżeli nie podano
      role: payload.role || 'employee',
      manager_id: payload.manager_id || null,
      employment: payload.employment || 'UOP',
      start_date: payload.start_date || null,
      vacation_days: Number.isFinite(+payload.vacation_days) ? +payload.vacation_days : 20,
      used_days: Number.isFinite(+payload.used_days) ? +payload.used_days : 0,
    };

    const { data, error } = await sb.from('users').insert(insert).select().maybeSingle();
    if (error) throw error;

    res.json(sanitizeUser(data));
  } catch (e) {
    console.error('POST /users', e);
    res.status(500).json({ error: 'create user failed' });
  }
});

app.put('/users/:id', requireAuth, async (req, res) => {
  try {
    const { id } = req.params;
    const payload = req.body || {};

    if (!isAdmin(req.user) && req.user.id !== id) {
      return res.status(403).json({ error: 'forbidden' });
    }

    const update = {
      name: payload.name,
      email: payload.email ? String(payload.email).toLowerCase() : undefined,
      role: isAdmin(req.user) ? payload.role : undefined, // roli nie zmienia sam pracownik
      manager_id: isAdmin(req.user) ? (payload.manager_id || null) : undefined,
      employment: payload.employment,
      start_date: payload.start_date || null,
      vacation_days: Number.isFinite(+payload.vacation_days) ? +payload.vacation_days : undefined,
      used_days: Number.isFinite(+payload.used_days) ? +payload.used_days : undefined,
    };

    if (payload.pass) update.pass = payload.pass; // proste hasło dev

    const { data, error } = await sb.from('users').update(update).eq('id', id).select().maybeSingle();
    if (error) throw error;

    res.json(sanitizeUser(data));
  } catch (e) {
    console.error('PUT /users', e);
    res.status(500).json({ error: 'update user failed' });
  }
});

app.delete('/users/:id', requireAuth, async (req, res) => {
  try {
    if (!isAdmin(req.user)) return res.status(403).json({ error: 'forbidden' });

    const { id } = req.params;
    // (opcjonalnie) usuń wnioski usera
    await sb.from('leaves').delete().eq('user_id', id);
    const { error } = await sb.from('users').delete().eq('id', id);
    if (error) throw error;

    res.json({ ok: true });
  } catch (e) {
    console.error('DELETE /users', e);
    res.status(500).json({ error: 'delete user failed' });
  }
});

// =============================================================
//                         LEAVES
// =============================================================
app.get('/leaves', requireAuth, async (req, res) => {
  try {
    const { status, status_in, scope, on } = req.query;
    const allowedIds = await visibleUserIds(req.user);

    let q = sb.from('leaves')
      .select('*')
      .in('user_id', allowedIds)
      .order('from', { ascending: false })
      .order('created_at', { ascending: false });

    if (status) q = q.eq('status', status);
    if (status_in) q = q.in('status', String(status_in).split(',').map(s => s.trim()).filter(Boolean));

    if (scope === 'mine') {
      q = q.eq('user_id', req.user.id);
    } else if (scope === 'mine_or_team' && isManager(req.user) && !isAdmin(req.user)) {
      // już ograniczone przez allowedIds
    }

    if (on) {
      // from <= on <= to
      q = q.lte('from', on).gte('to', on);
    }

    const { data, error } = await q;
    if (error) throw error;

    const allUserIds = Array.from(new Set((data || []).map(l => l.user_id)));
    const idx = await usersIndex(allUserIds);

    const out = (data || []).map(l => ({
      ...l,
      user_name: idx[l.user_id]?.name || '—',
    }));

    res.json(out);
  } catch (e) {
    console.error('GET /leaves', e);
    res.status(500).json({ error: 'list leaves failed' });
  }
});

app.post('/leaves', requireAuth, async (req, res) => {
  try {
    const { type, from, to, comment } = req.body || {};
    if (!type || !from || !to) return res.status(400).json({ error: 'missing fields' });

    const insert = {
      user_id: req.user.id,               // ważne: wnioskodawca = aktualny user
      type,
      from,
      to,
      comment: comment || null,
      status: 'submitted',
      created_at: new Date().toISOString(),
    };

    const { data, error } = await sb.from('leaves').insert(insert).select().maybeSingle();
    if (error) throw error;

    res.json(data);
  } catch (e) {
    console.error('POST /leaves', e);
    res.status(500).json({ error: 'create leave failed' });
  }
});

app.put('/leaves/:id', requireAuth, async (req, res) => {
  try {
    const { id } = req.params;
    // Edycja tylko gdy submitted i należy do usera (albo admin)
    const { data: l, error: e1 } = await sb.from('leaves').select('*').eq('id', id).maybeSingle();
    if (e1 || !l) return res.status(404).json({ error: 'not found' });

    if (!isAdmin(req.user) && !(l.user_id === req.user.id && l.status === 'submitted')) {
      return res.status(403).json({ error: 'forbidden' });
    }

    const { type, from, to, comment } = req.body || {};
    const upd = {
      type: type ?? l.type,
      from: from ?? l.from,
      to: to ?? l.to,
      comment: comment ?? l.comment,
    };

    const { data, error } = await sb.from('leaves').update(upd).eq('id', id).select().maybeSingle();
    if (error) throw error;

    res.json(data);
  } catch (e) {
    console.error('PUT /leaves/:id', e);
    res.status(500).json({ error: 'update leave failed' });
  }
});

app.post('/leaves/:id/approve', requireAuth, async (req, res) => {
  try {
    const { id } = req.params;
    const { level } = req.body || {};
    const { data: leave, error: e1 } = await sb.from('leaves').select('*').eq('id', id).maybeSingle();
    if (e1 || !leave) return res.status(404).json({ error: 'not found' });

    // dane wnioskującego
    const { data: owner } = await sb.from('users').select('id,manager_id').eq('id', leave.user_id).maybeSingle();

    if (level === 'manager') {
      if (!isManager(req.user)) return res.status(403).json({ error: 'forbidden' });
      if (leave.status !== 'submitted') return res.status(400).json({ error: 'bad status' });
      if (owner?.manager_id !== req.user.id) return res.status(403).json({ error: 'not your subordinate' });

      const { data, error } = await sb.from('leaves').update({
        status: 'manager_approved',
        manager_id: req.user.id,
        manager_decision_at: new Date().toISOString(),
      }).eq('id', id).select().maybeSingle();
      if (error) throw error;

      return res.json(data);
    }

    if (level === 'admin') {
      if (!isAdmin(req.user)) return res.status(403).json({ error: 'forbidden' });
      // ADMIN NIE MOŻE ZATWIERDZIĆ przed kierownikiem
      if (leave.status !== 'manager_approved') {
        return res.status(400).json({ error: 'manager approval required first' });
      }

      // (opcjonalnie) nalicz wykorzystane dni dla typów urlopowych
      let usedAdd = 0;
      const countableTypes = new Set([
        'Urlop wypoczynkowy',
        'Urlop na żądanie',
      ]);
      if (countableTypes.has(leave.type)) {
        usedAdd = daysInclusive(leave.from, leave.to);
      }

      const { data, error } = await sb.from('leaves').update({
        status: 'approved',
        admin_id: req.user.id,
        admin_decision_at: new Date().toISOString(),
      }).eq('id', id).select().maybeSingle();
      if (error) throw error;

      if (usedAdd > 0) {
        await sb.rpc('noop').catch(() => {}); // placeholder, by uniknąć "unused await" przy braku transakcji
        await sb.from('users')
          .update({ used_days: (owner?.used_days || 0) + usedAdd })
          .eq('id', leave.user_id);
      }

      return res.json(data);
    }

    return res.status(400).json({ error: 'invalid level' });
  } catch (e) {
    console.error('POST /leaves/:id/approve', e);
    res.status(500).json({ error: 'approve failed' });
  }
});

app.post('/leaves/:id/reject', requireAuth, async (req, res) => {
  try {
    const { id } = req.params;
    const { level, reason } = req.body || {};

    const { data: leave, error: e1 } = await sb.from('leaves').select('*').eq('id', id).maybeSingle();
    if (e1 || !leave) return res.status(404).json({ error: 'not found' });

    const { data: owner } = await sb.from('users').select('manager_id').eq('id', leave.user_id).maybeSingle();

    if (level === 'manager') {
      if (!isManager(req.user)) return res.status(403).json({ error: 'forbidden' });
      if (owner?.manager_id !== req.user.id) return res.status(403).json({ error: 'not your subordinate' });
      if (leave.status !== 'submitted') return res.status(400).json({ error: 'bad status' });
    } else if (level === 'admin') {
      if (!isAdmin(req.user)) return res.status(403).json({ error: 'forbidden' });
      // Admin może odrzucić na każdym etapie
    } else {
      return res.status(400).json({ error: 'invalid level' });
    }

    const { data, error } = await sb.from('leaves').update({
      status: 'rejected',
      reject_reason: reason || null,
      rejected_by: req.user.id,
      rejected_at: new Date().toISOString(),
    }).eq('id', id).select().maybeSingle();
    if (error) throw error;

    res.json(data);
  } catch (e) {
    console.error('POST /leaves/:id/reject', e);
    res.status(500).json({ error: 'reject failed' });
  }
});

// =============================================================
//                Static (frontend) + start
// =============================================================
app.use(express.static(path.join(__dirname, 'public')));

// fallback dla ścieżek SPA (opcjonalnie)
app.get(['/', '/index.html', '/dashboard', '/employees', '/leaves'], (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

const PORT = process.env.PORT || 10000;
app.listen(PORT, () => {
  console.log(`Emerlog Urlopy (Supabase) running on :${PORT}`);
});
