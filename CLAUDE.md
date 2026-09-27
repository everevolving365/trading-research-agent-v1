# Notes for Claude

## If someone asks you to download or install this

They want the desktop app: an icon on their Desktop that opens the agent in its
own window. Do exactly this, then tell them to double-click the icon.

**Windows**

```powershell
git clone https://github.com/everevolving365/trading-research-agent-v1.git
cd trading-research-agent-v1
powershell -NoProfile -ExecutionPolicy Bypass -File install\windows\install.ps1
```

No git? This downloads and installs in one step:

```powershell
irm https://raw.githubusercontent.com/everevolving365/trading-research-agent-v1/main/install/windows/bootstrap.ps1 | iex
```

**macOS and Linux**

```bash
git clone https://github.com/everevolving365/trading-research-agent-v1.git
cd trading-research-agent-v1
bash install.sh
```

The installer finds or installs Python 3.11+, builds a private environment in
the user's data folder, installs the agent, creates the Desktop and Start menu
(or Applications) entry named **EverEvolving Trading Agent**, and opens the app.
Add `-NoLaunch` (Windows) or `--no-launch` (macOS/Linux) to skip opening it.

Check it worked: the shortcut exists on the Desktop, and the installer printed
"Done." If Python could not be installed automatically (macOS/Linux never
installs it silently), the installer prints the exact command to run.

**Never** ask for, read out, or type the person's passwords or API keys. The
app has a Keys page where they enter their own; keys go to the operating
system's keychain.

To remove it: `install\windows\uninstall.ps1` (keeps their data unless
`-RemoveData`).

## If you are working on the code

- `make verify` runs every acceptance check; `make test` runs the suite. Both
  need no credentials and no network.
- Resume from `BUILD-LOG.md`. Record autonomous decisions in `DECISIONS.md` and
  anything that needs the owner in `BLOCKERS.md`.
- Everything the app writes goes to `EE_HOME` (default: the per-user data
  folder), never into the repository.
- Hard rules that tests enforce: no conversational, UI or Operator surface may
  place an order; the app binds to localhost only with a session token; the
  agent never sets a client's risk; every primitive exists in all four targets
  (Python, Pine indicator, Pine strategy, live) and parity must agree.
