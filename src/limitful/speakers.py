"""Speaker labels: work out which lines were said by the same voice.

Each transcript line long enough to judge gets a voice "fingerprint" (an
embedding) from a small local model. Lines are then grouped by fingerprint
into speakers, which the user can name. Fingerprints are stored, so grouping
can be redone later without the audio.

Honest limits: a line is given to one speaker even if two people spoke in it,
very short lines are left unlabelled, and similar voices can be merged.
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import numpy as np

MODEL_NAME = "nemo_en_titanet_small.onnx"
MODEL_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/" + MODEL_NAME  # the tag really is spelled this way
)
MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"

# Shorter lines do not carry enough voice to fingerprint reliably.
MIN_SPEAKER_MS = 1500
# Wait for this many fingerprints before grouping: the average of all of them
# is subtracted to cancel the pendant's own sound, and needs a few to be stable.
MIN_FINGERPRINTS = 10
# Similarity (after that subtraction) above which a line joins an existing
# speaker. Measured on pendant audio: same voice scored 0.41 or more, different
# voices 0.35 or less. That margin is thin, so expect some mistakes.
MATCH_THRESHOLD = 0.38

SAMPLE_RATE = 16000


class Embedder(Protocol):
    def embed(self, samples: "np.ndarray") -> "np.ndarray": ...


def cache_dir() -> Path:
    return Path(os.environ.get("LIMITFUL_CACHE") or Path.home() / ".cache" / "limitful")


def ensure_model() -> Path:
    """Download the speaker model on first use and check it is the expected file."""
    path = cache_dir() / MODEL_NAME
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URL, partial)
        digest = hashlib.sha256(partial.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            partial.unlink()
            raise RuntimeError("the downloaded speaker model did not match its checksum")
        os.replace(partial, path)
    return path


class TitanetEmbedder:
    """TitaNet-small through sherpa-onnx. Runs locally on the CPU."""

    def __init__(self) -> None:
        import sherpa_onnx

        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(ensure_model()), num_threads=2)
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)

    def embed(self, samples: "np.ndarray") -> "np.ndarray":
        import numpy as np

        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        return np.asarray(self._extractor.compute(stream), dtype=np.float32)


def load_audio(path: Path) -> "np.ndarray":
    """Decode an audio file to 16 kHz mono float samples."""
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)
