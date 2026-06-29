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
from typing import AsyncIterator, Literal, Protocol

from hermes_sts.audio import tone_pcm16
from hermes_sts.config import Settings

TtsEngineId = Literal["qwen3tts", "omnivoice", "sherpa_kokoro", "sherpa_onnx", "sapi", "tone"]
TtsMode = Literal["default", "preset", "design", "clone", "auto"]


@dataclass(frozen=True)
class TtsEngineConfig:
    engine: TtsEngineId
    bin_path: str = ""
    codec_bin_path: str = ""
    model_path: str = ""
    codec_path: str = ""
    backend: str = ""
    lang: str = ""
    audio_format: str = ""
    extra_args: str = ""
    seed: int | None = None
    timeout_seconds: float = 120.0
    max_new_frames: int = 0
    duration_seconds: float = 0.0
    chunk_duration_seconds: float = 15.0
    chunk_threshold_seconds: float = 30.0


@dataclass(frozen=True)
class TtsVoice:
    engine: TtsEngineId | str = "qwen3tts"
    mode: TtsMode | str = "default"
    speaker: str = ""
    instruct: str = ""
    ref_wav: str = ""
    ref_text: str = ""
    ref_spk: str = ""
    ref_rvq: str = ""
    omnivoice_ref_rvq: str = ""
    model: str = ""
    codec: str = ""
    lang: str = ""
    audio_format: str = ""
    seed: int | None = None
    extra_args: str = ""
    duration_seconds: float | None = None
    chunk_duration_seconds: float | None = None
    chunk_threshold_seconds: float | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "TtsVoice":
        provider = settings.tts_provider.strip().lower()
        if provider == "omnivoice":
            return cls.from_omnivoice_settings(settings)
        return cls.from_qwen_settings(settings)

    @classmethod
    def from_qwen_settings(cls, settings: Settings) -> "TtsVoice":
        mode = settings.qwentts_cpp_voice_mode.strip().lower()
        qwen_kwargs = {
            "engine": "qwen3tts",
            "mode": mode,
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

    @classmethod
    def from_omnivoice_settings(cls, settings: Settings) -> "TtsVoice":
        mode = settings.omnivoice_voice_mode.strip().lower()
        omni_kwargs = {
            "engine": "omnivoice",
            "mode": mode,
            "model": settings.omnivoice_model,
            "codec": settings.omnivoice_codec,
            "lang": settings.omnivoice_lang,
            "audio_format": settings.omnivoice_format,
            "seed": settings.omnivoice_seed,
            "extra_args": settings.omnivoice_extra_args,
            "duration_seconds": settings.omnivoice_duration_seconds,
            "chunk_duration_seconds": settings.omnivoice_chunk_duration_seconds,
            "chunk_threshold_seconds": settings.omnivoice_chunk_threshold_seconds,
        }
        if mode == "design":
            return cls(instruct=settings.omnivoice_voice_design, **omni_kwargs)
        if mode == "clone":
            return cls(
                ref_wav=settings.omnivoice_ref_wav,
                ref_text=settings.omnivoice_ref_text,
                omnivoice_ref_rvq=settings.omnivoice_ref_rvq,
                **omni_kwargs,
            )
        return cls(**omni_kwargs)

    def is_empty(self) -> bool:
        return not any(
            [
                self.speaker,
                self.instruct,
                self.ref_wav,
                self.ref_text,
                self.ref_spk,
                self.ref_rvq,
                self.omnivoice_ref_rvq,
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
            omnivoice_ref_rvq=str(value.get("omnivoice_ref_rvq") or "").strip(),
        )


class TtsProvider(Protocol):
    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        ...

    def stream_pcm(self, text: str, *, voice: TtsVoice | None = None) -> AsyncIterator[bytes]:
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
    supports_streaming = False

    def __init__(self, settings: Settings):
        self.settings = settings

    async def synthesize(self, text: str, *, voice: TtsVoice | None = None) -> bytes:
        return await asyncio.to_thread(self._synthesize_sync, text, voice)

    async def stream_pcm(self, text: str, *, voice: TtsVoice | None = None) -> AsyncIterator[bytes]:
        yield await self.synthesize(text, voice=voice)

    def _synthesize_sync(self, text: str, voice: TtsVoice | None = None) -> bytes:
        wav_path = Path(NamedTemporaryFile(delete=False, suffix=".wav").name)
        try:
            result = subprocess.run(
                self._command(wav_path, voice=voice),
                input=_stdin_text(text),
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

    async def _stream_wav_stdout_pcm(self, text: str, *, voice: TtsVoice | None = None) -> AsyncIterator[bytes]:
        cmd = self._command(Path("-"), voice=voice)
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(),
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        decoder = _StreamingWavPcm16Decoder(target_rate=self.settings.sample_rate, provider_name=self.provider_name)
        try:
            process.stdin.write(_stdin_text(text))
            await process.stdin.drain()
            process.stdin.close()
            while True:
                chunk = await asyncio.wait_for(
                    process.stdout.read(8192),
                    timeout=self._timeout_seconds(),
                )
                if not chunk:
                    break
                for pcm16 in decoder.feed(chunk):
                    if pcm16:
                        yield pcm16
            for pcm16 in decoder.flush():
                if pcm16:
                    yield pcm16
            code = await process.wait()
            if code != 0:
                stderr = await stderr_task
                raise RuntimeError(
                    f"{self.provider_name} stream failed with code {code}: "
                    f"{stderr.decode('utf-8', errors='replace').strip()[-1200:]}"
                )
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()
            raise
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            if not stderr_task.done():
                stderr_task.cancel()

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
    supports_streaming = True

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
        if voice.ref_wav and not (voice.ref_spk or voice.ref_rvq):
            cmd.extend(["--ref-wav", voice.ref_wav])
        if voice.ref_text:
            cmd.extend(["--ref-text", voice.ref_text])
        if voice.ref_spk:
            cmd.extend(["--ref-spk", voice.ref_spk])
        if voice.ref_rvq:
            cmd.extend(["--ref-rvq", voice.ref_rvq])
        cmd.extend(["--seed", str(voice.seed if voice.seed is not None else self.settings.qwentts_cpp_seed)])
        if self.settings.qwentts_cpp_max_new_frames > 0:
            cmd.extend(["--max-new", str(self.settings.qwentts_cpp_max_new_frames)])
        extra_args = voice.extra_args if voice.extra_args else self.settings.qwentts_cpp_extra_args
        if extra_args:
            cmd.extend(shlex.split(extra_args))
        cmd.extend(["-o", str(wav_path)])
        return cmd

    async def stream_pcm(self, text: str, *, voice: TtsVoice | None = None) -> AsyncIterator[bytes]:
        voice = voice or TtsVoice.from_settings(self.settings)
        async for chunk in self._stream_wav_stdout_pcm(text, voice=voice):
            yield chunk

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.qwentts_cpp_backend:
            env["GGML_BACKEND"] = self.settings.qwentts_cpp_backend
        return env

    def _timeout_seconds(self) -> float:
        return self.settings.qwentts_cpp_timeout_seconds


class QwenTtsEngine(QwenTtsCpp):
    pass


class OmniVoiceEngine(CommandWavTts):
    provider_name = "omnivoice.cpp"
    supports_streaming = True

    def __init__(self, settings: Settings):
        super().__init__(settings)
        required = {
            "OMNIVOICE_BIN": settings.omnivoice_bin,
            "OMNIVOICE_MODEL": settings.omnivoice_model,
            "OMNIVOICE_CODEC": settings.omnivoice_codec,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"{', '.join(missing)} are required for STS_TTS_PROVIDER=omnivoice"
            )
        self.bin_path = Path(settings.omnivoice_bin)
        if not self.bin_path.is_file():
            raise RuntimeError(f"OMNIVOICE_BIN does not exist: {self.bin_path}")
        for name, value in {
            "OMNIVOICE_MODEL": settings.omnivoice_model,
            "OMNIVOICE_CODEC": settings.omnivoice_codec,
        }.items():
            if not Path(value).is_file():
                raise RuntimeError(f"{name} does not exist: {value}")

    def _command(self, wav_path: Path, *, voice: TtsVoice | None = None) -> list[str]:
        voice = voice or TtsVoice.from_settings(self.settings)
        cmd = [
            str(self.bin_path),
            "--model",
            voice.model or self.settings.omnivoice_model,
            "--codec",
            voice.codec or self.settings.omnivoice_codec,
            "--format",
            voice.audio_format or self.settings.omnivoice_format,
        ]
        lang = voice.lang or self.settings.omnivoice_lang
        if lang:
            cmd.extend(["--lang", lang])
        if voice.instruct:
            cmd.extend(["--instruct", voice.instruct])
        omni_rvq = voice.omnivoice_ref_rvq or (voice.ref_rvq if voice.engine == "omnivoice" else "")
        if omni_rvq:
            cmd.extend(["--ref-rvq", omni_rvq])
        elif voice.ref_wav:
            cmd.extend(["--ref-wav", voice.ref_wav])
        if voice.ref_text:
            cmd.extend(["--ref-text", voice.ref_text])
        cmd.extend(["--seed", str(voice.seed if voice.seed is not None else self.settings.omnivoice_seed)])
        duration = voice.duration_seconds
        if duration is None:
            duration = self.settings.omnivoice_duration_seconds
        if duration and duration > 0:
            cmd.extend(["--duration", str(duration)])
        chunk_duration = voice.chunk_duration_seconds
        if chunk_duration is None:
            chunk_duration = self.settings.omnivoice_chunk_duration_seconds
        if chunk_duration is not None:
            cmd.extend(["--chunk-duration", str(chunk_duration)])
        chunk_threshold = voice.chunk_threshold_seconds
        if chunk_threshold is None:
            chunk_threshold = self.settings.omnivoice_chunk_threshold_seconds
        if chunk_threshold is not None:
            cmd.extend(["--chunk-threshold", str(chunk_threshold)])
        extra_args = voice.extra_args if voice.extra_args else self.settings.omnivoice_extra_args
        if extra_args:
            cmd.extend(shlex.split(extra_args))
        cmd.extend(["-o", str(wav_path)])
        return cmd

    async def stream_pcm(self, text: str, *, voice: TtsVoice | None = None) -> AsyncIterator[bytes]:
        voice = voice or TtsVoice.from_settings(self.settings)
        async for chunk in self._stream_wav_stdout_pcm(text, voice=voice):
            yield chunk

    def _environment(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.omnivoice_backend:
            env["GGML_BACKEND"] = self.settings.omnivoice_backend
        return env

    def _timeout_seconds(self) -> float:
        return self.settings.omnivoice_timeout_seconds


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


def _stdin_text(text: str) -> bytes:
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


class _StreamingWavPcm16Decoder:
    def __init__(self, *, target_rate: int, provider_name: str = "command"):
        self.target_rate = target_rate
        self.provider_name = provider_name
        self.buffer = bytearray()
        self.riff_seen = False
        self.in_data = False
        self.sample_rate = 0
        self.channels = 1
        self.bits_per_sample = 16
        self.data_pending = bytearray()
        self.resampler: _StreamingPcm16Resampler | None = None

    def feed(self, data: bytes) -> list[bytes]:
        if not data:
            return []
        self.buffer.extend(data)
        out: list[bytes] = []
        if not self.in_data:
            out.extend(self._parse_until_data())
        if self.in_data and self.buffer:
            out.extend(self._consume_data(bytes(self.buffer), final=False))
            self.buffer.clear()
        return out

    def flush(self) -> list[bytes]:
        out: list[bytes] = []
        if self.in_data and self.buffer:
            out.extend(self._consume_data(bytes(self.buffer), final=True))
            self.buffer.clear()
        if self.data_pending:
            out.extend(self._consume_data(b"", final=True))
        if self.resampler is not None:
            flushed = self.resampler.flush()
            if flushed:
                out.append(flushed)
        return out

    def _parse_until_data(self) -> list[bytes]:
        out: list[bytes] = []
        if not self.riff_seen:
            if len(self.buffer) < 12:
                return out
            if bytes(self.buffer[:4]) != b"RIFF" or bytes(self.buffer[8:12]) != b"WAVE":
                raise RuntimeError(f"{self.provider_name} stdout is not a WAV stream")
            del self.buffer[:12]
            self.riff_seen = True
        while len(self.buffer) >= 8:
            chunk_id = bytes(self.buffer[:4])
            chunk_size = int.from_bytes(self.buffer[4:8], "little", signed=False)
            if chunk_id == b"data":
                del self.buffer[:8]
                self.in_data = True
                self.resampler = _StreamingPcm16Resampler(
                    source_rate=self.sample_rate or self.target_rate,
                    target_rate=self.target_rate,
                )
                return out
            padded = chunk_size + (chunk_size % 2)
            if len(self.buffer) < 8 + padded:
                return out
            payload = bytes(self.buffer[8 : 8 + chunk_size])
            if chunk_id == b"fmt ":
                self._parse_fmt(payload)
            del self.buffer[: 8 + padded]
        return out

    def _parse_fmt(self, payload: bytes) -> None:
        if len(payload) < 16:
            raise RuntimeError(f"Invalid WAV fmt chunk from {self.provider_name}")
        audio_format = int.from_bytes(payload[0:2], "little")
        self.channels = int.from_bytes(payload[2:4], "little")
        self.sample_rate = int.from_bytes(payload[4:8], "little")
        self.bits_per_sample = int.from_bytes(payload[14:16], "little")
        if audio_format != 1 or self.bits_per_sample != 16 or self.channels <= 0:
            raise RuntimeError(
                f"Unsupported {self.provider_name} WAV format format={audio_format} "
                f"bits={self.bits_per_sample} channels={self.channels}"
            )

    def _consume_data(self, data: bytes, *, final: bool) -> list[bytes]:
        self.data_pending.extend(data)
        frame_bytes = max(2, self.channels * 2)
        complete = len(self.data_pending) - (len(self.data_pending) % frame_bytes)
        if complete <= 0:
            return []
        raw = bytes(self.data_pending[:complete])
        del self.data_pending[:complete]
        if self.channels == 1:
            mono = raw
        else:
            import numpy as np

            audio = np.frombuffer(raw, dtype=np.int16).reshape(-1, self.channels)
            mono = audio.mean(axis=1).astype(np.int16).tobytes()
        if self.resampler is None:
            self.resampler = _StreamingPcm16Resampler(
                source_rate=self.sample_rate or self.target_rate,
                target_rate=self.target_rate,
            )
        resampled = self.resampler.feed(mono)
        return [resampled] if resampled else []


class _StreamingPcm16Resampler:
    def __init__(self, *, source_rate: int, target_rate: int):
        self.source_rate = max(1, int(source_rate))
        self.target_rate = max(1, int(target_rate))
        self.start_index = 0
        self.next_target_index = 0
        self.samples = None

    def feed(self, pcm16: bytes) -> bytes:
        import numpy as np

        if not pcm16:
            return b""
        incoming = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
        if self.source_rate == self.target_rate:
            return incoming.astype(np.int16).tobytes()
        if self.samples is None or len(self.samples) == 0:
            self.samples = incoming
        else:
            self.samples = np.concatenate([self.samples, incoming])
        return self._drain(include_tail=False)

    def flush(self) -> bytes:
        if self.source_rate == self.target_rate:
            return b""
        return self._drain(include_tail=True)

    def _drain(self, *, include_tail: bool) -> bytes:
        import numpy as np

        if self.samples is None or len(self.samples) == 0:
            return b""
        out: list[float] = []
        max_source = self.start_index + len(self.samples) - 1
        while True:
            source_pos = self.next_target_index * self.source_rate / self.target_rate
            if include_tail:
                if source_pos > max_source:
                    break
            elif source_pos + 1 > max_source:
                break
            left = int(source_pos)
            frac = source_pos - left
            local = left - self.start_index
            if local < 0 or local >= len(self.samples):
                break
            if local + 1 < len(self.samples):
                sample = self.samples[local] * (1.0 - frac) + self.samples[local + 1] * frac
            else:
                sample = self.samples[local]
            out.append(float(sample))
            self.next_target_index += 1

        keep_from_source = int(self.next_target_index * self.source_rate / self.target_rate) - 1
        drop = max(0, keep_from_source - self.start_index)
        if drop > 0:
            self.samples = self.samples[drop:]
            self.start_index += drop
        if not out:
            return b""
        return np.clip(np.asarray(out, dtype=np.float32), -32768, 32767).astype(np.int16).tobytes()


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
        return QwenTtsEngine(settings)
    if provider in {"omnivoice", "omnivoice_cpp"}:
        return OmniVoiceEngine(settings)
    raise RuntimeError(f"Unsupported STS_TTS_PROVIDER={settings.tts_provider!r}")
