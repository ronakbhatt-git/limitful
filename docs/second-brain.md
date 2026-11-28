# Second brain on Windows: setup

Limitful syncs the pendant, transcribes on this PC with Whisper, and every 30 minutes exports
what is ready to two places:

- **Transcripts** go to a private GitHub repo (`lifelog`) as one Markdown and one JSONL file
  per day, plus `index.md`, `reminders.md`, `people.md` and `AGENTS.md` for other AI tools.
- **Audio** goes to `G:\My Drive\Lifelog Audio`, encrypted with age before it leaves this PC.
  Google Drive for desktop uploads it. This PC holds only the public key, which can lock files
  but not open them.

Every transcript goes to GitHub, 24 hours after its day ends. A scheduled task in Claude desktop
does the triage: it titles and tags each conversation, marks sensitive ones, and picks a tier
(keep, text or hold). The tier decides only the audio: keep sends the recording to Drive,
encrypted; text and hold keep it on this PC. Titles, tags and audio are added as Claude
reaches each conversation.

## 1. Install the tools

Install these once, if they are not already there:

- [Git for Windows](https://git-scm.com/download/win). Its credential manager handles the GitHub sign-in.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), from PowerShell:
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- [Google Drive for desktop](https://www.google.com/drive/download/), signed in to your Google
  account. By default it shows My Drive as `G:\My Drive`.
- Claude desktop, signed in.

## 2. Get Limitful and the lifelog repo

Keep both folders out of OneDrive and Google Drive: a sync tool fighting git over the same
files corrupts repos. If your Documents folder is synced by OneDrive, use `C:\Users\<you>\code`.

```powershell
mkdir $HOME\code; cd $HOME\code
git clone https://github.com/ronakbhatt-git/limitful
git clone https://github.com/ronakbhatt-git/lifelog
cd limitful
uv sync
```

The first `git clone` of the private `lifelog` repo opens a browser to sign in to GitHub. That
sign-in is saved, so later pushes from the background work without asking.

In Google Drive (on the web or in `G:\My Drive`), create a folder named `Lifelog Audio`.

## 3. Make the encryption key

```powershell
uv run limitful brain keygen
```

It prints a public key (`age1...`) and a private key (`AGE-SECRET-KEY-...`). Save the private
key in your password manager now, then close the window. Do not save it on this PC. Without
it the audio cannot be opened again, and with it anyone who gets your Drive can.

## 4. Point the export at the repo and Drive

```powershell
uv run limitful brain setup --repo $HOME\code\lifelog --audio "G:\My Drive\Lifelog Audio" --recipient age1...
```

Option: `--delay-hours 24`, how long after a day ends before it goes out.

## 5. Pair the pendant and bring in your Limitless data

1. Disconnect the pendant from your phone: quit the Limitless app or turn off the phone's Bluetooth.
2. Run `uv run limitful serve`, open http://localhost:4747, then plug the pendant in by USB and
   use **Settings, Pendant, Pair without erasing**. The other way, `uv run limitful pair`,
   needs pairing mode, which erases audio still stored on the pendant. Neither has been tried
   on Windows with a real pendant yet, so this is the step most likely to need a fix.
3. Import your Limitless export: `uv run limitful import C:\path\to\export.zip`.
4. Stop the server with Ctrl-C.

## 6. Start at login

```powershell
uv run limitful autostart on
```

This adds a task named `Limitful\...` to Windows Task Scheduler and a Limitful entry to the
Start menu. At login it opens the Lifelog in its own window with an icon in the system tray,
syncs the pendant every 2 minutes, transcribes, and runs the export at most every 30 minutes.
Closing the window leaves it running in the tray. It updates itself from GitHub, so you do not
need `git pull` again. Its log is `C:\Users\<you>\Limitful\limitful.log`.

## 7. Connect Claude desktop and schedule the triage

```powershell
uv run limitful mcp --config --triage
```

Copy what it prints into Claude desktop under **Settings, Developer, Edit Config**
(`claude_desktop_config.json`), merging it into `mcpServers` if the file already has some.
Restart Claude desktop. The `limitful` tools should appear.

Then create a scheduled task in Claude desktop that runs every 30 minutes, with this prompt:

> Triage my lifelog. Call limitful_pending_conversations with limit 10, read the guide it
> returns, and save each conversation with limitful_save_conversation. Repeat until nothing is
> waiting or you have saved 40. Reply with one line: how many you saved and how many are left.

The task runs only while the PC is awake and Claude desktop is open. Until a conversation is
triaged, its transcript goes out without a title or audio. An import of several months can leave hundreds of
conversations waiting; they clear at about 40 per run.

## 8. Check it

```powershell
uv run limitful brain status
uv run limitful brain export
```

`status` shows how many days are ready and how many wait for triage. Run `export` once by hand
so the first push happens where you can see it. After that the background task takes over.

## Calendars

Limitful can read your calendars, read-only, so each day page lists that day's events and draws
them on its timeline, each conversation says which meeting it was in, and triage sees who was
invited. It reads them every 6 hours while the app runs.

Open **Settings, Calendars** in the Limitful window. For a Google calendar: open Google Calendar
on the web, **Settings**, click the calendar under **Settings for my calendars**, and copy **Secret
address in iCal format**. Paste it under **Add a calendar** with a name, such as Personal, and
press **Add calendar**. It is read at once, so a wrong address shows straight away. Repeat for each
calendar. For Outlook or Office 365, publish the calendar (Outlook on the web, **Settings, Calendar,
Shared calendars, Publish a calendar**) and add its ICS link the same way; some organizations turn
publishing off.

The secret address works like a password for that calendar: it is kept only on this PC and never
shown again. If it leaks, reset it in Google Calendar's settings, remove the calendar in Limitful,
and add the new address. Event descriptions are not kept, since they often hold meeting passcodes.
(`limitful calendar add|list|read|remove` does the same from a terminal.)

## Notes in Obsidian

Open the lifelog folder (`C:\Users\<you>\code\lifelog`) as a vault in
[Obsidian](https://obsidian.md), or click **Notes** in the Limitful window once it is there.
You read the day pages and write your own notes in one place.

- Write only in `notes/`. Everything else is rebuilt from Limitful every 30 minutes, so an edit
  to a day page is overwritten. Rename speakers and conversations in Limitful instead.
- Each day page opens with a timeline chart of its conversations (red: sensitive, highlighted:
  audio kept). Obsidian and GitHub draw it with no plugin; in Obsidian, clicking a bar opens it in Limitful.
- Each conversation on a day page has a **Listen in Limitful** link. It opens that conversation
  in Limitful on this PC; press Play (or Space) if it does not start by itself.
- The export commits and pushes your notes along with the transcripts. It also brings in notes
  added elsewhere (on GitHub, or by Claude on the web) before it pushes. If the same lines were
  changed in both places, the export stops and says so; run `git pull --rebase` in the folder.
- In Obsidian's settings, under **Files and links**, turn off **Use [[Wikilinks]]** so links stay
  plain Markdown that other tools read.

## Getting audio back

Copy the encrypted folder from Drive, or point at it directly, and decrypt it:

```powershell
$env:LIMITFUL_AGE_KEY = "AGE-SECRET-KEY-..."   # or leave it unset and paste it when asked
uv run limitful brain restore --audio "G:\My Drive\Lifelog Audio" --to $HOME\Desktop\audio
```

Each line in the repo's `data/*.jsonl` files names its audio file and the offset in seconds.

## Forgetting something

Use Forget in the Lifelog. The next export removes the transcript from the repo and deletes the
audio file from Drive (Drive keeps it in its trash for 30 days). Git history still has earlier
commits; scrub those with `git filter-repo` if you need it gone from history too.
