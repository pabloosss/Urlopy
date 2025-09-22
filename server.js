// server.js — Emerlog Urlopy (Supabase)
// uruchamianie lokalnie: PORT=10000 node server.js

const express = require('express');
const cors = require('cors');
const path = require('path');
const jwt = require('jsonwebtoken');
const { createClient } = require('@supabase/supabase-js');
const crypto = require('node:crypto');

const PORT = process.env.PORT || 10000;
const JWT_SECRET = process.env.JWT_SECRET || 'dev-jwt-secret';
const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_SECRET = process.env.SUPABASE_SECRET || process.env.SUPABASE_SERVICE_ROLE || process.env.SUPABASE_SERVICE_KEY || process.env.SUPABASE_SERVICE || process.env.SUPABASE_KEY || process.env.SUPABASE_SECRET; // elastycznie

if (!SUPABASE_URL || !SUPABASE_SECRET) {
  console.error('❌ Brak SUPABASE_URL lub SUPABASE_SECRET w zmiennych środowiskowych.');
  process.exit(1);
}

const supa = createClient(SUPABASE_URL, SUPABASE_SECRET, {
  auth: { persistSession: false, autoRefreshToken: false },
});

const app = express();
app.use(cors());
app.use(express.json({ limit: '1mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// ========== helpers ==========
const newId = () =>
  (crypto.randomUUID ? crypto.randomUUID() : (Math.random().toString(36).slice(2) + Date.now().toString(36)));

const mapUserDbToApi = (r) => ({
  id: r.id,
  name: r.name,
  email: r.email,
  role: r.role,
  managerId: r.manager_id || null,
  employment: r.employment || 'UOP',
  startDate: r.start_date || null,
  vacationDays: r.vacation_days ?? 20,
});

const mapUserApiToDb = (u) => ({
  id: u.id,
  name: u.name,
  email: u.email,
  pass: u.pass, // jeśli brak, supabase zostawi poprzednie
  role: u.role,
  manager_id: u.managerId || null,
  employment: u.employment || 'UOP',
  start_date: u.startDate || null,
  vacation_days: u.vacationDays ?? 20,
  updated_at: new Date().toISOString(),
});

const mapLeaveDbToApi = (r) => ({
  id: r.id,
  userId: r.user_id,
  type: r.type,
  from: r.from,
  to: r.to,
  comment: r.comment || '',
  status: r.status,
  decidedByManager: r.decided_by_manager || null,
  decidedAtManager: r.decided_at_manager || null,
  decidedByAdmin: r.decided_by_admin || null,
  decidedAtAdmin: r.decided_at_admin || null,
});

const mapLeaveApiToDb = (l) => ({
  id: l.id,
  user_id: l.userId,
  type: l.type,
  from: l.from,
  to: l.to,
  comment: l.comment || null,
  status: l.status || 'submitted',
  updated_at: new Date().toISOString(),
});

// ========== auth ==========
function signToken(user) {
  return jwt.sign(
    { id: user.id, role: user.role, email: user.email, name: user.name },
    JWT_SECRET,
    { expiresIn: '7d' }
  );
}

function authRequired(req, res, next) {
  const h = req.headers.authorization || '';
  const m = h.match(/^Bearer (.+)$/i);
  if (!m) return res.status(401).json({ error: 'No token' });
  try {
    req.user = jwt.verify(m[1], JWT_SECRET);
    next();
  } catch {
    return res.status(401).json({ error: 'Invalid token' });
  }
}

async function getUserByEmailPass(email, pass) {
  const { data, error } = await supa
    .from('users')
    .select('*')
    .eq('email', email)
    .eq('pass', pass)
    .limit(1)
    .maybeSingle();
  if (error) throw error;
  return data;
}

async function getSubordinateIds(managerId) {
  const { data, error } = await supa.from('users').select('id').eq('manager_id', managerId);
  if (error) throw error;
  return (data || []).map((x) => x.id);
}

// ========== routes ==========

// health
app.get('/health', (_req, res) => res.json({ ok: true }));

// auth login
app.post('/auth/login', async (req, res) => {
  try {
    const { email, pass } = req.body || {};
    if (!email || !pass) return res.status(400).json({ error: 'email & pass required' });
    const user = await getUserByEmailPass(email, pass);
    if (!user) return res.status(401).json({ error: 'Invalid credentials' });
    const token = signToken(user);
    res.json({ token, user: mapUserDbToApi(user) });
  } catch (e) {
    console.error('login error', e);
    res.status(500).json({ error: 'login failed' });
  }
});

// USERS
app.get('/users', authRequired, async (req, res) => {
  try {
    const me = req.user;
    if (me.role === 'admin') {
      const { data, error } = await supa.from('users').select('*').order('name', { ascending: true });
      if (error) throw error;
      return res.json(data.map(mapUserDbToApi));
    }
    if (me.role === 'manager') {
      const { data, error } = await supa
        .from('users')
        .select('*')
        .or(`id.eq.${me.id},manager_id.eq.${me.id}`)
        .order('name', { ascending: true });
      if (error) throw error;
      return res.json(data.map(mapUserDbToApi));
    }
    // employee – tylko on sam
    const { data, error } = await supa.from('users').select('*').eq('id', me.id).limit(1);
    if (error) throw error;
    return res.json((data || []).map(mapUserDbToApi));
  } catch (e) {
    console.error('GET /users', e);
    res.status(500).json({ error: 'users fetch failed' });
  }
});

app.post('/users', authRequired, async (req, res) => {
  try {
    if (req.user.role !== 'admin') return res.status(403).json({ error: 'admin only' });
    const body = { ...req.body };
    body.id = body.id || newId();
    if (!body.pass) body.pass = '1';
    const payload = mapUserApiToDb(body);
    const { data, error } = await supa.from('users').insert(payload).select('*').single();
    if (error) throw error;
    res.json(mapUserDbToApi(data));
  } catch (e) {
    console.error('POST /users', e);
    res.status(500).json({ error: 'create user failed' });
  }
});

app.put('/users/:id', authRequired, async (req, res) => {
  try {
    if (req.user.role !== 'admin') return res.status(403).json({ error: 'admin only' });
    const id = req.params.id;
    const incoming = { ...req.body, id };
    const payload = mapUserApiToDb(incoming);
    // nie aktualizuj email/pass jeśli puste
    if (!('pass' in req.body)) delete payload.pass;
    if (!('email' in req.body)) delete payload.email;

    const { data, error } = await supa.from('users').update(payload).eq('id', id).select('*').single();
    if (error) throw error;
    res.json(mapUserDbToApi(data));
  } catch (e) {
    console.error('PUT /users/:id', e);
    res.status(500).json({ error: 'update user failed' });
  }
});

app.delete('/users/:id', authRequired, async (req, res) => {
  try {
    if (req.user.role !== 'admin') return res.status(403).json({ error: 'admin only' });
    const id = req.params.id;
    const { error } = await supa.from('users').delete().eq('id', id);
    if (error) throw error;
    res.json({ ok: true });
  } catch (e) {
    console.error('DELETE /users/:id', e);
    res.status(500).json({ error: 'delete user failed' });
  }
});

// LEAVES
app.get('/leaves', authRequired, async (req, res) => {
  try {
    const me = req.user;
    if (me.role === 'admin') {
      const { data, error } = await supa
        .from('leaves')
        .select('*')
        .order('from', { ascending: true });
      if (error) throw error;
      return res.json(data.map(mapLeaveDbToApi));
    }
    if (me.role === 'manager') {
      const subs = await getSubordinateIds(me.id);
      const ids = [me.id, ...subs];
      const { data, error } = await supa.from('leaves').select('*').in('user_id', ids).order('from', { ascending: true });
      if (error) throw error;
      return res.json(data.map(mapLeaveDbToApi));
    }
    // employee
    const { data, error } = await supa.from('leaves').select('*').eq('user_id', me.id).order('from', { ascending: true });
    if (error) throw error;
    return res.json(data.map(mapLeaveDbToApi));
  } catch (e) {
    console.error('GET /leaves', e);
    res.status(500).json({ error: 'leaves fetch failed' });
  }
});

function isManagerOf(managerId, userRow) {
  return userRow && userRow.manager_id === managerId;
}

async function fetchUser(id) {
  const { data, error } = await supa.from('users').select('*').eq('id', id).maybeSingle();
  if (error) throw error;
  return data;
}

app.post('/leaves', authRequired, async (req, res) => {
  try {
    const me = req.user;
    const body = { ...req.body };
    body.id = body.id || newId();
    // uprawnienia: pracownik może dla siebie; manager dla siebie/podopiecznych; admin dla każdego
    if (me.role === 'employee' && body.userId !== me.id) return res.status(403).json({ error: 'forbidden' });
    if (me.role === 'manager' && body.userId !== me.id) {
      const u = await fetchUser(body.userId);
      if (!isManagerOf(me.id, u)) return res.status(403).json({ error: 'forbidden' });
    }
    body.status = 'submitted';
    const payload = mapLeaveApiToDb(body);
    const { data, error } = await supa.from('leaves').insert(payload).select('*').single();
    if (error) throw error;
    res.json(mapLeaveDbToApi(data));
  } catch (e) {
    console.error('POST /leaves', e);
    res.status(500).json({ error: 'create leave failed' });
  }
});

app.put('/leaves/:id', authRequired, async (req, res) => {
  try {
    const me = req.user;
    const id = req.params.id;

    // pobierz obecny wniosek
    const { data: cur, error: errCur } = await supa.from('leaves').select('*').eq('id', id).single();
    if (errCur) throw errCur;

    // zmiana statusu?
    if (req.body && typeof req.body.status === 'string') {
      const status = req.body.status;
      // reguły:
      // - manager: tylko na podopiecznych i tylko z submitted -> manager_approved / rejected_manager
      // - admin: approve tylko gdy manager_approved; reject gdy submitted lub manager_approved
      if (me.role === 'manager') {
        const u = await fetchUser(cur.user_id);
        if (!isManagerOf(me.id, u)) return res.status(403).json({ error: 'forbidden' });
        if (cur.status !== 'submitted') return res.status(400).json({ error: 'bad state' });
        let patch = { status, updated_at: new Date().toISOString() };
        if (status === 'manager_approved') {
          patch.decided_by_manager = me.id;
          patch.decided_at_manager = new Date().toISOString();
        } else if (status === 'rejected_manager') {
          patch.decided_by_manager = me.id;
          patch.decided_at_manager = new Date().toISOString();
        } else {
          return res.status(400).json({ error: 'invalid status for manager' });
        }
        const { data, error } = await supa.from('leaves').update(patch).eq('id', id).select('*').single();
        if (error) throw error;
        return res.json(mapLeaveDbToApi(data));
      }
      if (me.role === 'admin') {
        if (status === 'approved') {
          if (cur.status !== 'manager_approved') return res.status(400).json({ error: 'admin can approve only after manager' });
          const patch = {
            status: 'approved',
            decided_by_admin: me.id,
            decided_at_admin: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          };
          const { data, error } = await supa.from('leaves').update(patch).eq('id', id).select('*').single();
          if (error) throw error;
          return res.json(mapLeaveDbToApi(data));
        }
        if (status === 'rejected_admin') {
          if (cur.status !== 'submitted' && cur.status !== 'manager_approved')
            return res.status(400).json({ error: 'bad state for admin reject' });
          const patch = {
            status: 'rejected_admin',
            decided_by_admin: me.id,
            decided_at_admin: new Date().toISOString(),
            updated_at: new Date().toISOString(),
          };
          const { data, error } = await supa.from('leaves').update(patch).eq('id', id).select('*').single();
          if (error) throw error;
          return res.json(mapLeaveDbToApi(data));
        }
        return res.status(400).json({ error: 'invalid status for admin' });
      }
      return res.status(403).json({ error: 'forbidden' });
    }

    // edycja pól (np. daty/komentarz) – dozwolone:
    // - owner: gdy status = submitted
    // - manager: dla podopiecznych, gdy status = submitted
    // - admin: zawsze
    const patchApi = { ...req.body, id, userId: cur.user_id };
    const patchDb = mapLeaveApiToDb(patchApi);
    delete patchDb.user_id; // nie zmieniamy właściciela tutaj
    if (me.role === 'admin') {
      const { data, error } = await supa.from('leaves').update(patchDb).eq('id', id).select('*').single();
      if (error) throw error;
      return res.json(mapLeaveDbToApi(data));
    }
    if (cur.status !== 'submitted') return res.status(403).json({ error: 'cannot edit non-submitted' });
    if (me.role === 'employee') {
      if (cur.user_id !== me.id) return res.status(403).json({ error: 'forbidden' });
      const { data, error } = await supa.from('leaves').update(patchDb).eq('id', id).select('*').single();
      if (error) throw error;
      return res.json(mapLeaveDbToApi(data));
    }
    if (me.role === 'manager') {
      const u = await fetchUser(cur.user_id);
      if (!isManagerOf(me.id, u)) return res.status(403).json({ error: 'forbidden' });
      const { data, error } = await supa.from('leaves').update(patchDb).eq('id', id).select('*').single();
      if (error) throw error;
      return res.json(mapLeaveDbToApi(data));
    }
    return res.status(403).json({ error: 'forbidden' });
  } catch (e) {
    console.error('PUT /leaves/:id', e);
    res.status(500).json({ error: 'update leave failed' });
  }
});

app.delete('/leaves/:id', authRequired, async (req, res) => {
  try {
    const me = req.user;
    const id = req.params.id;
    const { data: cur, error: errCur } = await supa.from('leaves').select('*').eq('id', id).single();
    if (errCur) throw errCur;

    // owner może usunąć tylko submitted; admin zawsze; manager gdy submitted podopiecznego
    if (me.role === 'admin') {
      const { error } = await supa.from('leaves').delete().eq('id', id);
      if (error) throw error;
      return res.json({ ok: true });
    }
    if (cur.status !== 'submitted') return res.status(403).json({ error: 'cannot delete non-submitted' });

    if (me.role === 'employee' && cur.user_id === me.id) {
      const { error } = await supa.from('leaves').delete().eq('id', id);
      if (error) throw error;
      return res.json({ ok: true });
    }
    if (me.role === 'manager') {
      const u = await fetchUser(cur.user_id);
      if (!isManagerOf(me.id, u)) return res.status(403).json({ error: 'forbidden' });
      const { error } = await supa.from('leaves').delete().eq('id', id);
      if (error) throw error;
      return res.json({ ok: true });
    }
    return res.status(403).json({ error: 'forbidden' });
  } catch (e) {
    console.error('DELETE /leaves/:id', e);
    res.status(500).json({ error: 'delete leave failed' });
  }
});

// SPA fallback
app.get('*', (_req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

app.listen(PORT, () => {
  console.log(`Emerlog Urlopy (Supabase) running on :${PORT}`);
});
