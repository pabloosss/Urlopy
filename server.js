// server.js — Emerlog Urlopy alfa 0.1
// ENV: PORT (3000), JWT_SECRET, DATA_DIR (np. "/data" na Render Persistent Disk)

const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');
const jwt = require('jsonwebtoken');

const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'devsecret_change_me';

const app = express();
app.use(cors());
app.use(express.json({ limit: '5mb' }));

// ====== plikowa "baza" ======
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const DB_FILE = path.join(DATA_DIR, 'db.json');

function uid(){ return Math.random().toString(36).slice(2,10)+Date.now().toString(36); }

function makeSeed() {
  return {
    org: { name: 'Emerlog Urlopy alfa 0.1', logo: null, hoursPerDay: 8 },
    users: [
      // employment: 'UOP' | 'JDG'
      { id: 'u1', name: 'Admin Demo',    email: 'admin@demo.local',   role: 'admin',   employment: 'UOP', managerId: null, contract: '1.0', hoursMonthly: null, startDate: '2025-09-01', pass: '1', vacationDays: 20 },
      { id: 'u2', name: 'Kierownik Demo',email: 'manager@demo.local', role: 'manager', employment: 'UOP', managerId: 'u1',  contract: '1.0', hoursMonthly: null, startDate: '2025-09-01', pass: '1', vacationDays: 20 },
      { id: 'u3', name: 'Pracownik Demo',email: 'worker@demo.local',  role: 'employee',employment: 'UOP', managerId: 'u2',  contract: '1.0', hoursMonthly: null, startDate: '2025-09-11', pass: '1', vacationDays: 20 }
    ],
    times: [], // (nieużywane w tej wersji, zostawiamy kompatybilność API)
    leaves: [
      // przykładowy wniosek do podglądu w kalendarzu
      // { id: uid(), userId:'u3', type:'wypoczynkowy', from:'2025-09-15', to:'2025-09-17', comment:'test', status:'manager_approved', decidedByManager:'u2', decidedAtManager: Date.now() }
    ]
  };
}

// SAMOLECZENIE: jeśli plik nie istnieje / pusty / uszkodzony -> zrób backup i stwórz seed
function loadDB() {
  if (!fs.existsSync(DB_FILE) || !fs.statSync(DB_FILE).size) {
    const seed = makeSeed();
    fs.writeFileSync(DB_FILE, JSON.stringify(seed, null, 2));
    return seed;
  }
  try {
    const txt = fs.readFileSync(DB_FILE, 'utf8').trim();
    if (!txt) throw new Error('empty');
    const parsed = JSON.parse(txt);
    if (!parsed.org || !parsed.users) throw new Error('invalid shape');
    return parsed;
  } catch (e) {
    try { fs.renameSync(DB_FILE, DB_FILE + '.bak-' + Date.now()); } catch {}
    const seed = makeSeed();
    fs.writeFileSync(DB_FILE, JSON.stringify(seed, null, 2));
    return seed;
  }
}
function saveDB(db){ fs.writeFileSync(DB_FILE, JSON.stringify(db, null, 2)); }

let db = loadDB();

// ====== helpers auth/role ======
function sign(user){ return jwt.sign({ id:user.id, role:user.role, email:user.email }, JWT_SECRET, { expiresIn:'12h' }); }
function auth(req,res,next){
  const hdr = req.headers.authorization || '';
  const token = hdr.startsWith('Bearer ') ? hdr.slice(7) : null;
  if(!token) return res.status(401).json({error:'missing_token'});
  try { req.user = jwt.verify(token, JWT_SECRET); next(); }
  catch(e){ return res.status(401).json({error:'invalid_token'}); }
}
function allow(...roles){
  return (req,res,next)=>{
    if(['admin','hr'].includes(req.user.role)) return next();
    if(roles.includes(req.user.role)) return next();
    return res.status(403).json({error:'forbidden'});
  };
}
function getUser(id){ return db.users.find(u=>u.id===id); }
function stripPass(u){ const {pass, ...rest}=u; return rest; }
function cleanUndefined(obj){ const out={}; Object.keys(obj).forEach(k=>{ if(obj[k]!==undefined) out[k]=obj[k]; }); return out; }
function isManagerOf(managerId, userId){ const u=getUser(userId); return !!u && u.managerId===managerId; }

// ====== AUTH ======
app.post('/auth/login',(req,res)=>{
  const {email, pass} = req.body || {};
  if(!email||!pass) return res.status(400).json({error:'missing_fields'});
  const u = db.users.find(x=>x.email.toLowerCase()===String(email).toLowerCase());
  if(!u || u.pass !== pass) return res.status(401).json({error:'bad_credentials'});
  return res.json({ token: sign(u), user: { id:u.id, name:u.name, email:u.email, role:u.role }});
});
app.get('/me', auth, (req,res)=>{
  const u=getUser(req.user.id);
  res.json({ id:u.id, name:u.name, email:u.email, role:u.role });
});

// ====== ORG (pozostawione dla zgodności; UI nie używa) ======
app.get('/org', auth, (req,res)=> res.json(db.org));

// ====== USERS ======
app.get('/users', auth, (req,res)=>{
  const me = getUser(req.user.id);
  let rows=[];
  if(['admin','hr'].includes(me.role)){
    rows = db.users.map(stripPass);
  } else if(me.role==='manager'){
    rows = db.users.filter(u=> u.id===me.id || u.managerId===me.id).map(stripPass);
  } else {
    rows = db.users.filter(u=> u.id===me.id).map(stripPass);
  }
  res.json(rows);
});
app.post('/users', auth, allow('manager'), (req,res)=>{
  // tylko admin/hr może dodawać/edytować
  const me = getUser(req.user.id);
  if(!['admin','hr'].includes(me.role)) return res.status(403).json({error:'forbidden'});
  const u = req.body||{};
  if(!u.name || !u.email || !u.role) return res.status(400).json({error:'missing_fields'});
  const id = u.id || uid();
  db.users.push({
    id,
    name:u.name,
    email:u.email,
    role:u.role,
    employment:u.employment || 'UOP',
    managerId:u.managerId||null,
    contract:u.contract||'1.0',
    hoursMonthly:u.hoursMonthly||null,
    startDate:u.startDate||null,
    pass:u.pass||'1',
    vacationDays: u.vacationDays ?? 20
  });
  saveDB(db);
  res.json({id});
});
app.put('/users/:id', auth, allow('manager'), (req,res)=>{
  const me = getUser(req.user.id);
  if(!['admin','hr'].includes(me.role)) return res.status(403).json({error:'forbidden'});
  const id=req.params.id; const u=db.users.find(x=>x.id===id);
  if(!u) return res.status(404).json({error:'not_found'});
  Object.assign(u, cleanUndefined({
    name:req.body.name, email:req.body.email, role:req.body.role, managerId:req.body.managerId,
    employment:req.body.employment,
    contract:req.body.contract, hoursMonthly:req.body.hoursMonthly, startDate:req.body.startDate, pass:req.body.pass,
    vacationDays:req.body.vacationDays
  }));
  saveDB(db);
  res.json({ok:true});
});
app.delete('/users/:id', auth, allow('manager'), (req,res)=>{
  const me = getUser(req.user.id);
  if(!['admin','hr'].includes(me.role)) return res.status(403).json({error:'forbidden'});
  const id=req.params.id;
  db.users = db.users.filter(u=>u.id!==id);
  db.times = db.times.filter(t=>t.userId!==id);
  db.leaves = db.leaves.filter(l=>l.userId!==id);
  saveDB(db);
  res.json({ok:true});
});

// ====== LEAVES (wnioski urlopowe) ======
// Statusy: submitted -> manager_approved -> approved ; odrzucenia: rejected_manager / rejected_admin
app.get('/leaves', auth, (req,res)=>{
  const me = getUser(req.user.id);
  let rows=[];
  if(['admin','hr'].includes(me.role)){
    rows = db.leaves;
  } else if(me.role==='manager'){
    rows = db.leaves.filter(l=> l.userId===me.id || isManagerOf(me.id,l.userId) );
  } else {
    rows = db.leaves.filter(l=> l.userId===me.id );
  }
  res.json(rows);
});
app.post('/leaves', auth, (req,res)=>{
  const me = getUser(req.user.id);
  const l=req.body||{};
  if(!l.userId || !l.from || !l.to || !l.type) return res.status(400).json({error:'missing_fields'});
  const can = ['admin','hr'].includes(me.role) || l.userId===me.id || (me.role==='manager' && isManagerOf(me.id,l.userId));
  if(!can) return res.status(403).json({error:'forbidden'});
  const id=l.id||uid();
  db.leaves.push({
    id,
    userId:l.userId,
    type:l.type, // 'wypoczynkowy' | 'na_zadanie' | ...
    from:l.from,
    to:l.to,
    comment:l.comment||null,
    status:'submitted',
    decidedByManager:null, decidedAtManager:null,
    decidedByAdmin:null, decidedAtAdmin:null
  });
  saveDB(db);
  res.json({id});
});
app.put('/leaves/:id', auth, (req,res)=>{
  const me = getUser(req.user.id); const id=req.params.id;
  const row = db.leaves.find(x=>x.id===id);
  if(!row) return res.status(404).json({error:'not_found'});

  const applicant = getUser(row.userId);
  const isSelf = row.userId === me.id;
  const isMgr = (me.role==='manager');
  const isAdm = (me.role==='admin' || me.role==='hr');
  const isMgrOf = isMgr && isManagerOf(me.id,row.userId);

  // Edycja pól wniosku (bez statusu)
  const wantsStatus = Object.prototype.hasOwnProperty.call(req.body,'status');
  if(!wantsStatus){
    // Właściciel może edytować dopóki submitted; Admin może zawsze
    if(isSelf && row.status!=='submitted' && !isAdm) return res.status(403).json({error:'cannot_edit_after_review'});
    Object.assign(row, cleanUndefined({
      type:req.body.type, from:req.body.from, to:req.body.to, comment:req.body.comment
    }));
    saveDB(db); return res.json({ok:true});
  }

  // Zmiana statusu — zasady:
  const next = req.body.status;
  // Blokada samoakceptacji (nikt nie zmienia statusu swojego wniosku)
  if(isSelf) return res.status(403).json({error:'self_approval_forbidden'});

  if(isMgrOf){
    // Manager może tylko z 'submitted' -> manager_approved / rejected_manager
    if(row.status!=='submitted') return res.status(400).json({error:'invalid_state'});
    if(next==='manager_approved'){
      row.status='manager_approved';
      row.decidedByManager=me.id; row.decidedAtManager=Date.now();
    } else if(next==='rejected_manager'){
      row.status='rejected_manager';
      row.decidedByManager=me.id; row.decidedAtManager=Date.now();
    } else {
      return res.status(400).json({error:'invalid_transition'});
    }
    saveDB(db); return res.json({ok:true});
  }

  if(isAdm){
    // Admin finalnie: z 'manager_approved' -> approved/rejected_admin
    // Specjalny przypadek: gdy wnioskodawcą jest manager, może z 'submitted' -> approved/rejected_admin
    const isApplicantManager = applicant?.role==='manager';
    const canFinalize = (row.status==='manager_approved') || (isApplicantManager && row.status==='submitted');
    if(!canFinalize) return res.status(400).json({error:'awaiting_manager'});
    if(next==='approved'){
      row.status='approved';
      row.decidedByAdmin=me.id; row.decidedAtAdmin=Date.now();
    } else if(next==='rejected_admin'){
      row.status='rejected_admin';
      row.decidedByAdmin=me.id; row.decidedAtAdmin=Date.now();
    } else {
      return res.status(400).json({error:'invalid_transition'});
    }
    saveDB(db); return res.json({ok:true});
  }

  return res.status(403).json({error:'forbidden'});
});
app.delete('/leaves/:id', auth, (req,res)=>{
  const me = getUser(req.user.id); const id=req.params.id;
  const row = db.leaves.find(x=>x.id===id);
  if(!row) return res.status(404).json({error:'not_found'});
  const can = ['admin','hr'].includes(me.role) || row.userId===me.id || (me.role==='manager' && isManagerOf(me.id,row.userId));
  if(!can) return res.status(403).json({error:'forbidden'});
  // Właściciel może usunąć tylko dopóki submitted
  if(row.userId===me.id && row.status!=='submitted' && !['admin','hr'].includes(me.role)) return res.status(403).json({error:'cannot_delete_after_review'});
  db.leaves = db.leaves.filter(x=>x.id!==id);
  saveDB(db);
  res.json({ok:true});
});

// ====== health ======
app.get('/health', (req,res)=> res.json({ok:true}));

// ====== front statyczny ======
app.use(express.static(path.join(__dirname, 'public')));
app.get('*', (req,res)=> res.sendFile(path.join(__dirname, 'public', 'index.html')));

app.listen(PORT, ()=> console.log(`Emerlog Urlopy running on :${PORT}`));
