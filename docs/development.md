# Development

```sh
uv run pytest
```

The promo video is rebuilt as described in [assets/promo/README.md](../assets/promo/README.md).

## Where the code is

All under `src/limitful/`.

- `cli.py` and `__main__.py`: the commands, and the entry point the login item uses.
- `protocol.py`: the wire format (protobuf-style messages over one BLE service).
- `client.py`: connect, set the clock, read storage status, download pages.
- `usb.py`: readings over the pendant's USB shell, and the one `ble unpair` command.
- `store.py`: the SQLite store: raw pages, recordings, lines, conversations,
  speakers, reminders, day recaps, pauses and chats.
- `ogg.py`: a small Ogg Opus writer.
- `transcribe.py`: speech-to-text engines (local Whisper by default).
- `speech_model.py`: whether the Whisper model is downloaded yet, and how far along.
- `hosted.py`: hosted transcription with speaker labels (Deepgram).
- `scoring.py`: scoring a service's speaker labels against Limitless's.
- `speakers.py`: voice fingerprints for speaker labels.
- `digest.py`: titles, summaries, notes and reminders from a language model.
- `ask.py`: question answering over the transcripts, with citations.
- `forget.py`: deleting a stretch for good: lines, notes, reminders and the audio.
- `mcp_server.py`: the same lookups, and a few more, offered to other apps over the Model
  Context Protocol.
- `search.py`: search by meaning: the local model and its index.
- `upkeep.py`: long jobs the Lifelog can start and stop (re-timing, indexing).
- `config.py`: saved settings, including the API keys.
- `importer.py`: import of a Limitless data export.
- `demo.py`: the made-up data folder. `demo_filler.py` writes its long stretches, the talks and the visitors at the stand.
- `watch.py`: the background loop.
- `autostart.py`: the login item: a macOS LaunchAgent, or a Windows Task Scheduler task and Start menu entry.
- `app.py`: `limitful app`, the Windows desktop app: app window, tray icon, one copy at a time.
- `updater.py`: the app updating itself from the git clone it runs from.
- `pendant_lock.py`: one process syncing the pendant at a time.
- `web.py` and `web/index.html`: the Lifelog web app.

## The website

[limitful.ai](https://limitful.ai) is the single page in `site/`, published
to GitHub Pages by `.github/workflows/site.yml` on every push to main that
touches it. The video, poster and logos are borrowed from `assets/` when the
site is assembled, so nothing is kept twice. To look at it locally:

```sh
sh site/build.sh && open _site/index.html
```

The screenshots in `site/img/` are crops of the app showing `limitful demo`
data; take them again when the app's look changes.
