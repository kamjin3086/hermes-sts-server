from __future__ import annotations

import asyncio
import base64
import os
import shlex
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Protocol

from hermes_sts.audio import tone_pcm16
from hermes_sts.config import Settings


@dataclass(frozen=True)
class TtsVoice:
    speaker: str = ""
    instruct: str = ""
    ref_wav: str = ""
    ref_text: str = ""
    ref_spk: str = ""
    ref_rvq: str = ""
    model: str = ""
    codec: str = ""
    lang: str = ""
    audio_format: str = ""
    seed: int | None = None
    extra_args: str = ""

    @classmethod
    def from_settings(cls, settings: Settings) -> "TtsVoice":
        mode = settings.qwentts_cpp_voice_mode.strip().lower()
        qwen_kwargs = {
            "model": settings.qwentts_cpp_model or _qwen_model_path(settings),
            "codec": settings.qwentts_cpp_codec,
            "lang": settings.qwentts_cpp_lang,
            "audio_format": settings.qwentts_cpp_format,
            "seed": settings.qwentts_cpp_seed,
            "extra_args": settings.qwentts_cpp_extra_args,
        }
        if mode == "preset":
            return cls(speaker=settings.qwentts_cpp_voice_preset, **qwen_kwargs)
        if mode == "design":
            return cls(instruct=settings.qwentts_cpp_voice_design, **qwen_kwargs)
        return cls(
            speaker=settings.qwentts_cpp_speaker,
            instruct=settings.qwentts_cpp_instruct,
            ref_wav=settings.qwentts_cpp_ref_wav,
            ref_text=settings.qwentts_cpp_ref_text,
            ref_spk=settings.qwentts_cpp_ref_spk,
            ref_rvq=settings.qwentts_cpp_ref_rvq,
            **qwen_kwargs,
        )

    def is_empty(self) -> bool:
        return not any(
            [
                self.speaker,
                self.instruct,
                self.ref_wav,
                self.ref_text,
                self.ref_spk,
                self.ref_rvq,
            ]
        )

    @classmethod
    def from_realtime(cls, value) -> "TtsVoice":
        if isinstance(value, str):
            return cls(speaker=value.strip())
        if not isinstance(value, dict):
            return cls()
        return cls(
            speaker=str(value.get("speaker") or value.get("name") or value.get("voice") or "").strip(),
            instruct=str(value.get("instruct") or value.get("instructions") or "").strip(),
            ref_wav=str(value.get("ref_wav") or value.get("reference_wav") or "").strip(),
            ref_text=str(value.get("ref_text") or value.get("reference_text") or "").strip(),
            ref_spk=str(value.get("ref_spk") or value.get("speaker_embedding") or "").strip(),
            ref_rvq=str(value.get("ref_rvq") or value.get("reference_rvq") or "").strip(),
        )


class TtsProvider(Protocol):
    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        ...


def _qwen_model_path(settings: Settings) -> str:
    mode = settings.qwentts_cpp_voice_mode.strip().lower()
    if mode == "preset":
        return settings.qwentts_cpp_customvoice_model or settings.qwentts_cpp_model
    if mode == "design":
        return settings.qwentts_cpp_voicedesign_model or settings.qwentts_cpp_model
    return settings.qwentts_cpp_base_model or settings.qwentts_cpp_model


class WindowsSapiTts:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
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


class ToneTts:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        return tone_pcm16(text=text, sample_rate=self.settings.sample_rate)


class CommandWavTts:
    provider_name = "command_wav"

    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        return await asyncio.to_thread(self._synthesize_sync, text, voice)

    def _synthesize_sync(self, text: str, voice: TtsVoice | None = None) -> bytes:
        wav_path = Path(NamedTemporaryFile(delete=False, suffix=".wav").name)
        try:
            result = subprocess.run(
                self._command(wav_path, voice=voice),
                input=text.encode("utf-8"),
                check=False,
                capture_output=True,
                timeout=self._timeout_seconds(),
                env=self._environment(),
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"{self.provider_name} failed with code {result.returncode}: {stderr}"
                )
            return _read_wav_as_pcm16_mono(wav_path, self.settings.sample_rate)
        finally:
            wav_path.unlink(missing_ok=True)

    def _command(self, wav_path: Path, *, voice: TtsVoice | None = None) -> list[str]:
        raise NotImplementedError

    def _environment(self) -> dict[str, str] | None:
        return None

    def _timeout_seconds(self) -> float:
        return 120.0


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

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
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

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        audio = self.tts.generate(text, sid=self.settings.sherpa_kokoro_voice, speed=1.0)
        import numpy as np

        samples = np.asarray(audio.samples, dtype=np.float32)
        if int(audio.sample_rate) != self.settings.sample_rate:
            samples = _resample_linear(samples, int(audio.sample_rate), self.settings.sample_rate)
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype(np.int16).tobytes()


class QwenTtsCpp(CommandWavTts):
    provider_name = "qwentts.cpp"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        model_path = self._model_path()
        required = {
            "QWENTTS_CPP_BIN": settings.qwentts_cpp_bin,
            "QWENTTS_CPP_MODEL": model_path,
            "QWENTTS_CPP_CODEC": settings.qwentts_cpp_codec,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} are required for STS_TTS_PROVIDER=qwen3tts"
            )
        self.bin_path = Path(settings.qwentts_cpp_bin)
        if not self.bin_path.is_file():
            raise RuntimeError(f"QWENTTS_CPP_BIN does not exist: {self.bin_path}")
        for name, value in {
            "QWENTTS_CPP_MODEL": model_path,
            "QWENTTS_CPP_CODEC": settings.qwentts_cpp_codec,
        }.items():
            if not Path(value).is_file():
                raise RuntimeError(f"{name} does not exist: {value}")

    def _model_path(self) -> str:
        if self.settings.qwentts_cpp_model:
            return self.settings.qwentts_cpp_model
        return _qwen_model_path(self.settings)

    def _command(self, wav_path: Path, *, voice: TtsVoice | None = None) -> list[str]:
        voice = voice or TtsVoice.from_settings(self.settings)
        cmd = [
            str(self.bin_path),
            "--model",
            voice.model or self._model_path(),
            "--codec",
            voice.codec or self.settings.qwentts_cpp_codec,
            "--format",
            voice.audio_format or self.settings.qwentts_cpp_format,
        ]
        lang = voice.lang or self.settings.qwentts_cpp_lang
        if lang:
            cmd.extend(["--lang", lang])
        has_clone = bool(voice.ref_wav or voice.ref_spk or voice.ref_rvq)
        if voice.speaker and not has_clone:
            cmd.extend(["--speaker", voice.speaker])
        if voice.instruct:
            cmd.extend(["--instruct", voice.instruct])
        if voice.ref_wav:
            cmd.extend(["--ref-wav", voice.ref_wav])
        if voice.ref_text:
            cmd.extend(["--ref-text", voice.ref_text])
        if voice.ref_spk:
            cmd.extend(["--ref-spk", voice.ref_spk])
        if voice.ref_rvq:
            cmd.extend(["--ref-rvq", voice.ref_rvq])
        cmd.extend(["--seed", str(voice.seed if voice.seed is not None else self.settings.qwentts_cpp_seed)])
        extra_args = voice.extra_args if voice.extra_args else self.settings.qwentts_cpp_extra_args
        if extra_args:
            cmd.extend(shlex.split(extra_args))
        cmd.extend(["-o", str(wav_path)])
        return cmd

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.qwentts_cpp_backend:
            env["GGML_BACKEND"] = self.settings.qwentts_cpp_backend
        return env

    def _timeout_seconds(self) -> float:
        return self.settings.qwentts_cpp_timeout_seconds


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
    if provider == "tone":
        return ToneTts(settings)
    if provider == "sapi":
        return WindowsSapiTts(settings)
    if provider == "sherpa_onnx":
        return SherpaOnnxTts(settings)
    if provider == "sherpa_kokoro":
        return SherpaKokoroTts(settings)
    if provider in {"qwen3tts", "qwentts_cpp", "qwen_tts_cpp"}:
        return QwenTtsCpp(settings)
    raise RuntimeError(f"Unsupported STS_TTS_PROVIDER={settings.tts_provider!r}")
