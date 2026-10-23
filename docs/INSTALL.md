# Installing YT Automation Studio

Runs on **macOS** (Apple Silicon or Intel) and **Windows 10/11**. Linux works with the same steps as macOS,
using your package manager for Python 3.12 and FFmpeg.
It needs about 4 GB of free disk space plus room for videos, 8 GB+ RAM, and an internet connection.

**Android / iPhone:** the studio can't run on a phone. It needs FFmpeg, Python and hours of rendering.
Run it on a Mac or PC; the phone is only for watching the results on YouTube.

## 1. Get the code

```bash
git clone https://github.com/RaviBhardawaj/YT-Automation.git
cd YT-Automation
```

(or download the ZIP from GitHub → Code → Download ZIP and unzip it).

## 2. Install

### macOS

```bash
install/install-mac.sh --service
```

This installs Homebrew (if missing), Python 3.12, FFmpeg and libraqm (for correct Hindi text), the Python
packages, and a background service that starts at login and keeps the Mac awake while it runs.
Leave out `--service` to start it by hand with `./run.sh`.

### Windows

Open **PowerShell** in the project folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File install\install-windows.ps1 -Service
```

This installs Python 3.12 and FFmpeg with winget, the Python packages, and a Task Scheduler task that starts
the studio at every login. Leave out `-Service` to start it by double-clicking `run.bat`.
Set **Settings → System → Power → Sleep** to *Never* so scheduled videos are made on time.

Then open **http://127.0.0.1:8765**.

## 3. Add your keys (Settings → Credentials)

| Key | Needed for | Where to get it |
|---|---|---|
| Ollama API key | scripts, ideas, research (required) | ollama.com/settings/keys |
| Sarvam AI API key | Hindi/Hinglish voices | dashboard.sarvam.ai |
| YouTube Data API key | competitor research (optional) | console.cloud.google.com |
| Google OAuth client (JSON) | uploading to YouTube | Settings → YouTube explains the steps |

Keys are stored in the computer's secure key store (macOS Keychain / Windows Credential Manager),
never in the project folder or on GitHub.

Then open **Channels**, create a channel, and connect its YouTube account (Channels → channel → YouTube & publishing).

## Moving to another computer (keeping everything)

On the **old** computer:

```bash
.venv/bin/python scripts/export_keys.py
```

Copy two things to the same project folder on the new computer: **`data/`** (database, channels, characters,
videos) and **`secrets/`** (the exported keys). Then run the installer on the new computer: it loads
`secrets/api_keys.env` into the new key store automatically. Delete `secrets/api_keys.env` afterwards.
Stop the studio on the old computer so two machines don't publish the same channel.

`data/` and `secrets/` are git-ignored: they never go to GitHub. Move them with a USB drive or a private
cloud folder, never a public link.

## Updating

```bash
git pull
install/install-mac.sh          # Windows: powershell -ExecutionPolicy Bypass -File install\install-windows.ps1
```

Then restart the studio. The macOS service restarts with:
`launchctl kickstart -k gui/$(id -u)/com.ytautomation.studio`. On Windows, sign out and back in.
Database changes apply automatically on start.

## Exact versions

`requirements.txt` lists the direct packages. `requirements-lock.txt` pins every package exactly as tested on
macOS (Python 3.12). Use it for an identical setup: `.venv/bin/python -m pip install -r requirements-lock.txt`.

## Selling it to creators: what to sort out first

The installers make setup easy, but a paid product needs these first:

1. **A licence for your code.** The GitHub repo is public, so anyone can copy it today. Make the repo private,
   and choose a licence or terms of sale for customers.
2. **Each customer uses their own accounts.** Each creator gets their own Ollama, Sarvam and Google keys and
   their own YouTube login. Never ship your keys inside the product.
3. **Google / YouTube.** Each customer needs their own Google Cloud OAuth client. Uploads stay *private* until
   their project passes YouTube's API audit. A shared client for all customers needs Google verification,
   and its upload quota would be shared by everyone.
4. **Voice and AI terms.** Check that Sarvam's and Ollama's terms allow resale and commercial use by your
   customers. Piper Hindi voices are non-commercial; Kokoro is Apache-2.0.
5. **FFmpeg.** Customers install it themselves through the installers. Shipping FFmpeg inside your package
   brings LGPL/GPL obligations.
6. **Finance content.** The NIFTY workflow publishes market commentary. In India that can need SEBI registration
   and disclaimers, so sell it with that warning, or without the NIFTY workflow.
7. **Support.** Customers need a way to update (`git pull` + the installer) and a contact for help.
   A signed Mac app or Windows installer (.exe) is a later step.
