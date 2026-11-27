<p align="center"><img src="assets/banner.svg" width="560" alt="Limitful. Fine print: limits apply."></p>

# Limitful

An open-source replacement for the Limitless app, for people who want to keep
using their Limitless Pendant. It pulls recordings off the pendant over
Bluetooth, transcribes them on your computer, and gives you a Lifelog to read,
search, listen to and ask questions of. No Limitless account or server is
involved.

Limitless ends its service on December 1, 2026 and deletes the data it holds.
This project keeps the hardware working after that, and keeps the history you
export before then usable.

https://github.com/user-attachments/assets/4c651f8a-7ab8-4a4d-a4d8-974244b0b999

<p align="center">A 79-second tour (with captions and music; the conversations in it are made up). <a href="assets/promo/limitful-promo.mp4">Download the video</a>.</p>

## What it does

- **Syncs the pendant** over Bluetooth: on demand, or by itself from login (macOS).
- **Transcribes on your machine** with Whisper.
- **Lifelog web app**: each day as a list of conversations, with playback from
  any sentence, a calendar, and search by exact words or by meaning.
- **Summaries, recaps and reminders**, written by Claude or a local Ollama
  model. Off until you choose one.
- **Ask**: questions about what was said, answered with links to the lines
  behind the answer.
- **Imports your Limitless export**: transcripts, audio and reminders.
- **Takes other recordings**: drop any audio file on the page, or let it bring in the Voice Memos your Mac syncs from your iPhone.
- **Works with other apps**: an MCP server lets an assistant such as Claude search and read your Lifelog.
- **Pendant controls**: battery, storage, light, pause, pair and unpair.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/andreibondarev/limitful.git
cd limitful
uv sync
uv run limitful serve
```

Open http://localhost:4747. The first time, a setup guide pairs the pendant,
does the first sync (with a one-off 1.6 GB speech model download) and offers to
import a Limitless export. Before you start:

- Turn off Bluetooth on your phone or quit the Limitless app. The pendant only
  shows up when nothing else is connected to it.
- **Pairing by the button resets the pendant and clears the audio stored on
  it**, so let the Limitless app sync first. With a USB cable you can
  [pair without erasing](docs/pendant.md#moving-a-pendant-to-another-computer-without-erasing-it).

No pendant? Look around with made-up conversations:

```sh
uv run limitful demo
uv run limitful serve --data-dir ~/Limitful-demo
```

## Commands

Everything the app does is also a command; `uv run limitful --help` lists them all.

```sh
uv run limitful pair               # first use only
uv run limitful sync               # download new audio into ~/Limitful
uv run limitful transcribe         # turn new audio into text
uv run limitful watch              # do both every two minutes, and free synced audio on the pendant
uv run limitful autostart on       # macOS: run from login, no terminal needed
uv run limitful import export.zip  # bring in a Limitless data export
uv run limitful ask "what did the dentist say about the crown?"
```

## What leaves your computer

Nothing, by default. Syncing, transcription, search and playback are local.
Two choices in Settings send data out, and both are off until you make them:

| If you choose | What is sent | To |
|---|---|---|
| Claude for summaries and Ask | The text of conversations, your questions, and what Claude reads to answer them | Anthropic |
| Deepgram for transcription | The audio of new recordings | Deepgram |

On the command line, a flag or an environment variable makes the same choice for one run,
and the `score-speakers` experiment sends the audio of the imported conversations it measures to Deepgram.

API keys are kept in `~/.config/limitful/config.json`, readable only by you.
Models are downloaded on first use from Hugging Face and GitHub.

## Status

Early, but working: it runs daily against one real pendant on macOS. Not yet
verified:

- Linux and Windows.
- Unpair over Bluetooth, and the pairing buttons in the app (run against a
  simulated pendant only).
- Any pendant other than the one it was built on.

## More

- [Using Limitful](docs/guide.md): every feature, option and environment variable.
- [The pendant](docs/pendant.md): buttons and lights, pairing without erasing, hardware notes.
- [Second brain on Windows](docs/second-brain.md): export transcripts to a private GitHub repo and encrypted audio to Google Drive, with Claude triage.
- [Experiments](docs/experiments.md): what was measured, including what did not work (speaker labels, Apple GPU, filler words).
- [Development](docs/development.md) and the [roadmap](ROADMAP.md).

## Credits

The pendant's Bluetooth protocol was reverse-engineered by the
[Omi](https://github.com/BasedHardware/omi) project (MIT); this is an
independent implementation of the same wire format. Built on
[Whisper](https://github.com/openai/whisper) through
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) and CTranslate2,
[bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) in
[Xenova's ONNX conversion](https://huggingface.co/Xenova/bge-small-en-v1.5),
TitaNet-small from [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx),
[bleak](https://github.com/hbldh/bleak) and
[pyserial](https://github.com/pyserial/pyserial). The wordmark is set in
[Outfit](https://github.com/Outfitio/Outfit-Fonts) (SIL Open Font License).

Not affiliated with or endorsed by Limitless or Meta. [MIT licence](LICENSE).
