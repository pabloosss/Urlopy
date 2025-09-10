);


const PORT = process.env.PORT || 3000;
const JWT_SECRET = process.env.JWT_SECRET || 'devsecret_change_me';


const app = express();
app.use(cors());
app.use(express.json({ limit: '5mb' }));


// ====== prosty "storage" w pliku db.json ======
const DATA_DIR = process.env.DATA_DIR || path.join(__dirname, 'data');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
const DB_FILE = path.join(DATA_DIR, 'db.json');


function uid(){ return Math.random().toString(36).slice(2,10)+Date.now().toString(36); }


function loadDB(){
if (!fs.existsSync(DB_FILE)){
const seed = {
org: { name: 'Calamari-lite (demo)', logo: null, hoursPerDay: 8 },
users: [
{ id: 'u1', name: 'Admin Demo', email: 'admin@demo.local', role: 'admin', managerId: null, contract: '1.0', hoursMonthly: null, startDate: '2025-09-01', pass: '1', vacationDays: 26 },
{ id: 'u2', name: 'Kierownik Demo',email: 'manager@demo.local', role: 'manager', managerId: 'u1', contract: '1.0', hoursMonthly: null, startDate: '2025-09-01', pass: '1', vacationDays: 26 },
{ id: 'u3', name: 'Pracownik Demo',email: 'worker@demo.local', role: 'employee',managerId: 'u2', contract: '0.75',hoursMonthly: null, startDate: '2025-09-11', pass: '1', vacationDays: 26 }
],
times: [
{ id: uid(), userId: 'u3', date: '2025-09-02', start: '08:00', end: '16:00', break: 30, project: 'U2 – instalacje', note: '', status: 'approved' },
{ id: uid(), userId: 'u3', date: '2025-09-03', start: '09:00', end: '17:00', break: 30, project: 'Serwis', note: '', status: 'approved' }
],
leaves: []
};
fs.writeFileSync(DB_FILE, JSON.stringify(seed, null, 2));
}
return JSON.parse(fs.readFileSync(DB_FILE, 'utf8'));
}
function saveDB(db){ fs.writeFileSync(DB_FILE, JSON.stringify(db, null, 2)); }


let db = loadDB();


// ====== auth / role helpers ======
function sign(user){ return jwt.sign({ id:user.id, role:user.role, email:user.email }, JWT_SECRET, { expiresIn:'12h' }); }
function auth(req,res,next){
const hdr=req.headers.authorization||''; const token = hdr.startsWith('Bearer ')? hdr.slice(7) : null;
if(!token) return res.status(401).json({error:'missing_token'});
try { req.user = jwt.verify(token, JWT_SECRET); next(); }
catch(e){ return res.status(401).json({error:'invalid_token'}); }
}
function allow(...roles){
return (req,res,next)=>{ if(['admin','hr'].includes(req.user.role)) return next(); if(roles.includes(req.user.role)) return next(); return res.status(403).json({error:'forbidden'}); };
}
function getUser(id){ return db.users.find(u=>u.id===id); }
function stripPass(u){ const {pass, ...rest}=u; return rest; }
function cleanUndefined(obj){ const out={}; Object.keys(obj).forEach(k=>{ if(obj[k]!==undefined) out[k]=obj[k]; }); return out; }
function isManagerOf(managerId,userId){ const u=getUser(userId); return !!u && u.managerId===managerId; }


// ====== AUTH ======
app.post('/auth/login',(req,res)=>{
const {email, pass} = req.body||{};
if(!email||!pass) return res.status(400).json({error:'missing_fields'});
const u = db.users.find(x=>x.email.toLowerCase()===String(email).toLowerCase());
if(!u || u.pass!==pass) return res.status(401).json({error:'bad_credentials'});
res.json({ token: sign(u), user: { id:u.id, name:u.name, email:u.email, role:u.role } });
});
app.get('/me', auth, (req,res)=>{ const u=getUser(req.user.id); res.json({ id:u.id, name:u.name, email:u.email, role:u.role }); });


// ====== ORG ======
app.get('/org', auth, (req,res)=> res.json(db.org));
app.put('/org', auth, allow('manager'), (req,res)=>{
const {name, logo, hoursPerDay} = req.body||{};
if(name!==undefined) db.org.name = name;
app.listen(PORT, ()=> console.log(`Calamari-lite running on :${PORT}`));
