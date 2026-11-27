"""Hosted speech-to-text with speaker labels, as an opt-in alternative to local Whisper.

Local transcription gets the words right but cannot tell speakers apart on
pendant audio (see docs/experiments.md, "Speaker labels"). A hosted service can. The
price is that the audio leaves this machine and is billed to the user's
account, so nothing here runs unless the user picks it in Settings.

A service's speaker numbers only mean something within one request. The
pendant stops recording during silence, so one conversation is many short
recordings; they are joined and sent as one piece (a "session") so that
"speaker 1" is the same person from the first recording to the last.

Deepgram is the first service: one request with the audio as its body, no
upload step and no polling. The request follows Deepgram's published
reference (POST /v1/listen, `Authorization: Token <key>`, `diarize_model`,
`utterances`). It has been run against the real service; its speaker labels
scored no better than always guessing the main speaker (see docs/experiments.md).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass

DEEPGRAM_URL = "https://api.deepgram.com"
DEEPGRAM_MODEL = "nova-3"


class HostedError(Exception):
    """The service could not be reached or refused the request."""


@dataclass(frozen=True)
class SpokenLine:
    start_s: float      # seconds from the start of the audio that was sent
    end_s: float
    text: str
    speaker: int | None  # the service's number for who spoke, counted from 0


def parse_deepgram(reply: object) -> list[SpokenLine]:
    """Lines with their speaker from a Deepgram reply.

    Uses the `utterances` list. If an utterance carries no `speaker`, the
    speaker heard on most of its words is used instead.
    """
    if not isinstance(reply, dict):
        raise HostedError("Deepgram's reply was not in the expected format")
    utterances = (reply.get("results") or {}).get("utterances")
    if not isinstance(utterances, list):
        raise HostedError("Deepgram's reply had no utterances; was `utterances=true` accepted?")
    lines = []
    for item in utterances:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("transcript") or "").split())
        start, end = item.get("start"), item.get("end")
        if not text or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        speaker = item.get("speaker")
        if not isinstance(speaker, int) or isinstance(speaker, bool):
            heard = Counter(
                word["speaker"] for word in item.get("words") or []
                if isinstance(word, dict) and isinstance(word.get("speaker"), int))
            speaker = heard.most_common(1)[0][0] if heard else None
        lines.append(SpokenLine(float(start), max(float(end), float(start) + 0.05), text, speaker))
    return sorted(lines, key=lambda line: line.start_s)


class DeepgramTranscriber:
    """Deepgram's pre-recorded endpoint. Sends a whole session in one request."""

    def __init__(self, api_key: str, model: str = DEEPGRAM_MODEL, language: str | None = None,
                 base_url: str = DEEPGRAM_URL) -> None:
        if not api_key:
            raise HostedError("add a Deepgram API key in Settings first")
        self._key = api_key
        self._model = model
        self._language = language
        self._base = base_url.rstrip("/")
        self.name = f"deepgram:{model}"

    def transcribe_session(self, audio: bytes, content_type: str = "audio/ogg") -> list[SpokenLine]:
        query = {
            "model": self._model,
            "diarize_model": "latest",   # turns speaker labels on; `diarize=true` is the old spelling
            "utterances": "true",
            "punctuate": "true",
            "smart_format": "true",
            "mip_opt_out": "true",       # keep the audio out of Deepgram's model training
        }
        if self._language:
            query["language"] = self._language
        request = urllib.request.Request(
            f"{self._base}/v1/listen?{urllib.parse.urlencode(query)}", data=audio,
            headers={"Authorization": f"Token {self._key}", "Content-Type": content_type})
        return parse_deepgram(_send(request, timeout=900))


def check_deepgram(api_key: str, base_url: str = DEEPGRAM_URL) -> None:
    """Confirm Deepgram accepts a key, without sending audio or spending anything."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/projects", headers={"Authorization": f"Token {api_key}"})
    _send(request, timeout=30)


def _send(request: urllib.request.Request, timeout: float) -> object:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:200]
        if err.code in (401, 403):
            raise HostedError("Deepgram did not accept that key") from None
        if err.code == 402:
            raise HostedError("the Deepgram account has no credit left") from None
        if err.code == 429:
            raise HostedError("Deepgram is rate-limiting requests; try again shortly") from None
        raise HostedError(f"Deepgram error {err.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        raise HostedError("could not reach Deepgram") from None
    except json.JSONDecodeError:
        raise HostedError("Deepgram's reply was not valid JSON") from None
