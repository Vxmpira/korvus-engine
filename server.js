/**
 * Crown — backend (Node + Express)
 * --------------------------------
 * nginx serves static files and proxies /api/* here (127.0.0.1:3000).
 * Secrets + config live in /etc/crown/crown.env (NOT in git). SQLite DB at /var/lib/crown/crown.db.
 *
 *   Accounts      — register/login/logout/me, hashed passwords, HTTP-only session cookies
 *   Metering      — per-user (and per-IP anonymous) monthly token budgets, enforced server-side
 *   Rate limiting — per-IP request caps on /api/chat and auth
 *   Billing       — Stripe Checkout + webhook writes Pro/Free to the DB, keyed to the user
 *
 * Card data NEVER touches this server. Stripe stays dormant until STRIPE_* keys are set.
 */
const express  = require("express");
const crypto   = require("crypto");
const fs       = require("fs");
const path     = require("path");
const bcrypt   = require("bcryptjs");
const Database = require("better-sqlite3");

const KEY    = process.env.ANTHROPIC_API_KEY;
const MODEL  = process.env.MODEL || "claude-sonnet-4-20250514";
const PORT   = process.env.PORT || 3000;
const SYSTEM = "You are Crown, the assistant for BlackCrown VxJ. Confident, refined, concise. Help with writing, code, strategy and ideas.";

// usage budgets (override in crown.env)
const FREE_LIMIT   = parseInt(process.env.FREE_TOKEN_LIMIT || "10000", 10);   // logged-in free / month
const ANON_LIMIT   = parseInt(process.env.ANON_TOKEN_LIMIT || "3000", 10);    // try-before-signup / month
const PRO_FAIR_USE = parseInt(process.env.PRO_FAIR_USE     || "5000000", 10); // fair-use ceiling on "unlimited" Pro
const PERIOD_MS    = 30 * 24 * 60 * 60 * 1000;
const SESSION_DAYS = 30;

// Stripe
const STRIPE_SECRET = process.env.STRIPE_SECRET_KEY;
const STRIPE_PRICE  = process.env.STRIPE_PRICE_ID;
const STRIPE_WHSEC  = process.env.STRIPE_WEBHOOK_SECRET;
const PUBLIC_URL    = process.env.PUBLIC_URL || "https://blackcrown-intelligence.com";
const stripe = STRIPE_SECRET ? require("stripe")(STRIPE_SECRET) : null;

// ---- database ----
const DB_PATH = process.env.CROWN_DB || "/var/lib/crown/crown.db";
try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); } catch (_) {}
const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");
db.exec(`
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  username TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  tier TEXT NOT NULL DEFAULT 'free',
  stripe_customer_id TEXT,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  period_start INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS anon_usage (
  ip TEXT PRIMARY KEY,
  tokens_used INTEGER NOT NULL DEFAULT 0,
  period_start INTEGER NOT NULL
);
`);

const q = {
  userByEmail:    db.prepare("SELECT * FROM users WHERE email = ?"),
  userById:       db.prepare("SELECT * FROM users WHERE id = ?"),
  userByCustomer: db.prepare("SELECT * FROM users WHERE stripe_customer_id = ?"),
  insertUser:     db.prepare("INSERT INTO users (email,username,password_hash,tier,tokens_used,period_start,created_at) VALUES (?,?,?,'free',0,?,?)"),
  setTokens:      db.prepare("UPDATE users SET tokens_used = ?, period_start = ? WHERE id = ?"),
  addTokens:      db.prepare("UPDATE users SET tokens_used = tokens_used + ? WHERE id = ?"),
  setTier:        db.prepare("UPDATE users SET tier = ? WHERE id = ?"),
  setTierCust:    db.prepare("UPDATE users SET tier = ?, stripe_customer_id = ? WHERE id = ?"),
  setCustomer:    db.prepare("UPDATE users SET stripe_customer_id = ? WHERE id = ?"),
  insertSession:  db.prepare("INSERT INTO sessions (token,user_id,expires_at) VALUES (?,?,?)"),
  sessionByToken: db.prepare("SELECT * FROM sessions WHERE token = ?"),
  delSession:     db.prepare("DELETE FROM sessions WHERE token = ?"),
  anonGet:        db.prepare("SELECT * FROM anon_usage WHERE ip = ?"),
  anonUpsert:     db.prepare("INSERT INTO anon_usage (ip,tokens_used,period_start) VALUES (?,?,?) ON CONFLICT(ip) DO UPDATE SET tokens_used=excluded.tokens_used, period_start=excluded.period_start"),
};

const now      = () => Date.now();
const inDays   = d => now() + d * 24 * 60 * 60 * 1000;
const genToken = () => crypto.randomBytes(32).toString("hex");
const validEmail = e => typeof e === "string" && /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e);
const clientIp = req => String(req.headers["x-real-ip"] || (req.headers["x-forwarded-for"] || "").split(",")[0] || req.socket.remoteAddress || "0.0.0.0").trim();

function parseCookies(req){
  const out = {}; const h = req.headers.cookie; if (!h) return out;
  h.split(";").forEach(p => { const i = p.indexOf("="); if (i > 0) out[p.slice(0,i).trim()] = decodeURIComponent(p.slice(i+1).trim()); });
  return out;
}
function rollPeriod(u){
  if (now() - u.period_start >= PERIOD_MS) { q.setTokens.run(0, now(), u.id); u.tokens_used = 0; u.period_start = now(); }
  return u;
}
function getSessionUser(req){
  const t = parseCookies(req).crown_session; if (!t) return null;
  const s = q.sessionByToken.get(t); if (!s) return null;
  if (s.expires_at < now()) { q.delSession.run(t); return null; }
  const u = q.userById.get(s.user_id); if (!u) return null;
  return rollPeriod(u);
}
function setSessionCookie(res, token){
  res.cookie("crown_session", token, { httpOnly:true, secure:true, sameSite:"lax", path:"/", maxAge: SESSION_DAYS*24*60*60*1000 });
}
const userLimit = u => (u.tier === "pro" ? PRO_FAIR_USE : FREE_LIMIT);
const publicUser = u => ({ username:u.username, email:u.email, tier:u.tier, tokensUsed:u.tokens_used, limit:userLimit(u) });

// in-memory rate limiter (per key)
const rl = new Map();
function rateLimited(key, max, windowMs){
  const t = now(); let e = rl.get(key);
  if (!e || t > e.reset) { e = { n:0, reset:t+windowMs }; rl.set(key, e); }
  e.n++; return e.n > max;
}
const sweep = setInterval(() => { const t = now(); for (const [k,v] of rl) if (t > v.reset) rl.delete(k); }, 5*60*1000);
if (sweep.unref) sweep.unref();

const app = express();
app.disable("x-powered-by");

/* Stripe webhook — RAW body, registered BEFORE express.json() */
app.post("/api/stripe/webhook", express.raw({ type: "application/json" }), (req, res) => {
  if (!stripe || !STRIPE_WHSEC) return res.status(400).send("Stripe not configured");
  let event;
  try { event = stripe.webhooks.constructEvent(req.body, req.headers["stripe-signature"], STRIPE_WHSEC); }
  catch (err) { console.error("Webhook signature failed:", err.message); return res.status(400).send(`Webhook Error: ${err.message}`); }
  try {
    if (event.type === "checkout.session.completed") {
      const s = event.data.object;
      let u = s.client_reference_id ? q.userById.get(parseInt(s.client_reference_id, 10)) : null;
      if (!u && s.customer_email) u = q.userByEmail.get(String(s.customer_email).toLowerCase());
      if (u) { q.setTierCust.run("pro", s.customer || u.stripe_customer_id || null, u.id); console.log("PRO  ->", u.email); }
      else console.log("PAID but no matching user:", s.customer_email);
    } else if (event.type === "customer.subscription.deleted") {
      const u = q.userByCustomer.get(event.data.object.customer);
      if (u) { q.setTier.run("free", u.id); console.log("FREE ->", u.email); }
    } else if (event.type === "customer.subscription.updated") {
      const sub = event.data.object; const u = q.userByCustomer.get(sub.customer);
      if (u) q.setTier.run((sub.status === "active" || sub.status === "trialing") ? "pro" : "free", u.id);
    }
  } catch (e) { console.error("Webhook handler error:", e.message); }
  res.json({ received: true });
});

app.use(express.json({ limit: "1mb" }));

app.get("/api/health", (_req, res) =>
  res.json({ ok:true, model:MODEL, keyLoaded:!!KEY, stripe:!!stripe, priceSet:!!STRIPE_PRICE, db:true, accounts:true }));

// ---- auth ----
app.post("/api/register", (req, res) => {
  if (rateLimited("auth:"+clientIp(req), 12, 60000)) return res.status(429).json({ error:"rate", message:"Too many attempts. Try again shortly." });
  const email = String(req.body.email || "").trim().toLowerCase();
  const username = String(req.body.username || "").trim();
  const password = String(req.body.password || "");
  if (!validEmail(email))    return res.status(400).json({ error:"bad_email", message:"Enter a valid email address." });
  if (username.length < 3)   return res.status(400).json({ error:"bad_username", message:"Username must be at least 3 characters." });
  if (password.length < 8)   return res.status(400).json({ error:"bad_password", message:"Password must be at least 8 characters." });
  if (q.userByEmail.get(email)) return res.status(409).json({ error:"exists", message:"That email is already registered." });
  const info = q.insertUser.run(email, username, bcrypt.hashSync(password, 10), now(), now());
  const token = genToken(); q.insertSession.run(token, info.lastInsertRowid, inDays(SESSION_DAYS));
  setSessionCookie(res, token);
  res.json({ user: publicUser(q.userById.get(info.lastInsertRowid)) });
});

app.post("/api/login", (req, res) => {
  if (rateLimited("auth:"+clientIp(req), 12, 60000)) return res.status(429).json({ error:"rate", message:"Too many attempts. Try again shortly." });
  const email = String(req.body.email || "").trim().toLowerCase();
  const u = q.userByEmail.get(email);
  if (!u || !bcrypt.compareSync(String(req.body.password || ""), u.password_hash))
    return res.status(401).json({ error:"bad_creds", message:"Wrong email or password." });
  const token = genToken(); q.insertSession.run(token, u.id, inDays(SESSION_DAYS));
  setSessionCookie(res, token);
  res.json({ user: publicUser(rollPeriod(u)) });
});

app.post("/api/logout", (req, res) => {
  const t = parseCookies(req).crown_session; if (t) q.delSession.run(t);
  res.clearCookie("crown_session", { path:"/" });
  res.json({ ok:true });
});

app.get("/api/me", (req, res) => {
  const u = getSessionUser(req);
  res.json({ user: u ? publicUser(u) : null });
});

// ---- chat (rate limited + metered) ----
app.post("/api/chat", async (req, res) => {
  if (rateLimited("chat:"+clientIp(req), 30, 60000)) return res.status(429).json({ error:"rate", message:"You're going a bit fast — give it a moment." });
  if (!KEY) return res.status(500).json({ error:"no_key", message:"ANTHROPIC_API_KEY not set on the server." });

  const u = getSessionUser(req);
  let tier, limit, used, apply;
  if (u) {
    tier = u.tier; limit = userLimit(u); used = u.tokens_used;
    apply = n => q.addTokens.run(n, u.id);
  } else {
    const ip = clientIp(req);
    tier = "free"; limit = ANON_LIMIT;
    let a = q.anonGet.get(ip);
    if (a && now() - a.period_start >= PERIOD_MS) a = null;
    used = a ? a.tokens_used : 0;
    const start = a ? a.period_start : now();
    apply = n => q.anonUpsert.run(ip, used + n, start);
  }
  if (used >= limit) {
    return res.status(402).json({ error:"limit", tier, used, limit,
      message: u ? (tier === "pro"
            ? "You've reached the fair-use ceiling for this period. Please contact support."
            : "You've used your free monthly allotment. Upgrade to Pro for unlimited use.")
          : "You've reached the free trial limit. Create an account to keep going." });
  }
  try {
    const messages = Array.isArray(req.body.messages) ? req.body.messages : [];
    const r = await fetch("https://api.anthropic.com/v1/messages", {
      method:"POST",
      headers:{ "content-type":"application/json", "x-api-key":KEY, "anthropic-version":"2023-06-01" },
      body: JSON.stringify({ model:MODEL, max_tokens:1024, system:SYSTEM, messages })
    });
    const data = await r.json();
    if (!r.ok) return res.status(502).json({ error:"model_error", detail:data });
    const reply = (data.content || []).filter(b => b.type === "text").map(b => b.text).join("\n").trim();
    const spent = (data.usage?.input_tokens || 0) + (data.usage?.output_tokens || 0);
    apply(spent);
    const nowUsed = used + spent;
    res.json({ reply, usage:{ tier, used:nowUsed, limit, remaining:Math.max(0, limit - nowUsed) } });
  } catch (e) {
    res.status(500).json({ error:"server_error", message:e.message });
  }
});

// ---- Stripe checkout / portal (require login) ----
async function resolvePriceId(){
  if (!STRIPE_PRICE) return null;
  if (STRIPE_PRICE.startsWith("price_")) return STRIPE_PRICE;
  if (STRIPE_PRICE.startsWith("prod_")) { const l = await stripe.prices.list({ product:STRIPE_PRICE, active:true, limit:1 }); return l.data[0] ? l.data[0].id : null; }
  return STRIPE_PRICE;
}
app.post("/api/checkout", async (req, res) => {
  if (!stripe || !STRIPE_PRICE) return res.status(503).json({ error:"stripe_unconfigured", message:"Stripe is not set up on the server yet." });
  const u = getSessionUser(req);
  if (!u) return res.status(401).json({ error:"auth_required", message:"Please log in first." });
  try {
    const price = await resolvePriceId();
    if (!price) return res.status(503).json({ error:"no_price", message:"No active price found for the configured Stripe product." });
    const session = await stripe.checkout.sessions.create({
      mode:"subscription",
      line_items:[{ price, quantity:1 }],
      customer: u.stripe_customer_id || undefined,
      customer_email: u.stripe_customer_id ? undefined : u.email,
      client_reference_id: String(u.id),
      allow_promotion_codes:true,
      success_url:`${PUBLIC_URL}/chat?checkout=success`,
      cancel_url:`${PUBLIC_URL}/chat?checkout=cancel`
    });
    res.json({ url: session.url });
  } catch (e) { console.error("Checkout error:", e.message); res.status(500).json({ error:"checkout_failed", message:e.message }); }
});
app.post("/api/portal", async (req, res) => {
  if (!stripe) return res.status(503).json({ error:"stripe_unconfigured", message:"Stripe is not set up yet." });
  const u = getSessionUser(req);
  if (!u) return res.status(401).json({ error:"auth_required", message:"Please log in first." });
  try {
    let customerId = u.stripe_customer_id;
    if (!customerId) { const c = await stripe.customers.list({ email:u.email, limit:1 }); customerId = c.data[0] && c.data[0].id; if (customerId) q.setCustomer.run(customerId, u.id); }
    if (!customerId) return res.status(404).json({ error:"no_customer", message:"No subscription found yet." });
    const session = await stripe.billingPortal.sessions.create({ customer:customerId, return_url:`${PUBLIC_URL}/chat` });
    res.json({ url: session.url });
  } catch (e) { console.error("Portal error:", e.message); res.status(500).json({ error:"portal_failed", message:e.message }); }
});

app.listen(PORT, "127.0.0.1", () => console.log(`Crown backend on 127.0.0.1:${PORT} | model ${MODEL} | stripe ${!!stripe} | db ${DB_PATH}`));
