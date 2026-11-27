"""Speech-to-text for exported audio.

Engines implement `Transcriber`. The default runs Whisper locally through
faster-whisper, so audio never leaves the machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DEFAULT_MODEL = "large-v3-turbo"

# Whisper invents short phrases ("Thank you.") for noise that slips past the
# silence filter. On pendant audio those score around -1.0 and real speech
# around -0.1 to -0.6, so drop anything the model itself is this unsure of.
MIN_AVG_LOGPROB = -0.8


@dataclass(frozen=True)
class Utterance:
    start_ms: int  # offset from the start of the audio file
    end_ms: int
    text: str


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> list[Utterance]: ...


def _resolve_device(device: str) -> str:
    """Turn "auto" into "cpu" on a Windows PC without an NVIDIA driver.

    There, letting CTranslate2 probe for CUDA by itself crashed the whole process
    with an access violation (exit code -1073741819), which no Python code can
    catch. nvcuda.dll ships with every NVIDIA driver, so without it there is no
    GPU to find.
    """
    import os
    import sys

    if device != "auto" or sys.platform != "win32":
        return device
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    return device if (system / "nvcuda.dll").exists() else "cpu"


class WhisperTranscriber:
    """Local Whisper via faster-whisper. The model downloads on first use."""

    def __init__(
        self, model: str = DEFAULT_MODEL, language: str | None = None, device: str = "auto"
    ) -> None:
        from faster_whisper import WhisperModel  # imported lazily: it is slow to load

        self.name = f"whisper:{model}"
        self._language = language
        self._model_name = model
        device = _resolve_device(device)
        self._on_cpu = device == "cpu"
        self._model = WhisperModel(model, device=device, compute_type="auto")

    def _run(self, work):
        """Run `work(model)`; if the GPU's libraries cannot be loaded, switch to the CPU and try again.

        On Windows a PC with an NVIDIA card but without the matching cuDNN or cuBLAS
        DLLs loads the model fine and fails at the first recording.
        """
        try:
            return work(self._model)
        except RuntimeError as err:
            text = str(err).lower()
            if self._on_cpu or not any(word in text for word in ("cuda", "cudnn", "cublas", ".dll", "gpu")):
                raise
        from faster_whisper import WhisperModel

        self._model = WhisperModel(self._model_name, device="cpu", compute_type="int8")
        self._on_cpu = True
        return work(self._model)

    reports_progress = True  # `transcribe` can say how far into the audio it has got

    def transcribe(self, audio_path: Path, on_heard=None) -> list[Utterance]:
        """`on_heard(seconds)` is called as the work moves through the recording."""
        def work(model):
            segments, _info = model.transcribe(
                str(audio_path),
                language=self._language,
                vad_filter=True,  # skip non-speech, where Whisper tends to invent text
                condition_on_previous_text=False,  # stops one bad guess repeating itself
            )
            utterances = []
            for seg in segments:
                if on_heard:
                    on_heard(seg.end)
                text = seg.text.strip()
                if text and seg.avg_logprob >= MIN_AVG_LOGPROB:
                    utterances.append(Utterance(int(seg.start * 1000), int(seg.end * 1000), text))
            return utterances

        return self._run(work)

    def words(self, audio_path: Path) -> list[tuple[float, str]]:
        """Every word heard in a recording with the second it starts at.

        Used to place lines transcribed elsewhere at the right moment in the audio.
        """
        def work(model):
            segments, _info = model.transcribe(
                str(audio_path),
                language=self._language,
                vad_filter=True,
                condition_on_previous_text=False,
                word_timestamps=True,
            )
            return [(word.start, word.word) for seg in segments for word in (seg.words or [])]

        return self._run(work)


def make_transcriber(engine: str = "local", model: str = DEFAULT_MODEL, language: str | None = None,
                     device: str = "auto"):
    """The engine the user chose: local Whisper, or a hosted service with speaker labels."""
    if engine == "deepgram":
        from . import config
        from .hosted import DeepgramTranscriber

        return DeepgramTranscriber(config.deepgram_api_key() or "", language=language)
    if engine != "local":
        raise ValueError(f"unknown transcription engine {engine!r}; use local or deepgram")
    return WhisperTranscriber(model, language, device)
