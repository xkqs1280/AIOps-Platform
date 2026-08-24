/**
 * 语音告警引擎（行业标准版：音效为主 + 可选 TTS，完全离线可用）
 *
 * - 音效：Web Audio 实时合成（无需任何音频文件）
 *     critical  严重 = 三声急促报警音
 *     major     重要 = 两声提示音
 *     recovered 恢复 = 柔和上行音
 * - 可选 TTS：speechSynthesis 念出告警内容（需系统中文语音，无则仅音效）
 * - 播报模式 mode：sound（仅音效）/ tts（仅语音）/ both（音效+语音）
 * - 级别过滤、静音时段、音量、浏览器解锁、未解锁缓存补播、去重节流
 */
const CFG_KEY = 'voiceAlertConfig'
const SEEN_KEY = 'voiceAlertSeen'

const LEVEL_RANK = { minor: 1, major: 2, critical: 3 }
const LEVEL_TEXT = { critical: '严重告警', major: '重要告警', minor: '一般告警' }
const SEEN_MAX = 300
const BURST_WINDOW_MS = 30000
const BURST_MAX = 3

const DEFAULTS = {
  enabled: false,        // 总开关
  level: 'critical',     // 播报阈值：critical / major / minor
  mode: 'sound',         // sound / tts / both
  volume: 0.8,           // 0-1
  foregroundOnly: true,  // 仅前台页面播放
  muteStart: 0,          // 静音时段开始小时(0-23)；start===end 表示不静音
  muteEnd: 0,
}

let cfg = loadConfig()
let audioCtx = null
let unlocked = false
let voices = []
let voicesReady = false
let lastSpeakAt = 0
let burstCount = 0
// 未解锁时收到的告警暂存，用户交互解锁后立即补播
let pendingAlerts = []

// ---------------- 配置 ----------------
function loadConfig() {
  try {
    const raw = localStorage.getItem(CFG_KEY)
    return raw ? { ...DEFAULTS, ...JSON.parse(raw) } : { ...DEFAULTS }
  } catch {
    return { ...DEFAULTS }
  }
}

export function getConfig() {
  return { ...cfg }
}

export function setConfig(patch) {
  cfg = { ...cfg, ...patch }
  try {
    localStorage.setItem(CFG_KEY, JSON.stringify(cfg))
  } catch { /* ignore */ }
  return getConfig()
}

// ---------------- 静音时段 ----------------
function inMuteWindow() {
  const { muteStart, muteEnd } = cfg
  if (!muteStart && !muteEnd) return false
  if (muteStart === muteEnd) return false
  const h = new Date().getHours()
  if (muteStart < muteEnd) return h >= muteStart && h < muteEnd
  return h >= muteStart || h < muteEnd // 跨天时段
}

// ---------------- 解锁（浏览器自动播放限制） ----------------
export function isUnlocked() {
  return unlocked
}

/** 必须在用户手势（点击）中调用 */
export function unlock() {
  unlocked = true
  if (window.AudioContext || window.webkitAudioContext) {
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)()
      if (audioCtx.state === 'suspended') audioCtx.resume()
    } catch { /* ignore */ }
  }
  if ('speechSynthesis' in window) {
    try {
      const u = new SpeechSynthesisUtterance(' ')
      u.volume = 0
      speechSynthesis.speak(u)
      speechSynthesis.resume()
    } catch { /* ignore */ }
  }
  // 补播解锁前缓存的告警
  if (pendingAlerts.length) {
    const batch = pendingAlerts.splice(0, pendingAlerts.length)
    batch.forEach((a) => {
      if (a._recovered) _playRecoveredNow(a)
      else _playNow(a)
    })
  }
}

// ---------------- 音频 ----------------
function ensureAudioCtx() {
  if (!audioCtx && (window.AudioContext || window.webkitAudioContext)) {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)()
  }
  if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume()
}

/** Web Audio 合成音效（完全离线，无音频文件依赖） */
function beep(kind) {
  ensureAudioCtx()
  if (!audioCtx) return
  try {
    const vol = Math.max(0, Math.min(1, cfg.volume || 0.8))
    const now = audioCtx.currentTime
    const notes = {
      critical:  [[0, 880, 0.18], [0.22, 880, 0.18], [0.44, 660, 0.25]],
      major:     [[0, 660, 0.22], [0.28, 660, 0.22]],
      recovered: [[0, 523, 0.25], [0.3, 784, 0.4]],
    }[kind] || [[0, 660, 0.2]]
    for (const [start, freq, dur] of notes) {
      const osc = audioCtx.createOscillator()
      const gain = audioCtx.createGain()
      osc.type = 'sine'
      osc.frequency.value = freq
      gain.gain.setValueAtTime(0.001, now + start)
      gain.gain.exponentialRampToValueAtTime(vol * 0.45, now + start + 0.02)
      gain.gain.exponentialRampToValueAtTime(0.001, now + start + dur)
      osc.connect(gain).connect(audioCtx.destination)
      osc.start(now + start)
      osc.stop(now + start + dur + 0.05)
    }
  } catch { /* ignore */ }
}

function ensureVoices() {
  if (!('speechSynthesis' in window)) return []
  if (voicesReady) return voices
  voices = speechSynthesis.getVoices() || []
  speechSynthesis.onvoiceschanged = () => {
    voices = speechSynthesis.getVoices() || []
    voicesReady = true
  }
  if (voices.length) voicesReady = true
  return voices
}

function pickVoice() {
  const vs = ensureVoices()
  if (!vs.length) return null
  const localZh = vs.find((v) => v.localService && v.lang && v.lang.toLowerCase().startsWith('zh'))
  if (localZh) return localZh
  const localAny = vs.find((v) => v.localService)
  if (localAny) return localAny
  return vs.find((v) => v.lang && v.lang.toLowerCase().startsWith('zh')) || vs[0]
}

function speak(text) {
  if (!('speechSynthesis' in window) || cfg.mode === 'sound') return
  try {
    const u = new SpeechSynthesisUtterance(text)
    const v = pickVoice()
    if (v) {
      u.voice = v
      u.lang = v.lang || 'zh-CN'
    } else {
      u.lang = 'zh-CN'
    }
    u.rate = 1
    u.volume = Math.max(0.05, cfg.volume || 0.8)
    speechSynthesis.speak(u)
  } catch { /* ignore */ }
}

// ---------------- 播报 ----------------
function _playNow(item) {
  if (inMuteWindow()) return
  const kind = item.severity || 'major'
  if (cfg.mode === 'sound' || cfg.mode === 'both') beep(kind)
  if (cfg.mode === 'tts' || cfg.mode === 'both') {
    const dev = item.device_name ? `${item.device_name}（${item.ip || ''}）` : (item.ip ? `设备（${item.ip}）` : '设备')
    speak(`${LEVEL_TEXT[kind] || '告警'}：${dev}，${item.message || item.rule_name || ''}`)
  }
}

function _playRecoveredNow(item) {
  if (inMuteWindow()) return
  if (cfg.mode === 'sound' || cfg.mode === 'both') beep('recovered')
  if (cfg.mode === 'tts' || cfg.mode === 'both') {
    const dev = item.device_name ? `${item.device_name}（${item.ip || ''}）` : (item.ip ? `设备（${item.ip}）` : '设备')
    speak(`设备${dev}已恢复正常`)
  }
}

/** 收到新告警（SSE 推送） */
export function playAlert(item) {
  if (!cfg.enabled || !item) return
  const minRank = LEVEL_RANK[cfg.level] ?? 3
  if ((LEVEL_RANK[item.severity] ?? 0) < minRank) return
  if (!unlocked) {
    pendingAlerts.push(item)
    if (pendingAlerts.length > 50) pendingAlerts.splice(0, pendingAlerts.length - 50)
    return
  }
  if (cfg.foregroundOnly && document.hidden) return
  const seen = loadSeen()
  if (seen.has(String(item.id))) return
  markSeen([String(item.id)])
  // 节流：30s 内最多逐条播 BURST_MAX 条
  const now = Date.now()
  if (now - lastSpeakAt > BURST_WINDOW_MS) burstCount = 0
  if (burstCount >= BURST_MAX) {
    speak(`当前共有新告警，请查看告警管理。`)
    return
  }
  burstCount += 1
  lastSpeakAt = now
  _playNow(item)
}

/** 收到恢复事件（SSE 推送），不受级别限制 */
export function playRecovered(item) {
  if (!cfg.enabled || !item) return
  if (!unlocked) {
    pendingAlerts.push({ ...item, _recovered: true })
    return
  }
  if (cfg.foregroundOnly && document.hidden) return
  const seen = loadSeen()
  const key = `r${item.id}`
  if (seen.has(key)) return
  markSeen([key])
  _playRecoveredNow(item)
}

// ---------------- 已播 id（跨标签去重） ----------------
function loadSeen() {
  try {
    const raw = localStorage.getItem(SEEN_KEY)
    return new Set(raw ? JSON.parse(raw) : [])
  } catch {
    return new Set()
  }
}

function markSeen(ids) {
  try {
    const s = loadSeen()
    ids.forEach((i) => s.add(i))
    const arr = Array.from(s)
    if (arr.length > SEEN_MAX) arr.splice(0, arr.length - SEEN_MAX)
    localStorage.setItem(SEEN_KEY, JSON.stringify(arr))
  } catch { /* ignore */ }
}

/** 试听：播放一段提示音 + 可选语音，返回环境检测结果 */
export function test() {
  if (!cfg.enabled) setConfig({ enabled: true })
  unlocked = true
  ensureAudioCtx()
  if (cfg.mode === 'tts') {
    speak('语音告警测试，当前环境播报正常。')
  } else {
    beep('critical')
    setTimeout(() => beep('recovered'), 800)
  }
  const vs = ensureVoices()
  const localZh = vs.some((v) => v.localService && v.lang && v.lang.toLowerCase().startsWith('zh'))
  return {
    hasSpeech: 'speechSynthesis' in window,
    voiceCount: vs.length,
    localChineseVoice: localZh,
    voiceName: pickVoice()?.name || null,
    audioSupport: !!(window.AudioContext || window.webkitAudioContext),
  }
}
