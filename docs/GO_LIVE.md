# Put the studio online from this Mac (Cloudflare Tunnel)

The tunnel is an encrypted outbound connection from this Mac to Cloudflare. Visitors open
`https://studio.yourdomain.com`; Cloudflare passes the traffic through the tunnel to the studio. Nothing on
your router changes, and your Mac's address stays hidden. `cloudflared` is already installed.

## What you do (your accounts)

1. **Put your domain on Cloudflare** (free plan). Open dash.cloudflare.com → *Add a domain* → *Free*, then
   change the domain's nameservers at the company you bought it from to the two Cloudflare shows you. Wait
   until Cloudflare says **Active**; this usually takes minutes, sometimes a few hours. (Or buy a domain in
   Cloudflare → *Domain Registration*; it's then Active straight away.)
2. **Sign this Mac in to Cloudflare.** In Terminal:
   ```bash
   cloudflared tunnel login
   ```
   A browser opens: pick your domain and click *Authorize*.
3. **Go online** (pick any sub-name; `studio` is just an example):
   ```bash
   cd ~/yt-automation-studio && scripts/go_public.sh studio.yourdomain.com
   ```
4. **Tell Google the new address.** Open console.cloud.google.com → *APIs & Services* → *Credentials* → your
   OAuth client → *Authorized redirect URIs* → add `https://studio.yourdomain.com/oauth/google/callback` →
   *Save*. Keep the existing `http://127.0.0.1:8765/…` entry.
5. **Test from your phone on mobile data** (not your Wi-Fi): open `https://studio.yourdomain.com` and sign in.

## What the script does

- Creates a tunnel named `yt-studio` (only once) and the DNS record `studio.yourdomain.com` → tunnel.
- Exposes only the studio. Every other service on this Mac returns 404.
- Restarts the studio with its public address. YouTube sign-in then returns there, cookies become HTTPS-only,
  browsers are told to always use HTTPS, and sign-in limits use each visitor's real address.
- Installs the tunnel as a login service, so it restarts by itself.

Go back to this-computer-only at any time:

```bash
scripts/go_public.sh --off
```

## Before you invite other creators

- **Google sign-in for other people's YouTube channels.** While your Google OAuth app is in *Testing* mode,
  only people you add as test users (up to 100) can connect YouTube, and their connection expires after 7
  days. For the public, open *OAuth consent screen* → *Publish app* and pass Google's verification. Uploading
  is a sensitive permission: you need a home page and a privacy-policy page on your domain, plus a short demo
  video, and the review can take weeks.
- **Costs land on your accounts.** Every creator's scripts, voices and AI clips use your Ollama, Sarvam and
  Gemini keys and this Mac's CPU. Keep the Free plan limits low (Admin → Workspaces) until billing is live.
- **Keep this Mac on**: plugged in, online, and not asleep. Videos stream to visitors through your
  connection, so a phone hotspot will run out of data quickly; use Wi-Fi or broadband.
- **Use the public address yourself too.** HTTPS-only cookies are not saved over plain `http://` in every
  browser (Safari refuses them on `http://127.0.0.1`), so sign in at `https://studio.yourdomain.com`.
- **Still missing for a public product**: email verification and self-service password reset (no email
  system yet; the admin can reset passwords), a privacy policy, and terms of use.
