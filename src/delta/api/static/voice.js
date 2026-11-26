// Voice mode: talk to Delta hands-free and hear it answer, like a call. Press the mic in the chat box to start or stop.
// Listening: the microphone stays on (echo cancellation and noise suppression on) and a simple loudness check finds
// where you start and stop talking, so there is no button to hold. Each turn is turned into text (Groq Whisper, the
// same free service as Telegram voice notes: Hindi, Telugu, English and mixes) and sent as a normal chat message,
// marked as spoken so the answer comes back short and plain. Speaking: the answer is read aloud with this PC's own
// Windows voices, in the language you spoke. Talk over Delta and it stops to listen (barge-in). "Wake word" mode
// answers only when a turn starts with "Hey Delta" (then follow-ups for 20 seconds without it). Tri listens and
// talks along. Nothing is recorded when voice mode is off, and recordings are never stored.
(() => {
  "use strict";
  const D = window.Delta, {$, h, icon} = D;
  const SPEECH = 0.035;          // loudness that counts as talking (RMS of the mic signal, 0..1)
  const BARGE = 0.09;            // louder, to talk over Delta (its own voice may reach the mic)
  const START_MS = 160;          // talking this long starts a turn
  const END_MS = 750;            // quiet this long ends it
  const MAX_TURN_MS = 30000, IDLE_RESTART_MS = 12000, FOLLOW_MS = 20000;
  const JUNK = /^(thank you\.?|thanks for watching!?|you|bye\.?|\.+|subtitles by .*)$/i;  // Whisper's guesses on silence
  const WAKE = /^\s*(?:hey|hi|hello|ok|okay)[\s,!.]*(?:delta|delhi|dealta|deltaa)\b[\s,!.]*/i;
  const LANGS = {english: "en", hindi: "hi", telugu: "te", tamil: "ta", kannada: "kn", malayalam: "ml", marathi: "mr", bengali: "bn",
    gujarati: "gu", punjabi: "pa", urdu: "ur", spanish: "es", french: "fr", german: "de", italian: "it", portuguese: "pt", arabic: "ar",
    japanese: "ja", korean: "ko", chinese: "zh", russian: "ru", indonesian: "id", turkish: "tr", dutch: "nl", nepali: "ne"};

  let on = false, wakeOnly = D.pref.get("delta_wake") === "1", stream = null, ctx = null, analyser = null, timer = 0;
  let rec = null, chunks = [], recStart = 0, talking = false, loudSince = 0, quietSince = 0, lastWake = 0;
  let speaking = false, waiting = "", lang = "en", queue = [];
  const btn = h("button", {type: "button", class: "attach-btn voice-btn", id: "voicebtn", "aria-label": "Talk to Delta (voice mode)", title: "Talk to Delta (voice mode)", "aria-pressed": "false"}, icon("mic"));
  const label = h("span", {class: "voice-state", text: ""});
  const wake = h("label", {class: "voice-wake"}, h("input", {type: "checkbox", checked: wakeOnly, onchange: e => { wakeOnly = e.target.checked; D.pref.set("delta_wake", wakeOnly ? "1" : null); show(); }}), h("span", {text: "Only after “Hey Delta”"}));
  const bar = h("div", {class: "voice-bar", hidden: true, role: "status", "aria-live": "polite"}, h("span", {class: "voice-dot"}), label, wake,
    h("button", {type: "button", class: "btn sm", onclick: () => stop(), text: "End voice mode"}));
  $("sendbtn").before(btn);
  $("send").prepend(bar);

  const pet = (mood, line) => { if (D.pet) { D.pet.set(mood); if (line) D.pet.say(line, 2500); } };
  function show(text) {
    bar.hidden = !on; btn.classList.toggle("on", on); btn.setAttribute("aria-pressed", String(on));
    if (!on) return;
    label.textContent = text || (speaking ? "Speaking. Talk to interrupt." : waiting ? "Thinking" : talking ? "Listening" : wakeOnly && Date.now() - lastWake > FOLLOW_MS ? "Say “Hey Delta” to talk" : "Listening. Just talk.");
    bar.dataset.state = speaking ? "speaking" : waiting ? "thinking" : talking ? "hearing" : "ready";
  }

  // ---- microphone and turns
  async function start() {
    if (on) return;
    if (!navigator.mediaDevices || !window.MediaRecorder) { D.toast({title: "Voice mode needs a newer browser", kind: "warn"}); return; }
    try { stream = await navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true}}); }
    catch (e) { D.toast({title: "Delta could not use the microphone", body: "Allow the microphone for Delta's window and try again. " + D.plain(e), kind: "warn", timeout: 0}); return; }
    ctx = new AudioContext();
    analyser = ctx.createAnalyser(); analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    on = true; D.go("chat"); show(); pet("listening", "I'm listening!");
    newRecorder(); timer = setInterval(tick, 40);
  }
  function stop() {
    if (!on) return;
    on = false; clearInterval(timer); speechSynthesis.cancel(); speaking = false; queue = []; waiting = ""; talking = false;
    if (rec && rec.state !== "inactive") { rec.onstop = null; rec.stop(); }
    rec = null;
    if (stream) stream.getTracks().forEach(t => t.stop());
    if (ctx) ctx.close().catch(() => {});
    stream = ctx = analyser = null;
    show(); pet("idle");
  }
  function newRecorder() {
    if (!on) return;
    const type = MediaRecorder.isTypeSupported("audio/webm;codecs=opus") ? "audio/webm;codecs=opus" : "audio/webm";
    chunks = []; rec = new MediaRecorder(stream, {mimeType: type, audioBitsPerSecond: 32000});
    rec.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    rec.start(250); recStart = Date.now();
  }
  function finishTurn(keep) {
    const r = rec, got = chunks;
    if (!r) return;
    r.onstop = () => { if (keep) send(new Blob(got, {type: "audio/webm"})); };
    r.stop(); newRecorder();  // the next turn is being recorded already, so its first word is never lost
  }
  function level() {
    const buf = new Float32Array(analyser.fftSize); analyser.getFloatTimeDomainData(buf);
    let s = 0; for (const v of buf) s += v * v;
    return Math.sqrt(s / buf.length);
  }
  function tick() {
    if (!on || !analyser) return;
    const now = Date.now(), lv = level(), loud = lv > (speaking ? BARGE : SPEECH);
    if (loud) { quietSince = 0; if (!loudSince) loudSince = now; } else { loudSince = 0; if (!quietSince) quietSince = now; }
    if (!talking && loudSince && now - loudSince >= START_MS) {
      if (speaking) { speechSynthesis.cancel(); speaking = false; queue = []; }  // barge-in: you talk, Delta stops
      talking = true; show(); pet("listening");
    } else if (talking && ((quietSince && now - quietSince >= END_MS) || now - recStart > MAX_TURN_MS)) {
      talking = false; show(); finishTurn(true);
    } else if (!talking && now - recStart > IDLE_RESTART_MS) {
      finishTurn(false);  // nothing said: start a fresh recording so turns stay small
    }
  }

  // ---- a turn: to text, to Delta
  async function send(blob) {
    if (!on || blob.size < 2000) return;
    show("Understanding");
    let r;
    try {
      const res = await fetch("/api/voice/transcribe", {method: "POST", body: blob, headers: {"Authorization": "Bearer " + D.token, "Content-Type": "audio/webm"}});
      r = await res.json();
    } catch (e) { r = {error: D.plain(e)}; }
    if (!on) return;
    if (r.error) { show(); D.toast({title: "Voice: " + r.error, kind: "warn"}); return; }
    let text = String(r.text || "").trim();
    if (!text || JUNK.test(text)) { show(); return; }
    if (wakeOnly) {
      const woke = WAKE.test(text);
      if (!woke && Date.now() - lastWake > FOLLOW_MS) { show(); return; }
      if (woke) { lastWake = Date.now(); text = text.replace(WAKE, "").trim(); }
      if (!text) { pet("curious", "Yes?"); speak("Yes?"); return; }
    }
    lastWake = Date.now();
    lang = LANGS[r.language] || (r.language && r.language.length <= 3 ? r.language : "en");
    waiting = "pending"; show(); pet("thinking");
    D.voiceTurn = true; $("text").value = text; $("send").requestSubmit(); D.voiceTurn = false;
  }
  D.on("chat:started", s => { if (on && waiting === "pending" && s.spoken) { waiting = s.task_id; show(); } });
  D.on("ws", m => {
    if (!on || m.type !== "result" || !waiting || m.result.task_id !== waiting) return;
    waiting = ""; speak(m.result.answer || "");
  });

  // ---- speaking: this PC's voices, sentence by sentence, in the language you spoke
  function plain(text) {
    return String(text).replace(/```[\s\S]*?```/g, " ").replace(/`([^`]*)`/g, "$1").replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/https?:\/\/\S+/g, "").replace(/[*_#>|]+/g, " ").replace(/\p{Extended_Pictographic}/gu, "").replace(/\s+/g, " ").trim();
  }
  let tipped = "";
  function voiceFor(code) {
    const all = speechSynthesis.getVoices(), mine = all.filter(v => v.lang.toLowerCase().startsWith(code));
    const pref = mine.find(v => /natural|neural/i.test(v.name)) || mine.find(v => /india|-IN/i.test(v.lang + v.name)) || mine[0];
    if (!pref && code !== "en" && tipped !== code) {  // once per language: how to get a voice for it
      tipped = code;
      D.toast({title: "No voice for this language on this PC yet", body: "Windows Settings > Time & language > Speech > Manage voices > Add voices. Until then Delta reads it with an English voice.", kind: "warn", timeout: 9000});
    }
    return pref || all.find(v => v.lang.toLowerCase().startsWith("en")) || null;
  }
  function speak(text) {
    const words = plain(text);
    if (!on || !words) { show(); pet("listening"); return; }
    queue = words.match(/[^.!?।]+[.!?।]*\s*/g) || [words];
    speaking = true; show(); pet("talking");
    next();
  }
  function next() {
    if (!on || !speaking) return;
    const part = queue.shift();
    if (!part) { speaking = false; show(); pet("listening"); return; }
    const u = new SpeechSynthesisUtterance(part.slice(0, 280));
    const v = voiceFor(lang); if (v) { u.voice = v; u.lang = v.lang; } else u.lang = lang;
    u.rate = 1.05;
    u.onend = u.onerror = () => next();
    speechSynthesis.speak(u);
  }
  speechSynthesis.getVoices();  // loads the voice list early

  btn.onclick = () => (on ? stop() : start());
  D.voice = {start, stop, say(text, code = "en") { lang = code; speak(text); }, get on() { return on; }};
})();
