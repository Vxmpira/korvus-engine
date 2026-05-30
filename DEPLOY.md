# Korvus — Phase 4: Going 24/7 on a Cloud Server

This takes Korvus off your PC and onto a cloud server that runs around the
clock, reachable from anywhere by a web address.

*by BlackCrownVxJ.LLC*

---

## What you'll end up with

```
  Your cloud server (always on)
  ┌─────────────────────────────────────────────┐
  │  korvus-engine.service  → loops 24/7,         │
  │     pulls Benzinga + scores with Claude       │
  │  korvus-server.service  → serves dashboard    │
  │     (Gunicorn, port 8000)                     │
  │  Nginx → public web on port 80/443 (HTTPS)    │
  └─────────────────────────────────────────────┘
            ▲
            │  https://korvus.yourdomain.com
       you, from anywhere
```

systemd keeps both pieces alive — they auto-start on boot and auto-restart if
they ever crash. That's what makes it genuinely 24/7.

---

## Step 0 — Pick a server (one decision)

You need a plain **Linux VPS running Ubuntu 24.04**. Any of these work; the
steps below are identical on all of them:

| Provider | Notes | Rough cost |
|----------|-------|-----------|
| **DigitalOcean** | Simplest dashboard, great docs — recommended for starting | ~$6–12/mo |
| **Hetzner** | Cheapest, excellent value | ~$5–9/mo |
| **AWS EC2** | What your coworker suggested — use a `t3.small` Ubuntu instance, **not Amplify** (Amplify can't run an always-on loop or hold the database). More setup, steeper billing. | ~$15/mo |

Pick the smallest "2 GB RAM / 1 vCPU" tier — plenty for Korvus. Choose
**Ubuntu 24.04 LTS** as the image. You'll get an IP address and SSH access.

> **Why not Amplify:** it's for static sites + serverless functions that run
> per-request. Korvus needs an always-on process (the engine loop) and a
> persistent database file — neither fits Amplify. A normal VPS is the right
> tool.

---

## Step 1 — Connect to the server

On **AWS EC2**, the easiest way in is the browser: select your instance →
**Connect** → **EC2 Instance Connect** → Connect. You land at a shell prompt
as the **`ubuntu`** user. (Desktop SSH also works:
`ssh -i C:\path\to\korvus-key.pem ubuntu@YOUR_SERVER_IP` — point `-i` at the
real location of your downloaded .pem file.)

> These deploy files are set up for the **`ubuntu`** user (the EC2 default), so
> there's no separate user to create. Just work from the `ubuntu` prompt.

---

## Step 2 — Install what's needed
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv nginx git -y
```

---

## Step 3 — Get Korvus onto the server

Easiest: push your `korvus-engine` folder to a **private** GitHub repo, then:
```bash
cd ~
git clone https://github.com/YOUR_USERNAME/korvus-engine.git
cd korvus-engine
```
*(Or use `scp` to copy the folder up directly — ask if you want those commands.)*

> ⚠️ Make sure `.env` is **NOT** in the repo — `.gitignore` already excludes it.
> You'll recreate `.env` on the server in Step 5.

---

## Step 4 — Python environment
```bash
cd ~/korvus-engine
python3 -m venv venv
venv/bin/pip install -r requirements.txt gunicorn
```

---

## Step 5 — Put your keys on the server

Recreate `.env` here (it never leaves your control):
```bash
nano .env
```
Paste the same keys you use locally (Anthropic, Benzinga, Alpha Vantage
premium, etc.), save with Ctrl+O / Enter, exit with Ctrl+X.

Test the engine runs once:
```bash
venv/bin/python korvus_engine.py
```
You should see it pull and score items, just like on your PC.

---

## Step 6 — Turn on the two services (the 24/7 part)
```bash
# copy the service files into place
sudo cp deploy/korvus-engine.service /etc/systemd/system/
sudo cp deploy/korvus-server.service /etc/systemd/system/
sudo systemctl daemon-reload

# start them and enable auto-start on boot
sudo systemctl enable --now korvus-engine
sudo systemctl enable --now korvus-server

# check they're running
sudo systemctl status korvus-engine
sudo systemctl status korvus-server
```
Watch the engine work live:
```bash
journalctl -u korvus-engine -f      # Ctrl+C to stop watching
```

---

## Step 7 — Put it on the public web (Nginx)
```bash
sudo cp deploy/korvus.nginx /etc/nginx/sites-available/korvus
sudo ln -s /etc/nginx/sites-available/korvus /etc/nginx/sites-enabled/
sudo nano /etc/nginx/sites-available/korvus   # set server_name to your domain or IP
sudo nginx -t                                  # should say "syntax is ok"
sudo systemctl reload nginx
```
At this point, visiting `http://YOUR_SERVER_IP` shows your live dashboard.

---

## Step 8 — Your domain + HTTPS (the padlock)

1. Buy a domain if you haven't (Namecheap, Cloudflare, etc. — e.g. `korvus.io`).
2. Add an **A record** pointing your domain to the server's IP.
3. Turn on free HTTPS:
```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d korvus.yourdomain.com
```
Certbot configures the certificate automatically and renews it for you.

**Done — Korvus is now live 24/7 at `https://korvus.yourdomain.com`.**

---

## Everyday commands you'll use
```bash
sudo systemctl restart korvus-engine     # after editing .env or code
sudo systemctl restart korvus-server
journalctl -u korvus-engine -f           # watch engine logs
journalctl -u korvus-server -f           # watch server logs
git pull && sudo systemctl restart korvus-server korvus-engine   # deploy updates
```

---

## ⚠️ Before you make it public for paying members (read this)

Right now anyone with the link can see the dashboard. **Do not charge members
yet** — that's **Phase 5**, and it needs real pieces:

1. **Login system** — accounts, password hashing, sessions. (I'll build this.)
   *You* set up the actual member sign-ups; never have code create accounts.
2. **Payments** — Stripe subscriptions for the fee. This is its own integration.
3. **The redistribution line** — serving live exchange quotes to *other people*
   needs an exchange license. The clean design: Korvus serves your news + AI
   intelligence (your content, fine to share) while members' live prices come
   from their own platform (Tradovate) or are delayed. Confirm with your
   attorney before going paid.
4. **Lock down the server** — firewall (ufw), fail2ban, and keep the dashboard
   behind the login before it's truly public.

So Phase 4 gives you a private, always-on, web-accessible Korvus you can reach
from anywhere. Phase 5 adds the gate and the paywall to safely open it to
members.

---

## Cost summary (running 24/7)
- Server (VPS): ~$5–15/mo
- Claude Haiku scoring: a few $/mo
- Benzinga Basic: free (premium = quote later)
- Alpha Vantage premium (live quotes): ~$50/mo *when you enable it*
- Domain: ~$12/year

A private always-on setup (before live quotes) runs well under $20/mo.
