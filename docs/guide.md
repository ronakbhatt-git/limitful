# Using Limitful

Everything the app and the commands do. The short version is in the [README](../README.md).

## Where things are kept

Transcripts are written to `transcripts/<date>.md`, one timestamped line
per utterance, with each summarised conversation's title and summary above
its lines. Transcription uses Whisper (`large-v3-turbo`) locally; the model
is about 1.6 GB and downloads on first use. No audio leaves your computer,
unless you choose a hosted service (see [Experiments](experiments.md#speaker-labels-from-a-hosted-service-opt-in-measured-not-better)).

Recordings, transcripts and the database are stored in `~/Limitful` (change
it with `--data-dir` or the `LIMITFUL_DATA` environment variable). Settings
are kept apart, in `~/.config/limitful/config.json`, and downloaded models in
`~/.cache/limitful` and the Hugging Face cache. Audio lands in
`audio/<date>/<time>_<page>.ogg` (Ogg Opus, plays in VLC, ffmpeg and most browsers).
The raw pages from the pendant are kept in `pendant.db`, so audio can be
re-exported later with `uv run limitful export`.

On macOS the first run asks for Bluetooth permission for your terminal app.

## Reading and listening

```sh
uv run limitful serve
```

Serves the Lifelog at http://localhost:4747 (another port with `--port` or
the `PORT` environment variable); open that address in a browser. The page
only answers on your own machine.

**A day.** Each day is a list of its conversations, with time, title and a bar
for how long each ran, under headings for night, morning, afternoon and evening.
Stray words with no conversation around them fold into one quiet row. On a
wide screen the day's recap, its open reminders and the named people heard
that day sit beside the list.

**The strip.** Above the list, a strip shows when speech happened, with a pin
where each open reminder came up. Pointing at a conversation lights up its
stretch of the strip, and pointing at the strip names the conversation there.
Click the strip to jump to that moment; click a pin to go to the line.

**A conversation.** Open one and it shows its **Notes**: what it came to, and
an outline of what it covered, each topic opening the transcript where it
begins. Switch to **Transcript** to read it all, under its section headings,
each speaker's turn set as a paragraph in that speaker's colour; the side you
choose is where the next conversation opens. A conversation with no notes
opens on its transcript. Notes proper are a few sentences on what a
conversation came to, the most important first, each with the moment it was
said, and what is still to be done is suggested as reminders. For now they
are written a day at a time with `limitful notes 2026-11-09`, which sends
those conversations to the summary provider you chose. While you read the transcript its summary takes the
recap's place beside it, above the day's reminders, which are kept, dismissed
or ticked off right there. Click a
speaker's name to say who it is; people you have already named are suggested
as you type. The pencil beside the title renames the conversation. Click any sentence to hear it; playback
carries on through the conversation, with a timeline to jump around in. The
title stays in view as you scroll, and the whole thing can be copied as
Markdown.

**Getting around.** The arrows either side of the date in the header step
through the days with recordings. The date itself opens a calendar, each day shaded
by how much was said, with its recap shown as you point at it. The search box
opens quick find (below); its "Search everything" row lists every hit across
all days, each with the words marked, the line before and
after, and the conversation it is from.

**Deleting.** "Delete…" on an open conversation, or on a short moment, asks
once and then removes it for good: the lines, the title, notes and reminders,
its entries in search, and the audio. A recording that runs past the
conversation has that part replaced with silence, so the rest keeps its
timing. What is deleted stays deleted: syncing the pendant again does not
bring it back. There is no undo, and it is refused while something else is
running. To delete several at once, click **Select** above the day's
conversations, tick the ones to go (the box on a row of short moments ticks
all of them), then **Delete…**. It asks once for the lot. Choosing is for one
day at a time and ends when you leave the day or click **Done**.

**People.** The People button lists everyone you have named, most recently
heard first, with how many conversations and days each was heard in and how
long they spoke. Open a person for those conversations, or to search what
they said. Only lines that carry the name are counted, and a name given in
one recording does not yet carry to that voice elsewhere, so the figures are
floors.

**Quick find.** Click the search box, or press ⌘K (Ctrl+K on Linux and Windows) or `/` anywhere, for one box
that goes places. Type a date ("yesterday", "12 nov", "thursday", "march
2025") to open that day, the start of "reminders", "ask" or "settings" to go
there, or any words to see the first lines that contain them, with lines
close in meaning underneath if that index is built. Arrow keys move, Enter
opens, and the last row hands what you typed to Ask as a question, unsent.

**What one person said.** Start a search with `from:` and the name of someone
you have named, as in `from:elena pilot` or `from:"Elena Ruiz" pilot`, to see
only their lines; `from:elena` alone lists everything they said. Typing a
name in quick find offers the same. The "Said by" chip above the results
takes the filter off. It finds only lines tagged with that name, and a name
given in one recording does not yet carry to that voice elsewhere.

**Appearance.** Light or dark follows your computer, or choose one under
Settings, Appearance. The layout adapts to a phone-sized window.

Recordings are Ogg Opus, which Chrome and Firefox play; older versions of
Safari do not.

## Summaries and reminders

Recordings are grouped into conversations (five minutes of silence ends one).
A language model can then give each a title and a short summary, and suggest
reminders for things someone said they would do. This is off until you choose
what writes them, under Settings, Summaries:

- **Claude**: paste an Anthropic API key, or leave the field empty to use the
  `ANTHROPIC_API_KEY` environment variable or an `ant auth login` sign-in.
  The text of each conversation is sent to Anthropic; audio is not.
- **Ollama**: a model running on your own machine (`llama3.1` by default), so
  nothing leaves it. Run `ollama pull llama3.1` first; Settings checks that
  Ollama is running and has the model before saving the choice. Tried on the
  made-up demo conversations, it took about seven seconds each and wrote
  usable titles and summaries, but suggested three times as many reminders as
  Claude and got some details wrong.

New conversations are summarised after each sync. `uv run limitful digest`
does it by hand, and `--redo` writes them again for conversations recorded
from the pendant. Imported conversations keep Limitless's titles and are not
summarised.

Each day also gets a recap at the top of its page: one sentence on what the
day was about, then its main threads. It is written from the titles and
summaries of that day's conversations, not from the transcripts, once the day
is over and it has at least two summarised conversations. For today, or for
days imported from Limitless, click "Write a recap of this day"; imported days
are not sent anywhere until you do. `uv run limitful digest --days` writes a
recap for every finished day that lacks one, imported history included.

The Reminders page shows suggestions grouped by day, each with a link that
plays the moment it came up. Keep one and it moves to your to-do list; tick it
off when it is done.

## Ask

| Ask, with links to the lines behind the answer | Reminders, found in what was said |
|---|---|
| <img src="../assets/screenshots/ask.png" alt="A question answered with citations"> | <img src="../assets/screenshots/reminders.png" alt="Suggested reminders beside a to-do list"> |

The conversations in these pictures are made up.

The Ask page answers questions such as "what did the dentist say about the
crown?" by searching and reading your transcripts. Answers cite the lines they
rest on, and each citation opens that line. Chats are saved. From the command
line: `uv run limitful ask "..."`.

Ask needs Claude chosen under Settings, Summaries. Your questions, the
earlier messages of that chat and whatever Claude reads to answer (search
hits, conversation titles, whole conversations) are sent to Anthropic.

## Bringing your Limitless history

Request a data export from Limitless before December 1, 2026, then import the
zip from Settings, Import, or with:

```sh
uv run limitful import data-export.zip
```

Transcripts, section headings, audio and reminders are imported, and
importing the same export twice adds nothing. Speakers keep Limitless's
"Speaker 1", "Speaker 2" within each conversation. Chats in the export are not
imported yet.

## Adding other recordings

Audio recorded somewhere else, such as a Voice Memo from your phone, can join
the Lifelog. Drop the files anywhere on the page, choose them in Settings
under Import, or run:

```sh
uv run limitful add "New Recording.m4a"
uv run limitful transcribe
```

To get a Voice Memo onto the computer, share it from the phone by AirDrop, or
drag it out of the Voice Memos app on a Mac. `.m4a`, `.mp3`, `.wav` and most
other kinds of audio are read. Each recording is kept as Ogg Opus beside the
pendant's audio and transcribed the same way, by Whisper on this computer or
by Deepgram if you chose it. Adding the same file twice adds nothing.

A recording is filed at the moment it began. That is read from a date and time
in the file's name (`20261114 093012.m4a`), then from the date written inside
the file, then from when the file was last changed. A file that has been
edited or copied by another app can carry the wrong date. Settings, Import
lists every recording you added: **Change time** moves one, with its lines and
summary, and **Remove** deletes its audio and lines from Limitful (never the
original).

### Voice Memos on a Mac

On a Mac signed in to the same Apple account as your iPhone, the Voice Memos
app keeps a copy of every memo. Limitful can read that copy, so a memo
recorded on the phone appears in the Lifelog at the next sync, with its title
and exact time. Turn it on in Settings, Import, **Bring in Voice Memos**, or
run it once with:

```sh
uv run limitful voice-memos --since 2026-11-01
uv run limitful transcribe
```

- Only memos recorded after you turn it on are brought in, unless you choose
  to bring in older ones too.
- Nothing is fetched from Apple, and the Voice Memos library is only read.
  Memos in Recently Deleted are skipped, and one you remove in Limitful does
  not come back.
- macOS guards that folder. Limitful needs **Full Disk Access**: System
  Settings, Privacy & Security, Full Disk Access, then add the program that
  Settings names (the Python that runs Limitful) and reopen Limitful. That
  lets the program read any file you can, so it is your call.
- Voice Memos must be turned on under System Settings, your name, iCloud, on
  both devices. Apple does not document this folder and has moved it before;
  if a macOS update breaks it, dropping files still works.

Lines in a Limitless transcript usually sit about a second away from where
they are heard. `uv run limitful retime` listens to the imported audio with Whisper
and moves each line to where its words are; `--minutes 60` does it in
stages and `--undo` puts everything back. It takes roughly a fifth of the
audio's length on a laptop CPU, and nearer a third when the laptop is busy.

The same job runs from Settings, under **Timing of imported lines**, which
shows how many recordings and hours are left. Progress shows at the top of the
page, the page stays usable, and nothing else (a sync, an import) can start
until it finishes. **Stop** lets the recording in hand finish, then stops;
starting again carries on with the rest.

## Search by meaning

The search box and Ask match words exactly. Search by meaning also finds lines
that say the same thing in other words: asking about a "massage for the lymph
system" finds the talk about lymphatic drainage. It is off until you set it
up, in Settings under **Search by meaning** or with:

```sh
uv run limitful index                       # build it, or add what is new
uv run limitful index --find "heat cabin"   # try it from the terminal
uv run limitful index --remove              # delete it; transcripts are untouched
```

It runs on this computer. A small model (34 MB, downloaded once and checked
against a pinned checksum) turns each line, together with the two lines either
side of it, into a list of numbers; these are kept in `search.db` next to
`pendant.db`, 26 MB for 31,750 lines. Building it for those lines took 9
minutes on one run and 23 on another, on a laptop busy with other work both
times; a search then takes about a fiftieth of a second. It can be stopped and carries
on where it left off, and once it exists new lines are added as they are
transcribed or imported. A big import takes a few minutes to add; in the app
it shows as a job at the top of the page and can be stopped.

Once the index exists:

- **Ask** gets a second way to look things up and is told when to use it.
- **A full search** lists exact matches first, exactly as before, and under
  them up to eight moments that are close in meaning.

How well it works was measured: see [Experiments](experiments.md#search-by-meaning).

## Using it from other apps (MCP)

Limitful can serve the Lifelog to an assistant over the Model Context
Protocol, so that Claude Desktop, Claude Code, Cursor or any other MCP client
can answer from what was said. Print the settings for your Lifelog:

```
uv run limitful mcp --config
```

and paste the result into the app's MCP settings (in Claude Desktop:
Settings, Developer, Edit Config). In Claude Code, the same thing is:

```
claude mcp add limitful -- /path/to/limitful/.venv/bin/python3 -m limitful mcp
```

with the path that `--config` printed. The app starts the server itself when
it needs it; the Lifelog page does not have to be open.

The assistant gets eight tools, and every one only reads:

| Tool | What it answers |
|---|---|
| `limitful_overview` | What the Lifelog holds, and the most recent days |
| `limitful_get_day` | A day's recap, conversations with their notes, and open reminders |
| `limitful_list_conversations` | The conversations in a range of dates |
| `limitful_read_conversation` | A conversation's transcript, 250 lines at a time |
| `limitful_search_lines` | Lines containing given words or names |
| `limitful_search_meaning` | Moments about a subject, once that index is built |
| `limitful_list_reminders` | Reminders, open or otherwise |
| `limitful_list_people` | The people you have named |

Nothing is changed through it: no reminder is kept or dismissed, nothing is
renamed or deleted. But whatever a tool returns goes to the app that asked and
on to the model that app uses, so connect only apps you would hand your
transcripts to. A hosted assistant sees every line it reads.

## The pendant, from the app

The Lifelog's Settings page has a Pendant section:

- **Check pendant** reads its battery level, how much audio is waiting and how
  much room is left.
- **Sync now** does what `watch` does, once: downloads new audio, frees it on
  the pendant, then transcribes and summarises it, with progress shown.
- **Light** sets the brightness of the pendant's light, from off to 100%. The
  pendant does not report its brightness, so the slider shows the last value
  set from here.

From the command line, `uv run limitful status` shows the same readings and
`--brightness 40` sets the light.

With the pendant plugged in by USB cable, the section also shows its firmware
version, battery voltage and temperature, and the light slider shows the
pendant's real brightness. These are read over the pendant's USB shell, using
three read-only commands. The only other thing Limitful ever types there is
`ble unpair`, and only when you click Forget the old computer (see
[Moving a pendant without erasing it](pendant.md#moving-a-pendant-to-another-computer-without-erasing-it)).

- **Pause** stops the app keeping audio. The pendant itself cannot be told to
  stop recording (no such command is known, and its live-stream mode keeps
  writing to storage, which was measured). So a pause is kept by the app: the
  pendant goes on recording, and anything from the paused period is deleted
  when it syncs, without being stored or transcribed. A Paused marker shows on
  every page until you resume. To stop the pendant recording at all, hold its
  button for two seconds to switch it off.
- **Pairing** has three buttons. Start pairing waits three minutes while
  you put the pendant in pairing mode. Unpair makes the pendant forget this
  computer so another can pair with it, keeping its recordings. Pair without
  erasing opens the USB route described in
  [Moving a pendant without erasing it](pendant.md#moving-a-pendant-to-another-computer-without-erasing-it).

On the command line: `uv run limitful pause`, `resume`, `pair` and `unpair`.

The Bluetooth unpair command has only been tested against a simulated
pendant. Unpairing over USB has been done on a real one.

## Running in the background

On macOS, Limitful can start when you log in and keep the pendant synced with
no terminal open. Switch it on under Settings, Pendant, or:

```sh
uv run limitful autostart on      # off, or status
```

This adds a login item (`~/Library/LaunchAgents/dev.limitful.app.plist`) that
runs the Lifelog with `--sync-every 120`: every two minutes it looks for the
pendant and, when it is in reach, downloads new audio, frees it on the pendant,
and transcribes and summarises it. A pendant that is away is not an error. Its
log is `limitful.log` in the data folder. Switched on from the app, it takes
effect at your next login, and the app you have open starts syncing by itself
straight away. The first time, macOS may ask whether Python can use Bluetooth.

On other systems, or to do the same by hand:

```sh
uv run limitful watch
```

`watch` checks for the pendant every two minutes. When it is in range it
downloads new audio, frees that audio on the pendant, transcribes it and, if
a summary writer is chosen, summarises it. Leave
it running in a terminal. Use `--keep` to leave audio on the pendant and
`--no-transcribe` to download only. `uv run limitful serve --sync-every 120` does the
same from inside the Lifelog.

## Freeing space on the pendant

By default `sync` is read-only: the pendant keeps its copy and will send the
same pages again next time (they are skipped locally).

```sh
uv run limitful sync --ack
```

`--ack` tells the pendant to delete pages once they are safely on disk. After
that your computer holds the only copy, so back up the data folder.

## Options and environment variables

`uv run limitful --help` lists the commands, and `uv run limitful <command>
--help` lists every option. The ones not mentioned above:

| Option | On | What it does |
|---|---|---|
| `--address` | `status`, `sync` | Name the pendant, from `scan`, when more than one is near |
| `--timeout` | `scan`, `status`, `sync`, `pair`, `unpair` | Wait longer for the pendant |
| `--no-export` | `sync` | Download pages without writing audio files |
| `--model`, `--language` | `transcribe`, `watch`, `retime` | Another Whisper model, or a fixed language such as `en` |
| `--redo` | `transcribe` | Transcribe the pendant's recordings again |
| `--reset`, `--unlink-imported` | `speakers` | Start speaker grouping over, or undo `--link-imported` |
| `--since`, `--until` | `speakers --link-imported` | Only listen to imported conversations from those days, to try a sample first |
| `--provider`, `--llm-model` | `digest`, `notes`, `watch`, `ask` | Use another summary writer or model for this run |
| `--redo` | `notes` | Write notes again for conversations that have them |
| `--data-dir` | `mcp` | Serve another Lifelog than the default |
| `--interval`, `--no-transcribe` | `watch` | Seconds between attempts; download only |
| `--port` | `serve`, `autostart` | Port for the Lifelog |

| Environment variable | What it sets |
|---|---|
| `LIMITFUL_DATA` | The data folder (default `~/Limitful`) |
| `LIMITFUL_CONFIG` | The settings file (default `~/.config/limitful/config.json`) |
| `LIMITFUL_CACHE` | Where the voice and search models are kept (default `~/.cache/limitful`) |
| `LIMITFUL_LLM` | `claude` or `ollama`, overriding the Summaries choice in Settings |
| `LIMITFUL_TRANSCRIBER` | `local` or `deepgram`, overriding the Transcription choice |
| `PORT` | The Lifelog's port (default 4747) |
| `ANTHROPIC_API_KEY` | Used for Claude when no key is saved in Settings |

Settings shows the saved choices, not these overrides. The login item passes
on only `LIMITFUL_CONFIG`, so at login the saved settings apply. A data folder
named `~/PendantSync`, from before the project had a name, is used if it
exists and `~/Limitful` does not.
