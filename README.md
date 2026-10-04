# Quote Compare

Quote Compare runs on your own computer and opens in your web browser. Setup is only needed once.
Follow the steps for your computer, Mac or Windows. Each grey box is a command: copy it, paste it
into the window named in that step, and press Return (Enter on Windows).

Have the API key you were given ready. The app asks for it the first time it starts.


## Mac

**1. Open Terminal.** Press Command and Space together, type `Terminal`, and press Return.

**2. Install Homebrew.** It sets up the tools the app needs. Paste this and follow what it asks. It
will ask for your Mac password; nothing shows while you type it, which is normal.

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

When it finishes, it shows a few commands under "Next steps". Copy those, paste them, and press Return.

**3. Download and start the app:**

```
cd ~ && git clone https://github.com/agothebi/quote-compare-tool.git && cd quote-compare-tool && ./start.command
```

The first start takes a few minutes while the app installs what it needs. When it asks for your API
key, paste it and press Return. The app opens in your browser.

**From now on:** open the `quote-compare-tool` folder in your home folder and double-click
`start.command`. Keep the window that opens while you use the app, and close it when you are done.


## Windows

**1. Open PowerShell.** Click Start, type `PowerShell`, and press Enter.

**2. Install the tools the app needs.** Paste this and allow anything Windows asks about:

```
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements; winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements; winget install -e --id UB-Mannheim.TesseractOCR --accept-package-agreements --accept-source-agreements
```

**3. Close PowerShell and open it again**, so it finds what you just installed.

**4. Download and start the app:**

```
cd $HOME; git clone https://github.com/agothebi/quote-compare-tool.git; cd quote-compare-tool; .\start.bat
```

The first start takes a few minutes while the app installs what it needs. When it asks for your API
key, paste it and press Enter. The app opens in your browser.

**From now on:** open the `quote-compare-tool` folder in your user folder and double-click `start.bat`.
Keep the window that opens while you use the app, and close it when you are done.


## Getting an update

When you are told there is a new version, close the app and paste this into Terminal (Mac):

```
cd ~/quote-compare-tool && git pull --autostash
```

or this into PowerShell (Windows):

```
cd $HOME\quote-compare-tool; git pull --autostash
```

Then start the app as usual. It installs anything new by itself. Any settings you changed in
`config/settings.yaml` are kept.


## Changing the AI model

The app uses the model named on the `model:` line of `config/settings.yaml`. To use a different one
on this computer, set it in your `.env` file instead. Updates never change that file.

1. Close the app.
2. Open `.env`: paste `open -e ~/quote-compare-tool/.env` into Terminal (Mac) or
   `notepad $HOME\quote-compare-tool\.env` into PowerShell (Windows).
3. Add a line with the model, for example `QUOTE_COMPARE_MODEL=claude-sonnet-5`.
4. Make sure the API key for that model's provider is in the file too: `ANTHROPIC_API_KEY=` for
   Claude models, `GEMINI_API_KEY=` for Gemini models.
5. Save and start the app again.

The models you can use are listed under `pricing:` in `config/settings.yaml`. A model that isn't
listed needs its price per million tokens added there first, because the spending limit
(`spend_limit_usd_per_month`, in the same file) is worked out from it. If the name is wrong, the
app's window says so when it starts. Quotes already read keep their results; files added after the
change use the new model. To go back, delete the `QUOTE_COMPARE_MODEL` line.


## Changing the email text

The Proposal page's email text comes from `email.txt` in the `quote-compare-tool` folder. The file
appears after the first start. Edit it in any text editor and save; the next Proposal page you open
uses the new text. Updates never change this file.

The words in curly brackets are filled in by the app. Keep them spelled exactly as they are; you
can move them, or delete the ones you don't want:

- `{client}`: the client's name
- `{recommendations}`: each recommended policy, with its carrier, price and reasons
- `{total}`: the yearly total, when Household total is ticked in the proposal options
- `{notes}`: your note on the Proposal page
- `{signature}`: the agency's name and phone number

A line holding only a bracket word with nothing to fill in is left out. If the file is deleted or
left empty, the app uses the default text. To start over, copy this back into `email.txt`:

```
Hi,

I compared the quotes we received for you. Here's what I recommend:

{recommendations}

{total}

{notes}

The full side-by-side comparison is attached.

{signature}
```


## Backups

While the app is open, it backs up your clients by itself: when it starts, and every hour that
something changed. The backups go to the `Quote Compare Backups` folder in your Documents folder,
outside the app's folder, so they're safe even if the app's folder is deleted. Each backup only
adds what changed, so the folder stays small. It keeps every backup from today, one a day for two
weeks, and one a month for a year.

**If your clients disappear** (the folder was deleted, or you downloaded the app again): start the
app. The board says there's a backup and offers **Restore it**. Click it and everything comes back.

**To go back to an earlier backup:** click **Backups** at the bottom of the board, pick one and
click **Restore**. Your clients as they are now get backed up first, so you can undo it.

To keep the backups somewhere else, for example a OneDrive or Dropbox folder, add a line like
`QUOTE_COMPARE_BACKUP_DIR=C:\Users\you\OneDrive\Quote Compare Backups` to `.env` and start the app again.
Don't edit or rename the files inside the backup folder.


## If something goes wrong

- **The page does not open:** wait a few seconds and reload it, or go to http://127.0.0.1:8000.
- **Quotes are not being read:** the window the app runs in says why. If the API key is missing or
  wrong, close the app and paste `open -e ~/quote-compare-tool/.env` into Terminal (Mac) or
  `notepad $HOME\quote-compare-tool\.env` into PowerShell (Windows). Put the right key after
  `GEMINI_API_KEY=` (or `ANTHROPIC_API_KEY=` for a Claude model), save, and start the app again.
- **Anything else:** send a photo or a copy of the text in the app's window to whoever set this up
  for you.

Your clients' quotes and comparisons stay on this computer, in the `data` folder inside
`quote-compare-tool`. They are never uploaded to GitHub.


## Technical notes (for an agent)

- Needs Python 3.13, Git and Tesseract. Windows: the `winget` line in step 2 (then open a new
  PowerShell so it finds them; Windows may ask the user to approve each install). Mac: Homebrew,
  whose installer needs the user's Mac password, so hand that step to the user; the start script
  installs the rest.
- `start.bat` / `start.command` create `.venv`, install `requirements.txt` into it, then run the
  app with `python -m app.launch`. It is a local web server that keeps running until its window is
  closed, so when scripting the setup, run it in the background. `--no-browser` skips opening the browser.
- The API key goes in `.env` (copy `.env.example`; the key line is `GEMINI_API_KEY=`). The app only
  asks for it when started in a real terminal window. Ask the user for the key; never print it,
  commit `.env`, or put the key anywhere else.
- Updates: `git pull --autostash` (keeps local edits to `config/settings.yaml`). Never commit or
  push from this folder; `data/`, `.env` and `email.txt` are the user's and stay untracked.
- It works when http://127.0.0.1:8000/api/version answers (or the next free port if 8000
  is taken; the window prints the address). The window also prints a warning if the key or
  Tesseract is missing.
- On Windows, Tesseract is found in `C:\Program Files\Tesseract-OCR` without being on PATH.
