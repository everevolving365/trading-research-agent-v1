# Installing the EverEvolving Trading Agent

It installs like any other program: you end up with an icon on your Desktop,
and double-clicking it opens the agent in its own window.

## Windows

1. On the GitHub page, click the green **Code** button, then **Download ZIP**.
2. Open the ZIP and drag the folder inside it to your Desktop (or anywhere).
3. Open that folder and double-click **Install.bat**.
4. Wait a minute or two. When it says **Done**, the app opens.

From then on, double-click **EverEvolving Trading Agent** on your Desktop.

If Windows asks whether to run the file, choose **More info**, then **Run anyway**.
The installer only writes to your own user folder, so it never asks for
administrator rights.

## Mac

1. Download the ZIP as above and open the folder.
2. Double-click **Install.command**. (If macOS blocks it, right-click it, choose
   **Open**, then **Open** again.)
3. When it says **Done**, the app opens. It is in your **Applications** folder
   and on your Desktop.

If you don't have Python 3.11 or newer, the installer tells you where to get it.

## Linux

```bash
bash install.sh
```

## Asking Claude to do it

Send Claude the link to this repository and say "install this on my
computer". Claude needs to be able to run commands on your computer, which
works in the Claude desktop app or Claude Code. In a normal browser chat
Claude can't install anything for you, so follow the steps above instead.
`CLAUDE.md` tells Claude exactly what to run.

## What it will ask you for

Nothing, to start. It runs a full backtest with no keys at all. When you want
more (live data, a chat model, your TradingView account), the app's **Keys**
page explains each key, what it unlocks and where to get it. Keys are stored in
your computer's keychain, never in a file and never sent anywhere else.

## Removing it

Windows: run `install\windows\uninstall.ps1`. Your saved research stays unless
you add `-RemoveData`.
