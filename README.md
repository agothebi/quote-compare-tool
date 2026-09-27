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
brew install --cask git-credential-manager && cd ~ && git clone https://github.com/agothebi/quote-compare-tool.git && cd quote-compare-tool && ./start.command
```

If a window asks you to sign in to GitHub, sign in. The first start then takes a few minutes while
the app installs what it needs. When it asks for your API key, paste it and press Return. The app
opens in your browser.

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

If a window asks you to sign in to GitHub, sign in. The first start then takes a few minutes while
the app installs what it needs. When it asks for your API key, paste it and press Enter. The app
opens in your browser.

**From now on:** open the `quote-compare-tool` folder in your user folder and double-click `start.bat`.
Keep the window that opens while you use the app, and close it when you are done.


## Getting an update

When you are told there is a new version, close the app and paste this into Terminal (Mac):

```
cd ~/quote-compare-tool && git pull
```

or this into PowerShell (Windows):

```
cd $HOME\quote-compare-tool; git pull
```

Then start the app as usual. It installs anything new by itself.


## If something goes wrong

- **The page does not open:** wait a few seconds and reload it, or go to http://127.0.0.1:8000.
- **Quotes are not being read:** the window the app runs in says why. If the API key is missing or
  wrong, close the app and paste `open -e ~/quote-compare-tool/.env` into Terminal (Mac) or
  `notepad $HOME\quote-compare-tool\.env` into PowerShell (Windows). Put the right key after
  `GEMINI_API_KEY=`, save, and start the app again.
- **Anything else:** send a photo or a copy of the text in the app's window to whoever set this up
  for you.

Your clients' quotes and comparisons stay on this computer, in the `data` folder inside
`quote-compare-tool`. They are never uploaded to GitHub.
