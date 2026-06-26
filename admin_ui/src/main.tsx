import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { motion } from "framer-motion";
import {
  Activity,
  AudioLines,
  Bot,
  Brain,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Cpu,
  Download,
  Gauge,
  KeyRound,
  LoaderCircle,
  Maximize2,
  Mic2,
  Minimize2,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Undo2,
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
  llm_context: Record<string, any>;
  settings: { values: SettingsValues; groups: Record<string, string[]>; raw: SettingsValues };
  setup: { complete: boolean; env_imported: boolean };
  runtime?: { started_at: number; uptime_seconds: number };
  personas: Persona[];
  voices: VoiceProfile[];
  qwen: { speakers: string[]; models: ModelStatus; modes: string[] };
  kokoro_voices: Array<{ id: number; name: string; note: string }>;
  metrics: Metric[];
  memory?: Record<string, any> | null;
};

const navItems = [
  { id: "dashboard", label: "总览", icon: Activity },
  { id: "studio", label: "角色声线", icon: AudioLines },
  { id: "setup", label: "首次设置", icon: Wand2 },
  { id: "advanced", label: "高级", icon: SlidersHorizontal },
  { id: "memory", label: "记忆", icon: Brain },
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
  { id: "scanner", name: "扫描线" },
  { id: "bars", name: "能量条" },
  { id: "core", name: "呼吸核心" },
  { id: "ribbon", name: "轨迹带" },
  { id: "needles", name: "声纹针列" },
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
    try {
      const data = await api<{ state: AdminState; restart_scheduled?: boolean }>("/api/settings", {
        method: "PATCH",
        body: JSON.stringify({ values }),
      });
      setState(data.state);
      setNotice(data.restart_scheduled ? "已保存，服务正在重启，几秒后自动恢复" : "已保存并热更新");
      window.setTimeout(() => setNotice(""), data.restart_scheduled ? 3600 : 1800);
    } catch (err) {
      setNotice(`保存失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3600);
      throw err;
    } finally {
      setBusy("");
    }
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

        <div className="main-scroll">
          {tab === "dashboard" && <Dashboard state={state} patch={patch} goStudio={() => setTab("studio")} />}
          {tab === "studio" && <Studio state={state} patch={patch} reload={load} busy={busy} setBusy={setBusy} setNotice={setNotice} goSetup={() => setTab("setup")} />}
          {tab === "setup" && <Setup state={state} patch={patch} reload={load} setBusy={setBusy} setNotice={setNotice} />}
          {tab === "advanced" && <Advanced state={state} patch={patch} busy={busy} reload={load} setNotice={setNotice} />}
          {tab === "memory" && <MemoryPanel state={state} patch={patch} reload={load} setNotice={setNotice} />}
        </div>
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
  const previewEvents = state.metrics.filter((item) => item.kind === "tts_preview").length;
  const rawWaveStyle = state.settings.values.dashboard_wave_style || "scanner";
  const waveStyle = waveStyles.some((item) => item.id === rawWaveStyle) ? rawWaveStyle : "scanner";
  const waveIndex = Math.max(0, waveStyles.findIndex((item) => item.id === waveStyle));
  const selectWave = (id: string) => patch({ dashboard_wave_style: id });
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
            {waveStyles.map((item) => (
              <button
                key={item.id}
                className={waveStyles[waveIndex]?.id === item.id ? "selected" : ""}
                onClick={() => selectWave(item.id)}
                title={item.name}
                aria-label={item.name}
              />
            ))}
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
  const [dirty, setDirty] = useState(false);
  const [personaId, setPersonaId] = useState(activePersona?.id ?? "operator");
  const [personaName, setPersonaName] = useState(activePersona?.name ?? "自定义人格");
  const [prompt, setPrompt] = useState(values.sts_persona_custom || activePersona?.prompt || "");
  const [previewText, setPreviewText] = useState("你好，我是 Hermes STS。现在用当前角色和音色说话。");
  const [audioUrl, setAudioUrl] = useState("");

  const personaListRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (dirty) return;
    const persona = state.personas.find((p) => p.id === values.sts_persona_preset) ?? state.personas[0];
    setPersonaId(persona?.id ?? "operator");
    setPersonaName(persona?.name ?? "自定义人格");
    setPrompt(values.sts_persona_custom || persona?.prompt || "");
  }, [dirty, state.personas, values.sts_persona_custom, values.sts_persona_preset]);

  useEffect(() => {
    const el = personaListRef.current?.querySelector(".selected");
    el?.scrollIntoView({ block: "nearest" });
  }, [state.personas]);

  const selectPersona = (persona: Persona) => {
    setPersonaId(persona.id);
    setPersonaName(persona.name);
    setPrompt(persona.prompt);
    setDirty(true);
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
    let appliedPersonaId = personaId;
    if (personaId === "custom" || !state.personas.some((p) => p.id === personaId)) {
      const payload = personaPayload(false);
      await api("/api/personas", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      appliedPersonaId = payload.id;
      setPersonaId(payload.id);
    }
    await patch({
      sts_persona_source: values.sts_persona_source || "settings",
      sts_persona_preset: appliedPersonaId,
      sts_persona_custom: prompt,
    });
    setDirty(false);
    await reload();
    setNotice("人格已应用");
    window.setTimeout(() => setNotice(""), 2200);
  };

  const reapplySavedConfig = async () => {
    setBusy("reapply");
    try {
      const data = await api<{ state: AdminState; restart_scheduled?: boolean }>("/api/settings/reapply", { method: "POST" });
      setNotice(data.restart_scheduled ? "服务正在重启，几秒后自动恢复" : "已重新应用数据库中的已保存配置");
      window.setTimeout(() => setNotice(""), data.restart_scheduled ? 3600 : 2200);
      if (!data.restart_scheduled) {
        await reload();
      }
      return data;
    } finally {
      setBusy("");
    }
  };

  const optimizePersona = async () => {
    const current = prompt.trim();
    if (!current) {
      setNotice("先填写提示词内容");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    setBusy("optimize");
    try {
      const data = await api<{ optimized_prompt: string }>("/api/persona/optimize", {
        method: "POST",
        body: JSON.stringify({ prompt: current, name: personaName }),
      });
      setPrompt(data.optimized_prompt);
      setDirty(true);
      setNotice("提示词已优化");
      window.setTimeout(() => setNotice(""), 1800);
    } catch (err) {
      setNotice(`优化失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3000);
    } finally {
      setBusy("");
    }
  };

  const resetToOriginal = () => {
    const original = state.personas.find((p) => p.id === personaId);
    if (!original) {
      setNotice("未找到原始人格预设");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    setPersonaName(original.name);
    setPrompt(original.prompt);
    setDirty(false);
    setNotice(`已恢复"${original.name}"的原始提示词`);
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
    setDirty(true);
  };

  const switchTtsProvider = async (provider: "qwen3tts" | "sherpa_kokoro") => {
    if (values.tts_provider === provider || busy === "tts-provider") return;
    await patch({ tts_provider: provider, tts_voice_source: "settings" });
    await reload();
    setNotice(provider === "qwen3tts" ? "已切换到 Qwen3TTS" : "已切换到 Kokoro");
    window.setTimeout(() => setNotice(""), 1600);
  };

  const preview = async () => {
    setBusy("preview");
    const data = await api<{ audio_wav_base64: string; elapsed_ms: number }>("/api/tts/preview", {
      method: "POST",
      body: JSON.stringify({ text: previewText, ...previewVoicePayload(values) }),
    });
    const blob = base64ToBlob(data.audio_wav_base64, "audio/wav");
    setAudioUrl(URL.createObjectURL(blob));
    setBusy("");
    setNotice(`试听完成 ${data.elapsed_ms}ms`);
    await reload();
  };

  const onPromptChange = (next: string) => {
    setPrompt(next);
    setDirty(true);
    if (personaId !== "custom") {
      setPersonaId("custom");
      setPersonaName("自定义人格");
    }
  };

  return (
    <div className="grid studio">
<section className="panel span-12 studio-commit">
        <div>
          <span className="eyebrow"><CheckCircle2 size={15} /> 重新应用</span>
          <p className="muted">声线和引擎选择已经在「使用/切换」时立即保存。这里仅从数据库重载配置。</p>
        </div>
        <button className="primary apply-global" onClick={reapplySavedConfig} disabled={busy === "reapply" || busy === "saving"}>
          <CheckCircle2 size={16} />{busy === "reapply" ? "应用中" : "重新应用配置"}
        </button>
      </section>

<section className="panel span-4">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Bot size={15} /> 人格</span>
            <h2>人格预设</h2>
          </div>
          <button className="secondary" onClick={newPersona}><Plus size={16} />新增</button>
        </div>
        <p className="muted">点选只会装载到编辑框；改完提示词后用"应用人格"提交。</p>
        <div className="persona-list studio-persona-list" ref={personaListRef}>
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

      <section className="panel span-8">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Sparkles size={15} /> 角色与提示词</span>
            <h2>{personaName}</h2>
          </div>
          <div className="persona-actions">
            <button className="secondary" onClick={optimizePersona} disabled={busy === "optimize" || !prompt.trim()}>
              <Wand2 size={16} />{busy === "optimize" ? "优化中" : "AI 优化"}
            </button>
            <button className="secondary" onClick={savePersona}><Save size={16} />保存预设</button>
            <button className="secondary" onClick={resetToOriginal} disabled={!state.personas.some((p) => p.id === personaId)}>
              <RefreshCw size={16} />重置
            </button>
            <button className="primary" onClick={applyPersona}><CheckCircle2 size={16} />应用人格</button>
          </div>
        </div>
        <label className="field">
          <span>角色名称</span>
          <input value={personaName} onChange={(e) => { setPersonaName(e.target.value); setPersonaId("custom"); setDirty(true); }} />
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
            <button disabled={busy === "tts-provider"} className={values.tts_provider === "qwen3tts" ? "selected" : ""} onClick={() => switchTtsProvider("qwen3tts")}>Qwen3TTS</button>
            <button disabled={busy === "tts-provider"} className={values.tts_provider === "sherpa_kokoro" ? "selected" : ""} onClick={() => switchTtsProvider("sherpa_kokoro")}>Kokoro</button>
          </div>
        </div>
        {values.tts_provider === "qwen3tts" ? (
          <QwenVoice state={state} values={values} patch={patch} reload={reload} busy={busy} setBusy={setBusy} setNotice={setNotice} goSetup={goSetup} />
        ) : (
          <KokoroVoice state={state} values={values} patch={patch} />
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
  values,
  patch,
  reload,
  busy,
  setBusy,
  setNotice,
  goSetup,
}: {
  state: AdminState;
  values: SettingsValues;
  patch: (v: SettingsValues) => Promise<void>;
  reload: () => Promise<void>;
  busy: string;
  setBusy: (v: string) => void;
  setNotice: (v: string) => void;
  goSetup: () => void;
}) {
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [cloneName, setCloneName] = useState("我的克隆音色");
  const [cloneText, setCloneText] = useState("");
  const [voicePreviewUrl, setVoicePreviewUrl] = useState("");
  const [lastRandomSeed, setLastRandomSeed] = useState<number | null>(null);
  const [randomName, setRandomName] = useState("收藏声线");
  const [randomTags, setRandomTags] = useState("沉稳,清晰");
  const [randomNote, setRandomNote] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [seedBatch, setSeedBatch] = useState<number[]>([]);
  const [queuedSeeds, setQueuedSeeds] = useState<number[]>([]);
  const [workshopBrief, setWorkshopBrief] = useState("冷静、清晰、有一点未来感，适合长期陪伴的中文语音助手");
  const [workshopScenario, setWorkshopScenario] = useState("跟随当前人格");
  const [workshopSuggestion, setWorkshopSuggestion] = useState<Record<string, any> | null>(null);
  const [designBrief, setDesignBrief] = useState("自然、清晰、冷静一点，适合中文语音助手长期陪伴");
  const [designDraft, setDesignDraft] = useState(String(values.qwentts_cpp_voice_design || ""));
  const customVoiceReady = Boolean(state.qwen.models.customvoice?.installed);
  const voiceDesignReady = Boolean(state.qwen.models.voicedesign?.installed);
  const favoriteVoices = state.voices.filter((voice) => ["seed", "design"].includes(String(voice.mode)));
  const favoriteTags = Array.from(new Set(favoriteVoices.flatMap((voice) => splitTags(voice.tags))));
  const shownVoices = tagFilter ? favoriteVoices.filter((voice) => splitTags(voice.tags).includes(tagFilter)) : favoriteVoices;

  useEffect(() => {
    setDesignDraft(String(values.qwentts_cpp_voice_design || ""));
  }, [values.qwentts_cpp_voice_design]);

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
      body: JSON.stringify({ name: randomName || `Seed ${lastRandomSeed}`, seed: lastRandomSeed, tags: splitTags(randomTags), note: randomNote }),
    });
    await patch({ qwentts_cpp_voice_mode: "default", qwentts_cpp_seed: lastRandomSeed, tts_voice_source: "settings" });
    await reload();
    setNotice(`已收藏并使用 seed ${lastRandomSeed}`);
    window.setTimeout(() => setNotice(""), 1800);
  };

  const keepSeed = async (seed: number, name = `Seed ${seed}`) => {
    await api("/api/qwen/voices/seed", {
      method: "POST",
      body: JSON.stringify({ name, seed, tags: splitTags(randomTags), note: randomNote }),
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
    const voice = state.voices.find((item) => item.id === voiceId);
    if (!voice) {
      setNotice("没有找到这条收藏声线");
      return;
    }
    await patch({ ...settingsForVoiceProfile(voice), tts_voice_source: "settings" });
    await reload();
    setNotice("收藏声线已启用");
    window.setTimeout(() => setNotice(""), 1800);
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
    try {
      const data = await api<{ suggestion: Record<string, any> }>("/api/qwen/workshop/suggest", {
        method: "POST",
        body: JSON.stringify({
          brief: workshopBrief,
          scenario: workshopScenario,
          persona_hint: state.health.persona_prompt || "",
          current_voice: describeCurrentVoice(values),
        }),
      });
      setWorkshopSuggestion(data.suggestion);
      setNotice("AI 已生成音色方案");
      window.setTimeout(() => setNotice(""), 1800);
    } finally {
      setBusy("");
    }
  };

  const generateDesignPrompt = async () => {
    const brief = designBrief.trim() || "自然、清晰、适合中文语音助手";
    setBusy("design-ai");
    try {
      const data = await api<{ suggestion: Record<string, any> }>("/api/qwen/workshop/suggest", {
        method: "POST",
        body: JSON.stringify({ brief: `只生成 VoiceDesign 音色描述。目标气质：${brief}`, persona_hint: state.health.persona_prompt || "" }),
      });
      const prompt = String(data.suggestion.design_prompt || "").trim() || `natural Mandarin voice, clear articulation, ${brief}, calm tone, comfortable pace`;
      setDesignDraft(prompt);
      setNotice("AI 已生成音色描述，确认后再应用");
      window.setTimeout(() => setNotice(""), 2200);
    } finally {
      setBusy("");
    }
  };

  const applyDesignDraft = async () => {
    const prompt = designDraft.trim();
    if (!prompt) {
      setNotice("先填写或生成音色描述");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    await patch({ qwentts_cpp_voice_mode: "design", qwentts_cpp_voice_design: prompt, tts_voice_source: "settings" });
    await reload();
    setNotice("描述造声已启用");
    window.setTimeout(() => setNotice(""), 1800);
  };

  const saveCurrentDesignVoice = async () => {
    const prompt = designDraft.trim();
    if (!prompt) {
      setNotice("先填写或生成音色描述");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    await api("/api/qwen/voices/design", {
      method: "POST",
      body: JSON.stringify({
        name: randomName || "描述造声音色",
        design_prompt: prompt,
        tags: splitTags(randomTags),
        note: randomNote,
      }),
    });
    await reload();
    setNotice("描述声线已收藏");
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
    await patch({ ...valuesToPatch, tts_voice_source: "settings" });
    await reload();
    if (workshopSuggestion.persona_prompt) {
      setNotice("音色方案已启用，提示词可复制到人格里微调");
    }
  };

  const saveSuggestionVoice = async () => {
    if (!workshopSuggestion) return;
    const mode = String(workshopSuggestion.voice_mode || "design");
    const tags = Array.isArray(workshopSuggestion.tags) ? workshopSuggestion.tags : splitTags(workshopSuggestion.tags);
    const note = String(workshopSuggestion.save_note || workshopSuggestion.rationale || workshopSuggestion.notes || "").slice(0, 240);
    if (mode === "design") {
      const designPrompt = String(workshopSuggestion.design_prompt || "").trim();
      if (!designPrompt) {
        setNotice("这条方案没有音色描述，无法收藏为描述造声");
        window.setTimeout(() => setNotice(""), 1800);
        return;
      }
      await api("/api/qwen/voices/design", {
        method: "POST",
        body: JSON.stringify({
          name: workshopSuggestion.name || "AI 描述声线",
          design_prompt: designPrompt,
          tags,
          note,
        }),
      });
    } else {
      const seed = Number(workshopSuggestion.seed || 42);
      await api("/api/qwen/voices/seed", {
        method: "POST",
        body: JSON.stringify({
          name: workshopSuggestion.name || `Seed ${seed}`,
          seed,
          tags,
          note,
        }),
      });
    }
    await reload();
    setNotice("AI 声线已收藏");
    window.setTimeout(() => setNotice(""), 1800);
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
        apply: false,
      }),
    });
    if (mode === "design") {
      await patch({ qwentts_cpp_voice_mode: "design", qwentts_cpp_voice_design: workshopSuggestion.design_prompt || "", tts_voice_source: "settings" });
    } else {
      await patch({ qwentts_cpp_voice_mode: "default", qwentts_cpp_seed: Number(workshopSuggestion.seed || 42), tts_voice_source: "settings" });
    }
    await reload();
    setNotice("完整角色已保存，声线已启用");
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
    await patch({ qwentts_cpp_voice_mode: "clone", qwentts_cpp_clone_voice_id: uploaded.voice.id, tts_voice_source: "settings" });
    setNotice("克隆音色已预编码并启用");
    await reload();
  };

  const switchQwenMode = async (mode: string) => {
    const valuesToPatch: SettingsValues = { qwentts_cpp_voice_mode: mode };
    if (mode === "preset" && !values.qwentts_cpp_voice_preset) {
      valuesToPatch.qwentts_cpp_voice_preset = "vivian";
    }
    if (mode === "design" && !values.qwentts_cpp_voice_design) {
      valuesToPatch.qwentts_cpp_voice_design = "clear, calm, natural Mandarin voice with a cool and reliable tone";
    }
    await patch({ ...valuesToPatch, tts_voice_source: "settings" });
    await reload();
  };

  return (
    <div className="voice-layout">
      <div>
        <div className="mode-grid">
          {state.qwen.modes.map((mode) => (
            <button key={mode} className={values.qwentts_cpp_voice_mode === mode ? "mode selected" : "mode"} onClick={() => switchQwenMode(mode)}>
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
                <select value={values.qwentts_cpp_voice_preset || "vivian"} onChange={(e) => patch({ qwentts_cpp_voice_mode: "preset", qwentts_cpp_voice_preset: e.target.value, tts_voice_source: "settings" })}>
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
                    <button className="primary" onClick={() => patch({ qwentts_cpp_voice_mode: "preset", qwentts_cpp_voice_preset: speaker, tts_voice_source: "settings" })}>
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
            <input className="compact-note-input" value={randomNote} onChange={(event) => setRandomNote(event.target.value)} placeholder="备注，可选：例如 低频、像某次随机里的第 3 条" />
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
          <div className="design-panel">
            {!voiceDesignReady && (
              <div className="missing-model">
                <span>描述造声需要 VoiceDesign 模型。</span>
                <button className="link-btn" onClick={goSetup}>去模型设置</button>
              </div>
            )}
            <label className="field">
              <span>想要的声音</span>
              <input value={designBrief} onChange={(e) => setDesignBrief(e.target.value)} placeholder="例如：温柔但不甜腻，中文清晰，语速自然" />
            </label>
            <button className="secondary" onClick={generateDesignPrompt} disabled={busy === "design-ai"}>
              <Wand2 size={16} />{busy === "design-ai" ? "生成中" : "AI 生成描述"}
            </button>
            <label className="field">
              <span>音色描述</span>
              <textarea value={designDraft} onChange={(e) => setDesignDraft(e.target.value)} placeholder="例如：female, young adult, clear warm Mandarin voice, natural pace, soft tone" />
            </label>
            <div className="field-row">
              <label className="field">
                <span>收藏名</span>
                <input value={randomName} onChange={(event) => setRandomName(event.target.value)} placeholder="例如 冷感播报" />
              </label>
              <label className="field">
                <span>标签</span>
                <input value={randomTags} onChange={(event) => setRandomTags(event.target.value)} placeholder="沉稳,清晰" />
              </label>
            </div>
            <input className="compact-note-input" value={randomNote} onChange={(event) => setRandomNote(event.target.value)} placeholder="备注，可选：记录这条声线适合什么场景" />
            <div className="design-actions">
              <button className="primary" onClick={applyDesignDraft}><Check size={16} />使用描述</button>
              <button className="secondary" onClick={() => previewVoice({ voice_mode: "design", design_prompt: designDraft }, "描述造声试听完成")} disabled={!designDraft.trim() || !voiceDesignReady}>
                <Play size={16} />试听
              </button>
              <button className="secondary" onClick={saveCurrentDesignVoice} disabled={!designDraft.trim()}>
                <Save size={16} />收藏描述
              </button>
            </div>
          </div>
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
                <button key={voice.id} className={values.qwentts_cpp_clone_voice_id === voice.id ? "pill selected" : "pill"} onClick={() => patch({ qwentts_cpp_voice_mode: "clone", qwentts_cpp_clone_voice_id: voice.id, tts_voice_source: "settings" })}>
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
          <p className="muted">依据当前人格、场景和偏好生成一条可试听、可启用、可收藏的声线。</p>
          <select value={workshopScenario} onChange={(e) => setWorkshopScenario(e.target.value)}>
            <option>跟随当前人格</option>
            <option>日常陪伴和快答</option>
            <option>播报、提醒、读消息</option>
            <option>设备控制和短指令</option>
            <option>夜间低打扰对话</option>
          </select>
          <textarea value={workshopBrief} onChange={(e) => setWorkshopBrief(e.target.value)} />
          <button className="primary" onClick={suggestVoice} disabled={false}>{busy === "workshop" ? "生成中" : "生成方案"}</button>
          {workshopSuggestion && (
            <div className="suggestion-card">
              <strong>{workshopSuggestion.name}</strong>
              <span>{workshopSuggestion.voice_mode === "design" ? "描述造声" : "默认声线 seed"}{workshopSuggestion.use_case ? ` · ${workshopSuggestion.use_case}` : ""}</span>
              {workshopSuggestion.design_prompt && <code>{workshopSuggestion.design_prompt}</code>}
              <em>seed {workshopSuggestion.seed}</em>
              {(workshopSuggestion.rationale || workshopSuggestion.notes) && <p>{workshopSuggestion.rationale || workshopSuggestion.notes}</p>}
              {workshopSuggestion.save_note && <small className="voice-note">收藏备注：{workshopSuggestion.save_note}</small>}
              {workshopSuggestion.persona_prompt && <textarea readOnly value={workshopSuggestion.persona_prompt} />}
              <button className="secondary" onClick={() => previewVoice({
                voice_mode: workshopSuggestion.voice_mode,
                design_prompt: workshopSuggestion.design_prompt,
                seed: workshopSuggestion.seed,
                text: workshopSuggestion.preview_text,
              }, "AI 方案试听完成")}><Play size={15} />试听方案</button>
              <button className="primary" onClick={applySuggestion}>使用方案</button>
              <button className="secondary" onClick={saveSuggestionVoice}><Save size={15} />收藏声线</button>
              <button className="primary" onClick={saveSuggestionAsPersona}>保存成完整角色</button>
            </div>
          )}
        </div>
        {shownVoices.length > 0 && (
          <div className="saved-voices">
            <span className="eyebrow"><Shuffle size={15} /> 收藏声线</span>
            {favoriteTags.length > 0 && (
              <div className="tag-filter">
                <button className={tagFilter === "" ? "pill selected" : "pill"} onClick={() => setTagFilter("")}>全部</button>
                {favoriteTags.map((tag) => (
                  <button key={tag} className={tagFilter === tag ? "pill selected" : "pill"} onClick={() => setTagFilter(tag)}>{tag}</button>
                ))}
              </div>
            )}
            {shownVoices.map((voice) => (
              <div className="saved-voice" key={voice.id}>
                <div>
                  <strong>{voice.name}</strong>
                  <span>{voiceSummary(voice)}{voice.tags ? ` · ${voice.tags}` : ""}</span>
                  {voice.note && <small className="voice-note">{voice.note}</small>}
                </div>
                <button className="icon-btn" onClick={() => applyVoiceProfile(voice.id)} title="使用"><Check size={16} /></button>
                <button className="icon-btn danger" onClick={() => deleteVoiceProfile(voice)} title="删除"><Trash2 size={16} /></button>
              </div>
            ))}
          </div>
        )}
        <div className="model-status-compact">
          {Object.entries(state.qwen.models).map(([key, model]) => (
            <span key={key} className={model.installed ? "model-tag ok" : "model-tag"}>
              <StatusDot ok={model.installed} />
              {modelName(key)}
            </span>
          ))}
          <button className="model-setup-btn" onClick={goSetup} title="模型设置">
            <Download size={13} />
          </button>
        </div>
        <div className="locked-note">
          <CheckCircle2 size={16} />
          <span>声线点击“使用/切换”后立即保存并热更新；WS 传入 voice 不会覆盖当前音色。</span>
        </div>
        {voicePreviewUrl && <audio controls src={voicePreviewUrl} className="audio" autoPlay onEnded={continueSeedQueue} />}
      </div>
    </div>
  );
}

function KokoroVoice({
  state,
  values,
  patch,
}: {
  state: AdminState;
  values: SettingsValues;
  patch: (v: SettingsValues) => Promise<void>;
}) {
  return (
    <div className="kokoro-grid">
      {state.kokoro_voices.map((voice) => (
        <button key={voice.id} className={Number(values.sherpa_kokoro_voice) === voice.id ? "voice-card selected" : "voice-card"} onClick={() => patch({ sherpa_kokoro_voice: voice.id, tts_voice_source: "settings" })}>
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
  const allModelsInstalled = Object.values(state.qwen.models).every((m) => m.installed);
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
        {!allModelsInstalled && (
          <button className="primary" disabled={Boolean(installingModel)} onClick={() => installModel()}>
            {installingAll ? <LoaderCircle className="spin" size={16} /> : <Download size={16} />}
            {installingAll ? "正在下载缺失模型" : "下载缺失模型"}
          </button>
        )}
      </section>
      <section className="panel span-12">
        <span className="eyebrow"><CheckCircle2 size={15} /> 部署边界</span>
        <h2>新机器先跑脚本，再进界面</h2>
        <div className="deploy-grid">
          <div className="deploy-step">
            <strong>1. 系统和编译环境</strong>
            <code>./scripts/bootstrap_fedora_amd.sh --system</code>
            <span>安装 Fedora/Vulkan/构建依赖，适合脚本阶段。</span>
          </div>
          <div className="deploy-step">
            <strong>2. Python、模型实验室、前端</strong>
            <code>./scripts/bootstrap_fedora_amd.sh</code>
            <span>准备 `.venv-sts`、Kokoro/SenseVoice、hermes-tts-lab、控制台构建。</span>
          </div>
          <div className="deploy-step">
            <strong>3. 运行期配置</strong>
            <code>http://127.0.0.1:8765/</code>
            <span>LLM 地址、API Key、TTS 引擎、声线和提示词放在界面里保存。</span>
          </div>
        </div>
      </section>
    </div>
  );
}

function Advanced({
  state,
  patch,
  busy,
  reload,
  setNotice,
}: {
  state: AdminState;
  patch: (v: SettingsValues) => Promise<void>;
  busy: string;
  reload: () => Promise<void>;
  setNotice: (v: string) => void;
}) {
  const values = state.settings.values;
  const raw: Record<string, any> = state.settings.raw || {};
  const resetContext = async () => {
    await api("/api/llm/context/reset", { method: "POST" });
    await reload();
    setNotice("语音短期上下文已清空，Hermes 长期记忆不受影响");
    window.setTimeout(() => setNotice(""), 2200);
  };
  const resetField = (key: string) => async () => {
    try {
      await api("/api/settings/reset-default", {
        method: "POST",
        body: JSON.stringify({ values: { [key]: null } }),
      });
      await reload();
      setNotice(`"${key}" 已恢复默认值`);
      window.setTimeout(() => setNotice(""), 1800);
    } catch (err) {
      setNotice(`恢复失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3000);
    }
  };
  const showReset = (key: string) => key in raw ? resetField(key) : undefined;
  return (
    <div className="grid">
      <section className="panel span-6">
        <span className="eyebrow"><Bot size={15} /> 上下文控制</span>
        <div className="context-meter">
          <div>
            <strong>{state.llm_context?.messages ?? 0}</strong>
            <span>本地上下文消息</span>
          </div>
          <div>
            <strong>{state.llm_context?.chars ?? 0}</strong>
            <span>约字符数</span>
          </div>
        </div>
        <p className="muted">控制 STS 调用 Hermes/Agent 时附带的短期对话历史。它不是人格提示词，也不会删除 Hermes 自己的长期记忆。</p>
        <button className="secondary" onClick={resetContext} disabled={!state.llm_context?.reset_available}>
          <RefreshCw size={16} />立即清空短期上下文
        </button>
        <EditableField label="最多保留消息数" value={values.hermes_history_max_messages ?? 300} onSave={(v) => patch({ hermes_history_max_messages: Number(v) })} onReset={showReset("hermes_history_max_messages")} />
        <EditableField label="最多保留字符数" value={values.hermes_history_max_chars ?? 65536} onSave={(v) => patch({ hermes_history_max_chars: Number(v) })} onReset={showReset("hermes_history_max_chars")} />
        <EditableField label="空闲多久后自动清空（秒）" value={values.hermes_history_idle_reset_seconds ?? 14400} onSave={(v) => patch({ hermes_history_idle_reset_seconds: Number(v) })} onReset={showReset("hermes_history_idle_reset_seconds")} />
      </section>
      <section className="panel span-6">
        <span className="eyebrow"><Mic2 size={15} /> 识别与打断</span>
        <EditableField label="VAD 阈值" value={values.vad_threshold} onSave={(v) => patch({ vad_threshold: Number(v) })} onReset={showReset("vad_threshold")} />
        <EditableField label="最短静音（秒）" value={values.vad_min_silence_seconds} onSave={(v) => patch({ vad_min_silence_seconds: Number(v) })} onReset={showReset("vad_min_silence_seconds")} />
        <p className="muted">{busy === "saving" ? "正在保存..." : "高级项保存后会按需重建 STT/TTS/LLM 组件。"}</p>
      </section>
      <section className="panel span-6">
        <span className="eyebrow"><Gauge size={15} /> 对话节奏</span>
        <label className="field">
          <span>Hermes 语音快答</span>
          <SwitchControl
            checked={values.hermes_voice_no_think !== false}
            onChange={(checked) => patch({ hermes_voice_no_think: checked })}
            onLabel="开启"
            offLabel="关闭"
          />
        </label>
        <EditableField label="最多等待 Hermes（秒）" value={values.hermes_agent_max_wait_seconds ?? 60} onSave={(v) => patch({ hermes_agent_max_wait_seconds: Number(v) })} onReset={showReset("hermes_agent_max_wait_seconds")} />
        <EditableField label="首次等待提示延迟（秒）" value={values.hermes_first_filler_delay_seconds} onSave={(v) => patch({ hermes_first_filler_delay_seconds: Number(v) })} onReset={showReset("hermes_first_filler_delay_seconds")} />
        <EditableField label="等待提示间隔（秒）" value={values.hermes_filler_interval_seconds ?? 12} onSave={(v) => patch({ hermes_filler_interval_seconds: Number(v) })} onReset={showReset("hermes_filler_interval_seconds")} />
        <EditableField label="最多等待提示次数" value={values.hermes_max_fillers ?? 1} onSave={(v) => patch({ hermes_max_fillers: Number(v) })} onReset={showReset("hermes_max_fillers")} />
        <EditableField label="每段最多字符" value={values.tts_segment_max_chars ?? 90} onSave={(v) => patch({ tts_segment_max_chars: Number(v) })} onReset={showReset("tts_segment_max_chars")} />
      </section>
      <section className="panel span-6">
        <span className="eyebrow"><Settings2 size={15} /> Qwen 底层配置</span>
        <EditableField label="后端" value={values.qwentts_cpp_backend} onSave={(v) => patch({ qwentts_cpp_backend: v })} onReset={showReset("qwentts_cpp_backend")} />
        <EditableField label="固定声线种子" value={values.qwentts_cpp_seed ?? 42} onSave={(v) => patch({ qwentts_cpp_seed: Number(v) })} onReset={showReset("qwentts_cpp_seed")} />
      </section>
    </div>
  );
}

function EditableField({ label, value, secret, onSave, onReset }: { label: string; value: any; secret?: boolean; onSave: (value: string) => void; onReset?: () => void }) {
  const [draft, setDraft] = useState(String(value ?? ""));
  useEffect(() => setDraft(String(value ?? "")), [value]);
  return (
    <label className="field inline-save">
      <span>{label}</span>
      <div>
        <div className="input-wrap">
          <input type={secret ? "password" : "text"} value={draft} onChange={(e) => setDraft(e.target.value)} />
          {onReset && <button className="reset-inline" onClick={onReset} title="恢复默认值"><Undo2 size={14} /></button>}
        </div>
        <button onClick={() => onSave(draft)}><Save size={15} /></button>
      </div>
    </label>
  );
}

function WaveMeter({ variant = "scanner" }: { variant?: string }) {
  const count = ({ scanner: 46, core: 36, ribbon: 42, needles: 52 } as Record<string, number>)[variant] ?? 34;
  const delayStep = ({ scanner: 42, core: 86, ribbon: 58, needles: 38 } as Record<string, number>)[variant] ?? 70;
  const heightStep = ({ scanner: 11, core: 19, ribbon: 13, needles: 9 } as Record<string, number>)[variant] ?? 17;
  return (
    <div className={`wave wave-${variant}`} aria-hidden="true">
      {Array.from({ length: count }).map((_, index) => (
        <span
          key={index}
          style={{
            animationDelay: `${index * delayStep}ms`,
            height: `${18 + ((index * heightStep) % 62)}%`,
            ["--i" as string]: index,
            ["--n" as string]: count,
          }}
        />
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

function errorMessage(err: unknown) {
  const raw = err instanceof Error ? err.message : String(err);
  try {
    const parsed = JSON.parse(raw);
    const detail = parsed?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map((item) => item.msg || item.type || "配置错误").join("；");
  } catch {
    // Plain text errors are already usable enough for the toast.
  }
  return raw.length > 160 ? `${raw.slice(0, 160)}...` : raw;
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

function voiceSummary(voice: VoiceProfile) {
  const mode = String(voice.mode || "default");
  if (mode === "seed") return `seed ${voice.seed ?? 42}`;
  if (mode === "design") return "描述造声";
  if (mode === "preset") return `预设 ${voice.speaker || ""}`.trim();
  if (mode === "clone") return voice.ref_spk ? "克隆音色 · ready" : "克隆音色 · 未预编码";
  return "默认音色";
}

function describeCurrentVoice(values: SettingsValues) {
  const mode = String(values.qwentts_cpp_voice_mode || "default");
  if (mode === "preset") return `预设声线 ${values.qwentts_cpp_voice_preset || "未选择"}`;
  if (mode === "design") return `描述造声：${values.qwentts_cpp_voice_design || "未填写"}`;
  if (mode === "clone") return `克隆音色：${values.qwentts_cpp_clone_voice_id || "未选择"}`;
  return `默认音色 seed ${values.qwentts_cpp_seed ?? 42}`;
}

function settingsForVoiceProfile(voice: VoiceProfile): SettingsValues {
  const mode = String(voice.mode || "default");
  if (mode === "seed") {
    return { qwentts_cpp_voice_mode: "default", qwentts_cpp_seed: Number(voice.seed || 42) };
  }
  if (mode === "preset") {
    return { qwentts_cpp_voice_mode: "preset", qwentts_cpp_voice_preset: voice.speaker || "" };
  }
  if (mode === "design") {
    return { qwentts_cpp_voice_mode: "design", qwentts_cpp_voice_design: voice.design_prompt || "" };
  }
  if (mode === "clone") {
    return { qwentts_cpp_voice_mode: "clone", qwentts_cpp_clone_voice_id: voice.id || "" };
  }
  return { qwentts_cpp_voice_mode: "default" };
}

function previewVoicePayload(values: SettingsValues): SettingsValues {
  const mode = String(values.qwentts_cpp_voice_mode || "default");
  const payload: SettingsValues = {
    voice_mode: mode,
    seed: Number(values.qwentts_cpp_seed ?? 42),
  };
  if (mode === "preset") {
    payload.speaker = values.qwentts_cpp_voice_preset || "vivian";
  }
  if (mode === "design") {
    payload.design_prompt = values.qwentts_cpp_voice_design || "";
  }
  if (mode === "clone") {
    payload.clone_voice_id = values.qwentts_cpp_clone_voice_id || "";
  }
  return payload;
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

function formatTime(unix: number): string {
  if (!unix) return "—";
  const d = new Date(unix * 1000);
  if (isNaN(d.getTime())) return "—";
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const hour = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${month}-${day} ${hour}:${min}`;
}

function activityLabel(kind: string): string {
  const labels: Record<string, string> = {
    memory_read: "记忆注入",
    memory_commit: "记忆提交",
    memory_extract: "记忆提取",
    memory_record_turn: "回合记录",
  };
  return labels[kind] || kind;
}

function activitySnippet(value: any): string {
  if (!value) return "";
  if (typeof value === "string") return value.slice(0, 100);
  if (typeof value === "object") {
    const parts: string[] = [];
    if (value.query) parts.push(`q="${value.query}"`);
    if (value.hits != null) parts.push(`${value.hits} hits`);
    if (value.uri) parts.push(value.uri);
    if (value.count != null) parts.push(`${value.count} 条`);
    if (value.session_id) parts.push(`session=${String(value.session_id).slice(0, 8)}`);
    return parts.join(" · ").slice(0, 120) || JSON.stringify(value).slice(0, 100);
  }
  return String(value).slice(0, 100);
}

function headlineFor(tab: string) {
  if (tab === "studio") return "人格和声线，一处调整";
  if (tab === "setup") return "首次设置向导";
  if (tab === "advanced") return "低频但可控的底层设置";
  if (tab === "memory") return "记忆管理与检索";
  return "语音助手状态大屏";
}

function base64ToBlob(base64: string, mime: string) {
  const bin = window.atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

function MemoryPanel({
  state,
  patch,
  reload,
  setNotice,
}: {
  state: AdminState;
  patch: (v: SettingsValues) => Promise<void>;
  reload: () => Promise<void>;
  setNotice: (v: string) => void;
}) {
  const values = state.settings.values;
  const memState = state.memory;
  const enabled = Boolean(values.memory_enabled);
  const provider = String(values.memory_provider || "noop");
  const webSearchEnabled = Boolean(values.web_search_enabled);
  const limit = 50;

  const [memories, setMemories] = useState<any[]>([]);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [activity, setActivity] = useState<any[]>([]);
  const [recallQuery, setRecallQuery] = useState("");
  const [recallHits, setRecallHits] = useState<any[]>([]);
  const [recallMs, setRecallMs] = useState<number | null>(null);
  const [recalling, setRecalling] = useState(false);
  const [editor, setEditor] = useState<{ uri: string; content: string; category: string; tags: string } | null>(null);
  const [editorSaving, setEditorSaving] = useState(false);

  const fetchList = async (q: string, off: number) => {
    setLoading(true);
    setError("");
    try {
      const data = await api<{ memories: any[] }>(`/api/memories?limit=${limit}&offset=${off}&q=${encodeURIComponent(q)}`);
      setMemories(data.memories || []);
      setHasMore((data.memories || []).length >= limit);
    } catch (err) {
      setError(errorMessage(err));
      setMemories([]);
    } finally {
      setLoading(false);
    }
  };

  const fetchActivity = async () => {
    try {
      const data = await api<{ activity: any[] }>("/api/memories/activity?limit=20");
      setActivity(data.activity || []);
    } catch {
      // activity stream is non-critical — silent failure
    }
  };

  useEffect(() => {
    if (enabled) {
      fetchList("", 0);
      fetchActivity();
    } else {
      setMemories([]);
      setActivity([]);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled]);

  const doSearch = () => {
    setOffset(0);
    fetchList(query, 0);
  };

  const prevPage = () => {
    const next = Math.max(0, offset - limit);
    setOffset(next);
    fetchList(query, next);
  };

  const nextPage = () => {
    if (!hasMore) return;
    const next = offset + limit;
    setOffset(next);
    fetchList(query, next);
  };

  const openAdd = () => {
    setEditor({ uri: "", content: "", category: "manual", tags: "" });
  };

  const openEdit = async (uri: string) => {
    try {
      const data = await api<{ memory: any }>(`/api/memories/${encodeURIComponent(uri)}`);
      const m = data.memory;
      setEditor({
        uri,
        content: m.content || "",
        category: m.category || "manual",
        tags: Array.isArray(m.tags) ? m.tags.join(", ") : String(m.tags || ""),
      });
    } catch (err) {
      setNotice(`读取失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 2400);
    }
  };

  const saveEditor = async () => {
    if (!editor) return;
    const content = editor.content.trim();
    if (!content) {
      setNotice("内容不能为空");
      window.setTimeout(() => setNotice(""), 1600);
      return;
    }
    setEditorSaving(true);
    try {
      const tags = splitTags(editor.tags);
      if (editor.uri) {
        await api(`/api/memories/${encodeURIComponent(editor.uri)}`, {
          method: "PUT",
          body: JSON.stringify({ uri: editor.uri, content, category: editor.category, tags }),
        });
        setNotice("记忆已更新");
      } else {
        await api("/api/memories", {
          method: "POST",
          body: JSON.stringify({ content, category: editor.category, tags }),
        });
        setNotice("记忆已添加");
      }
      setEditor(null);
      window.setTimeout(() => setNotice(""), 1600);
      await fetchList(query, offset);
      await fetchActivity();
      await reload();
    } catch (err) {
      setNotice(`保存失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3000);
    } finally {
      setEditorSaving(false);
    }
  };

  const deleteMemory = async (uri: string) => {
    if (!window.confirm("删除这条记忆？删除后不可恢复。")) return;
    try {
      await api(`/api/memories/${encodeURIComponent(uri)}`, { method: "DELETE" });
      setNotice("记忆已删除");
      window.setTimeout(() => setNotice(""), 1600);
      await fetchList(query, offset);
      await fetchActivity();
      await reload();
    } catch (err) {
      setNotice(`删除失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3000);
    }
  };

  const runRecall = async () => {
    const q = recallQuery.trim();
    if (!q) return;
    setRecalling(true);
    setRecallHits([]);
    setRecallMs(null);
    try {
      const data = await api<{ hits: any[]; ms: number }>("/api/memories/recall", {
        method: "POST",
        body: JSON.stringify({ query: q, limit: 5, min_score: 0 }),
      });
      setRecallHits(data.hits || []);
      setRecallMs(data.ms);
    } catch (err) {
      setNotice(`检索失败：${errorMessage(err)}`);
      window.setTimeout(() => setNotice(""), 3000);
    } finally {
      setRecalling(false);
    }
  };

  return (
    <div className="grid">
      <section className="panel span-12 mem-kpi-strip">
        <div className="kpi">
          <span>记忆状态</span>
          <strong>{enabled ? "已启用" : "未启用"}</strong>
          <em>{provider}</em>
        </div>
        <div className="kpi">
          <span>记忆条数</span>
          <strong>{String(memState?.count ?? "--")}</strong>
          <em>{memState?.latest_updated_at ? `最近 ${formatTime(memState.latest_updated_at)}` : "暂无更新"}</em>
        </div>
        <div className="kpi">
          <span>Web 搜索</span>
          <strong>{webSearchEnabled ? "已启用" : "未启用"}</strong>
          <em>{values.tavily_search_depth || "—"}</em>
        </div>
        <div className="kpi">
          <span>检索延迟</span>
          <strong>{recallMs != null ? `${recallMs}ms` : "—"}</strong>
          <em>最近一次 recall</em>
        </div>
      </section>

      <section className="panel span-6">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Brain size={15} /> 记忆后端</span>
            <h2>Provider 配置</h2>
          </div>
        </div>
        <div className="switch-line">
          <span>启用记忆</span>
          <SwitchControl checked={enabled} onChange={(checked) => patch({ memory_enabled: checked })} onLabel="开启" offLabel="关闭" />
        </div>
        <label className="field">
          <span>Provider</span>
          <div className="select-wrap">
            <select value={provider} onChange={(e) => patch({ memory_provider: e.target.value })}>
              <option value="sqlite">sqlite（本地）</option>
              <option value="openviking">openviking</option>
              <option value="noop">noop（禁用）</option>
            </select>
            <ChevronDown size={16} />
          </div>
        </label>
        {provider === "openviking" && (
          <>
            <EditableField label="OpenViking Base URL" value={values.openviking_base_url} onSave={(v) => patch({ openviking_base_url: v })} />
            <EditableField label="OpenViking API Key" value={values.openviking_api_key} secret onSave={(v) => patch({ openviking_api_key: v })} />
            <EditableField label="Account" value={values.openviking_account} onSave={(v) => patch({ openviking_account: v })} />
            <EditableField label="User" value={values.openviking_user} onSave={(v) => patch({ openviking_user: v })} />
          </>
        )}
      </section>

      <section className="panel span-6">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Gauge size={15} /> Web 搜索</span>
            <h2>Tavily 配置</h2>
          </div>
        </div>
        <div className="switch-line">
          <span>启用 Web 搜索</span>
          <SwitchControl checked={webSearchEnabled} onChange={(checked) => patch({ web_search_enabled: checked })} onLabel="开启" offLabel="关闭" />
        </div>
        <EditableField label="Tavily API Key" value={values.tavily_api_key} secret onSave={(v) => patch({ tavily_api_key: v })} />
        <label className="field">
          <span>搜索深度</span>
          <div className="select-wrap">
            <select value={values.tavily_search_depth || "fast"} onChange={(e) => patch({ tavily_search_depth: e.target.value })}>
              <option value="ultra-fast">ultra-fast</option>
              <option value="fast">fast</option>
              <option value="basic">basic</option>
            </select>
            <ChevronDown size={16} />
          </div>
        </label>
        <EditableField label="超时（秒，最大 3.0）" value={values.tavily_timeout_seconds ?? 2.0} onSave={(v) => patch({ tavily_timeout_seconds: Math.min(3.0, Number(v) || 2.0) })} />
      </section>

      <section className="panel span-8">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Activity size={15} /> 记忆库</span>
            <h2>记忆列表</h2>
          </div>
          <button className="primary" onClick={openAdd} disabled={!enabled}><Plus size={16} />新增</button>
        </div>
        {!enabled ? (
          <p className="muted">记忆未启用。在上方开启记忆后即可浏览、搜索和管理。</p>
        ) : (
          <>
            <div className="field-row mem-search-row">
              <input value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") doSearch(); }} placeholder="搜索记忆内容..." />
              <button className="secondary" onClick={doSearch} disabled={loading}>搜索</button>
            </div>
            {error && (
              <div className="mem-error">
                {error}
              </div>
            )}
            {loading ? (
              <p className="muted mem-loading"><LoaderCircle className="spin" size={16} /> 加载中...</p>
            ) : memories.length === 0 ? (
              <p className="muted">暂无记忆。点击"新增"添加第一条。</p>
            ) : (
              <div className="mem-table-wrap">
                <table className="mem-table">
                  <thead>
                    <tr>
                      <th className="col-uri">URI</th>
                      <th className="col-cat">分类</th>
                      <th className="col-abs">摘要</th>
                      <th className="col-time">创建时间</th>
                      <th className="col-acts">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {memories.map((m) => (
                      <tr key={m.uri}>
                        <td className="col-uri" title={m.uri}>{m.uri}</td>
                        <td className="col-cat">{m.category || "—"}</td>
                        <td className="col-abs">{m.abstract || (m.content || "").slice(0, 80)}</td>
                        <td className="col-time">{formatTime(m.created_at)}</td>
                        <td className="col-acts">
                          <button className="icon-btn mem-pill" onClick={() => openEdit(m.uri)} title="编辑"><Save size={14} /></button>
                          <button className="icon-btn danger mem-pill" onClick={() => deleteMemory(m.uri)} title="删除"><Trash2 size={14} /></button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="mem-paginate">
              <span className="subtle">{memories.length > 0 ? `${offset + 1}–${offset + memories.length} 条` : "无结果"}</span>
              <div className="mem-paginate-actions">
                <button className="secondary" onClick={prevPage} disabled={offset === 0 || loading}>上一页</button>
                <button className="secondary" onClick={nextPage} disabled={!hasMore || loading}>下一页</button>
              </div>
            </div>
          </>
        )}
      </section>

      <section className="panel span-4">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Clock3 size={15} /> 活动流</span>
            <h2>近期活动</h2>
          </div>
          <button className="icon-btn" onClick={fetchActivity} title="刷新活动"><RefreshCw size={16} /></button>
        </div>
        {!enabled ? (
          <p className="muted">记忆未启用。</p>
        ) : activity.length === 0 ? (
          <p className="muted">暂无活动。</p>
        ) : (
          <div className="tiny-models mem-activity-list">
            {activity.map((a, i) => (
              <div className="check-line mem-act-row" key={i}>
                <div>
                  <strong>{activityLabel(a.kind)}</strong>
                  <span>{activitySnippet(a.value)}</span>
                </div>
                <em>{formatTime(a.created_at)}</em>
              </div>
            ))}
          </div>
        )}
      </section>

      <section className="panel span-12">
        <div className="panel-head">
          <div>
            <span className="eyebrow"><Sparkles size={15} /> 召回测试</span>
            <h2>检索验证</h2>
          </div>
        </div>
        <p className="muted">输入查询语句，验证记忆检索效果。返回最相关的 5 条结果及其分数。</p>
        <div className="field-row mem-recall-row-action">
          <input value={recallQuery} onChange={(e) => setRecallQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") runRecall(); }} placeholder="例如：用户喜欢什么音乐" disabled={!enabled} />
          <button className="primary" onClick={runRecall} disabled={!enabled || recalling || !recallQuery.trim()}>
            {recalling ? <LoaderCircle className="spin" size={16} /> : null}
            {recalling ? "检索中" : "检索"}
          </button>
        </div>
        {recallMs != null && (
          <span className="subtle mem-recall-stats">{recallHits.length} 条命中 · {recallMs}ms</span>
        )}
        {recallHits.length > 0 && (
          <div className="tiny-models mem-recall-hits">
            {recallHits.map((h, i) => (
              <div className="suggestion-card" key={i}>
                <div className="mem-recall-row">
                  <strong>{h.abstract || (h.content || "").slice(0, 120)}</strong>
                  <em>{Number(h.score).toFixed(3)}</em>
                </div>
                <span className="mem-recall-uri">{h.uri}</span>
                <span className="mem-recall-meta">{h.source || "—"}{h.category ? ` · ${h.category}` : ""}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      {editor && (
        <div
          className="mem-modal-backdrop"
          onClick={() => { if (!editorSaving) setEditor(null); }}
        >
          <div className="panel mem-modal" onClick={(e) => e.stopPropagation()}>
            <div className="panel-head">
              <div>
                <span className="eyebrow">{editor.uri ? "编辑记忆" : "新增记忆"}</span>
                <h2>{editor.uri ? "修改内容" : "添加一条记忆"}</h2>
              </div>
              <button className="icon-btn mem-modal-close" onClick={() => { if (!editorSaving) setEditor(null); }} title="关闭">×</button>
            </div>
            {editor.uri && (
              <code className="mem-modal-uri">{editor.uri}</code>
            )}
            <label className="field">
              <span>内容</span>
              <textarea className="mem-editor-textarea" value={editor.content} onChange={(e) => setEditor({ ...editor, content: e.target.value })} placeholder="记忆的完整内容..." />
            </label>
            <div className="field-row">
              <label className="field">
                <span>分类</span>
                <input value={editor.category} onChange={(e) => setEditor({ ...editor, category: e.target.value })} placeholder="manual" />
              </label>
              <label className="field">
                <span>标签（逗号分隔）</span>
                <input value={editor.tags} onChange={(e) => setEditor({ ...editor, tags: e.target.value })} placeholder="偏好,事实" />
              </label>
            </div>
            <div className="mem-modal-actions">
              <button className="secondary" onClick={() => setEditor(null)} disabled={editorSaving}>取消</button>
              <button className="primary" onClick={saveEditor} disabled={editorSaving || !editor.content.trim()}>
                {editorSaving ? <LoaderCircle className="spin" size={16} /> : null}
                {editorSaving ? "保存中" : "保存"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
