"""Transcription engine wrapping the faster-whisper model with thread-safe access.

Provides a single shared WhisperModel instance protected by a threading lock
so that concurrent stream worker threads can safely transcribe audio chunks
without data corruption.
"""

import logging
import threading

from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)


class Transcriber:
    """Thread-safe wrapper around the faster-whisper WhisperModel.

    The model is not loaded until :meth:`load_model` is called explicitly,
    allowing the caller to control when the (potentially slow) model
    initialisation happens.
    """

    def __init__(self, model_name: str = "base.en") -> None:
        """Store the model name. The model is not loaded until load_model() is called.

        Args:
            model_name: Name of the faster-whisper model to load (e.g. "base.en").
        """
        self._model_name = model_name
        self._model: WhisperModel | None = None
        self._lock = threading.Lock()

    def load_model(self) -> None:
        """Load the WhisperModel onto the CPU with int8 quantisation.

        Raises:
            RuntimeError: If the model fails to load, with the model name
                and the underlying failure reason included in the message.
        """
        try:
            logger.info("Loading faster-whisper model '%s' ...", self._model_name)
            self._model = WhisperModel(
                self._model_name, device="cpu", compute_type="int8"
            )
            logger.info("Model '%s' loaded successfully.", self._model_name)
        except Exception as exc:
            logger.error(
                "Failed to load model '%s': %s", self._model_name, exc
            )
            self._model = None
            raise RuntimeError(
                f"Failed to load whisper model '{self._model_name}': {exc}"
            ) from exc

    def transcribe(self, audio_path: str) -> str:
        """Transcribe a WAV audio file and return the recognised text.

        Thread-safe — acquires an internal lock so that only one thread uses
        the model at a time.

        Args:
            audio_path: Path to the 16 kHz mono PCM WAV file to transcribe.

        Returns:
            The concatenated text from all recognised segments, or an empty
            string if no speech was detected or if an error occurred.
        """
        if self._model is None:
            logger.error("Transcription requested but model is not loaded.")
            return ""

        try:
            with self._lock:
                segments, info = self._model.transcribe(
                    audio_path, beam_size=5, language="en"
                )
                # The segments generator must be fully consumed while the lock
                # is held so that no other thread can access the model mid-decode.
                texts = [segment.text for segment in segments]
        except Exception as exc:
            logger.error("Transcription failed for '%s': %s", audio_path, exc)
            return ""

        result = " ".join(texts).strip()
        if not result:
            logger.debug("No speech detected in '%s'.", audio_path)
        return result

    @property
    def is_loaded(self) -> bool:
        """Whether the model has been successfully loaded."""
        return self._model is not None
