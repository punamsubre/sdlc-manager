# SDLC Manager

**Your AI-powered development lifecycle assistant** — validate code against live BigQuery, catch secrets before they leak, get senior-level AI code reviews, and ship changes through a fully governed Git workflow. All in plain English.

![Version](https://img.shields.io/badge/Version-0.1.18-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-informational)
![Powered by](https://img.shields.io/badge/AI-Gemini%20on%20Vertex%20AI-4285F4)

---

## What is SDLC Manager?

SDLC Manager is a multi-agent AI platform that acts as a quality gate, code reviewer, and Git assistant for your team. One orchestrator coordinates four specialists:

| Agent | What it does for you |
|---|---|
| 🔀 **Git & GitHub** | Branching, commits, pushes, and pull requests — always through your team's governed workflow |
| 🔍 **Validation** | SQL syntax checks, quality rules, secret detection, and **live BigQuery schema verification** |
| 📝 **AI Review** | Deep architectural reviews and a quality scorecard (performance, cost, security, compliance) |
| 🏗️ **Terraform** | Infrastructure validation, formatting, and plan previews |

You interact with the same intelligence through **three surfaces** — pick whichever fits your work:

| Surface | Best for | How you use it |
|---|---|---|
| 🌐 **Web Dashboard** | Visual validation, quality gates, PR studio, side-by-side diffs | Open the URL in your browser |
| 🔌 **Local Helper** | Connecting the dashboard to the repository on *your* machine | Runs quietly in the background |
| 💻 **CLI** | Everything, conversationally, from your terminal | `sdlc-manager` |

---

## Getting Started (one command)

**Prerequisites:** [uv](https://docs.astral.sh/uv/) (`pip install uv`) and the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), signed in with your work account.

Open PowerShell **inside your repository folder** and run:

```powershell
powershell -c "irm https://sdlc-manager-36231825761.us-central1.run.app/setup.ps1 | iex"
```

This single command:
1. Verifies your prerequisites and Google Cloud sign-in
2. Installs the SDLC Manager CLI and helper (authenticated with **your own** Google credentials — no shared tokens)
3. Starts the helper for your repository and sets it to start automatically with Windows
4. Prints your **pairing token** for the dashboard

**Updating** works the same way — just re-run the command.

---

## 🌐 Using the Web Dashboard

Open **https://sdlc-manager-36231825761.us-central1.run.app** and sign in with your Google work account.

**Connect your repository (one time):** click the **helper pill** (top bar) → **Connect** → paste the pairing token printed during setup. The dashboard now works directly against the repository on your machine.

What you can do:
- **Dashboard** — repository health at a glance: changed files, validation status, quality gate readiness, recent commits
- **Validate** — run syntax, quality, secret, and BigQuery schema checks on changed files; accept AI-suggested fixes with one click
- **Quality Gate** — see whether your changes pass the repository's own governance rules before you commit
- **Git & PR Studio** — compare branches, review file diffs side by side, cherry-pick change blocks, generate PR descriptions, and open pull requests
- **AI Review** — architecture-level review across your changes
- **Agent Chat** — the same conversational assistant as the CLI, in the browser

---

## 💻 Using the CLI

Start it from your repository folder:

```powershell
cd path\to\your\repository
$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project"
sdlc-manager
```

Then just type what you want, in plain English:

```
Do I have any uncommitted changes right now?
What has changed in my working copy compared to main? Summarize it
Validate Market_Insights.sql
Scan this repository for hardcoded secrets
Review Market_Insights.sql and give me a quality scorecard
Push my changes to main
Summarize everything we did in this session
```

**Approvals:** every state-changing operation (commit, push, PR, …) stops and shows you the exact operation and arguments. Reply `yes` to proceed or `no` to cancel — nothing is written without your consent.

**Handy commands inside the CLI:**

| Type | Effect |
|---|---|
| `exit` (or `quit` / `q`) | Leave the CLI |
| `clear` | Clear the screen |

**Tip:** start with `sdlc-manager -v` to see every tool call the agents make — useful for understanding what's happening under the hood.

---

## 🔌 The Local Helper

The helper is a small background service that lets the **hosted dashboard** securely work with the repository on **your machine**. It listens on `127.0.0.1` only — nothing is exposed to the network — and every request must carry your pairing token.

| Command | What it does |
|---|---|
| `sdlc-helper` | Start the helper for the current folder |
| `sdlc-helper --workspace "C:\path\to\repo"` | Start it for a specific repository |
| `sdlc-helper --show-token` | Print your pairing token again |
| `sdlc-helper --reset-token` | Generate a new pairing token |
| `sdlc-helper --install-autostart` | Start automatically when Windows starts |
| `sdlc-helper --remove-autostart` | Remove the autostart entry |
| `sdlc-helper --stop` | **Stop the running helper** |

---

## ⏹ Stopping & Restarting

| To stop… | Do this |
|---|---|
| The CLI | Type `exit` |
| The helper | `sdlc-helper --stop` |
| Helper autostart | `sdlc-helper --remove-autostart` |
| To switch the helper to another repository | `sdlc-helper --stop`, then start it in the new folder |

> **Before updating** (re-running the setup command): type `exit` in any open CLI session and run `sdlc-helper --stop` first — a running instance keeps its program files locked.

---

## 🛡️ Governance & Safety (what the platform guarantees)

- **Human in the loop** — every write operation requires your explicit approval, bound to the exact operation you saw. "No" means no.
- **Your repository's rules, enforced** — if your repo uses lefthook quality gates, the platform runs *the repo's own checks* on every commit and push, reports each verdict, and never claims a failed gate passed. If the hooks aren't installed yet, it refuses to commit unchecked and offers to wire them for you.
- **Pull-request-first** — "push to main" always means: feature branch → checks → push → pull request. No direct writes to main unless you explicitly insist.
- **Secrets are a hard stop** — hardcoded credentials block validation and commits outright, and their values are never displayed or logged.
- **Verified, not guessed** — SQL is checked against **live BigQuery metadata**: wrong column names are caught with the closest real match suggested; a table that doesn't exist yet is a note, never a false failure.
- **Your identity everywhere** — Git, GitHub, BigQuery, and the package install all use your own credentials. The platform holds no shared secrets.

---

## ❓ Troubleshooting

| Symptom | Fix |
|---|---|
| Setup says *"Your Google Cloud sign-in has expired"* | Run `gcloud auth login`, then re-run the setup command |
| *"Failed to install entrypoint … used by another process"* during update | Type `exit` in any open CLI and run `sdlc-helper --stop`, then re-run setup |
| *"A helper is already running on this machine"* | That's fine — it's the guard against duplicates. To restart: `sdlc-helper --stop`, then start again |
| Validation says a file is clean but you see an issue in your editor | Save the file first (Ctrl+S) — validation reads what's on disk |
| Dashboard shows the helper as disconnected | Check the helper is running (`sdlc-helper`), then reconnect with your pairing token |
| CLI says the environment is misconfigured | Set your project first: `$env:GOOGLE_CLOUD_PROJECT = "your-gcp-project"` |

---

*SDLC Manager — AI quality, Git & infrastructure orchestration for your development lifecycle.*
