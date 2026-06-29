#!/usr/bin/env bash
set -euo pipefail

LAB_DIR="${QWENTTS_LAB_DIR:-/home/kamjin/projects/hermes-tts-lab}"
BIN="${QWENTTS_CPP_BIN:-$LAB_DIR/src/qwentts.cpp/build/qwen-tts}"
MODEL="${QWENTTS_CPP_MODEL:-$LAB_DIR/models/qwen-talker-1.7b-base-Q8_0.gguf}"
CODEC="${QWENTTS_CPP_CODEC:-$LAB_DIR/models/qwen-tokenizer-12hz-Q8_0.gguf}"
BACKEND="${QWENTTS_CPP_BACKEND:-Vulkan0}"
LANG="${QWENTTS_CPP_LANG:-Chinese}"
SAMPLES_DIR="$LAB_DIR/samples"
BENCH_DIR="$LAB_DIR/benchmarks"
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$BENCH_DIR/qwentts-$STAMP.log"

mkdir -p "$SAMPLES_DIR" "$BENCH_DIR"

if [[ ! -x "$BIN" ]]; then
  echo "Missing qwen-tts binary: $BIN" >&2
  exit 1
fi
if [[ ! -f "$MODEL" || ! -f "$CODEC" ]]; then
  echo "Missing GGUF model or codec under $LAB_DIR/models" >&2
  exit 1
fi

run_case() {
  local name="$1"
  local text="$2"
  local out="$SAMPLES_DIR/${name}.wav"
  {
    echo "===== $name ====="
    echo "$text"
    start_ns="$(date +%s%N)"
    /usr/bin/time -f "wall_seconds=%e max_rss_kb=%M" env GGML_BACKEND="$BACKEND" "$BIN" \
      --model "$MODEL" \
      --codec "$CODEC" \
      --format wav16 \
      --lang "$LANG" \
      -o "$out" <<<"$text"
    end_ns="$(date +%s%N)"
    python - "$out" "$start_ns" "$end_ns" <<'PY'
import sys, wave
path = sys.argv[1]
wall_seconds = (int(sys.argv[3]) - int(sys.argv[2])) / 1_000_000_000
with wave.open(path, "rb") as wf:
    raw = wf.readframes(wf.getnframes())
    width = wf.getsampwidth()
    audio_seconds = wf.getnframes() / wf.getframerate() if wf.getframerate() else 0.0
    if width == 2:
        peak = max((abs(int.from_bytes(raw[i:i + 2], "little", signed=True)) for i in range(0, len(raw), 2)), default=0)
    else:
        peak = 0
    rtf = wall_seconds / audio_seconds if audio_seconds else 0.0
    print(f"wav={path} rate={wf.getframerate()} channels={wf.getnchannels()} width={width} frames={wf.getnframes()} audio_seconds={audio_seconds:.2f} rtf={rtf:.2f} peak={peak}")
PY
  } 2>&1 | tee -a "$LOG"
}

run_case short_cn "你好，我在。今天我们可以继续测试本地语音。"
run_case medium_cn "我已经切换到新的本地语音候选。接下来会优先比较首字延迟、整体速度和中文自然度，再决定是否替换默认声音。"
run_case mixed_cn_en "这个版本会在 Fedora 和 AMD Vulkan 上运行，目标是 low latency、natural voice，以及更容易维护的 C++ runtime。"
run_case punctuation_cn "等一下，我先确认三件事：模型是否加载成功？声音是否自然？延迟能不能接受！如果都可以，我们就继续。"

echo "Benchmark log: $LOG"
