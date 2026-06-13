from __future__ import annotations

import base64
import subprocess
import wave
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from hermes_sts.audio import tone_pcm16
from hermes_sts.config import Settings


class TtsProvider(Protocol):
    async def synthesize(self, text: str) -> bytes:
        ...


class WindowsSapiTts:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str) -> bytes:
        wav_path = Path(NamedTemporaryFile(delete=False, suffix=".wav").name)
        escaped = text.replace("'", "''")
        voice_filter = self.settings.sapi_voice.replace("'", "''")
        select_voice = ""
        if voice_filter:
            select_voice = (
                "$v = $s.GetInstalledVoices() | Where-Object "
                f"{{ $_.VoiceInfo.Name -like '*{voice_filter}*' }} | Select-Object -First 1; "
                "if ($v) { $s.SelectVoice($v.VoiceInfo.Name) }; "
            )
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{select_voice}"
            f"$s.SetOutputToWaveFile('{str(wav_path)}'); "
            f"$s.Speak('{escaped}'); "
            "$s.Dispose();"
        )
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded_script],
                check=True,
                capture_output=True,
                timeout=30,
            )
            return _read_wav_as_pcm16_mono(wav_path, self.settings.sample_rate)
        except Exception:
            return tone_pcm16(text=text, sample_rate=self.settings.sample_rate)
        finally:
            wav_path.unlink(missing_ok=True)


class SherpaOnnxTts:
    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.sherpa_tts_model or not settings.sherpa_tts_tokens:
            raise RuntimeError("SHERPA_TTS_MODEL and SHERPA_TTS_TOKENS are required")
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Install optional dependency: pip install -e .[sherpa]") from exc

        offline = sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=settings.sherpa_tts_model,
                tokens=settings.sherpa_tts_tokens,
                data_dir=settings.sherpa_tts_data_dir or "",
            ),
            num_threads=4,
        )
        config = sherpa_onnx.OfflineTtsConfig(model=offline)
        self.tts = sherpa_onnx.OfflineTts(config)

    async def synthesize(self, text: str) -> bytes:
        audio = self.tts.generate(text)
        import numpy as np

        samples = np.asarray(audio.samples, dtype=np.float32)
        if int(audio.sample_rate) != self.settings.sample_rate:
            samples = _resample_linear(samples, int(audio.sample_rate), self.settings.sample_rate)
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype(np.int16).tobytes()


class SherpaKokoroTts:
    def __init__(self, settings: Settings):
        self.settings = settings
        required = {
            "SHERPA_KOKORO_MODEL": settings.sherpa_kokoro_model,
            "SHERPA_KOKORO_VOICES": settings.sherpa_kokoro_voices,
            "SHERPA_KOKORO_TOKENS": settings.sherpa_kokoro_tokens,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(f"{', '.join(missing)} are required for STS_TTS_PROVIDER=sherpa_kokoro")
        try:
            import sherpa_onnx  # type: ignore
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("sherpa-onnx is required for STS_TTS_PROVIDER=sherpa_kokoro") from exc

        offline = sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=settings.sherpa_kokoro_model,
                voices=settings.sherpa_kokoro_voices,
                tokens=settings.sherpa_kokoro_tokens,
                lexicon=settings.sherpa_kokoro_lexicon,
                data_dir=settings.sherpa_kokoro_data_dir,
                lang=settings.sherpa_kokoro_lang,
            ),
            num_threads=4,
            provider="cpu",
        )
        config = sherpa_onnx.OfflineTtsConfig(model=offline)
        self.tts = sherpa_onnx.OfflineTts(config)

    async def synthesize(self, text: str) -> bytes:
        audio = self.tts.generate(text, sid=self.settings.sherpa_kokoro_voice, speed=1.0)
        import numpy as np

        samples = np.asarray(audio.samples, dtype=np.float32)
        if int(audio.sample_rate) != self.settings.sample_rate:
            samples = _resample_linear(samples, int(audio.sample_rate), self.settings.sample_rate)
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype(np.int16).tobytes()


def _read_wav_as_pcm16_mono(path: Path, target_rate: int) -> bytes:
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        return b""
    audio = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
    if rate != target_rate and audio.size:
        audio = _resample_linear(audio.astype(np.float32), rate, target_rate).astype(np.int16)
    return audio.tobytes()


def _resample_linear(samples, source_rate: int, target_rate: int):
    import numpy as np

    if source_rate == target_rate or len(samples) == 0:
        return samples
    duration = len(samples) / float(source_rate)
    target_count = max(1, int(duration * target_rate))
    src_x = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    dst_x = np.linspace(0.0, duration, num=target_count, endpoint=False)
    return np.interp(dst_x, src_x, samples).astype(samples.dtype)


def build_tts(settings: Settings) -> TtsProvider:
    provider = settings.tts_provider.strip().lower()
    if provider == "sapi":
        return WindowsSapiTts(settings)
    if provider == "sherpa_onnx":
        return SherpaOnnxTts(settings)
    if provider == "sherpa_kokoro":
        return SherpaKokoroTts(settings)
    raise RuntimeError(f"Unsupported STS_TTS_PROVIDER={settings.tts_provider!r}")
