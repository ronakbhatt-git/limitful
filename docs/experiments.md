# Experiments

What was tried and measured, including what did not work. Samples are small and from one person's recordings.

## Search by meaning

What was measured, on one person's 31,750 lines, with 15 questions worded to
avoid the words actually spoken (so this is the case it is meant for, not a
typical search), counting a hit when a result was on or within three lines of
the line holding the answer:

| Method | In the top 5 | Top 10 | Top 40 |
|---|---|---|---|
| Word matching, as Ask used before | 0 | 2 | 3 |
| Meaning, each line on its own | 6 | 9 | 12 |
| Meaning, each line with its neighbours (what is used) | 8 | 9 | 14 |

Neighbouring lines come back in clumps, so what is shown is one line per
moment. Counted that way, the answer was among the eight moments the search
box shows for 10 of the 15 questions, and among the thirty Ask is given for
all 15. Fifteen questions written by the person who built the feature, on one
person's recordings, is a small and friendly sample.

Limits worth knowing. It always returns its closest lines, related or not, and
the closeness score does not tell the two apart: right answers scored 0.57 to
0.79 and the unrelated lines beside them a median of 0.68. That is why the
search box keeps these results separate and labelled instead of mixing them
in. For names, numbers and exact phrases, word matching is still better. The
model is English-only.

## Speaker labels

Automatic speaker labels are **off by default**, because nothing tested so far
works on real pendant recordings.

What was measured, on ten imported conversations, as the share of speaking
time given to the same speaker Limitless gave it to (always guessing the main
speaker scores 63%):

| Method | Score |
|---|---|
| Separation model (pyannote segmentation with five different voice models), told the number of speakers | 62% |
| The same, deciding the number of speakers itself | 28% to 49% |
| Grouping transcript lines by voice fingerprint | 30% to 49% |
| Claude reading the words alone | 58% |

The same separation pipeline splits a clean two-speaker test recording
correctly, so the difficulty is the audio: one microphone, worn, with people
at different distances. Limitless's labels are also not a clean reference: in
43% of conversations, lines it gives to one speaker sound no more alike than
lines it gives to different speakers.

What does work:

- Imported conversations keep Limitless's per-conversation "Speaker 1",
  "Speaker 2". Click one to name that speaker in that conversation.
- Click a named speaker to rename that person everywhere; giving two speakers
  the same name merges them. "Only this conversation" changes one conversation.
- `uv run limitful speakers` runs the experimental voice labelling on pendant
  lines not yet labelled (downloading a small voice model the first time),
  then lists speakers; `--rename 2 Andrei` names one.

Telling which speaker is you, by loudness, was also measured and does not
work. The idea: the pendant is worn, so its wearer should be the loudest voice
in every conversation. In the six imported conversations where one speaker is
named as the wearer, the wearer was the loudest speaker in three (chance
would be 2.6 of six), and never by more than half a decibel either way. Across
twenty other conversations the loudest speaker led the next by a median of
1.7 dB, and by under 1 dB in 30% of them. The pendant appears to even out
loudness, so nothing was built on it. Six conversations is a small sample, and
some of those names may have come from the unreliable voice linking.

### Speaker labels from a hosted service (opt-in, measured: not better)

A hosted transcription service can label speakers where local methods could
not. Limitful can use [Deepgram](https://deepgram.com) in place of local
Whisper for new recordings from the pendant:

- Choose it under Settings, Transcription, and paste a Deepgram API key, or
  run `uv run limitful transcribe --engine deepgram` once a key is saved.
- **The audio of every new recording is then sent to Deepgram and billed to
  your Deepgram account.** Nothing is sent until you choose this. The key is
  kept in `~/.config/limitful/config.json`, readable only by you, and only its
  last four characters are shown again.
- A service's "Speaker 1" only means something within one request, and the
  pendant splits a conversation into many short recordings. So recordings are
  grouped into conversations (a five-minute silence ends one), joined, and sent
  as one piece. The newest conversation waits until it has been quiet for five
  minutes, so text appears a little later than with local transcription.
- Lines arrive as "Speaker 1", "Speaker 2" within each conversation. Click one
  to name that speaker in that conversation, exactly as with imported ones.
- Imported conversations are never sent for transcription; they keep
  Limitless's transcript. (`score-speakers`, below, does send the audio of the
  ones it measures.)

**Measured against the real service, its speaker labels are no better than
guessing.** Five imported conversations were sent to Deepgram (nova-3) and
its speakers compared with Limitless's, as the share of speaking time given to
the same speaker:

| Conversation | Limitless's speakers | Deepgram found | Score | Always guessing the main speaker |
|---|---|---|---|---|
| 1 | 3 | 2 | 59% | 57% |
| 2 | 2 | 1 | 48% | 64% |
| 3 | 3 | 1 | 18% | 64% |
| 4 | 3 | 2 | 45% | 55% |
| 5 | 3 | 3 | 48% | 45% |
| Average | | | 44% | 57% |

It found fewer speakers than Limitless in four of the five, and on a pendant
recording of one voice repeating a test phrase it found two. The score also
counts speech Deepgram did not transcribe as wrong, which is why finding one
speaker can score below the baseline, so read it as "not better", not as a
precise figure. Five conversations is a small sample, and Limitless's labels
are themselves an imperfect reference.

What was confirmed: Deepgram accepts both WAV and the pendant's Ogg Opus
(`audio/ogg`), saved keys are checked and accepted, and each line comes back
with a speaker. Not seen yet: what a refused key or exhausted credit looks
like in practice (assumed: HTTP 401 or 403, and 402).

To repeat the measurement, at your own cost:

```sh
uv run limitful score-speakers --limit 5
```

sends up to five imported conversations (two to four speakers, 50 to 220
lines, under twenty minutes each) to Deepgram and prints, for each, the share of speaking time it
gave to the same speaker Limitless did, next to the score for always guessing
the main speaker. On the conversations used for the table above that baseline
averaged 57%; a service worth switching to should beat it clearly.

The experimental voice labelling is still there for anyone who wants to try
it: `uv run limitful transcribe --speakers`, `uv run limitful watch --speakers`,
and `uv run limitful speakers --link-imported` for imported conversations. Add
`--since 2025-12-01 --until 2025-12-31` to try one month before the whole history.

## Transcription on the Apple GPU: tried, not the default

Local Whisper runs on the CPU. The library underneath (CTranslate2 4.9) has an
experimental Apple GPU mode, `--device mps`, which was measured on an M-series
laptop to see whether it should be the default. It should not, yet:

- **It fails at random.** Of six runs of the same test, three failed on every
  recording they were given (46 attempts in all), each time with an error
  from the library (`IndexError: vector`, or with full-precision numbers
  `Invalid token ID 18446744073709551615`). The other three ran the same
  recordings 53 times with no failure. A run that started 22 seconds after a
  failed one worked. Nothing that was varied (number precision, word timings,
  a fixed language) made the difference.
- **When it works it is quick and about as good.** On five recordings of real
  speech (17 minutes) it ran at 9 to 13 times real time, against 3 times for
  the CPU. Both were measured while the laptop was busy with other work, so
  read the ratio and not the absolute numbers. Its text matched Limitless's
  transcript of the same recordings as well as the CPU's did (a similarity of
  0.61 against 0.59, where 1 is identical), and empty files and recordings
  with no speech gave no lines on either. How well its word timings agree with
  the CPU's was not measured: the run meant to compare them was one that
  failed.

So the default stays on the CPU, and `--device mps` stays available on
`transcribe`, `watch` and `retime` for anyone who wants to try it; if it
fails, run the command again. The cause was not found. The laptop's GPU was
shared with other programs during the test, which may matter.

## Keeping "mhm" and "uh": tried, nothing adopted

Local Whisper leaves out most filler words that Limitless's transcripts keep.
Six alternative combinations of Whisper settings were tried to get them back, scored
against Limitless's own text for the same audio. The rule was to adopt a
setting only if it kept more fillers without inventing lines on silent audio,
which is the fault the current settings were chosen to stop.

First, on five imported recordings picked for having the most fillers (14
minutes, 67 filler words in Limitless's text) and twelve pendant recordings
with nobody speaking (3 minutes):

| Setting | Fillers kept, of 67 | Lines invented on the silent recordings |
|---|---|---|
| As shipped | 22 | 0 |
| Start each file with a prompt full of fillers | 30 | 1 ("Mm-hmm.") |
| Suppress no tokens | 22 | 0 |
| Looser speech detector | 33 | 3 ("Thank you." twice, and a line of Russian) |
| Carry the previous text forward | 50 | 0 |
| Prompt and looser detector | 29 | 3 |
| Prompt and carry the previous text | 62 | 1 ("Mm-hmm.") |

Loosening or removing the confidence filter kept no more fillers under any
setting and let a "Thank you." through on silence. The prompt and the looser
detector both bring invented lines back, so they are out.

Carrying the previous text forward looked like the answer, so it was run again
on recordings not chosen for their fillers. It did not hold up:

| | As shipped | Carrying text forward |
|---|---|---|
| Nine other imported recordings (22 minutes): fillers kept, of 30 | 8 | 8 |
| 21 of the pendant's own recordings with speech (12 minutes) | 374 words, no fillers | the same text in 20 of them, no fillers |
| 60 pendant recordings with nobody speaking (28 minutes): lines invented | 0 | 0 |

It did no harm in that run, but it gained nothing either, and the pendant's
own recordings are mostly under half a minute, where this setting cannot act
at all. One good result on hand-picked recordings is not enough to change a
setting that was turned off for making wrong guesses repeat, so the settings
are unchanged.

Caveats: fillers were counted, not matched one by one in time; the samples are
small and from one person's recordings; and Limitless's text is not a perfect
reference. One-word lines such as "Mhm." or "Yeah." could not be judged: the
sample held only four such words, and every setting kept all four.
