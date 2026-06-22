import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { motion } from "framer-motion";
import {
  Activity,
  AudioLines,
  Bot,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronDown,
  ChevronRight,
  Clock3,
  Cpu,
  Download,
  Gauge,
  KeyRound,
  LoaderCircle,
  Maximize2,
  Mic2,
  Minimize2,
  Music2,
  Play,
  Plus,
  RefreshCw,
  Save,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Shuffle,
  Trash2,
  Upload,
  Wand2,
} from "lucide-react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "./styles.css";

type SettingsValues = Record<string, any>;
type Persona = { id: string; name: string; prompt: string; voice_mode: string; voice_ref: string };
type VoiceProfile = Record<string, any>;
type ModelStatus = Record<string, { path: string; installed: boolean }>;
type Metric = { id: number; kind: string; value: Record<string, any>; created_at: number };
type AdminState = {
  health: Record<string, any>;
  settings: { values: SettingsValues; groups: Record<string, string[]>; raw: SettingsValues };
  setup: { complete: boolean; env_imported: boolean };
  runtime?: { started_at: number; uptime_seconds: number };
  personas: Persona[];
  voices: VoiceProfile[];
  qwen: { speakers: string[]; models: ModelStatus; modes: string[] };
  kokoro_voices: Array<{ id: number; name: string; note: string }>;
  metrics: Metric[];
};

const navItems = [
  { id: "dashboard", label: "总览", icon: Activity },
  { id: "studio", label: "角色声线", icon: AudioLines },
  { id: "setup", label: "首次设置", icon: Wand2 },
  { id: "advanced", label: "高级", icon: SlidersHorizontal },
] as const;

const modeLabels: Record<string, { title: string; text: string }> = {
  default: { title: "默认音色", text: "Base 模型自带音色，最稳，当前已可用。" },
  preset: { title: "预设声线", text: "CustomVoice 模型的官方 9 个 speaker。" },
  design: { title: "描述造声", text: "VoiceDesign 模型按文字描述生成气质。" },
  clone: { title: "克隆音色", text: "上传参考 WAV 和文本，预编码后复用。" },
};

const speakerNames: Record<string, string> = {
  serena: "Serena",
  vivian: "Vivian",
  uncle_fu: "Uncle Fu",
  ryan: "Ryan",
  aiden: "Aiden",
  ono_anna: "Ono Anna",
  sohee: "Sohee",
  eric: "Eric",
  dylan: "Dylan",
};

const waveStyles = [
  { id: "bars", name: "脉冲条" },
  { id: "halo", name: "声场环" },
  { id: "scanner", name: "扫描线" },
  { id: "minimal", name: "细线谱" },
] as const;

function App() {
  const [state, setState] = useState<AdminState | null>(null);
  const [tab, setTab] = useState<(typeof navItems)[number]["id"]>("dashboard");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");

  const load = async () => {
    const data = await api<AdminState>("/api/admin/state");
    setState(data);
  };

  useEffect(() => {
    load().catch((err) => setNotice(String(err)));
    const timer = window.setInterval(() => load().catch(() => undefined), 15000);
    return () => window.clearInterval(timer);
  }, []);

  const patch = async (values: SettingsValues) => {
    setBusy("saving");
    const data = await api<{ state: AdminState }>("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ values }),
    });
    setState(data.state);
    setBusy("");
    setNotice("已保存并热更新");
    window.setTimeout(() => setNotice(""), 1800);
  };

  if (!state) {
    return (
      <main className="boot">
        <div className="boot-mark"><AudioLines /></div>
        <p>正在连接 Hermes STS 控制台...</p>
      </main>
    );
  }

  const ActiveIcon = navItems.find((item) => item.id === tab)?.icon ?? Activity;

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <div className="brand-mark"><Sparkles size={20} /></div>
          <div>
            <strong>Hermes STS</strong>
            <span>Personal voice cockpit</span>
          </div>
        </div>
        <nav>
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>
                <span className="nav-icon"><Icon size={18} /></span>
                <span className="nav-label">{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="side-foot">
          <StatusDot ok={state.health.status === "ok"} />
          <span>{state.health.tts_provider === "qwen3tts" ? "Qwen3TTS" : "Kokoro"} online</span>
        </div>
      </aside>

      <main className="main">
        <header className="top">
          <div>
            <span className="eyebrow"><ActiveIcon size={15} /> {navItems.find((item) => item.id === tab)?.label}</span>
            <h1>{headlineFor(tab)}</h1>
          </div>
          <div className="top-actions">
            {notice && <span className="notice">{notice}</span>}
            <button className="icon-btn" onClick={() => load()} title="刷新"><RefreshCw size={18} /></button>
          </div>
        </header>

        {tab === "dashboard" && <Dashboard state={state} patch={patch} goStudio={() => setTab("studio")} />}
        {tab === "studio" && <Studio state={state} patch={patch} reload={load} busy={busy} setBusy={setBusy} setNotice={setNotice} goSetup={() => setTab("setup")} />}
        {tab === "setup" && <Setup state={state} patch={patch} reload={load} setBusy={setBusy} setNotice={setNotice} />}
        {tab === "advanced" && <Advanced state={state} patch={patch} busy={busy} />}
      </main>
    </div>
  );
}

function Dashboard({ state, patch, goStudio }: { state: AdminState; patch: (v: SettingsValues) => Promise<void>; goStudio: () => void }) {
  const metrics = useMemo(() => chartMetrics(state.metrics), [state.metrics]);
  const latest = state.metrics.find((item) => item.kind === "tts_preview")?.value;
  const turns = useMemo(() => turnStats(state.metrics), [state.metrics]);
  const currentPersona = state.personas.find((p) => p.id === state.settings.values.sts_persona_preset);
  const qwenReady = Object.values(state.qwen.models).filter((m) => m.installed).length;
  const [fullscreen, setFullscreen] = useState(false);
  const musicInputRef = useRef<HTMLInputElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [tracks, setTracks] = useState<Array<{ name: string; url: string }>>([]);
  const [trackIndex, setTrackIndex] = useState(0);
  const [musicPlaying, setMusicPlaying] = useState(false);
  const previewEvents = state.metrics.filter((item) => item.kind === "tts_preview").length;
  const waveStyle = state.settings.values.dashboard_wave_style || "bars";
  const waveIndex = Math.max(0, waveStyles.findIndex((item) => item.id === waveStyle));
  const switchWave = (direction: number) => {
    const next = waveStyles[(waveIndex + direction + waveStyles.length) % waveStyles.length];
    patch({ dashboard_wave_style: next.id });
  };
  const pickTracks = (files: FileList | null) => {
    if (!files?.length) return;
    setTracks(Array.from(files).map((file) => ({ name: file.name.replace(/\.[^.]+$/, ""), url: URL.createObjectURL(file) })));
    setTrackIndex(0);
    setMusicPlaying(false);
  };
  const toggleMusic = async () => {
    if (!tracks.length) {
      musicInputRef.current?.click();
      return;
    }
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      await audio.play();
      setMusicPlaying(true);
    } else {
      audio.pause();
      setMusicPlaying(false);
    }
  };
  const nextTrack = () => {
    if (!tracks.length) return;
    setTrackIndex((index) => (index + 1) % tracks.length);
    setMusicPlaying(false);
  };
  return (
    <div className={fullscreen ? "cockpit fullscreen" : "cockpit"}>
      <motion.section className="cockpit-hero panel" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}>
        <div className="cockpit-copy">
          <span className="eyebrow"><Bot size={15} /> Voice cockpit</span>
          <h2>{currentPersona?.name ?? "自定义角色"}</h2>
          <p>{state.health.persona_prompt}</p>
          <div className="chips">
            <Chip icon={<Mic2 size={14} />} label={voiceLabel(state)} />
            <Chip icon={<Cpu size={14} />} label={state.health.tts_provider === "qwen3tts" ? `Qwen3TTS · ${state.health.qwen_backend || "CPU"}` : "Kokoro 回退"} />
            <Chip icon={<Gauge size={14} />} label={latest?.rtf ? `RTF ${Number(latest.rtf).toFixed(2)}` : "等待试听数据"} />
          </div>
        </div>
        <div className="cockpit-visual">
          <button className="icon-btn cockpit-max" onClick={() => setFullscreen((v) => !v)} title={fullscreen ? "退出全屏视图" : "最大化驾驶舱"}>
            {fullscreen ? <Minimize2 size={18} /> : <Maximize2 size={18} />}
          </button>
          <WaveMeter variant={waveStyle} />
          <div className="wave-switcher">
            <button className="icon-btn" onClick={() => switchWave(-1)} title="上一种波形"><ChevronLeft size={18} /></button>
            <strong>{waveStyles[waveIndex]?.name ?? "脉冲条"}</strong>
            <button className="icon-btn" onClick={() => switchWave(1)} title="下一种波形"><ChevronRight size={18} /></button>
          </div>
        </div>
      </motion.section>

      <section className="stat-strip">
        <Kpi label="已运行" value={formatDuration(state.runtime?.uptime_seconds ?? 0)} hint="本次服务启动后" />
        <Kpi label="对话回合" value={String(turns.total)} hint={`${turns.completed} 完成 / ${turns.cancelled} 取消`} />
        <Kpi label="首声均值" value={turns.avgFirstAudio ? `${turns.avgFirstAudio}ms` : "--"} hint="真实 turn 到首个音频包" />
        <Kpi label="Qwen 模型" value={`${qwenReady}/4`} hint={`${state.health.sample_rate / 1000} kHz 实时输出`} />
      </section>

      <section className="panel signal-board">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Activity size={15} /> Recent signal</span>
            <h2>延迟轨迹</h2>
          </div>
          <span className="subtle">{latest?.elapsed_ms ? `${latest.elapsed_ms}ms last` : "no samples"}</span>
        </div>
        <ResponsiveContainer width="100%" height={210}>
          <AreaChart data={metrics}>
            <defs>
              <linearGradient id="latencyFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="#7be7d8" stopOpacity={0.82} />
                <stop offset="95%" stopColor="#d99545" stopOpacity={0.05} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="rgba(255,255,255,.08)" vertical={false} />
            <XAxis dataKey="label" tick={{ fill: "#9aa8a1", fontSize: 11 }} tickLine={false} axisLine={false} />
            <YAxis tick={{ fill: "#9aa8a1", fontSize: 11 }} tickLine={false} axisLine={false} width={38} />
            <Tooltip contentStyle={{ background: "#111713", border: "1px solid #33443d", color: "#eef4e8" }} />
            <Area type="monotone" dataKey="elapsed" stroke="#7be7d8" fill="url(#latencyFill)" strokeWidth={2} />
          </AreaChart>
        </ResponsiveContainer>
      </section>

      <section className="quick-actions">
        <button className="action-card" onClick={goStudio}>
          <AudioLines size={21} />
          <strong>调角色声线</strong>
          <span>音色工坊、A/B seed、完整角色</span>
        </button>
        <div className="music-dock">
          <div>
            <span className="eyebrow"><Music2 size={15} /> Music bay</span>
            <strong>{tracks[trackIndex]?.name || "本地音乐"}</strong>
            <span>{tracks.length ? `${trackIndex + 1}/${tracks.length}` : "选择音频文件后在本页播放"}</span>
          </div>
          <div className="music-controls">
            <button className="icon-btn" onClick={() => musicInputRef.current?.click()} title="选择音乐"><Plus size={17} /></button>
            <button className="icon-btn" onClick={toggleMusic} title={musicPlaying ? "暂停" : "播放"}><Play size={17} /></button>
            <button className="icon-btn" onClick={nextTrack} title="下一首"><ChevronRight size={17} /></button>
          </div>
          <input ref={musicInputRef} hidden type="file" accept="audio/*" multiple onChange={(event) => pickTracks(event.target.files)} />
          {tracks[trackIndex] && <audio ref={audioRef} src={tracks[trackIndex].url} onEnded={nextTrack} onPause={() => setMusicPlaying(false)} onPlay={() => setMusicPlaying(true)} />}
        </div>
        <button className="action-card muted-action" onClick={() => setFullscreen(true)}>
          <Clock3 size={21} />
          <strong>近期节奏</strong>
          <span>首声、取消、试听事件 {previewEvents}</span>
        </button>
      </section>
    </div>
  );
}

function Studio({
  state,
  patch,
  reload,
  busy,
  setBusy,
  setNotice,
  goSetup,
}: {
  state: AdminState;
  patch: (v: SettingsValues) => Promise<void>;
  reload: () => Promise<void>;
  busy: string;
  setBusy: (v: string) => void;
  setNotice: (v: string) => void;
  goSetup: () => void;
}) {
  const values = state.settings.values;
  const activePersona = state.personas.find((p) => p.id === values.sts_persona_preset) ?? state.personas[0];
  const [personaId, setPersonaId] = useState(activePersona?.id ?? "operator");
  const [personaName, setPersonaName] = useState(activePersona?.name ?? "自定义人格");
  const [prompt, setPrompt] = useState(values.sts_persona_custom || activePersona?.prompt || "");
  const [previewText, setPreviewText] = useState("你好，我是 Hermes STS。现在用当前角色和音色说话。");
  const [audioUrl, setAudioUrl] = useState("");

  useEffect(() => {
    const persona = state.personas.find((p) => p.id === values.sts_persona_preset) ?? state.personas[0];
    setPersonaId(persona?.id ?? "operator");
    setPersonaName(persona?.name ?? "自定义人格");
    setPrompt(values.sts_persona_custom || persona?.prompt || "");
  }, [state.personas, values.sts_persona_custom, values.sts_persona_preset]);

  const selectPersona = (persona: Persona) => {
    setPersonaId(persona.id);
    setPersonaName(persona.name);
    setPrompt(persona.prompt);
  };

  const personaPayload = (apply: boolean) => {
    const id = personaId === "custom" || !state.personas.some((p) => p.id === personaId) ? `custom_${Date.now()}` : personaId;
    return {
      id,
      name: personaName || "自定义人格",
      prompt,
      voice_mode: values.qwentts_cpp_voice_mode || "default",
      voice_ref: voiceRefForMode(values),
      apply,
    };
  };

  const savePersona = async () => {
    const payload = personaPayload(false);
    await api("/api/personas", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setPersonaId(payload.id);
    await reload();
    setNotice("人格预设已保存");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const applyPersona = async () => {
    const payload = personaPayload(true);
    await api("/api/personas", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setPersonaId(payload.id);
    await reload();
    setNotice("人格和声线已应用");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const deletePersona = async (persona: Persona) => {
    if (state.personas.length <= 1) {
      setNotice("至少保留一个人格");
      window.setTimeout(() => setNotice(""), 1800);
      return;
    }
    const ok = window.confirm(`删除人格“${persona.name}”？删除后不会再自动出现在列表里。`);
    if (!ok) return;
    const data = await api<{ state: AdminState }>(`/api/personas/${encodeURIComponent(persona.id)}`, {
      method: "DELETE",
    });
    if (personaId === persona.id) {
      const nextPersona = data.state.personas.find((p) => p.id === data.state.settings.values.sts_persona_preset) ?? data.state.personas[0];
      setPersonaId(nextPersona?.id ?? "custom");
      setPersonaName(nextPersona?.name ?? "自定义人格");
      setPrompt(data.state.settings.values.sts_persona_custom || nextPersona?.prompt || "");
    }
    await reload();
    setNotice("人格已删除");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const newPersona = () => {
    setPersonaId("custom");
    setPersonaName("新人格");
    setPrompt("你是 Hermes 的语音助手。保持回答自然、简洁、有分寸，先理解用户意图，再给出清晰可执行的回应。");
  };

  const preview = async () => {
    setBusy("preview");
    const data = await api<{ audio_wav_base64: string; elapsed_ms: number }>("/api/tts/preview", {
      method: "POST",
      body: JSON.stringify({ text: previewText }),
    });
    const blob = base64ToBlob(data.audio_wav_base64, "audio/wav");
    setAudioUrl(URL.createObjectURL(blob));
    setBusy("");
    setNotice(`试听完成 ${data.elapsed_ms}ms`);
    await reload();
  };

  const onPromptChange = (next: string) => {
    setPrompt(next);
    if (personaId !== "custom") {
      setPersonaId("custom");
      setPersonaName("自定义人格");
    }
  };

  return (
    <div className="grid studio">
      <section className="panel span-5">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Bot size={15} /> 人格</span>
            <h2>人格预设</h2>
          </div>
          <button className="secondary" onClick={newPersona}><Plus size={16} />新增</button>
        </div>
        <p className="muted">点选只会装载到编辑框，确认后再应用到实时助手。</p>
        <div className="persona-list">
          {state.personas.map((persona) => (
            <div key={persona.id} className={personaId === persona.id ? "persona selected" : "persona"}>
              <button className="persona-main" onClick={() => selectPersona(persona)}>
                <strong>{persona.name}</strong>
                <span>{persona.voice_mode === "default" ? "默认音色" : modeLabels[persona.voice_mode]?.title}</span>
              </button>
              <button className="persona-delete" onClick={() => deletePersona(persona)} title="删除人格">
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      </section>

      <section className="panel span-7">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Sparkles size={15} /> 角色与提示词</span>
            <h2>{personaName}</h2>
          </div>
          <div className="persona-actions">
            <button className="secondary" onClick={savePersona}><Save size={16} />保存预设</button>
            <button className="primary" onClick={applyPersona}><CheckCircle2 size={16} />应用</button>
          </div>
        </div>
        <label className="field">
          <span>角色名称</span>
          <input value={personaName} onChange={(e) => { setPersonaName(e.target.value); setPersonaId("custom"); }} />
        </label>
        <label className="field">
          <span>完整提示词</span>
          <textarea className="prompt-box" value={prompt} onChange={(e) => onPromptChange(e.target.value)} />
        </label>
        <div className="switch-line">
          <span>人格来源</span>
          <SwitchControl checked={values.sts_persona_source !== "ws"} onChange={(checked) => patch({ sts_persona_source: checked ? "settings" : "ws" })} onLabel="界面控制" offLabel="跟随 Reachy Profile" />
        </div>
      </section>

      <section className="panel span-12">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><AudioLines size={15} /> 声线</span>
            <h2>引擎与音色</h2>
          </div>
          <div className="segmented compact">
            <button className={values.tts_provider === "qwen3tts" ? "selected" : ""} onClick={() => patch({ tts_provider: "qwen3tts" })}>Qwen3TTS</button>
            <button className={values.tts_provider === "sherpa_kokoro" ? "selected" : ""} onClick={() => patch({ tts_provider: "sherpa_kokoro" })}>Kokoro</button>
          </div>
        </div>
        {values.tts_provider === "qwen3tts" ? (
          <QwenVoice state={state} patch={patch} reload={reload} busy={busy} setBusy={setBusy} setNotice={setNotice} goSetup={goSetup} />
        ) : (
          <KokoroVoice state={state} patch={patch} />
        )}
      </section>

      <section className="panel span-12">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Play size={15} /> 试听</span>
            <h2>实时确认当前角色和声线</h2>
          </div>
          <button className="primary" onClick={preview} disabled={busy === "preview"}><Play size={16} />{busy === "preview" ? "生成中" : "生成试听"}</button>
        </div>
        <textarea className="preview-text" value={previewText} onChange={(e) => setPreviewText(e.target.value)} />
        {audioUrl && <audio controls src={audioUrl} className="audio" autoPlay />}
      </section>
    </div>
  );
}

function QwenVoice({
  state,
  patch,
  reload,
  busy,
  setBusy,
  setNotice,
  goSetup,
}: {
  state: AdminState;
  patch: (v: SettingsValues) => Promise<void>;
  reload: () => Promise<void>;
  busy: string;
  setBusy: (v: string) => void;
  setNotice: (v: string) => void;
  goSetup: () => void;
}) {
  const values = state.settings.values;
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [cloneName, setCloneName] = useState("我的克隆音色");
  const [cloneText, setCloneText] = useState("");
  const [voicePreviewUrl, setVoicePreviewUrl] = useState("");
  const [lastRandomSeed, setLastRandomSeed] = useState<number | null>(null);
  const [randomName, setRandomName] = useState("收藏声线");
  const [randomTags, setRandomTags] = useState("沉稳,清晰");
  const [tagFilter, setTagFilter] = useState("");
  const [seedBatch, setSeedBatch] = useState<number[]>([]);
  const [queuedSeeds, setQueuedSeeds] = useState<number[]>([]);
  const [workshopBrief, setWorkshopBrief] = useState("冷静、清晰、有一点未来感，适合长期陪伴的中文语音助手");
  const [workshopSuggestion, setWorkshopSuggestion] = useState<Record<string, any> | null>(null);
  const customVoiceReady = Boolean(state.qwen.models.customvoice?.installed);
  const voiceDesignReady = Boolean(state.qwen.models.voicedesign?.installed);
  const allSeedVoices = state.voices.filter((voice) => voice.mode === "seed");
  const seedTags = Array.from(new Set(allSeedVoices.flatMap((voice) => splitTags(voice.tags))));
  const seedVoices = tagFilter ? allSeedVoices.filter((voice) => splitTags(voice.tags).includes(tagFilter)) : allSeedVoices;

  const previewVoice = async (payload: Record<string, any>, message = "音色试听完成") => {
    setBusy("voice-preview");
    const data = await api<{ audio_wav_base64: string; elapsed_ms: number; seed?: number }>("/api/tts/preview", {
      method: "POST",
      body: JSON.stringify({
        text: "你好，我是 Hermes。现在用这条声线做一次短试听。",
        ...payload,
      }),
    });
    setVoicePreviewUrl(URL.createObjectURL(base64ToBlob(data.audio_wav_base64, "audio/wav")));
    setBusy("");
    setNotice(`${message} ${data.elapsed_ms}ms`);
    window.setTimeout(() => setNotice(""), 1800);
    await reload();
    return data;
  };

  const randomPreview = async () => {
    const seed = Math.floor(Math.random() * 2147483647);
    setLastRandomSeed(seed);
    await previewVoice({ voice_mode: "default", seed }, `随机声线 seed ${seed}`);
  };

  const keepRandomSeed = async () => {
    if (lastRandomSeed == null) {
      setNotice("先随机试听一次");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    await api("/api/qwen/voices/seed", {
      method: "POST",
      body: JSON.stringify({ name: randomName || `Seed ${lastRandomSeed}`, seed: lastRandomSeed, tags: splitTags(randomTags) }),
    });
    await patch({ qwentts_cpp_voice_mode: "default", qwentts_cpp_seed: lastRandomSeed });
    await reload();
    setNotice(`已收藏并使用 seed ${lastRandomSeed}`);
    window.setTimeout(() => setNotice(""), 1800);
  };

  const keepSeed = async (seed: number, name = `Seed ${seed}`) => {
    await api("/api/qwen/voices/seed", {
      method: "POST",
      body: JSON.stringify({ name, seed, tags: splitTags(randomTags) }),
    });
    await reload();
    setNotice(`已收藏 seed ${seed}`);
    window.setTimeout(() => setNotice(""), 1800);
  };

  const generateSeedBatch = () => {
    setSeedBatch(Array.from({ length: 5 }, () => Math.floor(Math.random() * 2147483647)));
  };

  const playSeedBatch = async () => {
    const seeds = seedBatch.length ? seedBatch : Array.from({ length: 5 }, () => Math.floor(Math.random() * 2147483647));
    setSeedBatch(seeds);
    setQueuedSeeds(seeds.slice(1));
    await previewVoice({ voice_mode: "default", seed: seeds[0] }, `A/B seed ${seeds[0]}`);
  };

  const continueSeedQueue = async () => {
    if (!queuedSeeds.length) return;
    const [next, ...rest] = queuedSeeds;
    setQueuedSeeds(rest);
    await previewVoice({ voice_mode: "default", seed: next }, `A/B seed ${next}`);
  };

  const applyVoiceProfile = async (voiceId: string) => {
    const data = await api<{ state: AdminState }>("/api/qwen/voices/apply", {
      method: "POST",
      body: JSON.stringify({ voice_id: voiceId }),
    });
    setNotice("收藏声线已启用");
    window.setTimeout(() => setNotice(""), 1800);
    await reload();
    return data;
  };

  const deleteVoiceProfile = async (voice: VoiceProfile) => {
    if (!window.confirm(`删除「${voice.name}」？删除后不会再出现在收藏声线里。`)) return;
    await api(`/api/qwen/voices/${encodeURIComponent(voice.id)}`, { method: "DELETE" });
    setNotice("收藏声线已删除");
    window.setTimeout(() => setNotice(""), 1800);
    await reload();
  };

  const suggestVoice = async () => {
    setBusy("workshop");
    const data = await api<{ suggestion: Record<string, any> }>("/api/qwen/workshop/suggest", {
      method: "POST",
      body: JSON.stringify({ brief: workshopBrief, persona_hint: state.health.persona_prompt || "" }),
    });
    setWorkshopSuggestion(data.suggestion);
    setBusy("");
    setNotice("AI 已生成音色方案");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const applySuggestion = async () => {
    if (!workshopSuggestion) return;
    const valuesToPatch: SettingsValues = {
      qwentts_cpp_voice_mode: workshopSuggestion.voice_mode || "design",
      qwentts_cpp_seed: Number(workshopSuggestion.seed || 42),
    };
    if (valuesToPatch.qwentts_cpp_voice_mode === "design") {
      valuesToPatch.qwentts_cpp_voice_design = workshopSuggestion.design_prompt || "";
    }
    await patch(valuesToPatch);
    if (workshopSuggestion.persona_prompt) {
      setNotice("音色方案已应用，提示词可复制到人格里微调");
    }
  };

  const saveSuggestionAsPersona = async () => {
    if (!workshopSuggestion) return;
    const mode = workshopSuggestion.voice_mode || "design";
    await api("/api/personas", {
      method: "POST",
      body: JSON.stringify({
        id: `ai_${Date.now()}`,
        name: workshopSuggestion.name || "AI 角色",
        prompt: workshopSuggestion.persona_prompt || workshopBrief,
        voice_mode: mode,
        voice_ref: mode === "design" ? workshopSuggestion.design_prompt || "" : "qwen-default",
        apply: true,
      }),
    });
    if (mode === "default") {
      await patch({ qwentts_cpp_voice_mode: "default", qwentts_cpp_seed: Number(workshopSuggestion.seed || 42) });
    }
    await reload();
    setNotice("完整角色已保存并应用");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const uploadClone = async () => {
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setNotice("请先选择参考 WAV");
      return;
    }
    setBusy("upload");
    const form = new FormData();
    form.append("name", cloneName);
    form.append("reference_text", cloneText);
    form.append("file", file);
    const uploaded = await api<{ voice: VoiceProfile }>("/api/qwen/clone/upload", { method: "POST", body: form, raw: true });
    await api("/api/qwen/clone/encode", {
      method: "POST",
      body: JSON.stringify({ voice_id: uploaded.voice.id }),
    });
    setBusy("");
    setNotice("克隆音色已预编码并设为当前音色");
    await reload();
  };

  return (
    <div className="voice-layout">
      <div>
        <div className="mode-grid">
          {state.qwen.modes.map((mode) => (
            <button key={mode} className={values.qwentts_cpp_voice_mode === mode ? "mode selected" : "mode"} onClick={() => patch({ qwentts_cpp_voice_mode: mode })}>
              <strong>{modeLabels[mode]?.title ?? mode}</strong>
              <span>{modeLabels[mode]?.text}</span>
            </button>
          ))}
        </div>
        {values.qwentts_cpp_voice_mode === "preset" && (
          <div className="preset-panel">
            {!customVoiceReady && (
              <div className="missing-model">
                <span>预设声线需要 CustomVoice 模型。</span>
                <button className="link-btn" onClick={goSetup}>去模型设置</button>
              </div>
            )}
            <label className="field">
              <span>预设 speaker</span>
              <div className="select-wrap">
                <select value={values.qwentts_cpp_voice_preset || "vivian"} onChange={(e) => patch({ qwentts_cpp_voice_preset: e.target.value })}>
                  {state.qwen.speakers.map((speaker) => <option key={speaker} value={speaker}>{speakerNames[speaker] ?? speaker}</option>)}
                </select>
                <ChevronDown size={16} />
              </div>
            </label>
            <div className="speaker-grid">
              {state.qwen.speakers.map((speaker) => (
                <div key={speaker} className={values.qwentts_cpp_voice_preset === speaker ? "speaker-card selected" : "speaker-card"}>
                  <strong>{speakerNames[speaker] ?? speaker}</strong>
                  <span>{speaker}</span>
                  <div>
                    <button className="secondary" disabled={!customVoiceReady} onClick={() => previewVoice({ voice_mode: "preset", speaker }, `${speakerNames[speaker] ?? speaker} 试听完成`)}>
                      <Play size={15} />试听
                    </button>
                    <button className="primary" onClick={() => patch({ qwentts_cpp_voice_mode: "preset", qwentts_cpp_voice_preset: speaker })}>
                      使用
                    </button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
        {values.qwentts_cpp_voice_mode === "default" && (
          <div className="random-voice">
            <div>
              <span className="eyebrow"><Shuffle size={15} /> Seed audition</span>
              <h3>随机探索默认声线</h3>
              <p className="muted">每次生成一个可保存 seed。听到喜欢的，点“留下这个声线”。</p>
            </div>
            <div className="random-actions">
              <button className="secondary" onClick={randomPreview} disabled={false}><Shuffle size={16} />随机试听</button>
              <button className="primary" onClick={keepRandomSeed} disabled={lastRandomSeed == null}>留下这个声线</button>
            </div>
            <input value={randomName} onChange={(event) => setRandomName(event.target.value)} placeholder="给这条声线起个名字" />
            <input value={randomTags} onChange={(event) => setRandomTags(event.target.value)} placeholder="标签，用逗号分隔，例如 沉稳,清晰" />
            <code>{lastRandomSeed == null ? `当前固定 seed: ${values.qwentts_cpp_seed ?? 42}` : `刚试听 seed: ${lastRandomSeed}`}</code>
            <div className="ab-rack">
              <div>
                <strong>A/B seed deck</strong>
                <span>一次生成 5 条候选，逐个听，喜欢就收藏。</span>
              </div>
              <div className="random-actions">
                <button className="secondary" onClick={generateSeedBatch}><Shuffle size={16} />生成候选</button>
                <button className="secondary" onClick={playSeedBatch}><Play size={16} />连续试听</button>
              </div>
              <div className="seed-grid">
                {(seedBatch.length ? seedBatch : [0, 1, 2, 3, 4]).map((seed, index) => (
                  <div className="seed-card" key={`${seed}-${index}`}>
                    <span>{seed ? `seed ${seed}` : `slot ${index + 1}`}</span>
                    <button className="icon-btn" disabled={!seed} onClick={() => previewVoice({ voice_mode: "default", seed }, `seed ${seed}`)} title="试听"><Play size={15} /></button>
                    <button className="icon-btn" disabled={!seed} onClick={() => keepSeed(seed)} title="收藏"><Save size={15} /></button>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
        {values.qwentts_cpp_voice_mode === "design" && (
          <label className="field">
            {!voiceDesignReady && (
              <div className="missing-model">
                <span>描述造声需要 VoiceDesign 模型。</span>
                <button className="link-btn" onClick={goSetup}>去模型设置</button>
              </div>
            )}
            <span>音色描述</span>
            <textarea value={values.qwentts_cpp_voice_design || ""} onChange={(e) => patch({ qwentts_cpp_voice_design: e.target.value })} placeholder="例如：female, young adult, sweet voice, taiwan mandarin accent" />
          </label>
        )}
        {values.qwentts_cpp_voice_mode === "clone" && (
          <div className="clone-box">
            <div className="field-row">
              <label className="field">
                <span>音色名称</span>
                <input value={cloneName} onChange={(e) => setCloneName(e.target.value)} />
              </label>
              <label className="field">
                <span>参考 WAV</span>
                <input ref={fileRef} type="file" accept=".wav,audio/wav" />
              </label>
            </div>
            <label className="field">
              <span>参考文本</span>
              <textarea value={cloneText} onChange={(e) => setCloneText(e.target.value)} placeholder="参考音频里说的原文。越准确，克隆越稳。" />
            </label>
            <button className="primary" onClick={uploadClone}><Upload size={16} />上传并预编码</button>
            <div className="voice-pills">
              {state.voices.filter((v) => v.mode === "clone").map((voice) => (
                <button key={voice.id} className={values.qwentts_cpp_clone_voice_id === voice.id ? "pill selected" : "pill"} onClick={() => patch({ qwentts_cpp_voice_mode: "clone", qwentts_cpp_clone_voice_id: voice.id })}>
                  {voice.name}{voice.ref_spk ? " · ready" : " · 未预编码"}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      <div className="voice-side">
        <div className="workshop-box">
          <span className="eyebrow"><Wand2 size={15} /> 音色工坊</span>
          <h3>让 AI 先设计一版</h3>
          <textarea value={workshopBrief} onChange={(e) => setWorkshopBrief(e.target.value)} />
          <button className="primary" onClick={suggestVoice} disabled={false}>{busy === "workshop" ? "生成中" : "生成方案"}</button>
          {workshopSuggestion && (
            <div className="suggestion-card">
              <strong>{workshopSuggestion.name}</strong>
              <span>{workshopSuggestion.voice_mode === "design" ? "描述造声" : "默认声线 seed"}</span>
              {workshopSuggestion.design_prompt && <code>{workshopSuggestion.design_prompt}</code>}
              <em>seed {workshopSuggestion.seed}</em>
              {workshopSuggestion.notes && <p>{workshopSuggestion.notes}</p>}
              {workshopSuggestion.persona_prompt && <textarea readOnly value={workshopSuggestion.persona_prompt} />}
              <button className="secondary" onClick={() => previewVoice({
                voice_mode: workshopSuggestion.voice_mode,
                design_prompt: workshopSuggestion.design_prompt,
                seed: workshopSuggestion.seed,
                text: workshopSuggestion.preview_text,
              }, "AI 方案试听完成")}><Play size={15} />试听方案</button>
              <button className="primary" onClick={applySuggestion}>应用方案</button>
              <button className="primary" onClick={saveSuggestionAsPersona}>保存成完整角色</button>
            </div>
          )}
        </div>
        {seedVoices.length > 0 && (
          <div className="saved-voices">
            <span className="eyebrow"><Shuffle size={15} /> 收藏声线</span>
            {seedTags.length > 0 && (
              <div className="tag-filter">
                <button className={tagFilter === "" ? "pill selected" : "pill"} onClick={() => setTagFilter("")}>全部</button>
                {seedTags.map((tag) => (
                  <button key={tag} className={tagFilter === tag ? "pill selected" : "pill"} onClick={() => setTagFilter(tag)}>{tag}</button>
                ))}
              </div>
            )}
            {seedVoices.map((voice) => (
              <div className="saved-voice" key={voice.id}>
                <div>
                  <strong>{voice.name}</strong>
                  <span>seed {voice.seed}{voice.tags ? ` · ${voice.tags}` : ""}</span>
                </div>
                <button className="icon-btn" onClick={() => applyVoiceProfile(voice.id)} title="使用"><Check size={16} /></button>
                <button className="icon-btn danger" onClick={() => deleteVoiceProfile(voice)} title="删除"><Trash2 size={16} /></button>
              </div>
            ))}
          </div>
        )}
        <h3>Qwen3TTS 状态</h3>
        <div className="tiny-models">
          {Object.entries(state.qwen.models).map(([key, model]) => (
            <div className="check-line" key={key}>
              <StatusDot ok={model.installed} />
              <span>{modelName(key)}</span>
              <em>{model.installed ? "可用" : "未安装"}</em>
            </div>
          ))}
        </div>
        <button className="secondary" onClick={goSetup}>
          <Download size={16} />模型设置
        </button>
        <div className="switch-line">
          <span>声线来源</span>
          <SwitchControl checked={values.tts_voice_source !== "ws"} onChange={(checked) => patch({ tts_voice_source: checked ? "settings" : "ws" })} onLabel="界面控制" offLabel="跟随 WS" />
        </div>
        {voicePreviewUrl && <audio controls src={voicePreviewUrl} className="audio" autoPlay onEnded={continueSeedQueue} />}
      </div>
    </div>
  );
}

function KokoroVoice({ state, patch }: { state: AdminState; patch: (v: SettingsValues) => Promise<void> }) {
  const values = state.settings.values;
  return (
    <div className="kokoro-grid">
      {state.kokoro_voices.map((voice) => (
        <button key={voice.id} className={Number(values.sherpa_kokoro_voice) === voice.id ? "voice-card selected" : "voice-card"} onClick={() => patch({ sherpa_kokoro_voice: voice.id })}>
          <strong>{voice.name}</strong>
          <span>{voice.note}</span>
        </button>
      ))}
    </div>
  );
}

function Setup({ state, patch, reload, setBusy, setNotice }: { state: AdminState; patch: (v: SettingsValues) => Promise<void>; reload: () => Promise<void>; setBusy: (v: string) => void; setNotice: (v: string) => void }) {
  const values = state.settings.values;
  const [installingModel, setInstallingModel] = useState("");
  const installModel = async (kind?: string) => {
    const target = kind || "__all";
    setInstallingModel(target);
    setBusy("models");
    try {
      await api("/api/qwen/models/install", {
        method: "POST",
        body: JSON.stringify(kind ? { kinds: [kind] } : { kinds: [] }),
      });
      setNotice(kind ? `${modelName(kind)} 下载/检查完成` : "模型检查/下载完成");
      window.setTimeout(() => setNotice(""), 1800);
      await reload();
    } finally {
      setInstallingModel("");
      setBusy("");
    }
  };
  const installingAll = installingModel === "__all";
  return (
    <div className="grid">
      <section className="panel span-6">
        <span className="eyebrow"><KeyRound size={15} /> 服务与 LLM</span>
        <h2>第一次只需要填这里</h2>
        <EditableField label="Hermes / LLM 地址" value={values.hermes_base_url} onSave={(v) => patch({ hermes_base_url: v, llm_base_url: v })} />
        <EditableField label="模型名称" value={values.hermes_model} onSave={(v) => patch({ hermes_model: v, llm_model: v })} />
        <EditableField label="API Key" value={values.hermes_api_key} secret onSave={(v) => patch({ hermes_api_key: v, llm_api_key: v })} />
      </section>
      <section className="panel span-6">
        <span className="eyebrow"><Cpu size={15} /> Qwen3TTS</span>
        <h2>本机模型</h2>
        <div className="model-grid single">
          {Object.entries(state.qwen.models).map(([key, model]) => {
            const isInstalling = installingAll || installingModel === key;
            return (
              <div className={`model-card ${isInstalling ? "is-installing" : ""}`} key={key}>
                <StatusDot ok={model.installed} />
                <strong>{modelName(key)}</strong>
                <span>{model.installed ? "已安装" : isInstalling ? "下载中" : "可下载"}</span>
                <code>{model.path}</code>
                {isInstalling && <div className="model-progress" aria-hidden="true" />}
                {!model.installed && (
                  <button className="secondary" disabled={Boolean(installingModel)} onClick={() => installModel(key)}>
                    {isInstalling ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
                    {isInstalling ? "下载中" : "下载"}
                  </button>
                )}
              </div>
            );
          })}
        </div>
        <button className="primary" disabled={Boolean(installingModel)} onClick={() => installModel()}>
          {installingAll ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
          {installingAll ? "正在下载缺失模型" : "下载缺失模型"}
        </button>
      </section>
      <section className="panel span-12">
        <span className="eyebrow"><Check size={15} /> 完成</span>
        <h2>{state.setup.complete ? "设置向导已完成" : "确认后进入日常控制台"}</h2>
        <p className="muted">以后配置会从 SQLite 自动读取；这个项目不再使用 `.env` 文件。</p>
        <button className="primary" onClick={async () => { await api("/api/setup/complete", { method: "POST" }); await reload(); }}>标记完成</button>
      </section>
    </div>
  );
}

function Advanced({ state, patch, busy }: { state: AdminState; patch: (v: SettingsValues) => Promise<void>; busy: string }) {
  const values = state.settings.values;
  return (
    <div className="grid">
      <section className="panel span-6">
        <span className="eyebrow"><Settings2 size={15} /> Qwen 底层配置</span>
        <EditableField label="qwen-tts 程序" value={values.qwentts_cpp_bin} onSave={(v) => patch({ qwentts_cpp_bin: v })} />
        <EditableField label="Base 模型" value={values.qwentts_cpp_base_model} onSave={(v) => patch({ qwentts_cpp_base_model: v })} />
        <EditableField label="CustomVoice 模型" value={values.qwentts_cpp_customvoice_model} onSave={(v) => patch({ qwentts_cpp_customvoice_model: v })} />
        <EditableField label="VoiceDesign 模型" value={values.qwentts_cpp_voicedesign_model} onSave={(v) => patch({ qwentts_cpp_voicedesign_model: v })} />
        <EditableField label="Codec 模型" value={values.qwentts_cpp_codec} onSave={(v) => patch({ qwentts_cpp_codec: v })} />
        <EditableField label="后端" value={values.qwentts_cpp_backend} onSave={(v) => patch({ qwentts_cpp_backend: v })} />
        <EditableField label="固定声线种子" value={values.qwentts_cpp_seed ?? 42} onSave={(v) => patch({ qwentts_cpp_seed: Number(v) })} />
      </section>
      <section className="panel span-6">
        <span className="eyebrow"><Gauge size={15} /> 对话细节</span>
        <EditableField label="首次等待提示延迟（秒）" value={values.hermes_first_filler_delay_seconds} onSave={(v) => patch({ hermes_first_filler_delay_seconds: Number(v) })} />
        <EditableField label="每段最多字符" value={values.tts_segment_max_chars ?? 90} onSave={(v) => patch({ tts_segment_max_chars: Number(v) })} />
        <EditableField label="VAD 阈值" value={values.vad_threshold} onSave={(v) => patch({ vad_threshold: Number(v) })} />
        <EditableField label="最短静音（秒）" value={values.vad_min_silence_seconds} onSave={(v) => patch({ vad_min_silence_seconds: Number(v) })} />
        <p className="muted">{busy === "saving" ? "正在保存..." : "高级项保存后会按需重建 STT/TTS/LLM 组件。"}</p>
      </section>
    </div>
  );
}

function EditableField({ label, value, secret, onSave }: { label: string; value: any; secret?: boolean; onSave: (value: string) => void }) {
  const [draft, setDraft] = useState(String(value ?? ""));
  useEffect(() => setDraft(String(value ?? "")), [value]);
  return (
    <label className="field inline-save">
      <span>{label}</span>
      <div>
        <input type={secret ? "password" : "text"} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <button onClick={() => onSave(draft)}><Save size={15} /></button>
      </div>
    </label>
  );
}

function WaveMeter({ variant = "bars" }: { variant?: string }) {
  return (
    <div className={`wave wave-${variant}`} aria-hidden="true">
      {Array.from({ length: 34 }).map((_, index) => (
        <span key={index} style={{ animationDelay: `${index * 55}ms`, height: `${22 + ((index * 17) % 58)}%` }} />
      ))}
    </div>
  );
}

function StatusDot({ ok }: { ok: boolean }) {
  return <i className={ok ? "dot ok" : "dot warn"} />;
}

function Chip({ icon, label }: { icon: React.ReactNode; label: string }) {
  return <span className="chip">{icon}{label}</span>;
}

function SwitchControl({
  checked,
  onChange,
  onLabel,
  offLabel,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  onLabel: string;
  offLabel: string;
}) {
  return (
    <button className={checked ? "switch-control on" : "switch-control"} onClick={() => onChange(!checked)} type="button">
      <span className="switch-track"><span /></span>
      <strong>{checked ? onLabel : offLabel}</strong>
    </button>
  );
}

function Kpi({ label, value, hint }: { label: string; value: string; hint: string }) {
  return <div className="kpi"><span>{label}</span><strong>{value}</strong><em>{hint}</em></div>;
}

async function api<T = any>(path: string, init: RequestInit & { raw?: boolean } = {}): Promise<T> {
  const headers = init.raw ? init.headers : { "Content-Type": "application/json", ...(init.headers || {}) };
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function chartMetrics(metrics: Metric[]) {
  const turns = metrics
    .slice()
    .reverse()
    .filter((item) => item.kind === "turn")
    .slice(-18)
    .map((item, index) => ({
      label: String(index + 1),
      elapsed: Math.round(Number(item.value.first_audio_ms || item.value.total_ms || 0)),
      rtf: "",
    }));
  if (turns.length) return turns;
  return metrics
    .slice()
    .reverse()
    .filter((item) => item.kind === "tts_preview")
    .slice(-18)
    .map((item, index) => ({
      label: String(index + 1),
      elapsed: Math.round(Number(item.value.elapsed_ms || 0)),
      rtf: Number(item.value.rtf || 0).toFixed(2),
    }));
}

function turnStats(metrics: Metric[]) {
  const turns = metrics.filter((item) => item.kind === "turn");
  const completed = turns.filter((item) => item.value.status === "completed").length;
  const cancelled = turns.filter((item) => item.value.status === "cancelled").length;
  const firstAudio = turns.map((item) => Number(item.value.first_audio_ms || 0)).filter(Boolean);
  const avgFirstAudio = firstAudio.length ? Math.round(firstAudio.reduce((sum, value) => sum + value, 0) / firstAudio.length) : 0;
  return { total: turns.length, completed, cancelled, avgFirstAudio };
}

function splitTags(raw: any) {
  if (Array.isArray(raw)) return raw.map((tag) => String(tag).trim()).filter(Boolean);
  return String(raw || "")
    .split(/[，,\s]+/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function voiceLabel(state: AdminState) {
  const values = state.settings.values;
  if (values.tts_provider === "sherpa_kokoro") {
    const voice = state.kokoro_voices.find((v) => v.id === Number(values.sherpa_kokoro_voice));
    return voice ? `Kokoro ${voice.name}` : "Kokoro";
  }
  const mode = values.qwentts_cpp_voice_mode || "default";
  if (mode === "preset") {
    const speaker = values.qwentts_cpp_voice_preset || "preset";
    return `Qwen ${speakerNames[speaker] ?? speaker}`;
  }
  if (mode === "design") return "Qwen 描述造声";
  if (mode === "clone") return "Qwen 克隆音色";
  return "Qwen 默认音色";
}

function voiceRefForMode(values: SettingsValues) {
  const mode = values.qwentts_cpp_voice_mode || "default";
  if (mode === "preset") return values.qwentts_cpp_voice_preset || "";
  if (mode === "design") return values.qwentts_cpp_voice_design || "";
  if (mode === "clone") return values.qwentts_cpp_clone_voice_id || "";
  return "qwen-default";
}

function modelName(key: string) {
  return ({ base: "Base 默认音色", customvoice: "CustomVoice 预设声线", voicedesign: "VoiceDesign 描述造声", codec: "Codec 预编码" } as Record<string, string>)[key] ?? key;
}

function formatDuration(seconds: number) {
  const safe = Math.max(0, Math.floor(seconds));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m ${safe % 60}s`;
}

function headlineFor(tab: string) {
  if (tab === "studio") return "人格和声线，一处调整";
  if (tab === "setup") return "首次设置向导";
  if (tab === "advanced") return "低频但可控的底层设置";
  return "语音助手状态大屏";
}

function base64ToBlob(base64: string, mime: string) {
  const bin = window.atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

createRoot(document.getElementById("root")!).render(<App />);
