from __future__ import annotations

import wave
import importlib.metadata
import importlib.util
import sys
import types
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

import httpx

from hermes_sts.audio import Utterance
from hermes_sts.config import Settings


class SttProvider(Protocol):
    async def transcribe(self, utterance: Utterance) -> str:
        ...


class DevStt:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def transcribe(self, utterance: Utterance) -> str:
        return self.settings.dev_transcript.strip()


class FunAsrOnnxStt:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.funasr_model_dir:
            raise RuntimeError("FUNASR_MODEL_DIR is required for STS_STT_PROVIDER=funasr_onnx")
        try:
            Paraformer = _load_funasr_paraformer()
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Install optional dependency: pip install -e .[funasr]") from exc

        self.model = Paraformer(
            settings.funasr_model_dir,
            batch_size=1,
            quantize=settings.funasr_quantize,
        )

    async def transcribe(self, utterance: Utterance) -> str:
        wav_path = _write_temp_wav(utterance.pcm16, self.settings.sample_rate)
        try:
            result = self.model(str(wav_path))
            if isinstance(result, list) and result:
                item = result[0]
                if isinstance(item, dict):
                    return str(item.get("preds") or item.get("text") or "").strip()
                return str(item).strip()
            if isinstance(result, dict):
                return str(result.get("preds") or result.get("text") or "").strip()
            return str(result).strip()
        finally:
            wav_path.unlink(missing_ok=True)


class LemonadeWhisperStt:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def transcribe(self, utterance: Utterance) -> str:
        wav_path = _write_temp_wav(utterance.pcm16, self.settings.sample_rate)
        headers = {}
        if self.settings.lemonade_api_key:
            headers["Authorization"] = f"Bearer {self.settings.lemonade_api_key}"
        data = {
            "model": self.settings.lemonade_stt_model,
            "language": self.settings.lemonade_stt_language,
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.lemonade_stt_timeout_seconds) as client:
                with wav_path.open("rb") as fh:
                    files = {"file": ("speech.wav", fh, "audio/wav")}
                    resp = await client.post(
                        f"{self.settings.lemonade_base_url.rstrip('/')}/audio/transcriptions",
                        data=data,
                        files=files,
                        headers=headers,
                    )
                resp.raise_for_status()
                payload = resp.json()
            return str(payload.get("text") or "").strip()
        finally:
            wav_path.unlink(missing_ok=True)


class SherpaSenseVoiceStt:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.sherpa_sensevoice_model:
            raise RuntimeError("SHERPA_SENSEVOICE_MODEL is required for STS_STT_PROVIDER=sherpa_sensevoice")
        if not settings.sherpa_sensevoice_tokens:
            raise RuntimeError("SHERPA_SENSEVOICE_TOKENS is required for STS_STT_PROVIDER=sherpa_sensevoice")
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("sherpa-onnx is required for STS_STT_PROVIDER=sherpa_sensevoice") from exc

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=settings.sherpa_sensevoice_model,
            tokens=settings.sherpa_sensevoice_tokens,
            num_threads=4,
            sample_rate=settings.sample_rate,
            provider="cpu",
            language=settings.sherpa_sensevoice_language,
            use_itn=settings.sherpa_sensevoice_use_itn,
        )

    async def transcribe(self, utterance: Utterance) -> str:
        import numpy as np

        samples = np.frombuffer(utterance.pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        stream = self.recognizer.create_stream()
        stream.accept_waveform(self.settings.sample_rate, samples)
        self.recognizer.decode_stream(stream)
        return str(stream.result.text or "").strip()


def _write_temp_wav(pcm16: bytes, sample_rate: int) -> Path:
    tmp = NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()
    path = Path(tmp.name)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return path


def _load_funasr_paraformer():
    # funasr_onnx.__init__ imports SenseVoiceSmall, which currently pulls torch.
    # Load the ONNX Paraformer module directly so this provider stays lightweight.
    package_root = Path(importlib.metadata.distribution("funasr-onnx").locate_file("funasr_onnx"))
    package = types.ModuleType("funasr_onnx")
    package.__path__ = [str(package_root)]  # type: ignore[attr-defined]
    sys.modules["funasr_onnx"] = package

    module_name = "funasr_onnx.paraformer_bin"
    spec = importlib.util.spec_from_file_location(module_name, package_root / "paraformer_bin.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load funasr_onnx.paraformer_bin")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.Paraformer


def build_stt(settings: Settings) -> SttProvider:
    provider = settings.stt_provider.strip().lower()
    if provider == "dev":
        return DevStt(settings)
    if provider == "funasr_onnx":
        return FunAsrOnnxStt(settings)
    if provider == "lemonade_whisper":
        return LemonadeWhisperStt(settings)
    if provider == "sherpa_sensevoice":
        return SherpaSenseVoiceStt(settings)
    raise RuntimeError(f"Unsupported STS_STT_PROVIDER={settings.stt_provider!r}")
