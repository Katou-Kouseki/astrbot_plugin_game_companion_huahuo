(() => {
  "use strict";

  const match = window.location.pathname.match(/\/room\/([A-Za-z0-9_-]{8,80})\/?$/);
  const accessToken = match ? match[1] : "";
  const storageKey = `game-companion:${accessToken}:visitor`;
  const rememberIdentityKey = "game-companion:remember-identity";
  let ucPrevMyTurn = false;         // 上一帧本机是否处于发言轮，用于“轮到你了”提醒
  let ucTurnBannerDone = new Set();  // 已弹出“轮到 X号 发言”提示的 `${轮次}:${玩家号}`（按局重置），被“花火选词”遮罩延迟时也能补弹
  const ucSpeechTyping = {};         // key `${gameUid}:${round}:${pn}` -> {text,node,idx,started,done}，发言文字“逐字加速打出”动画
  let ucSpeechTypingRaf = null;
  let ucResultShownKey = "";         // 已展示过结算动画的本局标识（防重复弹出）
  let ucVoteRevealSet = new Set();   // 已播放票数揭晓动画的轮次号（防重复）
  let ucShownOutSet = new Set();      // 已播放淘汰动画的玩家编号（防重复，按局重置）
  let ucLastGameUid = "";             // 上一帧对局 uid（用于新一局重置动画/提示状态）
  let ucLastRoundCount = 0;          // 上一帧时间线已渲染的轮次数（用于新轮高亮）
  let ucVoteFlipTimeout = null;       // 票数「？」→数字翻拍的延时句柄
  let ucLastHostScalesKey = "";        // 上次渲染的房主阵营比例键（用于仅在校验变化时回填输入框）
  let ucLastResult = null;             // 本局结果快照，供「分享战报」canvas 生成 PNG
  let ucShownResultUid = "";           // 当前结算卡已展示的游戏 uid（用于跨轮询只在换局时重置按钮状态）
  let ucRecapCache = {};               // uid -> 复盘文案：同局缓存，不重复请求 LLM
  let ucPrevMyOut = false;             // 上一帧本机是否已出局（用于触发出局提示）
  const ucRevealNodeCache = {};        // key `${gameUid}:${round}` -> {node, at}，让票数揭晓动画跨轮询存活

  // 发言文字“逐字加速打出”：驱动所有在播的发言节点按“先慢后快”的进度曲线逐字展示。
  // 时间线每次轮询会重建 DOM，因此这里缓存节点并在重建时重新挂载，保证动画跨轮询连续。
  // 时间线自动跟随：仅在“贴底”（用户在看最新消息）时把滚动条钉在最底部；
  // 玩家向上翻看历史时绝不强制下拉，不干扰手动滑动。
  function ucAutoFollowTimeline(el) {
    if (!el || !el.isConnected) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 64) {
      el.scrollTop = el.scrollHeight;
    }
  }
  function ucDriveSpeechTyping() {
    const now = performance.now();
    let active = false;
    let typingInTimeline = false;
    for (const k in ucSpeechTyping) {
      const t = ucSpeechTyping[k];
      if (!t.text || !t.node || !t.node.isConnected) { t.done = true; t.node = null; continue; }
      if (t.done) continue;
      if (t.node.closest && t.node.closest("#ucTimeline")) typingInTimeline = true;
      const duration = Math.min(2400, 360 + t.text.length * 55); // 越短越快，一两秒内打完
      const p = Math.min(1, (now - t.started) / duration);
      const revealed = Math.floor(Math.pow(p, 1.7) * t.text.length); // 先慢后快 = 逐字加速
      if (revealed !== t.idx) { t.idx = revealed; t.node.textContent = t.text.slice(0, revealed); }
      if (p < 1) { active = true; }
      else { t.done = true; t.node.textContent = t.text; t.node = null; }
    }
    // 打字过程会让时间线高度缓慢增长：贴底跟随的玩家保持在最新一行可见
    if (typingInTimeline) ucAutoFollowTimeline(document.getElementById("ucTimeline"));
    if (active) { ucSpeechTypingRaf = requestAnimationFrame(ucDriveSpeechTyping); }
    else { ucSpeechTypingRaf = null; }
  }
  function ucStartSpeechTyping(key, text, container) {
    const span = document.createElement("span");
    span.className = "uc-speech-typing";
    container.appendChild(span);
    ucSpeechTyping[key] = { text, node: span, idx: 0, started: performance.now(), done: false };
    if (ucSpeechTypingRaf == null) ucSpeechTypingRaf = requestAnimationFrame(ucDriveSpeechTyping);
  }
  const mobileVisitorToken = new URLSearchParams(window.location.search).get("visitor_token") || "";
  const board = document.getElementById("board");
  const boardStage = document.querySelector(".board-stage");
  const soupStage = document.getElementById("soupStage");
  const diceStage = document.getElementById("diceStage");
  const blackjackStage = document.getElementById("blackjackStage");
  const drawStage = document.getElementById("drawStage");
  const undercoverStage = document.getElementById("undercoverStage");
  const drawCanvas = document.getElementById("drawCanvas");
  const drawContext = drawCanvas.getContext("2d");
  const chatInput = document.getElementById("chatInput");
  const context = board.getContext("2d");
  const toast = document.getElementById("toast");
  let visitorToken = accessToken
    ? window.localStorage.getItem(storageKey) || mobileVisitorToken
    : "";
  let room = null;
  let selectedSide = "human_black";
  let selectedPiece = null;
  let pollTimer = 0;
  let toastTimer = 0;
  let busy = false;
  let chatBusy = false;
  let pendingMove = null;
  let activeRoomView = "game";
  let lastSeenMessageId = 0;
  let renderedDiceSequence = 0;
  let drawStrokes = [];
  let activeDrawStroke = null;
  let drawSyncBusy = false;
  let drawSyncPromise = null;
  let drawDirty = false;
  let drawRevision = -1;

  /* ============================================================
     Canvas 游戏（五子棋/井字棋/象棋/你画我猜）深/浅配色
     - 所有颜色按"暗色 vs 浅色"分别给值
     - 每个 canvas 重绘函数取对应 palette
     ============================================================ */
  function isDarkTheme() {
    return document.body.classList.contains("dark");
  }

  function getGamePalette(gameType) {
    const dark = isDarkTheme();
    switch (gameType) {
      case "tictactoe":
        return dark
          ? {
              bg: "#2a2a2e",
              line: "#d2d7d4",
              x: "#e07b71",
              o: "#6fb8d0",
              lastMoveHint: "rgba(85, 192, 150, .16)",
            }
          : {
              bg: "#f3f0e8",
              line: "#3e4a44",
              x: "#a33d35",
              o: "#236a72",
              lastMoveHint: "rgba(33, 92, 69, .09)",
            };
      case "gomoku":
        return dark
          ? {
              board: "#3a3530",
              line: "#cbb998",
              star: "#b3a48b",
              blackStone: "#151514",
              whiteStone: "#e9ecef",
              whiteEdge: "#6b7271",
              lastMoveDot: "#e6857c",
            }
          : {
              board: "#d4a85f",
              line: "#5d472c",
              star: "#4a3823",
              blackStone: "#242724",
              whiteStone: "#f7f8f5",
              whiteEdge: "#9da39e",
              lastMoveDot: "#b8483c",
            };
      case "xiangqi":
        return dark
          ? {
              board: "#37332a",
              line: "#ccb48a",
              river: "#a58d60",
              red: "#e08a79",
              blackText: "#cdd8d3",
              pieceBg: "#2d2b27",
              pieceRing: "#7a684a",
              legalHint: "rgba(85, 192, 150, .72)",
              lastBox: "#e6857c",
            }
          : {
              board: "#d7a85d",
              line: "#563c23",
              river: "#654528",
              red: "#8b2b24",
              blackText: "#1e2220",
              pieceBg: "#f1d49a",
              pieceRing: "#704e22",
              legalHint: "rgba(28, 104, 70, .72)",
              lastBox: "#b43e35",
            };
      default:
        return null;
    }
  }

  /* ============================================================
     花火打字机动画 + 思考中占位
     - TYPED_PROGRESS：key -> { shownChars, timerId, fullText }
     - TYPED_DONE：已显示完整的消息 key（下次 render 直接跳过动画）
     ============================================================ */
  const TYPED_PROGRESS = new Map();
  const TYPED_DONE = new Set();
  // 每 tick 推进的字符数：1 时平稳，2 时偶尔加速，更有真人感
  const TYPING_BASE_MS = 22;
  const TYPING_JITTER_MS = 18;
  // 思考中占位超时阈值：最后一条玩家消息超过 N 秒还没有 bot 回复，就不再显示了
  const THINKING_TIMEOUT_SEC = 90;

  /** 消息稳定ID（同一消息跨 refresh 不变，用于延续打字机进度） */
  function messageKey(message) {
    const tsMs = Math.round(Number(message.ts || 0) * 1000);
    const sender = String(
      message.role === "bot" ? "花火" : (message.sender_name || message.sender_number || "?")
    );
    const raw = String(message.content || "");
    // 首 24 字符参与指纹，避免长文本拼接过慢
    const head = raw.length > 24 ? raw.slice(0, 24) + raw.length.toString(36) : raw;
    return `${tsMs.toString(36)}|${sender.length.toString(36)}|${head}`;
  }

  /**
   * 停止并清理所有现存打字机定时器（每次 replaceChildren 全量重绘前必须调用，
   * 避免旧 DOM 上的定时器继续跑造成内存泄漏）
   */
  function teardownAllTypingTimers() {
    for (const state of TYPED_PROGRESS.values()) {
      if (state.timerId) {
        clearTimeout(state.timerId);
        state.timerId = null;
      }
    }
  }

  /** 某条 bot 消息的打字机主循环：按自然间隔推进一步，然后 setTimeout 自调度 */
  function runTypingTick(contentEl, key, fullText, onProgress) {
    const state = TYPED_PROGRESS.get(key);
    if (!state) return;
    if (state.shownChars >= fullText.length) {
      state.timerId = null;
      TYPED_DONE.add(key);
      TYPED_PROGRESS.delete(key);
      onProgress?.(fullText, /* done */ true);
      return;
    }
    // 偶尔在句末 / 标点后多停顿一下，模仿真人思考节奏
    const nextChar = fullText.charAt(state.shownChars);
    const isPunctuation = /[。，、！？!?.;:：；,.]/.test(nextChar);
    const step = isPunctuation ? 1 : (Math.random() < 0.15 ? 2 : 1);
    state.shownChars = Math.min(fullText.length, state.shownChars + step);
    const currentText = fullText.slice(0, state.shownChars);
    // 推进后调用回调：更新 content 文本 + 滚到底
    onProgress?.(currentText, false);
    if (state.shownChars >= fullText.length) {
      state.timerId = null;
      TYPED_DONE.add(key);
      TYPED_PROGRESS.delete(key);
      onProgress?.(fullText, true);
      return;
    }
    const delay = isPunctuation
      ? TYPING_BASE_MS + 110 + Math.floor(Math.random() * TYPING_JITTER_MS)
      : TYPING_BASE_MS + Math.floor(Math.random() * TYPING_JITTER_MS);
    state.timerId = setTimeout(() => runTypingTick(contentEl, key, fullText, onProgress), delay);
  }

  /**
   * 是否需要展示"花导思考中…"占位：
   * 条件：
   * 1. 有历史消息
   * 2. 最后一条非 system 消息是玩家（user）发的
   * 3. 距该条玩家消息发出时间 < THINKING_TIMEOUT_SEC 秒
   * 4. 该条玩家消息之后再也没有 bot 的发言
   */
  function shouldShowThinking(messages, nowSec) {
    if (!messages || !messages.length) return false;
    let lastBotIdx = -1;
    let lastUserIdx = -1;
    for (let i = messages.length - 1; i >= 0; i--) {
      const role = messages[i].role || "system";
      if (lastBotIdx === -1 && role === "bot") { lastBotIdx = i; continue; }
      if (lastUserIdx === -1 && role === "user") { lastUserIdx = i; continue; }
      if (lastBotIdx !== -1 && lastUserIdx !== -1) break;
    }
    if (lastUserIdx === -1) return false;
    // 最近一条是 bot 回复了 → 不再思考
    if (lastBotIdx > lastUserIdx) return false;
    const ts = Number(messages[lastUserIdx].ts || 0);
    if (!ts) return false;
    return nowSec - ts < THINKING_TIMEOUT_SEC;
  }

  /* ============================================================
     文本过滤：去掉 QQ 表情代码(如 &&happy&&)，统一 Bot→花火，
     统一 "游戏伴侣"→"花火陪你玩"
     ============================================================ */
  const EMOTICON_MAP = {
    happy: "😊", laugh: "😂", smile: "🙂", grin: "😁", cheerful: "😄", joy: "🤗",
    love: "🥰", like: "❤️", heart: "💗", kiss: "😘", shy: "🥺", cute: "😽",
    sad: "😢", cry: "😭", disappointed: "😞",
    angry: "😠", mad: "😡",
    confused: "😕", thinking: "🤔", hmm: "🧐", worry: "😟",
    surprised: "😲", shocked: "😱",
    cool: "😎", sunglasses: "🕶️",
    sleepy: "😴", tired: "😩",
    sick: "🤒",
    playful: "😜", tease: "😝", naughty: "😈",
    clap: "👏", nice: "👍", good: "✨", star: "⭐", luck: "🍀",
    sorry: "💦", sweat: "💧",
  };
  const EMOTICON_REGEX = /&&([A-Za-z0-9_\u4e00-\u9fa5]{1,16})&&/g;

  /** 清洗显示文本：表情、Bot 字样、旧品牌名。对非字符串安全返回原值 */
  function sanitizeDisplayText(value) {
    if (value == null) return value;
    if (typeof value !== "string") return value;
    let out = value.replace(EMOTICON_REGEX, (_match, key) => {
      const k = String(key).toLowerCase();
      return EMOTICON_MAP[k] ?? "";
    });
    // 独立的 Bot 字样替换为花火（避免影响变量名，但此处只在展示文本中调用，安全）
    out = out.replace(/\bBot\b/g, "花火");
    out = out.replace(/游戏伴侣/g, "花火陪你玩");
    return out;
  }

  /* ============================================================
     主题 & 背景外观（localStorage 持久化）
     ============================================================ */
  const THEME_KEY = "game-companion:theme";
  const APPEARANCE_KEY = "game-companion:appearance";
  const DEFAULT_BG_URL = new URL("../../assets/background.webp", window.location.href).href;

  const DEFAULT_APPEARANCE = Object.freeze({
    enabled: true,   // 是否启用自定义背景
    dataUrl: null,   // 用户上传的 dataURL，null = 用内置默认图
    blur: 14,        // 0 ~ 30 px
    overlay: 0.75,   // 0 ~ 1 蒙层不透明度（1 = 纯主题色）
  });

  let appearance = loadAppearance();

  function loadTheme() {
    const saved = window.localStorage.getItem(THEME_KEY);
    if (saved === "light" || saved === "dark") return saved;
    // 首次：跟随系统
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return "light";
  }

  function applyTheme(theme) {
    document.body.classList.toggle("dark", theme === "dark");
    const iconEl = document.getElementById("themeIcon");
    if (iconEl) {
      iconEl.setAttribute("data-lucide", theme === "dark" ? "sun" : "moon");
      icons();
    }
    render();
  }

  function saveTheme(theme) {
    window.localStorage.setItem(THEME_KEY, theme);
  }

  function loadAppearance() {
    try {
      const raw = window.localStorage.getItem(APPEARANCE_KEY);
      // 首次访问：默认直接启用内置背景图（写入 localStorage，避免后续 load/save 不一致）
      if (!raw) {
        const defaults = { ...DEFAULT_APPEARANCE };
        try {
          window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify(defaults));
        } catch (_) { /* ignore */ }
        return defaults;
      }
      const parsed = JSON.parse(raw);
      return {
        enabled: typeof parsed.enabled === "boolean" ? parsed.enabled : DEFAULT_APPEARANCE.enabled,
        dataUrl: typeof parsed.dataUrl === "string" ? parsed.dataUrl : DEFAULT_APPEARANCE.dataUrl,
        blur: Number.isFinite(parsed.blur) ? Math.max(0, Math.min(30, parsed.blur)) : DEFAULT_APPEARANCE.blur,
        overlay: Number.isFinite(parsed.overlay) ? Math.max(0, Math.min(1, parsed.overlay)) : DEFAULT_APPEARANCE.overlay,
      };
    } catch (_e) {
      return { ...DEFAULT_APPEARANCE };
    }
  }

  function saveAppearance() {
    try {
      window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify({
        enabled: appearance.enabled,
        // 如果图片太大导致 localStorage 溢出，至少其他参数能保住
        dataUrl: null,
        blur: appearance.blur,
        overlay: appearance.overlay,
      }));
      // 尝试把 dataUrl 塞进去（失败则降级为无图）
      if (appearance.dataUrl) {
        const tmp = JSON.stringify({
          enabled: appearance.enabled,
          dataUrl: appearance.dataUrl,
          blur: appearance.blur,
          overlay: appearance.overlay,
        });
        window.localStorage.setItem(APPEARANCE_KEY, tmp);
      }
    } catch (err) {
      // 配额溢出：清除 dataUrl，至少保留开关和模糊度
      appearance.dataUrl = null;
      try {
        window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify({
          enabled: appearance.enabled,
          dataUrl: null,
          blur: appearance.blur,
          overlay: appearance.overlay,
        }));
      } catch (_) { /* ignore */ }
      showToast("图片太大，无法保存到浏览器；本次已启用，但刷新后会回到内置图。");
    }
  }

  /**
   * 通过 CSS 自定义属性 + body class（bg-on）来驱动背景外观。
   * 不能再用 <style> 动态注入整段文本 —— 浏览器 CSP "style-src 'self'" 会直接拦截，
   * 背景完全不生效还会报错。改用 classList.toggle + CSSOM.setProperty 这两个 CSP 不拦截的操作。
   */
  function applyAppearance() {
    const root = document.documentElement;
    const body = document.body;
    // 1. 开关：控制伪元素显示与否、body 是否透明露出背景图
    body.classList.toggle("bg-on", !!appearance.enabled);
    // 2. 背景图：设置 CSS 变量。 URL 中含引号先转义防止 url("…") 闭合逃逸。
    const imageUrl = (appearance.dataUrl || DEFAULT_BG_URL).replace(/"/g, '\\"');
    root.style.setProperty("--bg-image", `url("${imageUrl}")`);
    // 3. 模糊度
    root.style.setProperty("--bg-blur", `${appearance.blur}px`);
    // 4. 蒙层不透明度（写在 CSS 渐变里的 rgba alpha 中）
    root.style.setProperty("--bg-opacity", String(appearance.overlay));

    // 同步 UI 控件显示
    const bgEnabled = document.getElementById("bgEnabled");
    const blurRange = document.getElementById("blurRange");
    const blurValue = document.getElementById("blurValue");
    const overlayRange = document.getElementById("overlayRange");
    const overlayValue = document.getElementById("overlayValue");
    const fileLabel = document.getElementById("bgFileLabel");
    if (bgEnabled) bgEnabled.checked = appearance.enabled;
    if (blurRange) blurRange.value = String(appearance.blur);
    if (blurValue) blurValue.textContent = `${appearance.blur} px`;
    if (overlayRange) overlayRange.value = String(Math.round(appearance.overlay * 100));
    if (overlayValue) overlayValue.textContent = appearance.overlay.toFixed(2);
    if (fileLabel) {
      fileLabel.textContent = appearance.dataUrl
        ? "已使用我的背景图（点击可更换）"
        : "上传自己的背景图（推荐 16:9 ）";
    }
  }

  function initAppearance() {
    const theme = loadTheme();
    // 先应用主题，再应用背景（背景需要知道暗色/亮色）
    applyTheme(theme);
    applyAppearance();
  }

  function bindAppearanceControls() {
    const themeToggle = document.getElementById("themeToggle");
    if (themeToggle) {
      themeToggle.addEventListener("click", () => {
        const current = document.body.classList.contains("dark") ? "dark" : "light";
        const next = current === "dark" ? "light" : "dark";
        saveTheme(next);
        applyTheme(next);
        // 主题变化后，背景的蒙层颜色也要重绘
        applyAppearance();
      });
    }

    const panel = document.getElementById("appearancePanel");
    const openBtn = document.getElementById("appearanceBtn");
    const closeBtn = document.getElementById("appearanceClose");
    const mask = document.getElementById("appearanceMask");
    const openPanel = () => { if (panel) panel.hidden = false; icons(); };
    const closePanel = () => { if (panel) panel.hidden = true; };
    if (openBtn) openBtn.addEventListener("click", openPanel);
    if (closeBtn) closeBtn.addEventListener("click", closePanel);
    if (mask) mask.addEventListener("click", closePanel);
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && panel && !panel.hidden) closePanel();
    });

    const bgEnabled = document.getElementById("bgEnabled");
    if (bgEnabled) {
      bgEnabled.addEventListener("change", (event) => {
        appearance.enabled = !!event.target.checked;
        saveAppearance();
        applyAppearance();
      });
    }

    const blurRange = document.getElementById("blurRange");
    if (blurRange) {
      blurRange.addEventListener("input", (event) => {
        appearance.blur = Math.max(0, Math.min(30, parseInt(event.target.value || "0", 10)));
        applyAppearance();
      });
      blurRange.addEventListener("change", () => saveAppearance());
    }

    const overlayRange = document.getElementById("overlayRange");
    if (overlayRange) {
      overlayRange.addEventListener("input", (event) => {
        const v = parseInt(event.target.value || "0", 10) / 100;
        appearance.overlay = Math.max(0, Math.min(1, v));
        applyAppearance();
      });
      overlayRange.addEventListener("change", () => saveAppearance());
    }

    const bgFile = document.getElementById("bgFile");
    if (bgFile) {
      bgFile.addEventListener("change", (event) => {
        const file = event.target.files && event.target.files[0];
        event.target.value = ""; // 允许下次选择相同文件
        if (!file) return;
        if (!/^image\//.test(file.type)) {
          showToast("请选择图片格式文件");
          return;
        }
        const reader = new FileReader();
        reader.onerror = () => showToast("图片读取失败");
        reader.onload = () => {
          appearance.dataUrl = String(reader.result || "");
          appearance.enabled = true;
          saveAppearance();
          applyAppearance();
          showToast("背景已应用并保存在本机浏览器");
        };
        reader.readAsDataURL(file);
      });
    }

    const bgUseBuiltin = document.getElementById("bgUseBuiltin");
    if (bgUseBuiltin) {
      bgUseBuiltin.addEventListener("click", () => {
        appearance.dataUrl = null;
        appearance.enabled = true;
        saveAppearance();
        applyAppearance();
        showToast("已恢复内置默认背景图");
      });
    }
    const bgClear = document.getElementById("bgClear");
    if (bgClear) {
      bgClear.addEventListener("click", () => {
        appearance.dataUrl = null;
        saveAppearance();
        applyAppearance();
        showToast("已清除我的背景图，现使用内置默认图");
      });
    }
  }

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function endpoint(action, query = "") {
    return new URL(`../../api/room/${accessToken}/${action}${query}`, window.location.href).toString();
  }

  function showToast(message, duration = 2600) {
    if (!toast) return;
    window.clearTimeout(toastTimer);
    toast.textContent = sanitizeDisplayText(message);
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, Math.max(800, Number(duration) || 2600));
  }

  function showUcIdentityReveal(my) {
    const overlay = document.getElementById("ucRevealOverlay");
    if (!overlay || !my || (!my.camp && !my.word)) return;
    // 结束预热，展示正式身份牌
    const preheat = document.getElementById("ucPreheat");
    if (preheat) preheat.hidden = true;
    const cardEl = document.getElementById("ucRevealCard");
    if (cardEl) cardEl.hidden = false;
    const decktop = document.querySelector(".uc-reveal-decktop");
    const campEl = document.getElementById("ucRevealCamp");
    const wordEl = document.getElementById("ucRevealWord");
    const hintEl = document.getElementById("ucRevealHint");
    if (my.camp) {
      const campMap = {
        civilian: ["平民", "is-civilian", "你是平民：你的词条和大多数玩家一致。找到卧底，把卧底投票出局即可获胜。"],
        undercover: ["卧底", "is-undercover", "你是卧底：你的词条与多数人不同。隐藏好自己，把平民投票出局即可获胜。"],
        whiteboard: ["白板", "is-whiteboard", "你是白板：你没有词条。先模仿别人混入，等卧底全部出局后你就赢了。"],
      };
      const [campName, cls, hint] = campMap[my.camp] || [ucCampText(my.camp), "", "请妥善保管自己的词条，不要向其他玩家透露。"];
      if (decktop) decktop.textContent = "你的身份是";
      campEl.textContent = my.camp === "whiteboard" ? "白板" : campName;
      campEl.className = `uc-reveal-camp ${cls || ""}`;
      if (my.camp === "whiteboard") {
        wordEl.textContent = "无词条 · 靠猜";
      } else {
        wordEl.textContent = my.word || "—";
      }
      hintEl.textContent = hint;
    } else {
      // 关闭「告知身份」：只展示词条，不显示身份
      if (decktop) decktop.textContent = "本局你的词条";
      campEl.textContent = "词条已发放";
      campEl.className = "uc-reveal-camp is-word-only";
      wordEl.textContent = my.word || "—";
      hintEl.textContent = "本局未告知身份，请凭词条谨慎发言，不要向其他玩家透露。";
    }
    const card = document.getElementById("ucRevealCard");
    // 重新触发入场动画
    card.style.animation = "none";
    void card.offsetWidth;
    card.style.animation = "";
    overlay.hidden = false;
  }

  // 发词前先播放一段“甄选词条”预热，再弹出身份牌
  const UC_PREHEAT_MS = 6000;
  let ucPreheatTimer = null;
  let ucPreheatCounter = null;
  function showUcPreheatCountdown() {
    const secEl = document.getElementById("ucPreheatSec");
    if (!secEl) return;
    const end = Date.now() + UC_PREHEAT_MS;
    window.clearInterval(ucPreheatCounter);
    const tick = () => {
      const remain = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      secEl.textContent = remain > 0 ? `约 ${remain} 秒后发放…` : "";
    };
    tick();
    ucPreheatCounter = window.setInterval(tick, 500);
  }
  function revealUcIdentity(my) {
    if (!my || (!my.camp && !my.word)) return;
    const overlay = document.getElementById("ucRevealOverlay");
    if (!overlay) return;
    const cardEl = document.getElementById("ucRevealCard");
    const preheat = document.getElementById("ucPreheat");
    if (cardEl) cardEl.hidden = true;
    if (preheat) preheat.hidden = false;
    showUcPreheatCountdown();
    overlay.hidden = false;
    window.clearTimeout(ucPreheatTimer);
    ucPreheatTimer = window.setTimeout(() => {
      window.clearInterval(ucPreheatCounter);
      showUcIdentityReveal(my);
    }, UC_PREHEAT_MS);
  }

  /**
   * 按游戏类型展示的规则与标题：右侧通用「游戏规则 + 全局战绩」面板按当前游戏显示对应内容。
   */
  const UC_GAME_META = {
    gomoku: {
      name: "五子棋",
      rules: [
        "黑白双方轮流落子于 15×15 棋盘交叉点。",
        "任意方向率先连成五子的一方获胜。",
        "落子后不可悔棋（与花火对弈时由棋力决定强弱）。",
      ],
    },
    xiangqi: {
      name: "中国象棋",
      rules: [
        "按中国象棋标准规则，红黑双方轮流走子。",
        "将死或困毙对方「将/帅」即获胜；提前认输或超时判负。",
        "由独立 Pikafish 引擎与你对弈。",
      ],
    },
    tictactoe: {
      name: "井字棋",
      rules: [
        "3×3 网格上双方轮流落子。",
        "任意一行、一列或一条对角线率先集满三子的一方获胜。",
      ],
    },
    turtle_soup: {
      name: "海龟汤",
      rules: [
        "主持人给出一个神秘汤面，你通过提问还原背后真相。",
        "只有能回答「是 / 否 / 无关」的问题才会被揭晓。",
        "可用提示、猜关键事实来逐步逼近完整汤底。",
      ],
    },
    pig_dice: {
      name: "贪心骰子",
      rules: [
        "轮流掷骰：只要不出「1」就把本轮点数累加，可随时「收手」稳拿积分。",
        "一旦掷出「1」，本轮累计点数全部作废。",
        "先达到目标分、或轮次结束时积分最高者获胜。",
      ],
    },
    draw_guess: {
      name: "你画我猜",
      rules: [
        "玩家按题目作画，花火根据画面猜答案。",
        "每次作答消耗一次猜测机会，限次内猜中即算合作成功。",
      ],
    },
    blackjack: {
      name: "二十一点",
      rules: [
        "目标：手牌点数尽量接近 21 且不超过 21（爆牌即输）。",
        "A 可作 1 或 11，J/Q/K 记为 10。",
        "你决定「要牌」或「停牌」，再与庄家（花火）比大小。",
      ],
    },
    undercover: {
      name: "谁是卧底",
      rules: [
        "【平民】找出混在玩家中所有的卧底和白板",
        "【卧底】隐藏自己，直到所有平民出局，或者存活至仅剩2人为止",
        "【白板】没有词条，根据其他玩家的发言推测词条，找出卧底或者存活到仅剩2人为止，白板不会出现在前两名发言玩家当中",
        "【词条】除了白板以外，其他玩家的发言中不允许出现词条本身。当白板的发言内容中包含任意一个词条时，白板获胜",
      ],
    },
  };

  /**
   * 渲染全局胜场榜（跨房间汇总当前游戏类型数据，按胜场降序，最多前 10 名）。
   */
  function renderLeaderboard(room) {
    const board = document.getElementById("ucLeaderboard");
    if (!board) return;
    const list = Array.isArray(room.leaderboard) ? room.leaderboard : [];
    board.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("p");
      empty.className = "uc-lb-empty";
      empty.textContent = "暂无全局战绩，快来打一局吧～";
      board.appendChild(empty);
      return;
    }
    const medals = ["🥇", "🥈", "🥉"];
    list.slice(0, 10).forEach((row, idx) => {
      const el = document.createElement("div");
      el.className = "uc-lb-row" + (idx === 0 ? " is-top" : "");
      const rank = document.createElement("span");
      rank.className = "uc-lb-rank";
      rank.textContent = medals[idx] || `${idx + 1}`;
      const name = document.createElement("span");
      name.className = "uc-lb-name";
      name.textContent = sanitizeDisplayText(String(row.name || "未知玩家"));
      const wins = document.createElement("span");
      wins.className = "uc-lb-wins";
      wins.textContent = `${Number(row.wins) || 0} 胜`;
      el.appendChild(rank);
      el.appendChild(name);
      el.appendChild(wins);
      board.appendChild(el);
    });
  }

  /**
   * 按当前游戏类型刷新右栏「游戏规则 + 全局战绩」的内容（规则文案、标题、排行榜）。
   */
  function renderGameInfoPanel(room) {
    const meta = UC_GAME_META[(room && room.game_type) || ""] || UC_GAME_META.undercover;
    const rulesBody = document.getElementById("ucRulesBody");
    if (rulesBody) {
      rulesBody.innerHTML = "";
      meta.rules.forEach((t) => {
        const p = document.createElement("p");
        p.textContent = t;
        rulesBody.appendChild(p);
      });
    }
    const rulesToggle = document.getElementById("ucRulesToggle");
    const boardToggle = document.getElementById("ucBoardToggle");
    if (rulesToggle) {
      const span = rulesToggle.querySelector("span");
      if (span) span.textContent = `📜 ${meta.name}游戏规则`;
    }
    if (boardToggle) {
      const span = boardToggle.querySelector("span");
      if (span) span.textContent = `🏆 ${meta.name}全局战绩`;
    }
    renderLeaderboard(room);
  }

  // 时间线票数揭晓：先让数字滚动翻转，再下落砸走问号、错峰停到最终值，节奏放缓
  function scheduleUcVoteFlip(roundNumber) {
    window.clearTimeout(ucVoteFlipTimeout);
    ucVoteFlipTimeout = window.setTimeout(() => {
      const block = document.querySelector(`.uc-votes-block[data-round-reveal="${roundNumber}"]`);
      if (!block) return;
      const cells = block.querySelectorAll(".uc-vote-count.is-question");
      cells.forEach((cell) => {
        const finalVal = cell.dataset.count || "0";
        const delay = Number(cell.style.getPropertyValue("--d")) || 0;
        window.setTimeout(() => {
          // 前置滚动：数字 0-9 快速翻转约 4 拍，制造“抽盲盒”感
          let ticks = 0;
          const rollTimer = window.setInterval(() => {
            cell.textContent = String(Math.floor(Math.random() * 10));
            ticks++;
            if (ticks >= 4) {
              window.clearInterval(rollTimer);
              cell.textContent = finalVal;
              cell.classList.remove("is-question", "is-rolling");
              cell.classList.add("is-flipped");
              if (finalVal === "0") cell.classList.add("is-zero");
            }
          }, 90);
        }, delay);
      });
      ucVoteFlipTimeout = null;
    }, 320);
  }

  /**
   * 结算获胜动画卡片（所有玩家/观众都能看到）
   * 进入 finished 时弹出，展示获胜阵营、结语、双方词条与全体身份揭晓，并播放彩带。
   */
  function showUndercoverResultOverlay(snap, players) {
    const overlay = document.getElementById("ucResultOverlay");
    const card = document.getElementById("ucResultCard");
    if (!overlay || !card) return;
    const winner = snap.winner || {};
    const camp = winner.camp || "unknown";
    const campMeta = {
      civilian: ["平民获胜", "is-civilian", "🛡️"],
      undercover: ["卧底获胜", "is-undercover", "🕵️"],
      whiteboard: ["白板获胜", "is-whiteboard", "📋"],
    };
    const [title, cls, icon] = campMeta[camp] || ["本局结束", "", "🏆"];
    const titleEl = document.getElementById("ucResultTitle");
    titleEl.textContent = title;
    titleEl.className = `uc-result-title ${cls || ""}`;
    card.className = `uc-result-card ${cls || ""}`;
    document.getElementById("ucResultBadge").textContent = icon;
    document.getElementById("ucResultMessage").textContent =
      sanitizeDisplayText(winner.message || "本局对局结束，胜负已分。");

    // 平民词条 vs 卧底词条对比
    const wordsBox = document.getElementById("ucResultWords");
    wordsBox.innerHTML = "";
    const addWord = (label, value, wordCls) => {
      const chip = document.createElement("div");
      chip.className = `uc-result-word ${wordCls || ""}`;
      const tag = document.createElement("span");
      tag.textContent = label;
      const text = document.createElement("strong");
      text.textContent = sanitizeDisplayText(value || "—");
      chip.appendChild(tag);
      chip.appendChild(text);
      wordsBox.appendChild(chip);
    };
    if (winner.civilian_word || winner.undercover_word) {
      addWord("平民词条", winner.civilian_word, "is-civilian");
      addWord("卧底词条", winner.undercover_word, "is-undercover");
    }

    // 全体身份揭晓
    const list = document.getElementById("ucResultPlayers");
    list.innerHTML = "";
    (Array.isArray(players) ? players : []).forEach((p) => {
      const row = document.createElement("div");
      row.className = `uc-result-player is-${p.camp || ""}`;
      if (p.is_out) row.classList.add("is-out");
      const name = document.createElement("span");
      name.className = "uc-rp-name";
      name.textContent = `${p.player_number}号${p.display_name ? " · " + sanitizeDisplayText(p.display_name) : ""}`;
      const role = document.createElement("span");
      role.className = "uc-rp-role";
      role.textContent = p.camp
        ? `${ucCampText(p.camp)}${p.word ? "「" + sanitizeDisplayText(p.word) + "」" : ""}`
        : "—";
      row.appendChild(name);
      row.appendChild(role);
      list.appendChild(row);
    });

    // 彩带
    const confetti = document.getElementById("ucResultConfetti");
    if (confetti) {
      confetti.innerHTML = "";
      const colors = ["var(--green)", "var(--red)", "var(--gold)", "#4f8ef7", "#c084fc"];
      for (let i = 0; i < 28; i++) {
        const piece = document.createElement("i");
        piece.className = "uc-confetti";
        piece.style.left = `${(Math.random() * 100).toFixed(2)}%`;
        piece.style.setProperty("--cf-color", colors[i % colors.length]);
        piece.style.setProperty("--cf-drift", `${(Math.random() * 120 - 60).toFixed(1)}px`);
        piece.style.animationDelay = `${(Math.random() * 1.6).toFixed(2)}s`;
        piece.style.animationDuration = `${(2.4 + Math.random() * 1.8).toFixed(2)}s`;
        confetti.appendChild(piece);
      }
    }

    // 重新触发入场动画
    card.style.animation = "none";
    void card.offsetWidth;
    card.style.animation = "";
    overlay.hidden = false;
    // 记下本局结果，供「分享战报」按钮生成 PNG 卡片
    const seatsMap = {};
    (Array.isArray(room?.player_seats) ? room.player_seats : []).forEach((s) => {
      seatsMap[Number(s.number)] = s;
    });
    const myNumber = Number(room.visitor_number || 0);
    ucLastResult = {
      title,
      icon,
      camp,
      message: winner.message || "",
      civilian_word: winner.civilian_word || "",
      undercover_word: winner.undercover_word || "",
      players: (Array.isArray(players) ? players : []).map((p) => {
        const n = Number(p.player_number);
        const seat = seatsMap[n] || {};
        return {
          player_number: n,
          display_name: p.display_name || seat.display_name || "",
          camp: p.camp || "",
          is_out: !!p.is_out,
          avatar_url: seat.avatar_url || "",
          is_me: n === myNumber,
        };
      }),
      // 屏幕前这位玩家的信息：座位号、名字、身份（用于“以我视角”的分享文案）
      me: {
        number: myNumber,
        name: seatsMap[myNumber]?.display_name || "",
        camp: (Array.isArray(players) ? players : [])
          .find((p) => Number(p.player_number) === myNumber)?.camp || "",
      },
    };
    const shareBtn = document.getElementById("ucResultShare");
    const recapBtn = document.getElementById("ucResultRecapBtn");
    const recapBox = document.getElementById("ucResultRecap");
    const announceBtn = document.getElementById("ucResultAnnounce");
    const uid = String((snap || {}).game_uid || "");
    if (shareBtn) shareBtn.hidden = false;
    // 换局时才重置按钮状态与复盘展示；同局（轮询重复渲染）保持现状，避免打断已展示内容
    if (uid !== ucShownResultUid) {
      ucShownResultUid = uid;
      if (recapBtn) { recapBtn.hidden = false; recapBtn.disabled = false; recapBtn.textContent = "✨ 花火复盘"; }
      if (recapBox) { recapBox.hidden = true; recapBox.textContent = ""; }
      if (announceBtn) { announceBtn.hidden = false; announceBtn.disabled = false; announceBtn.textContent = "📢 通知到群"; }
      // 本局已有缓存的复盘则直接展示
      if (recapBox && uid && ucRecapCache[uid]) {
        recapBox.textContent = ucRecapCache[uid];
        recapBox.hidden = false;
      }
      if (announceBtn && !uid) announceBtn.hidden = true;
    }
  }

  /**
   * 从座位号解析头像：已绑定 QQ（identity_confirmed）用 QQ 头像；
   * AI 座位/未绑定则回退显示名字首字。
   * @param {number} number 座位号
   * @param {string} size 尺寸，small | medium | big（用于 CSS 类）
   * @param {boolean} [isMine] 是否本人（强调高亮）
   * @returns {string} 头像 HTML 片段
   */
  // 首字占位头像的稳定底色：用名字+座号做哈希，让每个 AI 玩家有自己专属的底色区分
  function ucAvatarHueClass(name, number, isAi) {
    if (!isAi) return "uc-avatar-tone-neutral"; // 未绑定真人：中性底色 + 强对比文字
    const seed = String(name || "") + ":" + String(number);
    let hash = 0;
    for (let i = 0; i < seed.length; i++) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
    return `uc-avatar-hue-${hash % 6}`;
  }

  function ucSeatAvatarHtml(number, size = "medium", isMine = false) {
    const seats = Array.isArray(room?.player_seats) ? room.player_seats : [];
    const seat = seats.find((s) => Number(s.number) === Number(number));
    const name = seat && seat.display_name ? String(seat.display_name) : "";
    const url = seat && seat.avatar_url ? String(seat.avatar_url) : "";
    const isAi = seat ? !!seat.is_ai : false;
    const cls = ["uc-avatar", `uc-avatar-${size}`];
    if (isMine) cls.push("is-me");
    if (url) {
      return `<span class="${cls.join(" ")}"><img src="${url}" alt="" loading="lazy" referrerpolicy="no-referrer"></span>`;
    }
    // AI / 未绑定：首字占位，AI 用专属底色区分
    const letter = (name || (Number(number) >= 0 ? `${number}` : "？")).trim().charAt(0) || "？";
    cls.push("uc-avatar-text", ucAvatarHueClass(name, number, isAi));
    return `<span class="${cls.join(" ")}">${letter}</span>`;
  }

  // 复用已加载的 QQ 头像 <img> 节点：玩家网格每次轮询都会重建卡片，
  // 若不复用 img，移动端会反复重新请求 qlogo 导致头像闪烁。
  const ucAvatarImgCache = {}; // 座位号 -> {src, el}
  function ucSeatAvatarNode(number, size = "medium", isMine = false) {
    const seats = Array.isArray(room?.player_seats) ? room.player_seats : [];
    const seat = seats.find((s) => Number(s.number) === Number(number));
    const name = seat && seat.display_name ? String(seat.display_name) : "";
    const url = seat && seat.avatar_url ? String(seat.avatar_url) : "";
    const isAi = seat ? !!seat.is_ai : false;
    const cls = ["uc-avatar", `uc-avatar-${size}`];
    const span = document.createElement("span");
    if (isMine) cls.push("is-me");
    if (url) {
      span.className = cls.join(" ");
      let cached = ucAvatarImgCache[Number(number)];
      let img;
      if (cached && cached.src === url) {
        img = cached.el; // 复用同一 img，避免重新下载
      } else {
        img = document.createElement("img");
        img.src = url;
        img.alt = "";
        img.loading = "lazy";
        img.referrerPolicy = "no-referrer";
        ucAvatarImgCache[Number(number)] = { src: url, el: img };
      }
      span.appendChild(img);
      return span;
    }
    const letter = (name || (Number(number) >= 0 ? `${number}` : "？")).trim().charAt(0) || "？";
    cls.push("uc-avatar-text", ucAvatarHueClass(name, number, isAi));
    span.className = cls.join(" ");
    span.textContent = letter;
    return span;
  }

  /**
   * 醒目弹出“轮到谁发言”全屏动画通知（所有玩家/观众都能看到）
   * 传入 nextNumber：true 表示显示“接下来谁发言”，false 表示当前发言者本人。
   */
  function showSpeechTurnNotification(playerNumber, isMine) {
    const old = document.querySelector(".speech-turn-notification");
    if (old) old.remove();
    const note = document.createElement("div");
    note.className = "speech-turn-notification";
    note.dataset.live = "1";
    const avatarHtml = ucSeatAvatarHtml(Number(playerNumber), "big", isMine);
    if (isMine) {
      note.innerHTML = `${avatarHtml}<div class="stn-text"><span>到你发言了！</span><strong>${playerNumber}号</strong><em class="speech-sec"></em></div>`;
    } else {
      note.innerHTML = `${avatarHtml}<div class="stn-text"><span>接下来发言</span><strong>${playerNumber}号</strong><em class="speech-sec"></em></div>`;
    }
    document.body.appendChild(note);
    // 弹出动画结束后，逐渐缩小收缩到棋盘顶部，持续显示发言倒计时（由每秒 tick 同步）
    window.setTimeout(() => {
      if (note && document.contains(note)) note.classList.add("is-dock");
    }, 1800);
  }

  function rememberIdentity() {
    return window.localStorage.getItem(rememberIdentityKey) !== "0";
  }

  function setRoom(nextRoom) {
    if (!nextRoom) {
      room = nextRoom;
      return;
    }
    if (room) {
      [
        "trusted_browser_available",
        "trusted_browser_active",
        "trusted_browser_expires_at",
        "trusted_browser_ttl_days",
      ].forEach((key) => {
        if (nextRoom[key] === undefined) nextRoom[key] = room[key];
      });
    }
    nextRoom._snapshot_local_ms = Date.now();
    room = nextRoom;
  }

  // 每秒刷新 谁是卧底 setup 阶段倒计时（不用等长轮询）
  window.setInterval(() => {
    if (!room || room.game_type !== "undercover") {
      const cc = document.getElementById("ucStartCountdown");
      if (cc) cc.hidden = true;
      return;
    }
    const big = document.getElementById("ucStartCountdown");
    if (big) big.hidden = room.status !== "setup";
    const note = document.getElementById("seatNote");
    if (note) note.hidden = room.status === "setup"; // 大字倒计时显示时隐去小字
    if (room.status !== "setup") return;
    const deadline = Number(room.turn_deadline || 0);
    const capacity = Number(room.player_capacity || 0);
    const current = (room.player_seats || []).length;
    const serverBase = Number(room.server_time || 0);
    const takenAt = Number(room._snapshot_local_ms || Date.now()) / 1000;
    const nowServer = serverBase + ((Date.now() / 1000) - takenAt);
    const remain = deadline ? Math.max(0, Math.ceil(deadline - nowServer)) : 0;
    const capacityText = capacity > 0 ? `${current}/${capacity}` : `${current}`;
    // 全员就绪：进入确认窗口，提示“马上开始”
    const seats = Array.isArray(room.player_seats) ? room.player_seats : [];
    const liveSeats = seats.filter((s) => s.is_ai || s.identity_confirmed);
    const allReady = liveSeats.length >= Number(room.undercover_min_players || 2)
      && liveSeats.every((s) => s.ready);
    const remainingText = allReady
      ? "玩家均已准备，马上开始…"
      : (deadline && remain > 0
        ? (capacity > 0 && current >= capacity ? "人满，即将开局" : `${remain} 秒后自动开局`)
        : "等待更多玩家加入...");
    if (big) {
      const mainNum = allReady
        ? "准备开局"
        : (capacity > 0 && current >= capacity ? "准备开局" : (deadline && remain > 0 ? `${remain}` : "--"));
      big.innerHTML = allReady
        ? `所有玩家均已准备 · <strong>马上开始</strong>`
        : `开始倒计时 · <strong>${mainNum}</strong> ${(capacity > 0 && current >= capacity) || !(deadline && remain > 0) ? "" : "秒"}`;
      big.setAttribute("data-detail", `已入座 ${capacityText} 人 · ${remainingText}`);
    }
  }, 1000);

  // 每秒刷新 谁是卧底 对局中的轮次/发言/投票倒计时（不等长轮询）
  window.setInterval(() => {
    if (!room || room.game_type !== "undercover" || room.status !== "active") return;
    const timer = document.getElementById("ucTurnTimer");
    if (!timer) return;
    const game = room.game || {};
    const deadline = Number(room.turn_deadline || 0);
    const serverBase = Number(room.server_time || 0);
    const takenAt = Number(room._snapshot_local_ms || Date.now()) / 1000;
    const nowServer = serverBase + ((Date.now() / 1000) - takenAt);
    const remain = deadline ? Math.max(0, Math.ceil(deadline - nowServer)) : 0;
    const timerActive = deadline && remain > 0;
    const phase = game.phase;
    let text = "";
    if (phase === "speech" || phase === "pk") {
      // “轮到 X号 发言”由 ucExpectedSpeaker 展示，这里只补充倒计时，避免重复
      const who = game.expected_speaker_number;
      text = who ? (timerActive ? `（剩余 ${remain} 秒）` : "（不限时）") : "";
    } else if (phase === "voting") {
      const voted = Array.isArray(game.voted_this_round_player_numbers)
        ? game.voted_this_round_player_numbers.length
        : 0;
      const rnds = game.rounds_public || [];
      const cur = rnds.length ? rnds[rnds.length - 1] : null;
      const voters = Array.isArray(cur ? cur.vote_player_numbers : null)
        ? cur.vote_player_numbers.length
        : 0;
      text = timerActive
        ? `已投 ${voted}/${voters || "-"} · 剩余 ${remain} 秒`
        : `已投 ${voted}/${voters || "-"}`;
    } else if (phase === "finished") {
      text = "";
    } else {
      text = "";
    }
    timer.textContent = text;
    // 同步顶部停靠的“轮到 X号”发言倒计时；离开发言/PK 阶段时自动收起
    const dock = document.querySelector(".speech-turn-notification.is-dock");
    if (dock) {
      if (!["speech", "pk"].includes(phase)) {
        dock.remove();
      } else {
        const secEl = dock.querySelector(".speech-sec");
        if (secEl) secEl.textContent = timerActive ? `⏳ ${remain}s` : "不限时";
        // 倒计时 ≤30 秒：加剧横幅波动以催促玩家发言
        dock.classList.toggle("is-urgent", timerActive && remain > 0 && remain <= 30);
      }
    }
  }, 1000);

  async function request(method, action, payload = {}) {
    const response = await window.fetch(endpoint(action), {
      method,
      headers: method === "POST" ? { "Content-Type": "application/json" } : {},
      body: method === "POST" ? JSON.stringify(payload) : undefined,
      cache: "no-store",
    });
    let data = null;
    try { data = await response.json(); } catch (_error) { data = null; }
    if (!response.ok || data?.status === "error") {
      throw new Error(data?.message || data?.error || "请求失败");
    }
    return data?.data ?? data;
  }

  async function join() {
    if (!accessToken) throw new Error("房间链接无效");
    const data = await request("POST", "join", {
      visitor_token: visitorToken,
      remember_identity: rememberIdentity(),
    });
    visitorToken = String(data.visitor_token || "");
    window.localStorage.setItem(storageKey, visitorToken);
    setRoom(data.room);
    lastSeenMessageId = latestMessageId(room);
    syncGameUi();
    render();
  }

  async function poll() {
    window.clearTimeout(pollTimer);
    if (!visitorToken) return;
    try {
      await loadState();
      setConnection("online", "已连接");
      const fastPolling = (
        room?.game_type === "pig_dice" && room.game?.turn === "bot"
      ) || (
        room?.game_type === "blackjack" && room.game?.phase === "dealer_turn"
      );
      const interval = fastPolling ? 300 : 1000;
      pollTimer = window.setTimeout(poll, interval);
    } catch (error) {
      setConnection("error", "连接中断");
      document.getElementById("overlayTitle").textContent = "房间不可用";
      document.getElementById("overlayText").textContent = error?.message || "链接已失效";
      document.getElementById("boardOverlay").hidden = false;
      pollTimer = window.setTimeout(poll, 3000);
    }
  }

  async function loadState() {
    const response = await window.fetch(
      endpoint(
        "state",
        `?visitor_token=${encodeURIComponent(visitorToken)}&remember_identity=${rememberIdentity() ? "1" : "0"}`,
      ),
      { cache: "no-store" },
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data?.message || "房间状态不可用");
    const previousType = room?.game_type;
    const previousMessageId = latestMessageId(room);
    setRoom(data?.data?.room);
    if (previousType !== room?.game_type) {
      selectedPiece = null;
      pendingMove = null;
      drawStrokes = [];
      drawDirty = false;
      drawRevision = -1;
      syncGameUi();
    }
    syncDrawState();
    if (
      activeRoomView !== "chat"
      && latestMessageId(room) > Math.max(previousMessageId, lastSeenMessageId)
    ) {
      document.getElementById("chatUnread").hidden = false;
    }
    render();
  }

  function notifyLeave() {
    if (!visitorToken) return;
    const body = JSON.stringify({ visitor_token: visitorToken });
    if (typeof window.navigator.sendBeacon === "function") {
      const payload = new Blob([body], { type: "application/json" });
      if (window.navigator.sendBeacon(endpoint("leave"), payload)) return;
    }
    window.fetch(endpoint("leave"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
    }).catch(() => {});
  }

  function setConnection(mode, label) {
    document.getElementById("connectionDot").className = `connection-dot ${mode}`;
    document.getElementById("roomStatus").textContent = label;
  }

  function statusLabel(status) {
    return {
      waiting: "等待玩家", setup: "等待开局", active: "对局中", paused: "已暂停",
      finished: "本局结束", rematch_pending: "等待 花火 回应", closed: "房间已结束",
    }[status] || "等待中";
  }

  function latestMessageId(currentRoom) {
    const messages = Array.isArray(currentRoom?.messages) ? currentRoom.messages : [];
    return messages.reduce((latest, message) => {
      const value = Number(message.id || 0);
      return Number.isFinite(value) ? Math.max(latest, value) : latest;
    }, 0);
  }

  function setRoomView(view) {
    activeRoomView = view === "chat" ? "chat" : "game";
    document.getElementById("gameWorkspace").classList.toggle("room-view-hidden", activeRoomView !== "game");
    document.getElementById("chatPanel").classList.toggle("room-view-hidden", activeRoomView !== "chat");
    document.querySelectorAll("[data-room-view]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.roomView === activeRoomView);
    });
    if (activeRoomView === "chat") {
      lastSeenMessageId = latestMessageId(room);
      document.getElementById("chatUnread").hidden = true;
      window.setTimeout(() => chatInput.focus(), 0);
    }
  }

  function difficultyLabel(value) {
    return { easy: "简单", normal: "普通", hard: "困难" }[value] || "普通";
  }

  function gameLabel() {
    return {
      gomoku: "五子棋",
      xiangqi: "中国象棋",
      tictactoe: "井字棋",
      turtle_soup: "海龟汤",
      pig_dice: "贪心骰子",
      draw_guess: "你画我猜",
      blackjack: "二十一点",
      undercover: "谁是卧底",
    }[room?.game_type] || "棋类游戏";
  }

  function syncGameUi() {
    if (!room) return;
    const xiangqi = room.game_type === "xiangqi";
    const tictactoe = room.game_type === "tictactoe";
    const turtleSoup = room.game_type === "turtle_soup";
    const pigDice = room.game_type === "pig_dice";
    const drawGuess = room.game_type === "draw_guess";
    const blackjack = room.game_type === "blackjack";
    const undercover = room.game_type === "undercover";
    document.title = sanitizeDisplayText(`花火陪你玩 · ${gameLabel()}`);
    document.getElementById("gameTitle").textContent = sanitizeDisplayText(gameLabel());
    document.getElementById("brandIcon").setAttribute(
      "data-lucide",
      turtleSoup ? "shell" : (pigDice ? "dice-5" : (blackjack ? "spade" : (undercover ? "spy" : (drawGuess ? "paintbrush" : (xiangqi ? "circle-dot" : (tictactoe ? "badge-x" : "grid-3x3")))))),
    );
    boardStage.hidden = turtleSoup || pigDice || blackjack || drawGuess || undercover;
    const turnPanel = document.querySelector("section.turn-panel");
    if (turnPanel) {
      // 谁是卧底、二十一点各有专属阶段/状态显示，隐藏通用"当前回合 走棋"面板
      turnPanel.hidden = blackjack || undercover;
    }
    soupStage.hidden = !turtleSoup;
    diceStage.hidden = !pigDice;
    blackjackStage.hidden = !blackjack;
    drawStage.hidden = !drawGuess;
    undercoverStage.hidden = !undercover;
    boardStage.classList.toggle("xiangqi", xiangqi);
    boardStage.classList.toggle("tictactoe", tictactoe);
    board.width = xiangqi ? 720 : 760;
    board.height = xiangqi ? 800 : 760;
    board.setAttribute(
      "aria-label",
      turtleSoup
        ? "海龟汤问答区"
        : pigDice
        ? "贪心骰子操作区"
        : blackjack
        ? "二十一点牌桌"
        : xiangqi
        ? "九乘十中国象棋棋盘"
        : drawGuess
        ? "你画我猜作画区"
        : (tictactoe ? "三乘三井字棋棋盘" : "十五乘十五五子棋棋盘"),
    );
    const buttons = Array.from(document.querySelectorAll("[data-side]"));
    let values = [["human_black", "我先手"], ["bot_black", "花火先手"], ["random", "随机"]];
    if (xiangqi) {
      values = [["human_red", "我执红"], ["human_black", "我执黑"], ["random", "随机"]];
    } else if (tictactoe) {
      values = [["human_x", "我执 X"], ["human_o", "我执 O"], ["random", "随机"]];
    }
    selectedSide = values[0][0];
    buttons.forEach((button, index) => {
      button.dataset.side = values[index][0];
      button.textContent = values[index][1];
      button.classList.toggle("is-active", index === 0);
    });
    icons();
    syncDrawState();
  }

  // 防窥屏：未绑定 QQ 的访客整页遮挡，仅展示绑定引导；绑定后自动进入观众席
  function renderPeekGate() {
    const gate = document.getElementById("peekGate");
    if (!gate) return;
    const blocked = Boolean(room) && room.status !== "closed" && !room.player_confirmed;
    gate.hidden = !blocked;
    if (!blocked || !room) return;
    const challenge = document.getElementById("peekChallenge");
    const rememberOption = document.getElementById("peekRememberOption");
    const rememberInput = document.getElementById("peekRemember");
    const statusEl = document.getElementById("peekStatus");
    const token = room.identity_token || "";
    document.getElementById("peekToken").textContent = token || "--------";
    const inline = document.getElementById("peekTokenInline");
    if (inline) inline.textContent = token || "--------";
    const note = document.getElementById("peekTokenNote");
    if (room.admin_room) {
      challenge.hidden = true;
      if (statusEl) statusEl.textContent = "管理员将在游戏管理台为你绑定 QQ，绑定成功后自动进入观众席。";
    } else if (!token) {
      challenge.hidden = false;
      note.textContent = "绑定码已过期，刷新页面后重新获取";
      if (statusEl) statusEl.textContent = "";
    } else {
      challenge.hidden = false;
      note.textContent = room.source === "group"
        ? `群内直接发送：/绑定玩家 ${token}；或点击上方「一键复制」直接粘贴到群里发送即可。`
        : `私聊里发送：/绑定玩家 ${token}；或点击上方「一键复制」直接粘贴发送即可。`;
      if (statusEl) statusEl.textContent = "绑定成功后页面会自动切换为观众席。";
    }
    rememberOption.hidden = !(challenge.hidden === false && room.trusted_browser_available);
    if (rememberInput) rememberInput.checked = rememberIdentity();
  }

  function render() {
    if (!room) return;
    // 谁是卧底房间：隐藏通用「玩家/平局/花火」比分（多人语音局不适用），右侧由战绩榜与阶段条接管
    document.body.classList.toggle("room-undercover", room.game_type === "undercover");
    // 右栏「游戏规则 + 全局战绩」面板：按当前游戏类型动态展示对应内容
    renderGameInfoPanel(room);
    // 本局身份卡只属于谁是卧底，其它玩法的右侧面板不显示它
    const ucIdCard = document.getElementById("ucIdentityCard");
    if (ucIdCard) ucIdCard.hidden = room.game_type !== "undercover";
    renderPeekGate();
    document.getElementById("roomId").textContent = room.room_id || "";
    document.getElementById("roomStatus").textContent = statusLabel(room.status);
    document.getElementById("visitorLabel").textContent = room.visitor_number
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（${room.visitor_number}号）`
        : `${room.visitor_number} 号`)
      : "访客";
    document.getElementById("chatIdentity").textContent = room.is_player
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（玩家）`
        : `${room.visitor_number || "?"}号玩家`)
      : room.player_confirmed
      ? (room.visitor_display_name
        ? `${room.visitor_display_name}（观众）`
        : `${room.visitor_number || "?"}号观众`)
      : `匿名观众（${room.visitor_number || "?"}号）`;
    chatInput.placeholder = room.game_type === "turtle_soup"
      ? "提问、给线索，或和 花火 聊天"
      : "和 花火 说点什么";
    document.getElementById("chatSend").disabled = chatBusy;
    const pigDice = room.game_type === "pig_dice";
    const drawGuess = room.game_type === "draw_guess";
    document.getElementById("difficulty").textContent = pigDice
      ? ({ easy: "稳健", normal: "均衡", hard: "大胆" }[room.difficulty] || "均衡")
      : difficultyLabel(room.difficulty);
    document.getElementById("humanScore").textContent = room.score?.human ?? 0;
    document.getElementById("botScore").textContent = room.score?.bot ?? 0;
    document.getElementById("drawScore").textContent = room.score?.draws ?? 0;
    const turtleSoup = room.game_type === "turtle_soup";
    const playerHostedSoup = turtleSoup && room.turtle_soup_mode === "player_host";
    document.getElementById("humanScoreLabel").textContent = drawGuess ? "猜中" : turtleSoup ? (playerHostedSoup ? "玩家" : "解开") : "玩家";
    document.getElementById("drawScoreLabel").textContent = drawGuess ? "总轮数" : turtleSoup ? "总题数" : (pigDice ? "总局数" : "平局");
    document.getElementById("botScoreLabel").textContent = drawGuess ? "未猜中" : turtleSoup ? (playerHostedSoup ? "花火 猜中" : "放弃") : "花火";
    if (turtleSoup || pigDice || drawGuess) document.getElementById("drawScore").textContent = room.score?.games ?? 0;
    renderSeat();
    renderUcReady();
    renderPeople();
    renderMessages();
    renderTurtleSoup();
    renderPigDice();
    renderBlackjack();
    renderDrawGuess();
    renderUndercover();
    drawBoard();
    renderTurn();
    icons();
  }

  function renderSeat() {
    const badge = document.getElementById("seatBadge");
    // 当前浏览者头像：优先本人座位头像，观众则显示首字占位
    const myAvatar = document.getElementById("mySeatAvatar");
    if (myAvatar) {
      const vNum = Number(room.visitor_number || 0);
      const mySeat = (Array.isArray(room.player_seats) ? room.player_seats : [])
        .find((s) => Number(s.number) === vNum);
      const vName = room.visitor_display_name || "";
      if (mySeat && mySeat.avatar_url) {
        // 复用已加载的 img 节点，避免每次轮询重刷头像（移动端会闪烁）
        const n = Number(mySeat.number);
        let cached = ucAvatarImgCache[n];
        let img;
        if (cached && cached.src === mySeat.avatar_url) {
          img = cached.el;
        } else {
          img = document.createElement("img");
          img.src = mySeat.avatar_url;
          img.alt = "";
          img.loading = "lazy";
          img.referrerPolicy = "no-referrer";
          ucAvatarImgCache[n] = { src: mySeat.avatar_url, el: img };
        }
        myAvatar.replaceChildren(img);
      } else if (mySeat && mySeat.is_ai) {
        myAvatar.textContent = (mySeat.display_name || "AI").charAt(0) || "?";
      } else if (vName) {
        myAvatar.textContent = vName.trim().charAt(0) || "?";
      } else {
        myAvatar.innerHTML = vNum ? `<strong class="uc-avatar-fallback-num">${vNum}</strong>` : "?";
      }
      myAvatar.classList.toggle("uc-avatar-text", !(mySeat && mySeat.avatar_url));
    }
    const action = document.getElementById("seatAction");
    const note = document.getElementById("seatNote");
    const identityChallenge = document.getElementById("identityChallenge");
    const identityToken = document.getElementById("identityToken");
    const identityTokenNote = document.getElementById("identityTokenNote");
    const rememberOption = document.getElementById("rememberIdentityOption");
    const rememberInput = document.getElementById("rememberIdentity");
    const trustedStatus = document.getElementById("trustedIdentityStatus");
    const trustedText = document.getElementById("trustedIdentityText");
    const forgetIdentity = document.getElementById("forgetIdentity");
    const sideChoice = document.getElementById("sideChoice");
    const identityTokenInline = document.getElementById("identityTokenInline");
    badge.textContent = room.is_player ? "玩家席" : "观众席";
    badge.className = `seat-badge ${room.is_player ? "player" : ""}`;
    sideChoice.hidden = ["turtle_soup", "pig_dice", "draw_guess", "blackjack", "undercover"].includes(room.game_type) || !(["waiting", "setup", "finished"].includes(room.status));
    action.hidden = false;
    action.disabled = busy;
    const identityRequired = !room.admin_room && !room.player_confirmed;
    identityChallenge.hidden = !identityRequired;
    rememberOption.hidden = !(identityRequired && room.trusted_browser_available);
    rememberInput.checked = rememberIdentity();
    trustedStatus.hidden = !(room.trusted_browser_available && room.player_confirmed);
    if (!trustedStatus.hidden) {
      // 对局进行中禁止解绑：置灰并提示，避免误触导致房间状态错乱
      const inPlay = room.status === "active";
      if (inPlay) {
        trustedText.textContent = "对局进行中，结束后才能解绑玩家";
        forgetIdentity.hidden = false;
        forgetIdentity.disabled = true;
        forgetIdentity.title = "对局进行中，结束后才能解绑玩家";
      } else {
        trustedText.textContent = room.trusted_browser_active
          ? "此浏览器已记住你的身份"
          : "身份仅在当前房间有效";
        forgetIdentity.disabled = false;
        forgetIdentity.title = "解绑玩家，回到绑定引导界面";
        forgetIdentity.hidden = !room.trusted_browser_active;
      }
    }
    if (identityRequired) {
      const token = room.identity_token || "--------";
      identityToken.textContent = token;
      if (identityTokenInline) identityTokenInline.textContent = token;
      if (!room.identity_token) {
        identityTokenNote.textContent = "绑定码已过期，刷新页面后重新获取";
      } else if (room.source === "group") {
        identityTokenNote.textContent = `群内直接发送：/绑定玩家 ${token}；或点击上方「一键复制」直接粘贴到群里发送即可。`;
      } else {
        identityTokenNote.textContent = `私聊里发送：/绑定玩家 ${token}；或点击上方「一键复制」直接粘贴发送即可。`;
      }
    }
    if (!room.is_player && room.admin_room) {
      action.innerHTML = '<i data-lucide="clock-3"></i><span>等待管理员安排</span>';
      action.disabled = true;
      note.textContent = "管理员将在游戏管理台绑定玩家序号与 QQ。";
    } else if (!room.is_player) {
      action.innerHTML = '<i data-lucide="log-in"></i><span>加入对局</span>';
      const capacity = Number(room.player_capacity || 1);
      const full = capacity > 0 && (room.player_numbers || []).length >= capacity;
      action.disabled = busy || full || identityRequired;
      note.textContent = full
        ? "玩家席已满，可向席内玩家申请交换。"
        : identityRequired
        ? "请先用页面令牌在 QQ 中绑定身份。"
        : room.multiplayer_enabled
        ? `玩家席 ${room.player_numbers?.length || 0} / ${capacity || "不限"}，加入后按顺序轮流操作。`
        : room.player_number ? `${room.player_number} 号正在玩家席。` : "第一个加入玩家席的人开始对局。";
    } else if (room.status === "setup") {
      if (room.game_type === "undercover") {
        // 谁是卧底：倒计时自动开，不需要任何按钮
        action.hidden = true;
        const capacity = Number(room.player_capacity || 0);
        const current = (room.player_seats || []).length;
        const deadline = Number(room.turn_deadline || 0);
        const serverNow = Number(room.server_time || Date.now() / 1000);
        const remain = Math.max(0, Math.ceil(deadline - serverNow));
        const capacityText = capacity > 0 ? ` ${current}/${capacity} 人` : ` ${current} 人`;
        const remainText = remain > 0
          ? ((capacity > 0 && current >= capacity) ? "，人满立即开始" : `，约 ${remain} 秒后自动开始（人满立即开）`)
          : "，等待开始...";
        note.textContent = `已入座${capacityText}${remainText}。`;
      } else {
        action.innerHTML = room.game_type === "turtle_soup"
          ? room.turtle_soup_mode === "player_host"
            ? '<i data-lucide="message-circle-question"></i><span>开始让 花火 猜</span>'
            : '<i data-lucide="sparkles"></i><span>开始出题</span>'
          : room.game_type === "pig_dice"
          ? '<i data-lucide="dice-5"></i><span>开始掷骰</span>'
          : room.game_type === "draw_guess"
          ? '<i data-lucide="paintbrush"></i><span>开始作画</span>'
          : room.game_type === "blackjack"
          ? '<i data-lucide="spade"></i><span>开始发牌</span>'
          : '<i data-lucide="play"></i><span>开始新一局</span>';
        note.textContent = room.player_confirmed ? "身份已确认。" : "身份尚未通过 QQ 确认，暂不允许进入玩家席。";
      }
    } else if (room.status === "finished") {
      action.innerHTML = room.game_type === "turtle_soup"
        ? `<i data-lucide="rotate-ccw"></i><span>${room.turtle_soup_mode === "player_host" ? "申请再出一题" : "申请再来一道"}</span>`
        : '<i data-lucide="rotate-ccw"></i><span>申请再来一局</span>';
      note.textContent = "花火 会结合当前人格决定是否接受。";
    } else if (room.status === "rematch_pending") {
      action.innerHTML = '<i data-lucide="loader-circle"></i><span>等待 花火 回应</span>';
      action.disabled = true;
      note.textContent = "";
    } else {
      action.hidden = true;
      note.textContent = room.player_confirmed ? "身份已确认。" : "请先在 QQ 中绑定页面令牌。";
    }
  }

  // 谁是卧底集结阶段：玩家准备按钮（默认未准备；全员就绪自动开局，倒计时为强制开启兜底）
  function renderUcReady() {
    const area = document.getElementById("ucReadyArea");
    const btn = document.getElementById("ucReadyButton");
    if (!area || !btn) return;
    const inSetup = room.game_type === "undercover" && room.status === "setup";
    area.hidden = !(inSetup && room.is_player);
    if (!inSetup || !room.is_player) return;
    const seats = Array.isArray(room.player_seats) ? room.player_seats : [];
    const mySeat = seats.find((s) => s.visitor_token === visitorToken)
      || seats.find((s) => Number(s.number) === Number(room.visitor_number))
      || null;
    const myReady = !!mySeat?.ready;
    btn.classList.toggle("is-ready", myReady);
    // 按钮保持图标+文字居中（不塞进度，避免偏移），进度单独显示在下方
    btn.innerHTML = myReady
      ? '<i data-lucide="check-check"></i><span>已准备</span>'
      : '<i data-lucide="check-check"></i><span>准备</span>';
    btn.dataset.ready = myReady ? "1" : "0";
    const readyCount = seats.filter((s) => s.ready).length;
    btn.setAttribute("data-progress", `${readyCount}/${seats.length}`);
    const progEl = document.getElementById("ucReadyProgress");
    if (progEl) progEl.textContent = `${readyCount}/${seats.length} 人已准备`;
  }

  // 随机英文名：未绑定 QQ 的成员显示“观众-<英文>”，根据成员号稳定生成，避免每次重绘变化
  const _FAKE_ADJ = ["Clear", "Swift", "Bright", "Quiet", "Bold", "Calm", "Crimson", "Ever", "Frost", "Grand"];
  const _FAKE_NOUN = ["River", "Fox", "Pine", "Comet", "Lark", "North", "Echo", "Cedar", "Mist", "Raven"];
  function fakeEnglishName(seed) {
    let h = 0;
    const s = String(seed == null ? "" : seed);
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return _FAKE_ADJ[h % _FAKE_ADJ.length] + _FAKE_NOUN[(h >>> 3) % _FAKE_NOUN.length];
  }
  // 房间成员显示名：绑定了 QQ 显示其昵称；否则显示“观众-<英文>”。一律不带座位号，避免与游戏内玩家号码混淆
  function memberName(visitor) {
    return visitor.identity_confirmed && visitor.display_name
      ? visitor.display_name
      : `观众-${fakeEnglishName(visitor.number)}`;
  }

  function renderPeople() {
    const list = document.getElementById("peopleList");
    list.replaceChildren();
    const visitors = Array.isArray(room.visitors) ? room.visitors : [];
    // 已入座的玩家始终保留；离线/已关闭页面残留的观众不再计入，避免“莫名其妙多出的观众”
    const players = visitors.filter((v) => v.is_player);
    const spectators = visitors.filter((v) => !v.is_player && v.online);
    const countParts = [
      players.length ? `玩家${players.length}` : "",
      spectators.length ? `观众${spectators.length}` : "",
    ].filter(Boolean).join(" / ");
    // 总人数只统计“仍在场的”：已入座玩家 + 在线观众；已关闭页面的离线观众不计入
    const total = players.length + spectators.length;
    document.getElementById("peopleCount").textContent =
      `${total} 人${countParts ? " · " + countParts : ""}`;
    const groups = [
      { label: "玩家", members: players },
      { label: "观众", members: spectators },
    ];
    groups.forEach((group) => {
      if (!group.members.length) return;
      const head = document.createElement("div");
      head.className = "people-group";
      const labelSpan = document.createElement("span");
      labelSpan.className = "people-group-label";
      labelSpan.textContent = group.label;
      const countSpan = document.createElement("span");
      countSpan.className = "people-group-count";
      countSpan.textContent = group.members.length;
      head.appendChild(labelSpan);
      head.appendChild(countSpan);
      list.appendChild(head);
      group.members.forEach((visitor) => {
        const chip = document.createElement("span");
        chip.className = `person-chip ${visitor.online ? "online" : ""} ${group.label === "玩家" ? "player" : ""}`;
        // 绑定了显示 QQ 昵称，未绑定显示“观众-<英文>”，不再显示座位号
        chip.textContent = memberName(visitor);
        if (room.multiplayer_enabled && !room.is_player && visitor.is_player) {
          const request = document.createElement("button");
          request.type = "button";
          request.textContent = "申请交换";
          const cooldown = Number(room.swap_cooldown_until || 0);
          request.disabled = !room.player_confirmed || Boolean(room.outgoing_swap_request) || (cooldown && cooldown > (room.server_time || Date.now() / 1000));
          request.addEventListener("click", () => requestSeatSwap(visitor.number));
          chip.appendChild(request);
        }
        if (room.multiplayer_enabled && visitor.number === room.visitor_number && room.is_player) {
          (room.incoming_swap_requests || []).forEach((swap) => {
            const accept = document.createElement("button");
            accept.type = "button";
            accept.textContent = `${swap.requester_number}号申请，接受`;
            accept.addEventListener("click", () => respondSeatSwap(swap.request_id, true));
            chip.appendChild(accept);
            const decline = document.createElement("button");
            decline.type = "button";
            decline.textContent = "拒绝";
            decline.addEventListener("click", () => respondSeatSwap(swap.request_id, false));
            chip.appendChild(decline);
          });
        }
        list.appendChild(chip);
      });
    });
  }

  async function requestSeatSwap(targetNumber) {
    if (busy || room?.is_player) return;
    busy = true;
    try {
      const data = await request("POST", "seat/swap/request", {
        visitor_token: visitorToken,
        target_number: targetNumber,
      });
      setRoom(data.room);
      showToast("交换申请已发送");
    } catch (error) {
      showToast(error?.message || "无法发送交换申请");
    } finally {
      busy = false;
      render();
    }
  }

  async function respondSeatSwap(requestId, accepted) {
    if (busy || !room?.is_player) return;
    busy = true;
    try {
      const data = await request("POST", "seat/swap/respond", {
        visitor_token: visitorToken,
        request_id: requestId,
        accepted,
      });
      setRoom(data.room);
      showToast(accepted ? "席位已交换" : "已拒绝交换申请");
    } catch (error) {
      showToast(error?.message || "无法处理交换申请");
    } finally {
      busy = false;
      render();
    }
  }

  // 玩家主动从玩家席退到观众席
  async function leavePlayerSeat() {
    if (busy || !room?.is_player) return;
    busy = true;
    try {
      const data = await request("POST", "seat/leave", {
        visitor_token: visitorToken,
      });
      setRoom(data.room);
      showToast("已退出对局，回到观众席");
    } catch (error) {
      showToast(error?.message || "退出失败");
    } finally {
      busy = false;
      render();
    }
  }

  async function forgetTrustedIdentity() {
    if (busy || !room?.trusted_browser_active) return;
    if (room.status === "active") {
      showToast("对局进行中，结束后才能解绑玩家");
      return;
    }
    busy = true;
    try {
      const data = await request("POST", "identity/forget", {
        visitor_token: visitorToken,
        unbind: true,
      });
      setRoom(data.room);
      window.localStorage.setItem(rememberIdentityKey, "0");
      showToast("已解绑玩家，请重新绑定 QQ 身份");
    } catch (error) {
      showToast(error?.message || "解绑失败");
    } finally {
      busy = false;
      render();
    }
  }

  function renderMessages() {
    const list = document.getElementById("messages");
    // 全量重绘前必须清掉旧 DOM 上的打字机定时器，避免泄漏
    teardownAllTypingTimers();
    const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 48;
    list.replaceChildren();
    const messages = Array.isArray(room.messages) ? room.messages : [];
    // 用户是否主动留在底部附近（打字机推进时滚动跟随需要实时判断）
    const userStaysNearBottom = () =>
      list.scrollHeight - list.scrollTop - list.clientHeight < 80;

    if (!messages.length) {
      const empty = document.createElement("span");
      empty.className = "empty-message";
      empty.textContent = "房间对话会显示在这里";
      list.appendChild(empty);
      return;
    }

    messages.slice(-60).forEach((message) => {
      const role = message.role || "system";
      const item = document.createElement("article");
      item.className = `message ${role} ${message.message_type || "chat"}`;
      if (role !== "system") {
        const meta = document.createElement("span");
        meta.className = "message-meta";
        if (role === "bot") {
          meta.textContent = "花火";
        } else {
          const sender = String(message.sender_name || "匿名观众");
          meta.textContent = message.sender_number
            ? `${sender}（${message.sender_number}号）`
            : sender;
        }
        item.appendChild(meta);
      }
      const content = document.createElement("p");
      content.className = "message-content";
      const rawText = sanitizeDisplayText(String(message.content || ""));

      if (role === "bot" && rawText.length > 0) {
        // —— 花火的消息：打字机动画
        const key = messageKey(message);
        if (TYPED_DONE.has(key)) {
          // 之前已完整显示过 → 秒显
          content.textContent = rawText;
        } else {
          // 从现有进度继续（如果有），否则从 0 开始
          const cached = TYPED_PROGRESS.get(key);
          const startShown = cached ? cached.shownChars : 0;
          const safeStart = Math.max(0, Math.min(rawText.length, startShown));
          if (!cached) {
            TYPED_PROGRESS.set(key, { shownChars: safeStart, timerId: null, fullText: rawText });
          } else {
            // 文本变化？（极少发生）强制重置进度
            if (cached.fullText !== rawText) {
              cached.shownChars = 0;
              cached.fullText = rawText;
            } else {
              cached.shownChars = safeStart;
            }
            cached.timerId = null;
          }
          content.textContent = rawText.slice(0, safeStart);
          if (safeStart < rawText.length) {
            // 打字中：加闪烁光标 class
            content.classList.add("typing-active");
            // 用短延迟启动第一个 tick，保证首次渲染后再跑动画
            const state = TYPED_PROGRESS.get(key);
            if (state) {
              state.timerId = setTimeout(() => {
                runTypingTick(content, key, rawText, (_shown, done) => {
                  // 进度回调：刷新文本 + 用户没手动滑上去就跟随滚动
                  content.textContent = done ? rawText : _shown;
                  if (done) content.classList.remove("typing-active");
                  if (userStaysNearBottom() || activeRoomView === "chat") {
                    list.scrollTop = list.scrollHeight;
                  }
                });
              }, 60);
            }
          }
        }
      } else {
        // 玩家消息 / system 消息：一次性直接显示
        content.textContent = rawText;
      }
      item.appendChild(content);
      list.appendChild(item);
    });

    // —— 思考中占位：玩家刚发完消息，花火还没回复时显示
    const nowSec = Date.now() / 1000;
    if (shouldShowThinking(messages, nowSec)) {
      const think = document.createElement("article");
      think.className = "message bot chat thinking-wrap";
      const meta = document.createElement("span");
      meta.className = "message-meta";
      meta.textContent = "花火";
      think.appendChild(meta);
      const indicator = document.createElement("p");
      indicator.className = "message-content typing-indicator";
      indicator.innerHTML = [
        "<span class=\"thinking-label\">花导思考中</span>",
        "<span class=\"dots\"><b></b><b></b><b></b></span>",
      ].join("");
      think.appendChild(indicator);
      list.appendChild(think);
    }

    if (wasNearBottom || activeRoomView === "chat") list.scrollTop = list.scrollHeight;
    if (activeRoomView === "chat") lastSeenMessageId = latestMessageId(room);
  }

  function renderTurtleSoup() {
    if (room?.game_type !== "turtle_soup") return;
    const game = room.game;
    const puzzle = game?.puzzle;
    const playerHosted = game?.mode === "player_host" || room.turtle_soup_mode === "player_host";
    document.getElementById("soupTitle").textContent = playerHosted
      ? "玩家出题 · 花火 猜"
      : sanitizeDisplayText(puzzle?.title || "正在准备题目");
    document.getElementById("soupSurface").textContent = playerHosted
      ? "玩家轮流提供公开线索或回答 花火 的问题；花火 不会提前看到隐藏汤底。"
      : sanitizeDisplayText(puzzle?.surface || (game?.failure_reason ? "花火 正在重新整理题目。" : "花火 正在构思一道新的海龟汤。"));
    document.getElementById("soupContentLevel").textContent = {
      all_ages: "全年龄", normal: "普通", unrestricted: "不限制",
    }[puzzle?.content_level || game?.content_level] || "普通";
    document.getElementById("soupQuestionCount").textContent = game?.question_count ?? 0;
    document.getElementById("soupHintCount").textContent = playerHosted
      ? `${room.player_numbers?.length || 0} 人`
      : `${game?.hints_used ?? 0} / ${game?.hint_limit ?? 0}`;
    document.getElementById("soupAnswerCount").textContent = game?.answer_attempts ?? 0;
    document.getElementById("soupFactCount").textContent = playerHosted
      ? game?.turn_count ?? 0
      : `${game?.discovered_fact_count ?? 0} / ${game?.key_fact_count ?? 0}`;
    const progressLabels = document.querySelectorAll(".soup-progress dt");
    if (progressLabels.length === 4) {
      progressLabels[0].textContent = playerHosted ? "花火 提问" : "提问";
      progressLabels[1].textContent = playerHosted ? "参与玩家" : "提示";
      progressLabels[2].textContent = playerHosted ? "花火 猜测" : "答案尝试";
      progressLabels[3].textContent = playerHosted ? "公开回合" : "关键事实";
    }

    const turn = document.getElementById("soupTurn");
    const remaining = room.turn_deadline
      ? Math.max(0, Math.ceil(room.turn_deadline - Number(room.server_time || 0)))
      : 0;
    const currentPlayerLabel = room.current_player_name
      ? `${room.current_player_name}（${room.current_player_number}号）`
      : (room.current_player_number ? `${room.current_player_number}号` : "未知玩家");
    turn.textContent = room.current_player_number
      ? `当前轮到 ${currentPlayerLabel}${remaining ? ` · 剩余 ${remaining} 秒` : ""}${room.is_current_player ? " · 轮到你" : ""}`
      : "等待玩家加入";

    const history = document.getElementById("soupHistory");
    history.replaceChildren();
    const entries = Array.isArray(game?.entries) ? game.entries.slice() : [];
    if (!entries.length) {
      const empty = document.createElement("span");
      empty.className = "soup-empty";
      empty.textContent = game?.preparing ? "题目生成并校验后会显示在这里" : "还没有公开问答";
      history.appendChild(empty);
    } else {
      entries.forEach((entry) => {
        const item = document.createElement("article");
        item.className = `soup-entry ${entry.kind || "question"} ${entry.pending ? "pending" : ""}`;
        const prompt = document.createElement("p");
        prompt.className = "prompt";
        prompt.textContent = entry.kind === "reverse"
          ? `${entry.player_number || "?"} 号线索/回答：${entry.prompt || ""}`
          : entry.kind === "hint"
          ? "玩家申请了提示"
          : `${entry.player_number ? `${entry.player_number} 号` : "玩家"}${entry.kind === "answer" ? "猜测" : "问题"}：${entry.prompt || ""}`;
        const response = document.createElement("p");
        response.className = "response";
        response.textContent = entry.kind === "reverse" && entry.pending
          ? sanitizeDisplayText(entry.response || "花火 推理中")
          : entry.kind === "reverse"
          ? `花火 ${entry.bot_action === "guess" ? "猜测" : "提问"}：${sanitizeDisplayText(entry.response || "")}`
          : sanitizeDisplayText(entry.response || "花火 判断中");
        item.append(prompt, response);
        history.appendChild(item);
      });
      history.scrollTop = history.scrollHeight;
    }

    const solution = document.getElementById("soupSolution");
    solution.hidden = playerHosted || !puzzle?.solution;
    document.getElementById("soupSolutionText").textContent = sanitizeDisplayText(puzzle?.solution || "");

  }

  function renderPigDice() {
    if (room?.game_type !== "pig_dice") return;
    const game = room.game;
    if (!game) renderedDiceSequence = 0;
    document.getElementById("diceTarget").textContent = game?.target_score ?? 50;
    document.getElementById("diceHumanScore").textContent = game?.human_score ?? 0;
    document.getElementById("diceBotScore").textContent = game?.bot_score ?? 0;
    document.getElementById("diceTurnTotal").textContent = game?.turn_total ?? 0;
    document.getElementById("diceRisk").textContent = `花火 风格：${{
      cautious: "稳健", balanced: "均衡", bold: "大胆",
    }[game?.risk_style] || "均衡"}`;

    const cube = document.getElementById("diceCube");
    const value = Number(game?.last_roll || 0);
    cube.className = value ? `dice-cube value-${value}` : "dice-cube waiting";
    cube.setAttribute("aria-label", value ? `骰子点数 ${value}` : "尚未掷骰");
    if (game?.action_count && game.action_count !== renderedDiceSequence) {
      renderedDiceSequence = game.action_count;
      cube.classList.add("is-rolling");
      window.setTimeout(() => cube.classList.remove("is-rolling"), 420);
    }

    const status = document.getElementById("diceStatus");
    if (!game) status.textContent = "等待开局";
    else if (game.finished) status.textContent = game.winner === "human" ? "玩家获胜" : "花火 获胜";
    else if (room.status === "paused") status.textContent = "对局已暂停";
    else status.textContent = game.turn === "human" ? "轮到玩家" : "花火 正在掷骰";

    const history = document.getElementById("diceHistory");
    history.replaceChildren();
    const entries = Array.isArray(game?.history) ? game.history.slice(-10).reverse() : [];
    if (!entries.length) {
      const empty = document.createElement("p");
      empty.className = "dice-empty";
      empty.textContent = "开局后，每次掷骰和存分都会记录在这里。";
      history.appendChild(empty);
    } else {
      entries.forEach((entry) => {
        const item = document.createElement("div");
        item.className = `dice-event ${entry.actor || "human"} ${entry.action || "roll"}`;
        const actor = entry.actor === "human" ? "玩家" : "花火";
        let text = `${actor} 掷出 ${entry.value}`;
        if (entry.action === "bust") text = `${actor} 掷出 1，损失 ${entry.lost || 0} 分`;
        if (entry.action === "hold") text = `${actor} 收手，存下 ${entry.banked || 0} 分`;
        if (entry.action === "win") text = `${actor} 存下 ${entry.banked || 0} 分并获胜`;
        if (entry.action === "resign") text = "玩家投降，本局结束";
        item.textContent = text;
        history.appendChild(item);
      });
    }

    const canAct = Boolean(
      room.is_player && room.status === "active" && game && !game.finished
      && game.turn === "human" && !busy
    );
    document.getElementById("diceRollAction").disabled = !canAct;
    document.getElementById("diceHoldAction").disabled = !canAct || !(game?.turn_total > 0);
  }

  function blackjackCardNode(card, hidden = false) {
    const node = document.createElement("span");
    const red = card && (card.suit === "♥" || card.suit === "♦");
    node.className = `playing-card ${hidden ? "face-down" : ""} ${red ? "red" : ""}`;
    if (hidden || !card) {
      node.textContent = "?";
      node.setAttribute("aria-label", "庄家暗牌");
      return node;
    }
    node.setAttribute("aria-label", `${card.rank}${card.suit}`);
    const rank = document.createElement("strong");
    rank.textContent = card.rank;
    const suit = document.createElement("span");
    suit.textContent = card.suit;
    node.append(rank, suit);
    return node;
  }

  function renderBlackjack() {
    if (room?.game_type !== "blackjack") return;
    const game = room.game || {};
    document.getElementById("blackjackDifficulty").textContent = difficultyLabel(room.difficulty);

    const dealerCards = document.getElementById("blackjackDealerCards");
    dealerCards.replaceChildren();
    (Array.isArray(game.dealer_cards) ? game.dealer_cards : []).forEach((card) => {
      dealerCards.appendChild(blackjackCardNode(card));
    });
    if (game.dealer_hidden) dealerCards.appendChild(blackjackCardNode(null, true));
    document.getElementById("blackjackDealerTotal").textContent = game.dealer_hidden
      ? "?"
      : String(game.dealer_total ?? "?");
    if (game.dealer_blackjack) {
      document.getElementById("blackjackDealerTotal").textContent += " · 21点";
    }

    const resultLabels = { blackjack_win: "21点获胜", win: "赢", push: "平", loss: "输" };
    const statusLabels = {
      playing: "进行中", stand: "停牌", bust: "爆牌", blackjack: "21点", surrendered: "已投降",
    };
    const hands = document.getElementById("blackjackHands");
    hands.replaceChildren();
    Object.entries(game.hands || {})
      .sort(([left], [right]) => Number(left) - Number(right))
      .forEach(([number, hand]) => {
        const own = number === String(room.visitor_number);
        const current = room.is_current_player && number === String(room.current_player_number);
        const item = document.createElement("article");
        item.className = `blackjack-hand ${hand.status || "playing"} ${own ? "own" : ""} ${current ? "current" : ""}`;
        const header = document.createElement("header");
        const title = document.createElement("strong");
        const label = (room.player_labels || []).find((entry) => entry.includes(`${number}号`)) || `${number}号`;
        title.textContent = own ? `${label}（你）` : label;
        const badge = document.createElement("span");
        badge.className = `hand-status ${hand.status || "playing"}`;
        badge.textContent = statusLabels[hand.status] || "进行中";
        header.append(title, badge);
        const cards = document.createElement("div");
        cards.className = "playing-cards";
        (Array.isArray(hand.cards) ? hand.cards : []).forEach((card) => {
          cards.appendChild(blackjackCardNode(card));
        });
        const footer = document.createElement("footer");
        const total = document.createElement("strong");
        total.textContent = `${hand.value} 点`;
        const result = document.createElement("span");
        result.textContent = resultLabels[hand.result] || "";
        footer.append(total, result);
        item.append(header, cards, footer);
        hands.appendChild(item);
      });

    const turn = document.getElementById("blackjackTurn");
    const remaining = room.turn_deadline
      ? Math.max(0, Math.ceil(room.turn_deadline - Number(room.server_time || 0)))
      : 0;
    if (game.finished) {
      turn.textContent = "本局已经结算";
    } else if (game.phase === "dealer_turn") {
      turn.textContent = "庄家已经开牌，正在按规则补牌";
    } else {
      const currentLabel = room.current_player_name
        ? `${room.current_player_name}（${room.current_player_number}号）`
        : `${room.current_player_number || "?"}号`;
      turn.textContent = room.is_current_player
        ? `轮到你：要牌还是停牌${remaining ? ` · 剩余 ${remaining} 秒` : ""}`
        : `轮到 ${currentLabel}${remaining ? ` · 剩余 ${remaining} 秒` : ""}`;
    }

    const history = document.getElementById("blackjackHistory");
    history.replaceChildren();
    const events = Array.isArray(game.history) ? game.history.slice(-12).reverse() : [];
    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "blackjack-empty";
      empty.textContent = "发牌后，要牌、停牌和庄家补牌都会记录在这里。";
      history.appendChild(empty);
    } else {
      events.forEach((entry) => {
        const item = document.createElement("div");
        item.className = `blackjack-event ${entry.action || ""}`;
        let text;
        if (entry.action === "hit") {
          text = `${entry.number} 号要牌 ${entry.card?.rank || ""}${entry.card?.suit || ""}，${entry.value} 点`;
        } else if (entry.action === "stand") {
          text = `${entry.number} 号停牌，${entry.value} 点`;
        } else if (entry.action === "surrender") {
          text = `${entry.number} 号投降`;
        } else if (entry.action === "dealer_hit") {
          text = `庄家补牌 ${entry.card?.rank || ""}${entry.card?.suit || ""}，${entry.dealer_total} 点`;
        } else if (entry.action === "settle") {
          text = `本局结算，庄家 ${entry.dealer_total} 点`;
        } else {
          text = "牌局进展";
        }
        item.textContent = text;
        history.appendChild(item);
      });
    }

    const hand = game.hands?.[String(room.visitor_number)];
    const canAct = Boolean(
      room.is_current_player
      && room.is_player
      && room.status === "active"
      && game.phase === "player_turns"
      && !game.finished
      && hand?.status === "playing"
      && !busy
    );
    document.getElementById("blackjackHitAction").disabled = !canAct;
    document.getElementById("blackjackStandAction").disabled = !canAct;
  }

  async function blackjackAction(action) {
    if (busy) return;
    busy = true;
    render();
    try {
      const data = await request("POST", "blackjack/action", {
        visitor_token: visitorToken,
        action,
      });
      setRoom(data.room);
      render();
    } catch (error) {
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "无法完成操作");
    } finally {
      busy = false;
      render();
    }
  }

  function syncDrawState() {
    if (room?.game_type !== "draw_guess") return;
    const serverGame = room.game || {};
    if (
      !activeDrawStroke
      && !drawSyncBusy
      && !drawDirty
      && Number(serverGame.revision ?? -1) >= drawRevision
    ) {
      drawStrokes = Array.isArray(serverGame.strokes) ? serverGame.strokes : [];
      drawRevision = Number(serverGame.revision ?? 0);
    }
  }

  function ucCampText(camp) {
    return { civilian: "平民", undercover: "卧底", whiteboard: "白板" }[camp] || camp || "未公布";
  }
  function ucPlayerLabel(player) {
    const base = `${player.player_number}号${player.display_name ? " · " + player.display_name : ""}`;
    if (player.camp && room?.status === "finished") {
      return `${base}（${ucCampText(player.camp)}）`;
    }
    return base;
  }

  function renderUndercover() {
    if (!room || room.game_type !== "undercover") return;
    const snap = room.game || {};
    const stage = document.getElementById("undercoverStage");
    if (stage.hidden) return;

    // 新一局开始（game_uid 变化）：重置“上一帧”状态，保证淘汰动画、发言提示按新对局重新触发
    if (snap.game_uid && ucLastGameUid !== snap.game_uid) {
      ucLastGameUid = snap.game_uid;
      ucShownOutSet.clear();
      ucVoteRevealSet.clear();
      ucPrevMyTurn = false;
      ucTurnBannerDone.clear();
      ucLastRoundCount = 0;
      ucPrevMyOut = false;
      for (const k of Object.keys(ucRevealNodeCache)) delete ucRevealNodeCache[k];
      for (const k of Object.keys(ucSpeechTyping)) delete ucSpeechTyping[k];
      window.clearTimeout(ucVoteFlipTimeout);
      ucVoteFlipTimeout = null;
    }

    // 1. 顶部 round label + 阵营统计
    const roundNumber = Number(snap.current_round_number || 0);
    const phaseText = {
      idle: "等待玩家入座",
      preparing: "发词准备",
      speech: "发言轮",
      pk: "PK 子轮",
      voting: "投票轮",
      finished: "本局结束",
    }[snap.phase] || snap.phase || "等待开始";
    document.getElementById("ucRoundLabel").textContent = roundNumber
      ? `第 ${roundNumber} 轮 · ${phaseText}`
      : `${phaseText}`;

    const campCounts = snap.camp_info?.counts_live || snap.camp_info?.counts_all || {};
    const campUl = document.getElementById("ucCampCounts");
    campUl.innerHTML = "";
    const campOrder = ["civilian", "undercover", "whiteboard"];
    campOrder.forEach((camp) => {
      const total = Number(snap.camp_info?.counts_all?.[camp] || 0);
      const live = Number(campCounts[camp] || 0);
      // 白板即使未配置也始终展示（0/0）
      const li = document.createElement("li");
      li.className = `uc-camp-${camp}`;
      li.innerHTML = `<span>${ucCampText(camp)}</span><strong>${live}/${total}</strong>`;
      campUl.appendChild(li);
    });

    // 1b. 房主阵营自定义面板 + 手动添AI按钮
    const hostCard = document.getElementById("ucHostCampCard");
    if (hostCard) {
      const waiting = ["waiting", "setup"].includes(room.status || "");
      const canCustomize = !!room.undercover_allow_host_customize_camp_scales;
      const seats = Array.isArray(room.player_seats) ? room.player_seats : [];
      // 房主定义：第一个绑定QQ身份的真人玩家；admin_room 直接取第一个非AI入座者作为管理权限
      let hostSeat = null;
      if (!!room.admin_room) {
        hostSeat = seats.find((s) => !s.is_ai) || seats[0] || null;
      } else {
        hostSeat = seats.find((s) => !s.is_ai && !!s.identity_confirmed) || null;
      }
      const amHost = !!hostSeat && (hostSeat.visitor_token === visitorToken);
      hostCard.hidden = !(waiting && (canCustomize || !!room.admin_room) && amHost);
      if (!hostCard.hidden) {
        const hostHint = document.getElementById("ucHostCampHint");
        const civ = document.getElementById("ucHostCivInput");
        const uc = document.getElementById("ucHostUcInput");
        const wb = document.getElementById("ucHostWbInput");
        const save = document.getElementById("ucHostCampSave");
        const addAi = document.getElementById("ucHostAddAi");
        const seatCount = document.getElementById("ucHostSeatCount");
        const defaultScales = (room.undercover_host_camp_scales || "4 1 0")
          .split(/[\s:：,，]+/).map((x) => Number(x) || 0);
        if (defaultScales.length < 3) defaultScales.push(0, 0, 0);
        // 只在「房主已保存的比例」发生变化时才回填输入框；否则不做任何覆盖。
        // 这样玩家在中途编辑（哪怕已失焦还没点保存）时，数字不会因每次轮询被重置回默认。
        const scalesKey = String(room.undercover_host_camp_scales || "4 1 0").trim();
        if (ucLastHostScalesKey !== scalesKey) {
          ucLastHostScalesKey = scalesKey;
          civ.value = String(defaultScales[0] || 4);
          uc.value = String(defaultScales[1] || 1);
          wb.value = String(defaultScales[2] || 0);
        }
        if (hostHint) hostHint.textContent = `当前比例：${civ.value} 民 ${uc.value} 卧 ${wb.value} 白（按实际人数按比例折算）`;
        const capacity = Number(room.player_capacity || 0);
        const current = seats.length;
        if (seatCount) seatCount.textContent = capacity > 0
          ? `当前 ${current}/${capacity} 人 · 至少 ${Number(room.undercover_min_players || 2)} 人开局`
          : `当前 ${current} 人 · 至少 ${Number(room.undercover_min_players || 2)} 人开局`;
        // —— 房主「告知身份」开关：本局生效，开局前可随时更改
        const revealCheck = document.getElementById("ucHostRevealCheck");
        const revealHint = document.getElementById("ucHostRevealHint");
        if (revealCheck) {
          const revealOn = room.undercover_reveal_identity !== false;
          if (document.activeElement !== revealCheck) revealCheck.checked = revealOn;
          if (revealHint) revealHint.textContent = revealOn
            ? "开启：发放身份 + 词条；关闭：只发词条、不告知平民/卧底身份（白板不受影响）"
            : "当前已关闭：开场只发放词条，不告知平民/卧底身份（白板不受影响）。";
          if (!revealCheck.dataset.bound) {
            revealCheck.dataset.bound = "1";
            revealCheck.addEventListener("change", async () => {
              if (!accessToken || !visitorToken) {
                showToast("请先进入玩家席");
                revealCheck.checked = !revealCheck.checked;
                return;
              }
              const next = revealCheck.checked;
              try {
                revealCheck.disabled = true;
                const res = await request(
                  "POST",
                  "undercover/reveal",
                  { visitor_token: visitorToken, reveal_identity: next }
                );
                if (res?.room) {
                  setRoom(res.room);
                  render();
                  showToast(next ? "已开启「告知身份」：开场发放身份 + 词条" : "已关闭「告知身份」：开场只发放词条");
                } else if (res?.error) {
                  showToast(res.error);
                  revealCheck.checked = !next;
                }
              } catch (err) {
                showToast(err?.message || "设置失败");
                revealCheck.checked = !next;
              } finally {
                revealCheck.disabled = false;
              }
            });
          }
        }
        // —— 保存房间设置
        if (save && !save.dataset.bound) {
          save.dataset.bound = "1";
          save.addEventListener("click", async () => {
            const civN = Math.max(1, Math.min(20, Number(civ.value) || 1));
            const ucN = Math.max(1, Math.min(10, Number(uc.value) || 1));
            const wbN = Math.max(0, Math.min(5, Number(wb.value) || 0));
            if (!accessToken || !visitorToken) {
              showToast("请先进入玩家席");
              return;
            }
            try {
              save.disabled = true;
              const res = await request(
                "POST",
                "undercover/camp_scales",
                { visitor_token: visitorToken, camp_scales: `${civN} ${ucN} ${wbN}` }
              );
              if (res?.camp_scales && res?.room) {
                setRoom(res.room);
                render();
                showToast(`已保存房间设置：${res.camp_scales.raw}`);
              } else if (res?.error) {
                showToast(res.error);
              } else {
                showToast("保存失败");
              }
            } catch (err) {
              showToast(err?.message || "保存失败");
            } finally {
              save.disabled = false;
            }
          });
        }
        // —— 添加一位 AI 玩家
        if (addAi && !addAi.dataset.bound) {
          addAi.dataset.bound = "1";
          addAi.addEventListener("click", async () => {
            if (!accessToken || !visitorToken) {
              showToast("请先进入玩家席");
              return;
            }
            try {
              addAi.disabled = true;
              const res = await request(
                "POST",
                "undercover/add_ai",
                { visitor_token: visitorToken }
              );
              if (res?.room) {
                setRoom(res.room);
                render();
                showToast(res?.display_name
                  ? `已加入 ${res.display_name}（现在 ${res.live_count || current + 1} 人）`
                  : "已添加 AI 玩家");
              } else if (res?.error) {
                showToast(res.error);
              } else {
                showToast("添加失败");
              }
            } catch (err) {
              showToast(err?.message || "添加失败");
            } finally {
              addAi.disabled = false;
            }
          });
        }
      }
    }

    // 2. 玩家卡片网格
    const grid = document.getElementById("ucPlayersGrid");
    grid.innerHTML = "";
    const players = snap.players_public || [];
    const expectedSpeaker = snap.expected_speaker_number;
    const voterNumber = snap.voter_player_number;
    // 座位准备状态（按座位号映射，集结阶段展示）
    const seatReadyMap = new Map(
      (Array.isArray(room.player_seats) ? room.player_seats : []).map((s) => [
        Number(s.number),
        !!s.ready,
      ])
    );
    const inAssembly = room.status === "setup" && (!snap.phase || snap.phase === "idle");
    players.forEach((p) => {
      const card = document.createElement("article");
      card.className = "uc-player-card";
      if (p.is_out) card.classList.add("is-out");
      if (expectedSpeaker === p.player_number && !p.is_out) card.classList.add("is-speaking");
      if (snap.phase === "voting" && voterNumber && p.player_number !== voterNumber && !p.is_out) {
        card.classList.add("can-vote-target");
      }
      // 是否本轮被投最高（平票）
      if ((snap.last_pk_targets || []).includes(p.player_number)) {
        card.classList.add("is-pk");
      }
      // 刚被淘汰的玩家卡片：播放叉掉动画（仅对局进行中、且该玩家头一次以出局状态出现，保证只播一次）
      if (p.is_out && room.status === "active" && !ucShownOutSet.has(p.player_number)) {
        card.classList.add("is-just-out");
        ucShownOutSet.add(p.player_number);
      }
      const headRow = document.createElement("div");
      headRow.className = "uc-pn-head";
      card.appendChild(headRow);
      headRow.appendChild(ucSeatAvatarNode(Number(p.player_number), "medium", (room.visitor_number && p.player_number === room.visitor_number)));
      const nameLine = document.createElement("strong");
      nameLine.className = "uc-pn-name";
      nameLine.textContent = `${p.player_number}号${p.display_name ? " · " + sanitizeDisplayText(p.display_name) : ""}`;
      headRow.appendChild(nameLine);
      // 如果 visitor 是本人，要高亮显示
      if (room.visitor_number && p.player_number === room.visitor_number) {
        card.classList.add("is-me");
      }
      const meta = document.createElement("div");
      meta.className = "uc-pn-meta";
      const statusChip = document.createElement("span");
      statusChip.className = p.is_out ? "chip chip-out" : "chip chip-live";
      statusChip.textContent = p.is_out ? "已出局" : "存活";
      meta.appendChild(statusChip);
      if (inAssembly && seatReadyMap.has(Number(p.player_number))) {
        const readyChip = document.createElement("span");
        readyChip.className = seatReadyMap.get(Number(p.player_number))
          ? "chip chip-ready"
          : "chip chip-not-ready";
        readyChip.textContent = seatReadyMap.get(Number(p.player_number)) ? "已准备" : "未准备";
        meta.appendChild(readyChip);
      }
      if (p.camp && room?.status === "finished") {
        const campSpan = document.createElement("span");
        campSpan.className = "chip chip-camp";
        campSpan.textContent = ucCampText(p.camp);
        meta.appendChild(campSpan);
      }
      if (p.word && room?.status === "finished") {
        const wordSpan = document.createElement("span");
        wordSpan.className = "chip chip-word";
        wordSpan.textContent = `词条「${p.word}」`;
        meta.appendChild(wordSpan);
      }
      // 藏品/护身符徽章：独立成行展示，与身份/词条徽章分开，避免结算后与词条挤在同一行
      if (room?.game_type === "undercover" && Array.isArray(room.player_seats)) {
        const seatBadges = (room.player_seats.find((s) => Number(s.number) === Number(p.player_number)) || {}).badges;
        const badges = Array.isArray(seatBadges) ? seatBadges : [];
        if (badges.length) {
          const badgeRow = document.createElement("div");
          badgeRow.className = "uc-badge-row";
          badges.forEach((badge) => {
            const wrap = document.createElement("span");
            wrap.className = "uc-badge-wrap";
            const chip = document.createElement("span");
            chip.className = `chip uc-badge uc-badge-mini uc-badge-${badge.tone || ""}`;
            chip.textContent = badge.emoji || "🏅";
            const tip = document.createElement("span");
            tip.className = "uc-badge-tip";
            // 荣誉段位（gold）按总胜场描述，阵营徽章才写“该阵营已胜”
            tip.textContent = badge.tone === "gold"
              ? `${badge.label || "徽章"} · 累计获胜 ${badge.wins || 0} 局`
              : `${badge.label || "徽章"} · 该阵营已胜 ${badge.wins || 0} 局`;
            wrap.appendChild(chip);
            wrap.appendChild(tip);
            // 移动端没有悬停：点按切换提示窗
            wrap.addEventListener("click", (e) => {
              e.stopPropagation();
              const active = document.querySelectorAll(".uc-badge-wrap.active");
              active.forEach((n) => { if (n !== wrap) n.classList.remove("active"); });
              wrap.classList.toggle("active");
            });
            badgeRow.appendChild(wrap);
          });
          card.appendChild(badgeRow);
        }
      }
      // 被投票数（进行中只在投票阶段显示各目标得票，不显示投手；全员投完后才揭晓数字）
      if (
        snap.vote_tally_live &&
        snap.phase === "voting" &&
        snap.voting_all_voted
      ) {
        const votes = Number(snap.vote_tally_live[p.player_number] || 0);
        if (votes > 0) {
          const v = document.createElement("span");
          v.className = "chip chip-votes";
          v.textContent = `${votes} 票`;
          meta.appendChild(v);
        }
      }
      card.appendChild(meta);
      grid.appendChild(card);
    });

    // 3. 我的身份卡（只有本人能看到 camp/word）
    const my = snap.my || {};
    const idCardEl = document.getElementById("ucIdentityCard");
    // 本机刚被淘汰：弹出居中的离场提示（过渡到本局身份卡收尾交给关闭按钮处理）
    if (!!my.is_out && !ucPrevMyOut && room.status === "active") {
      const outOverlay = document.getElementById("ucOutOverlay");
      if (outOverlay) {
        const card = document.getElementById("ucOutCard");
        if (card) {
          card.style.animation = "none";
          void card.offsetWidth;
          card.style.animation = "";
        }
        outOverlay.hidden = false;
      } else {
        showToast("很遗憾，你被票出局了。剩下的交给队友吧。", 3600);
      }
    }
    ucPrevMyOut = !!my.is_out;
    const campEl = document.getElementById("ucMyCamp");
    const wordEl = document.getElementById("ucMyWord");
    const hintEl = document.getElementById("ucCampHint");
    campEl.className = "uc-id-camp";
    if (!my.is_player) {
      campEl.textContent = "观众席";
      wordEl.textContent = "—";
      hintEl.textContent = "仅玩家能看到身份词条；请先绑定并加入玩家席。";
    } else if (my.is_out && my.camp) {
      // 已出局：身份卡变灰并提示离场
      campEl.classList.add("is-word-only");
      const campName = ucCampText(my.camp);
      campEl.textContent = `${campName} · 已出局`;
      wordEl.textContent = my.word || "—";
      hintEl.textContent = "你已被淘汰，接下来只能观战聆听，为队友加油吧。";
    } else if (my.camp === "whiteboard") {
      campEl.classList.add("is-whiteboard");
      campEl.textContent = "白板";
      wordEl.textContent = "无词条 · 靠猜";
      hintEl.textContent = "你是白板：你没有词条；先模仿他人描述混入，等卧底全出局后你就赢了。";
    } else if (my.camp && my.word) {
      const campName = ucCampText(my.camp);
      campEl.classList.add(`is-${my.camp}`);
      campEl.textContent = campName;
      wordEl.textContent = my.word;
      const map = {
        civilian: "你是平民：你的词条和大多数玩家一致，找到卧底并把他们投出局就赢了。",
        undercover: "你是卧底：你的词条与多数人不同，隐藏自己并把平民投票出局即可获胜。",
        whiteboard: "你是白板：你没有词条；先模仿他人描述混入，等卧底全出局后你就赢了。",
      };
      hintEl.textContent = map[my.camp] || "请妥善保管自己的词条，不要向其他玩家透露。";
    } else if (my.word && !my.camp) {
      // 关闭「告知身份」：只展示词条，不显示身份
      campEl.classList.add("is-word-only");
      campEl.textContent = "词条已发放";
      wordEl.textContent = my.word;
      hintEl.textContent = "本局未告知身份，请凭词条谨慎发言，不要向其他玩家透露。";
    } else {
      // 开局抽选身份词条时的提示
      const dealing =
        my.is_player &&
        room.status === "active" &&
        roundNumber >= 1 &&
        ["speech", "pk", "preparing"].includes(snap.phase) &&
        !my.camp &&
        !my.word;
      if (dealing) {
        campEl.textContent = "抽选中";
        wordEl.textContent = "…";
        hintEl.textContent = "花火 正在抽选你的身份词条卡，请稍候…";
      } else {
        campEl.textContent = "未开始";
        wordEl.textContent = "—";
        hintEl.textContent = "对局开始后这里将显示你个人的身份与词条。";
      }
    }

    // 4. 发言 & 投票时间线
    const timeline = document.getElementById("ucTimeline");
    // 重建前记录“是否贴底”：贴底说明在跟随最新消息，重建后自动滚回底部看新信息；
    // 玩家向上翻看历史（未贴底）时保持原位，不做强制滚动。
    const ucWasNearBottom = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 64;
    timeline.innerHTML = "";
    const rounds = snap.rounds_public || [];
    const currentRoundIdx = rounds.length ? rounds.length - 1 : -1;
    rounds.forEach((r, roundIdx) => {
      // 每一轮包进统一条目，便于双列平铺布局（第1轮左、第2轮右，依自然列流左右交替）
      const roundBox = document.createElement("div");
      roundBox.className = "uc-round";
      const header = document.createElement("header");
      header.className = "uc-round-header";
      const title = document.createElement("h4");
      title.textContent = r.is_pk
        ? `第 ${r.round_number} 轮 · PK 子轮`
        : `第 ${r.round_number} 轮`;
      header.appendChild(title);
      if (r.out_player_number) {
        const outBadge = document.createElement("span");
        outBadge.className = "chip chip-out";
        outBadge.textContent = `${r.out_player_number}号 被淘汰`;
        header.appendChild(outBadge);
      }
      if (r.pk_reason) {
        const pkBadge = document.createElement("span");
        pkBadge.className = "chip chip-pk";
        pkBadge.textContent = "平票 → PK";
        header.appendChild(pkBadge);
      }
      roundBox.appendChild(header);
      const ul = document.createElement("ul");
      ul.className = "uc-round-list";
      // 发言列表（按 speaking_order 顺序）
      (r.speech_order || []).forEach((pn) => {
        const sp = (r.speeches || []).find((s) => Number(s.player_number) === Number(pn));
        const li = document.createElement("li");
        li.className = "uc-round-speech";
        const head = document.createElement("strong");
        head.textContent = `${pn}号`;
        const content = document.createElement("div");
        content.className = "uc-speech-content" + ((r.skipped_player_numbers || []).includes(Number(pn)) ? " is-skipped" : "");
        const skipped = (r.skipped_player_numbers || []).includes(Number(pn));
        if (sp) {
          // 已发言：文字“逐字加速打出”（先慢后快），跨轮询复用节点保持动画连续
          const typeText = sanitizeDisplayText(sp.content);
          const typeKey = `${snap.game_uid}:${r.round_number}:${Number(pn)}`;
          const cached = ucSpeechTyping[typeKey];
          if (cached && cached.text === typeText) {
            if (cached.done) { content.textContent = typeText; }
            else if (cached.node) { content.appendChild(cached.node); }
          } else {
            ucStartSpeechTyping(typeKey, typeText, content);
          }
        } else if (skipped) {
          content.textContent = "（发言超时，已跳过）";
        } else if (
          roundIdx === currentRoundIdx &&
          Number(pn) === Number(expectedSpeaker) &&
          ["speech", "pk"].includes(snap.phase)
        ) {
          // 当前正在发言的玩家：展示“思考中”状态
          content.classList.add("is-thinking");
          content.textContent = "我正在思考中…";
          li.classList.add("is-now");
        } else {
          content.textContent = "（尚未发言）";
        }
        li.appendChild(head);
        li.appendChild(content);
        ul.appendChild(li);
      });
      // 投票列表（管理台开启「展示具体投票人」时显示逐票明细，否则只显示得票统计）
      const votes = r.votes || [];
      const voteHeaderLi = document.createElement("li");
      voteHeaderLi.className = "uc-round-votes";
      const vh = document.createElement("strong");
      // 完成态依据「该轮应投票的人数」而非 phase：投票是实时写入 r.votes 的，phase 在最后一票后立刻推进，
      // 因此不能用 phase/voting_all_voted 判断，否则揭晓动画永不触发。
      const expectedVoters = (r.vote_player_numbers || []).length;
      const votesComplete = expectedVoters > 0 && votes.length >= expectedVoters;
      const isLatestRound = roundIdx === currentRoundIdx;
      // 正在投票：仅当前轮、仍在投票阶段、票未集齐时展示（不暴露任何数字）
      const stillVoting = isLatestRound && snap.phase === "voting" && !votesComplete && expectedVoters > 0;
      // 统一格式化「X号·昵称」
      const fmtPn = (n) => {
        const p = players.find((x) => Number(x.player_number) === Number(n));
        const label = p ? p.display_name || "" : "";
        return `${n}号${label && label !== `${n}号` ? " · " + label : ""}`;
      };
      if (stillVoting) {
        // 投票进行中：只显示「正在投票」，不提前暴露任何票数数字
        vh.textContent = "投票进行中…";
        const votingBlock = document.createElement("div");
        votingBlock.className = "uc-votes-block is-voting";
        const line = document.createElement("div");
        line.className = "uc-vote-row is-tally";
        const who = document.createElement("span");
        who.className = "uc-target";
        who.textContent = "正在投票中";
        const num = document.createElement("em");
        num.className = "uc-vote-count is-placeholder";
        num.textContent = "…";
        line.appendChild(who);
        line.appendChild(num);
        votingBlock.appendChild(line);
        voteHeaderLi.appendChild(vh);
        voteHeaderLi.appendChild(votingBlock);
      } else if (votesComplete && votes.length) {
        // 这一轮刚集齐票：给投票块播放「放大→票数从？翻成数字」的揭晓动画（每轮只播一次）
        // 注意：此处只读 justRevealed 用于 show_voters 分支；集齐标记的写入在各自分支内完成，
        // 避免在进入票数统计分支前就提前把标记置为“已揭晓”而导致下图动画被跳过。
        const preclude = !ucVoteRevealSet.has(r.round_number);
        if (snap.show_voters) {
          if (preclude) ucVoteRevealSet.add(r.round_number);
          vh.textContent = "投票结果（含投票人）";
          const voteBlock = document.createElement("div");
          voteBlock.className = "uc-votes-block" + (preclude ? " uc-reveal-pop" : "");
          if (preclude) voteBlock.dataset.roundReveal = String(r.round_number);
          votes.forEach((v) => {
            const row = document.createElement("div");
            row.className = "uc-vote-row is-flow";
            const voter = document.createElement("span");
            voter.className = "uc-voter";
            voter.textContent = fmtPn(v.voter_number);
            const arrow = document.createElement("i");
            arrow.className = "uc-vote-arrow";
            arrow.textContent = "→";
            const target = document.createElement("span");
            target.className = "uc-target";
            target.textContent = fmtPn(v.target_number);
            row.appendChild(voter);
            row.appendChild(arrow);
            row.appendChild(target);
            voteBlock.appendChild(row);
          });
          voteHeaderLi.appendChild(vh);
          voteHeaderLi.appendChild(voteBlock);
        } else {
          vh.textContent = "投票结果（票数统计）";
          const tally = {};
          votes.forEach((v) => {
            tally[v.target_number] = (tally[v.target_number] || 0) + 1;
          });
          const voterNums = (r.vote_player_numbers || []).slice();
          if (!voterNums.length) {
            Object.keys(tally).forEach((k) => voterNums.push(Number(k)));
          }
          // 揭晓帧缓存：时间线每轮询会清空重建，若直接重建会立刻杀死正在播放的动画。
          // 这里把刚集齐票的投票块缓存起来，在揭晓进行期间复用同一个 DOM 节点（动画不中断）。
          const cacheKey = `${snap.game_uid}:${r.round_number}`;
          const REVEAL_TTL = 2000;
          const cached = ucRevealNodeCache[cacheKey];
          const justRevealed = !ucVoteRevealSet.has(r.round_number);
          const nowMs = Date.now();
          let voteBlock;
          if (justRevealed) {
            ucVoteRevealSet.add(r.round_number);
            voteBlock = document.createElement("div");
            voteBlock.className = "uc-votes-block uc-reveal-pop";
            voteBlock.dataset.roundReveal = String(r.round_number);
            let tallyIdx = 0;
            voterNums.forEach((n) => {
              const count = Number(tally[n] || 0);
              const row = document.createElement("div");
              row.className = "uc-vote-row is-tally";
              const who = document.createElement("span");
              who.className = "uc-target";
              who.textContent = fmtPn(Number(n));
              const num = document.createElement("em");
              num.className = "uc-vote-count is-question";
              num.dataset.count = String(count);
              num.style.setProperty("--d", `${tallyIdx * 130}ms`);
              num.textContent = "？";
              row.appendChild(who);
              row.appendChild(num);
              voteBlock.appendChild(row);
              tallyIdx++;
            });
            ucRevealNodeCache[cacheKey] = { node: voteBlock, at: nowMs };
            scheduleUcVoteFlip(r.round_number);
          } else if (cached && nowMs - cached.at < REVEAL_TTL) {
            // 揭晓动画仍在进行：复用缓存的节点，保持动画连续
            voteBlock = cached.node;
          } else {
            if (cached) delete ucRevealNodeCache[cacheKey];
            voteBlock = document.createElement("div");
            voteBlock.className = "uc-votes-block";
            let tallyIdx = 0;
            voterNums.forEach((n) => {
              const count = Number(tally[n] || 0);
              const row = document.createElement("div");
              row.className = "uc-vote-row is-tally";
              const who = document.createElement("span");
              who.className = "uc-target";
              who.textContent = fmtPn(Number(n));
              const num = document.createElement("em");
              num.className = "uc-vote-count" + (count === 0 ? " is-zero" : "");
              num.textContent = `${count}`;
              row.appendChild(who);
              row.appendChild(num);
              voteBlock.appendChild(row);
              tallyIdx++;
            });
          }
          voteHeaderLi.appendChild(vh);
          voteHeaderLi.appendChild(voteBlock);
        }
      }
      if (voteHeaderLi.childNodes.length > 0) ul.appendChild(voteHeaderLi);
      roundBox.appendChild(ul);
      // 新进入的一轮：给整轮时间线淡入动画（仅轮次新增时触发一次，避免每次轮询闪烁）
      if (rounds.length && r.is_pk === false && roundIdx === rounds.length - 1 && rounds.length > ucLastRoundCount) {
        roundBox.classList.add("uc-round-new");
      }
      timeline.appendChild(roundBox);
    });

    // 记录本轮时间线轮次数（供新轮淡入动画判断）
    ucLastRoundCount = rounds.length;

    // 新信息出现时自动跟随到底部（仅贴底时滚动，向上翻看历史时保持不动）
    if (ucWasNearBottom) timeline.scrollTop = timeline.scrollHeight;

    // 5. 操作区：发言 / 投票 / PK banner
    document.getElementById("ucPhase").textContent = phaseText;

    // 5b. 发词阶段的提示 + 开场身份卡动画
    if (snap.phase === "preparing") {
      document.getElementById("ucPhase").textContent = "发词中… 花火正在抽选你的身份词条卡";
    }
    if (
      snap.phase === "speech" &&
      roundNumber === 1 &&
      my.is_player &&
      (my.camp || my.word) &&
      room.status !== "finished"
    ) {
      // 身份卡只在本局首次发放时弹出一次；标记写入 localStorage（键含房间令牌 + 本局 uid），
      // 刷新页面 / 重新打开链接后不会重复弹出，新一局 uid 变化会再次弹出。
      // 关闭「告知身份」时，card 只展示词条（revealUcIdentity 内部处理）。
      const revealKey = `uc:revealed:${accessToken}:${snap.game_uid || ""}`;
      let alreadyRevealed = false;
      try {
        alreadyRevealed = !!window.localStorage.getItem(revealKey);
        if (!alreadyRevealed) window.localStorage.setItem(revealKey, "1");
      } catch (_err) {
        alreadyRevealed = false; // localStorage 不可用时退回“总是弹出”
      }
      if (!alreadyRevealed) revealUcIdentity(my);
    }
    const expectedSpan = document.getElementById("ucExpectedSpeaker");
    if (expectedSpeaker && snap.phase !== "finished") {
      const nextNumber = snap.next_speaker_number;
      expectedSpan.textContent = `轮到 ${expectedSpeaker}号 发言` +
        (nextNumber ? ` · 下一位 ${nextNumber}号` : "");
    } else {
      expectedSpan.textContent = "";
    }
    const speechBox = document.getElementById("ucSpeechBox");
    const voteBox = document.getElementById("ucVoteBox");
    const pkBanner = document.getElementById("ucPkBanner");
    const speechBtn = document.getElementById("ucSpeechSubmit");
    const input = document.getElementById("ucSpeechInput");
    const counter = document.getElementById("ucSpeechCount");
    const isMyTurn = my.is_player && my.player_number && expectedSpeaker && Number(my.player_number) === Number(expectedSpeaker);
    const canSpeak = ["speech", "pk"].includes(snap.phase) && isMyTurn;
    // 开场身份卡/“花火选词”遮罩是否仍打开：发词期间不叠加“轮到谁发言”的提示，
    // 避免刚开局时“花火选词”转圈和发言提示同时弹出来。
    const revealOverlayOpen = !(document.getElementById("ucRevealOverlay")?.hidden ?? true);
    // 轮到你发言时的提醒（进入发言轮才提示，避免每次 render 都弹）
    if (canSpeak && !revealOverlayOpen && !ucPrevMyTurn) {
      const myCampName = my.camp ? ucCampText(my.camp) : "";
      showToast(
        `本轮轮到你发言${myCampName ? "（你是" + myCampName + "）" : ""}！请在下方描述你的词条。`,
        3400
      );
    }
    ucPrevMyTurn = canSpeak && !revealOverlayOpen;
    // 发言切换的醒目全屏提示：每位发言者在本轮只弹一次；若被“花火选词”遮罩延迟，
    // 会在遮罩关闭后的下一帧补弹（不再因遮罩期间已“见过”该玩家而永久吞掉首个发言横幅）。
    if (
      ["speech", "pk"].includes(snap.phase) &&
      !revealOverlayOpen &&
      expectedSpeaker
    ) {
      const turnKey = `${roundNumber}:${expectedSpeaker}`;
      if (!ucTurnBannerDone.has(turnKey)) {
        ucTurnBannerDone.add(turnKey);
        showSpeechTurnNotification(expectedSpeaker, isMyTurn);
      }
    }
    speechBox.hidden = !["speech", "pk", "preparing"].includes(snap.phase) || !!my.is_out;
    speechBtn.disabled = !canSpeak || input.value.trim().length < 1;
    input.disabled = !canSpeak;
    input.placeholder = canSpeak
      ? "轮到你发言：输入 1-500 字描述你的词条，不要直接点名词条本身。"
      : "当前不是你的发言轮次。";
    counter.textContent = `${(input.value || "").length}/500`;

    // 投票卡（已被淘汰的玩家不能再投）
    voteBox.hidden = snap.phase !== "voting" || !my.is_player || !!my.is_out;
    const voteGrid = document.getElementById("ucVoteGrid");
    voteGrid.innerHTML = "";
    if (!voteBox.hidden && my.is_player) {
      const alreadyVoted =
        Array.isArray(snap.voted_this_round_player_numbers) &&
        snap.voted_this_round_player_numbers.includes(Number(my.player_number));
      const canVote =
        !alreadyVoted &&
        my.camp !== "whiteboard"
          ? true
          : !alreadyVoted; // 所有人都能投（包括白板），Theresa3rd 允许白板投票
      // PK 子轮的投票：只能投重新发言的平票候选人
      const pkOnlyTargets = (snap.pending_pk_targets || []).map((n) => Number(n));
      players.forEach((p) => {
        if (Number(p.player_number) === Number(my.player_number)) return; // 不能投自己
        if (p.is_out) return; // 不能投已出局
        if (pkOnlyTargets.length && !pkOnlyTargets.includes(Number(p.player_number))) return; // PK 只投平票者
        const card = document.createElement("button");
        card.type = "button";
        card.className = "uc-vote-card";
        card.dataset.target_number = String(p.player_number);
        card.disabled = !canVote;
        const votedCount = Number(snap.vote_tally_live?.[p.player_number] || 0);
        // 投票进行中不暴露票数（显示「？」），全员投完后才揭晓真实票数
        const revealed = !!snap.voting_all_voted;
        card.classList.toggle("uc-vote-revealed", revealed);
        const numCls = revealed ? "uc-vote-num" : "uc-vote-num is-placeholder";
        const numText = revealed ? `${votedCount}` : "？";
        if (!revealed && votedCount > 0) card.classList.add("has-votes");
        card.innerHTML = `<strong>${p.player_number}号</strong><span>${sanitizeDisplayText(p.display_name || "")}</span><em class="${numCls}">${numText}</em>`;
        voteGrid.appendChild(card);
      });
      if (alreadyVoted) {
        const tip = document.createElement("p");
        tip.className = "uc-vote-tip";
        // 全员投完前仅提示等待；投完后给出「投票结束」提示
        tip.textContent = snap.voting_all_voted
          ? "本轮投票已结束，正在揭晓结果…"
          : "你已完成本轮投票，正在等待其他玩家投票…";
        voteGrid.appendChild(tip);
      }
    }

    // PK banner
    const pkTargets = snap.pending_pk_targets || [];
    if (pkTargets.length && snap.phase !== "finished") {
      pkBanner.hidden = false;
      pkBanner.textContent = `平票！${pkTargets.map((n) => n + "号").join("、")} 需要补充发言 PK，稍后重新投票。`;
    } else {
      pkBanner.hidden = true;
    }

    // 操作区状态引导：当发言框 / 投票卡 / PK 横幅全部收起时，用一个引导块填补空白（idle/setup/voting观众/finished）
    const opGuide = document.getElementById("ucOpGuide");
    if (opGuide) {
      const opGuidesAllHidden = speechBox.hidden && voteBox.hidden && pkBanner.hidden;
      opGuide.hidden = !opGuidesAllHidden;
      if (!opGuide.hidden) {
        const badgeEl = document.getElementById("ucOpGuideBadge");
        const titleEl = document.getElementById("ucOpGuideTitle");
        const textEl = document.getElementById("ucOpGuideText");
        const winner = snap.winner || {};
        const seatsArr = Array.isArray(room.player_seats) ? room.player_seats : [];
        // 已入座人数：AI 恒计入；真人按是否已绑定身份计入（简化口径，仅用于引导提示）
        const liveSeats = seatsArr.filter((s) => s.is_ai || s.identity_confirmed).length;
        let badge = "🌀", title = "", text = "";
        if (room.status === "finished" || snap.phase === "finished") {
          badge = "🏆";
          title = "本局已结束";
          text = sanitizeDisplayText(winner.message || "胜负已分，可申请再来一局或退出对局。");
        } else if (my.is_out && my.is_player) {
          badge = "👀";
          title = "已出局 · 观战中";
          text = "你已被淘汰，正在观战，为仍在场的队友加油吧。";
        } else if (snap.phase === "voting") {
          badge = "🗳️";
          title = "玩家正在投票";
          text = "票数结果将在全员投出后揭晓，稍安勿躁。";
        } else if (room.status === "setup") {
          const capacity = Number(room.player_capacity || 0);
          badge = "⏳";
          title = "集结中 · 等待开局";
          text = `已入座 ${liveSeats}/${capacity || "不限"} 人 · 点击「准备」参与本局，全员就绪或倒计时结束自动开局。`;
        } else {
          badge = "🌀";
          title = "等待玩家入座";
          text = "绑定 QQ 并加入玩家席后，即可开始一局谁是卧底。";
        }
        if (badgeEl) badgeEl.textContent = badge;
        if (titleEl) titleEl.textContent = title;
        if (textEl) textEl.textContent = text;
      }
    }

    // 游戏结束结算动画：进入 finished 时每局只弹出一次，新对局开始后重置
    const resultOverlay = document.getElementById("ucResultOverlay");
    if (snap.phase === "finished" && room.status === "finished") {
      const key = `g${Number(room.score?.games || 0)}`;
      if (ucResultShownKey !== key) {
        ucResultShownKey = key;
        showUndercoverResultOverlay(snap, players);
      }
    } else if (ucResultShownKey) {
      // 离开结束态（申请再来一局/新对局开局）时清除标记并收起结算卡
      ucResultShownKey = "";
      if (resultOverlay && !resultOverlay.hidden) resultOverlay.hidden = true;
    }
  }

  function renderDrawGuess() {
    if (room?.game_type !== "draw_guess") return;
    syncDrawState();
    const game = room.game || {};
    const remaining = room.status === "paused"
      ? Number(game.remaining_seconds || 0)
      : Math.max(0, Math.ceil(Number(game.deadline || 0) - Number(room.server_time || Date.now() / 1000)));
    document.getElementById("drawTimer").textContent = !room.game
      ? "等待开始"
      : game.finished
      ? (game.solved ? "已猜中" : game.timed_out ? "已超时" : "本轮结束")
      : `${remaining} 秒`;
    document.getElementById("drawGuessCount").textContent = `猜测 ${game.guess_count || 0} / ${game.max_guesses || 5}`;
    const prompt = document.getElementById("drawPrompt");
    if (game.finished && game.answer) {
      prompt.textContent = sanitizeDisplayText(`答案是“${game.answer}”。${game.solved ? "这轮合作成功。" : "下一轮可以换个画法。"}`);
    } else if (game.answer && room.is_player) {
      prompt.textContent = sanitizeDisplayText(`题目：${game.answer}。请把它画出来，观众和 花火 不会看到答案。`);
    } else if (game.processing) {
      prompt.textContent = "花火 正在看图，只会消耗一次猜测。";
    } else if (!room.game) {
      prompt.textContent = room.is_player ? "开始后，你会在这里看到题目。" : "等待玩家开始新一轮。";
    } else {
      prompt.textContent = room.is_player ? "画出题目后，点击“让 花火 猜”；可以继续补画。" : "玩家正在作画，你可以在对话区和 花火 聊天。";
    }
    drawContext.clearRect(0, 0, drawCanvas.width, drawCanvas.height);
    drawContext.fillStyle = isDarkTheme() ? "#22282d" : "#fffdf8";
    drawContext.fillRect(0, 0, drawCanvas.width, drawCanvas.height);
    drawStrokes.forEach(drawStroke);
    const readonly = busy || !room.is_player || room.status !== "active" || game.processing || game.finished;
    document.getElementById("drawColor").disabled = readonly;
    document.getElementById("drawWidth").disabled = readonly;
    document.getElementById("drawUndo").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    document.getElementById("drawClear").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    document.getElementById("drawGuessAction").disabled = readonly || !drawStrokes.length || drawSyncBusy;
    const overlay = document.getElementById("drawCanvasOverlay");
    overlay.hidden = !(room.status === "waiting" || room.status === "setup") || !room.is_player;
    document.getElementById("drawOverlayTitle").textContent = room.status === "waiting" ? "先加入玩家席" : "准备开始作画";
    document.getElementById("drawOverlayText").textContent = room.status === "waiting" ? "绑定身份后点击加入对局" : "点击右侧开始作画";
    const history = document.getElementById("drawHistory");
    history.replaceChildren();
    const guesses = Array.isArray(game.guesses) ? game.guesses : [];
    if (!guesses.length) {
      const empty = document.createElement("p");
      empty.className = "draw-empty";
      empty.textContent = "花火 的每次猜测会显示在这里";
      history.appendChild(empty);
    } else {
      guesses.forEach((item) => {
        const entry = document.createElement("div");
        entry.className = `draw-guess ${item.correct ? "correct" : "wrong"}`;
        entry.textContent = sanitizeDisplayText(`第 ${item.number} 次：${item.guess}${item.correct ? " · 猜中" : " · 不对"}`);
        history.appendChild(entry);
      });
    }
  }

  function drawStroke(stroke) {
    const points = Array.isArray(stroke?.points) ? stroke.points : [];
    if (!points.length) return;
    drawContext.beginPath();
    drawContext.strokeStyle = stroke.color || "#202522";
    drawContext.lineWidth = Number(stroke.width || 5);
    drawContext.lineCap = "round";
    drawContext.lineJoin = "round";
    points.forEach(([x, y], index) => {
      const px = Number(x) * drawCanvas.width;
      const py = Number(y) * drawCanvas.height;
      if (index === 0) drawContext.moveTo(px, py);
      else drawContext.lineTo(px, py);
    });
    if (points.length === 1) drawContext.lineTo(Number(points[0][0]) * drawCanvas.width + .1, Number(points[0][1]) * drawCanvas.height + .1);
    drawContext.stroke();
  }

  function drawPoint(event) {
    const rect = drawCanvas.getBoundingClientRect();
    return [
      Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
      Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
    ];
  }

  function beginDrawing(event) {
    if (busy || !room?.is_player || room.status !== "active" || room.game?.processing || room.game?.finished) return;
    event.preventDefault();
    drawCanvas.setPointerCapture?.(event.pointerId);
    activeDrawStroke = {
      color: document.getElementById("drawColor").value || "#202522",
      width: Number(document.getElementById("drawWidth").value || 5),
      points: [drawPoint(event)],
    };
    drawStrokes.push(activeDrawStroke);
    drawDirty = true;
    renderDrawGuess();
  }

  function continueDrawing(event) {
    if (!activeDrawStroke) return;
    event.preventDefault();
    activeDrawStroke.points.push(drawPoint(event));
    renderDrawGuess();
  }

  async function finishDrawing(event) {
    if (!activeDrawStroke) return;
    event.preventDefault();
    activeDrawStroke = null;
    drawDirty = true;
    await syncDrawing();
  }

  function syncDrawing() {
    if (!room?.is_player) return Promise.resolve(false);
    if (drawSyncPromise) return drawSyncPromise;
    if (!drawDirty) return Promise.resolve(true);
    drawSyncBusy = true;
    renderDrawGuess();
    drawSyncPromise = (async () => {
      try {
        while (drawDirty) {
          drawDirty = false;
          const strokes = drawStrokes.map((stroke) => ({
            color: stroke.color,
            width: stroke.width,
            points: stroke.points.map((point) => [...point]),
          }));
          const data = await request("POST", "draw/strokes", { visitor_token: visitorToken, strokes });
          setRoom(data.room);
          drawRevision = Number(room.game?.revision ?? drawRevision);
        }
        return true;
      } catch (error) {
        drawDirty = true;
        showToast(error?.message || "画布同步失败");
        return false;
      } finally {
        drawSyncBusy = false;
        drawSyncPromise = null;
        render();
      }
    })();
    return drawSyncPromise;
  }

  async function changeDrawing(nextStrokes) {
    if (busy || !room?.is_player || room.status !== "active" || room.game?.processing || room.game?.finished) return;
    drawStrokes = nextStrokes;
    drawDirty = true;
    await syncDrawing();
  }

  async function guessDrawing() {
    if (busy || !room?.is_player || room.status !== "active" || !drawStrokes.length) return;
    busy = true;
    renderDrawGuess();
    try {
      if (activeDrawStroke) {
        activeDrawStroke = null;
        drawDirty = true;
      }
      if (!(await syncDrawing())) return;
      const format = drawCanvas.toDataURL("image/webp", 0.78);
      const data = await request("POST", "draw/guess", { visitor_token: visitorToken, image_data_url: format });
      setRoom(data.room);
      showToast(data.correct ? "花火 猜中了" : `花火 猜：${data.guess}`);
    } catch (error) {
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "花火 暂时无法看图");
    } finally {
      busy = false;
      render();
    }
  }

  async function diceAction(action) {
    if (busy || room?.game_type !== "pig_dice") return;
    busy = true;
    renderPigDice();
    try {
      const data = await request("POST", "dice/action", { visitor_token: visitorToken, action });
      setRoom(data.room);
      render();
    } catch (error) {
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "骰子操作失败");
    } finally {
      busy = false;
      render();
    }
  }

  async function submitChat(event) {
    event.preventDefault();
    if (chatBusy || !room) return;
    const rawText = chatInput.value;
    const text = rawText.trim();
    if (!text) return;
    chatBusy = true;
    // 发送前立即清空输入框，解决"回车后文字不消失"的问题
    chatInput.value = "";
    chatInput.style.height = "";
    const optimisticId = `pending-${Date.now()}`;
    if (!Array.isArray(room.messages)) room.messages = [];
    room.messages.push({
      id: optimisticId,
      role: "user",
      message_type: "chat",
      content: text,
      sender_name: room.player_confirmed ? (room.visitor_display_name || "已绑定观众") : "匿名观众",
      sender_number: room.visitor_number,
    });
    render();
    try {
      const data = await request("POST", "chat", { visitor_token: visitorToken, text });
      setRoom(data.room);
    } catch (error) {
      // 发送失败：恢复输入框中的原文，方便用户重试
      chatInput.value = rawText;
      chatInput.style.height = "auto";
      chatInput.style.height = `${Math.min(chatInput.scrollHeight, 126)}px`;
      room.messages = room.messages.filter((message) => message.id !== optimisticId);
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "消息发送失败");
    } finally {
      chatBusy = false;
      render();
    }
  }

  function renderTurn() {
    const stone = document.getElementById("turnStone");
    const label = document.getElementById("turnLabel");
    stone.className = "turn-stone";
    stone.textContent = "";
    if (room.game_type === "turtle_soup") {
      stone.classList.add("o");
      stone.textContent = "?";
      const game = room.game;
      label.textContent = game?.preparing
        ? "花火 出题中"
        : game?.processing
          ? "花火 判断中"
          : room.status === "finished"
            ? (game?.mode === "player_host" ? (game?.bot_solved ? "花火 已猜中" : "出题结束") : (game?.solved ? "已经解开" : "汤底揭晓"))
            : room.status === "paused"
              ? "已经暂停"
              : game?.phase === "ready"
                ? `轮到 ${room.current_player_name ? `${room.current_player_name}（${room.current_player_number}号）` : `${room.current_player_number || "?"}号`}${game?.mode === "player_host" ? "给线索" : "提问"}`
                : statusLabel(room.status);
      return;
    }
    if (room.game_type === "draw_guess") {
      stone.classList.add("o");
      stone.textContent = room.game?.solved ? "✓" : "✎";
      label.textContent = room.game?.processing
        ? "花火 看图中"
        : room.status === "finished"
        ? (room.game?.solved ? "合作猜中" : "本轮结束")
        : room.is_player ? "轮到你作画" : "观看玩家作画";
      return;
    }
    if (room.game_type === "pig_dice") {
      stone.classList.add("o");
      stone.textContent = room.game?.last_roll || "?";
      label.textContent = room.game?.finished
        ? (room.game.winner === "human" ? "玩家获胜" : "花火 获胜")
        : room.status === "paused"
          ? "已经暂停"
          : room.game?.turn === "human" ? "玩家回合" : "花火 回合";
      return;
    }
    if (room.game_type === "blackjack") {
      const game = room.game;
      stone.classList.add("o");
      stone.textContent = "♠";
      if (room.status === "finished" || game?.finished) {
        label.textContent = game?.winner ? (game.winner === "player" ? "闲家赢" : "庄家赢") : "本局结束";
      } else if (room.status === "paused") {
        label.textContent = "已暂停";
      } else {
        label.textContent = game?.phase === "dealer_turn"
          ? "庄家补牌"
          : `闲家回合 ${game?.hand_count || 0}/${room.player_numbers?.length || 1}`;
      }
      return;
    }
    if (room.game_type === "undercover") {
      stone.textContent = "·";
      label.textContent = statusLabel(room.status);
      return;
    }
    if (!room.game) {
      label.textContent = statusLabel(room.status);
      return;
    }
    const xiangqi = room.game_type === "xiangqi";
    const tictactoe = room.game_type === "tictactoe";
    const humanSide = xiangqi
      ? room.game.human_side
      : (tictactoe ? room.game.human_mark : room.game.human_color);
    const humanTurn = room.game.turn === humanSide;
    if (pendingMove) {
      label.textContent = "花火 思考中";
    } else {
      label.textContent = room.game.winner
        ? (room.game.winner === humanSide ? "玩家获胜" : "花火 获胜")
        : (room.game.draw ? "平局" : (humanTurn ? "玩家走棋" : "花火 思考中"));
    }
    if (xiangqi) {
      stone.classList.add(room.game.turn === "red" ? "red" : "black");
    } else if (tictactoe) {
      stone.classList.add(room.game.turn === 1 ? "x" : "o");
      stone.textContent = room.game.turn === 1 ? "X" : "O";
    } else {
      stone.classList.add(room.game.turn === 1 ? "black" : "white");
    }
  }

  function drawBoard() {
    if (["turtle_soup", "pig_dice", "draw_guess"].includes(room?.game_type)) return;
    if (room?.game_type === "xiangqi") drawXiangqi();
    else if (room?.game_type === "tictactoe") drawTicTacToe();
    else drawGomoku();
  }

  function drawTicTacToe() {
    const palette = getGamePalette("tictactoe");
    const size = board.width;
    const inset = 58;
    const playSize = size - inset * 2;
    const cell = playSize / 3;
    context.clearRect(0, 0, size, size);
    context.fillStyle = palette.bg;
    context.fillRect(0, 0, size, size);
    context.strokeStyle = palette.line;
    context.lineWidth = 8;
    context.lineCap = "round";
    for (let index = 1; index < 3; index += 1) {
      const position = inset + index * cell;
      context.beginPath();
      context.moveTo(position, inset);
      context.lineTo(position, size - inset);
      context.stroke();
      context.beginPath();
      context.moveTo(inset, position);
      context.lineTo(size - inset, position);
      context.stroke();
    }
    const cells = (room?.game?.board || []).map((row) => row.slice());
    if (
      pendingMove?.kind === "tictactoe"
      && !cells?.[pendingMove.row]?.[pendingMove.column]
    ) {
      cells[pendingMove.row][pendingMove.column] = pendingMove.mark;
    }
    const lastMove = pendingMove?.kind === "tictactoe"
      ? [pendingMove.row, pendingMove.column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      context.fillStyle = palette.lastMoveHint;
      context.fillRect(
        inset + lastMove[1] * cell + 10,
        inset + lastMove[0] * cell + 10,
        cell - 20,
        cell - 20,
      );
    }
    cells.forEach((row, rowIndex) => row.forEach((mark, columnIndex) => {
      if (mark) drawTicTacToeMark(rowIndex, columnIndex, mark, inset, cell);
    }));
  }

  function drawTicTacToeMark(row, column, mark, inset, cell) {
    const palette = getGamePalette("tictactoe");
    const centerX = inset + (column + 0.5) * cell;
    const centerY = inset + (row + 0.5) * cell;
    const radius = cell * 0.27;
    context.lineWidth = 15;
    context.lineCap = "round";
    if (mark === 1) {
      context.strokeStyle = palette.x;
      context.beginPath();
      context.moveTo(centerX - radius, centerY - radius);
      context.lineTo(centerX + radius, centerY + radius);
      context.moveTo(centerX + radius, centerY - radius);
      context.lineTo(centerX - radius, centerY + radius);
      context.stroke();
      return;
    }
    context.strokeStyle = palette.o;
    context.beginPath();
    context.arc(centerX, centerY, radius, 0, Math.PI * 2);
    context.stroke();
  }

  function drawGomoku() {
    const palette = getGamePalette("gomoku");
    const size = board.width;
    const margin = 48;
    const gap = (size - margin * 2) / 14;
    context.clearRect(0, 0, size, size);
    context.fillStyle = palette.board;
    context.fillRect(0, 0, size, size);
    context.strokeStyle = palette.line;
    context.lineWidth = 1.6;
    for (let index = 0; index < 15; index += 1) {
      const point = margin + index * gap;
      context.beginPath(); context.moveTo(margin, point); context.lineTo(size - margin, point); context.stroke();
      context.beginPath(); context.moveTo(point, margin); context.lineTo(point, size - margin); context.stroke();
    }
    context.fillStyle = palette.star;
    [[3, 3], [3, 11], [7, 7], [11, 3], [11, 11]].forEach(([row, column]) => {
      context.beginPath();
      context.arc(margin + column * gap, margin + row * gap, 4, 0, Math.PI * 2);
      context.fill();
    });
    const cells = room?.game?.board || [];
    cells.forEach((row, rowIndex) => row.forEach((color, columnIndex) => {
      if (color) drawGomokuStone(rowIndex, columnIndex, color, margin, gap);
    }));
    if (pendingMove?.kind === "gomoku" && !room?.game?.board?.[pendingMove.row]?.[pendingMove.column]) {
      drawGomokuStone(pendingMove.row, pendingMove.column, pendingMove.color, margin, gap);
    }
    const lastMove = pendingMove?.kind === "gomoku"
      ? [pendingMove.row, pendingMove.column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      context.beginPath();
      context.arc(margin + lastMove[1] * gap, margin + lastMove[0] * gap, 5, 0, Math.PI * 2);
      context.fillStyle = palette.lastMoveDot;
      context.fill();
    }
  }

  function drawGomokuStone(row, column, color, margin, gap) {
    const palette = getGamePalette("gomoku");
    const isBlack = color === 1;
    context.beginPath();
    context.arc(margin + column * gap, margin + row * gap, gap * 0.41, 0, Math.PI * 2);
    context.fillStyle = isBlack ? palette.blackStone : palette.whiteStone;
    context.fill();
    context.strokeStyle = isBlack ? palette.blackStone : palette.whiteEdge;
    context.lineWidth = 1.5;
    context.stroke();
  }

  function xiangqiFlipped() {
    return room?.game?.human_side === "black";
  }

  function displayPoint(row, column) {
    return xiangqiFlipped() ? [9 - row, 8 - column] : [row, column];
  }

  function modelPoint(displayRow, displayColumn) {
    return xiangqiFlipped() ? [9 - displayRow, 8 - displayColumn] : [displayRow, displayColumn];
  }

  function drawXiangqi() {
    const palette = getGamePalette("xiangqi");
    const width = board.width;
    const height = board.height;
    const marginX = 54;
    const marginY = 48;
    const gapX = (width - marginX * 2) / 8;
    const gapY = (height - marginY * 2) / 9;
    context.clearRect(0, 0, width, height);
    context.fillStyle = palette.board;
    context.fillRect(0, 0, width, height);
    context.strokeStyle = palette.line;
    context.lineWidth = 1.7;
    for (let row = 0; row < 10; row += 1) {
      const y = marginY + row * gapY;
      context.beginPath(); context.moveTo(marginX, y); context.lineTo(width - marginX, y); context.stroke();
    }
    for (let column = 0; column < 9; column += 1) {
      const x = marginX + column * gapX;
      context.beginPath();
      context.moveTo(x, marginY);
      if (column === 0 || column === 8) {
        context.lineTo(x, height - marginY);
      } else {
        context.lineTo(x, marginY + 4 * gapY);
        context.moveTo(x, marginY + 5 * gapY);
        context.lineTo(x, height - marginY);
      }
      context.stroke();
    }
    [[0, 3, 2, 5], [0, 5, 2, 3], [7, 3, 9, 5], [7, 5, 9, 3]].forEach(([r1, c1, r2, c2]) => {
      const first = displayPoint(r1, c1);
      const second = displayPoint(r2, c2);
      context.beginPath();
      context.moveTo(marginX + first[1] * gapX, marginY + first[0] * gapY);
      context.lineTo(marginX + second[1] * gapX, marginY + second[0] * gapY);
      context.stroke();
    });
    context.save();
    context.fillStyle = palette.river;
    context.font = '600 29px "Noto Serif SC", "Songti SC", serif';
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(xiangqiFlipped() ? "漢界" : "楚河", width * 0.29, height / 2);
    context.fillText(xiangqiFlipped() ? "楚河" : "漢界", width * 0.71, height / 2);
    context.restore();

    const legal = Array.isArray(room?.game?.legal_moves) ? room.game.legal_moves : [];
    if (selectedPiece) {
      legal.filter((move) => move[0] === selectedPiece[0] && move[1] === selectedPiece[1]).forEach((move) => {
        const [row, column] = displayPoint(move[2], move[3]);
        context.beginPath();
        context.arc(marginX + column * gapX, marginY + row * gapY, 10, 0, Math.PI * 2);
        context.fillStyle = palette.legalHint;
        context.fill();
      });
    }

    const cells = (room?.game?.board || []).map((row) => row.slice());
    if (pendingMove?.kind === "xiangqi") {
      cells[pendingMove.to_row][pendingMove.to_column] = cells[pendingMove.from_row][pendingMove.from_column];
      cells[pendingMove.from_row][pendingMove.from_column] = ".";
    }
    cells.forEach((row, modelRow) => row.forEach((piece, modelColumn) => {
      if (piece !== ".") drawXiangqiPiece(modelRow, modelColumn, piece, marginX, marginY, gapX, gapY);
    }));
    const lastMove = pendingMove?.kind === "xiangqi"
      ? [pendingMove.from_row, pendingMove.from_column, pendingMove.to_row, pendingMove.to_column]
      : room?.game?.last_move;
    if (Array.isArray(lastMove)) {
      [[lastMove[0], lastMove[1]], [lastMove[2], lastMove[3]]].forEach(([modelRow, modelColumn]) => {
        const [row, column] = displayPoint(modelRow, modelColumn);
        context.strokeStyle = palette.lastBox;
        context.lineWidth = 3;
        context.strokeRect(
          marginX + column * gapX - gapX * 0.31,
          marginY + row * gapY - gapY * 0.31,
          gapX * 0.62,
          gapY * 0.62,
        );
      });
    }
  }

  function drawXiangqiPiece(modelRow, modelColumn, piece, marginX, marginY, gapX, gapY) {
    const palette = getGamePalette("xiangqi");
    const [row, column] = displayPoint(modelRow, modelColumn);
    const x = marginX + column * gapX;
    const y = marginY + row * gapY;
    const isRed = piece === piece.toUpperCase();
    const labels = {
      K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵",
      k: "将", a: "士", b: "象", n: "马", r: "车", c: "炮", p: "卒",
    };
    const textColor = isRed ? palette.red : palette.blackText;
    context.beginPath();
    context.arc(x, y, Math.min(gapX, gapY) * 0.39, 0, Math.PI * 2);
    context.fillStyle = palette.pieceBg;
    context.fill();
    context.strokeStyle = palette.pieceRing;
    context.lineWidth = 2.5;
    context.stroke();
    context.fillStyle = textColor;
    context.font = `700 ${Math.floor(Math.min(gapX, gapY) * 0.43)}px "Noto Serif SC", "Songti SC", serif`;
    context.textAlign = "center";
    context.textBaseline = "middle";
    context.fillText(labels[piece] || piece, x, y + 1);
    if (selectedPiece?.[0] === modelRow && selectedPiece?.[1] === modelColumn) {
      context.strokeStyle = isDarkTheme() ? "#55c096" : "#176347";
      context.lineWidth = 4;
      context.stroke();
    }
  }

  async function seatAction() {
    if (busy || !room) return;
    busy = true;
    renderSeat();
    try {
      if (!room.is_player) {
        await request("POST", "claim", { visitor_token: visitorToken, side: selectedSide });
      } else if (room.status === "setup") {
        await request("POST", "start", { visitor_token: visitorToken, side: selectedSide });
      } else if (room.status === "finished") {
        await request("POST", "rematch", { visitor_token: visitorToken });
      }
      await loadState();
    } catch (error) {
      showToast(error?.message || "操作失败");
    } finally {
      busy = false;
      renderSeat();
    }
  }

  async function moveAt(event) {
    if (busy || !room?.is_player || room.status !== "active" || !room.game) return;
    if (room.game_type === "pig_dice") return;
    if (room.game_type === "xiangqi") await moveXiangqi(event);
    else if (room.game_type === "tictactoe") await moveTicTacToe(event);
    else await moveGomoku(event);
  }

  async function moveTicTacToe(event) {
    if (room.game.turn !== room.game.human_mark) return;
    const rect = board.getBoundingClientRect();
    const scaleX = board.width / rect.width;
    const scaleY = board.height / rect.height;
    const x = (event.clientX - rect.left) * scaleX;
    const y = (event.clientY - rect.top) * scaleY;
    const inset = 58;
    const cell = (board.width - inset * 2) / 3;
    const column = Math.floor((x - inset) / cell);
    const row = Math.floor((y - inset) / cell);
    if (row < 0 || row > 2 || column < 0 || column > 2) return;
    if (room.game.board?.[row]?.[column]) {
      showToast("这个位置已经有棋子了");
      return;
    }
    busy = true;
    pendingMove = { kind: "tictactoe", row, column, mark: room.game.human_mark };
    render();
    try {
      const data = await request("POST", "move", { visitor_token: visitorToken, row, column });
      pendingMove = null;
      setRoom(data.room);
      render();
    } catch (error) {
      pendingMove = null;
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "无法落子");
    } finally {
      busy = false;
      render();
    }
  }

  async function moveGomoku(event) {
    if (room.game.turn !== room.game.human_color) return;
    const rect = board.getBoundingClientRect();
    const scale = board.width / rect.width;
    const x = (event.clientX - rect.left) * scale;
    const y = (event.clientY - rect.top) * scale;
    const margin = 48;
    const gap = (board.width - margin * 2) / 14;
    const column = Math.round((x - margin) / gap);
    const row = Math.round((y - margin) / gap);
    if (row < 0 || row > 14 || column < 0 || column > 14) return;
    if (room.game.board?.[row]?.[column]) {
      showToast("这个位置已经有棋子了");
      return;
    }
    busy = true;
    pendingMove = { kind: "gomoku", row, column, color: room.game.human_color };
    render();
    try {
      const data = await request("POST", "move", { visitor_token: visitorToken, row, column });
      pendingMove = null;
      setRoom(data.room);
      render();
    } catch (error) {
      pendingMove = null;
      try { await loadState(); } catch (_syncError) { /* polling will retry */ }
      showToast(error?.message || "无法落子");
    } finally {
      busy = false;
      render();
    }
  }

  async function moveXiangqi(event) {
    if (room.game.turn !== room.game.human_side) return;
    const rect = board.getBoundingClientRect();
    const scaleX = board.width / rect.width;
    const scaleY = board.height / rect.height;
    const x = (event.clientX - rect.left) * scaleX;
    const y = (event.clientY - rect.top) * scaleY;
    const marginX = 54;
    const marginY = 48;
    const gapX = (board.width - marginX * 2) / 8;
    const gapY = (board.height - marginY * 2) / 9;
    const displayColumn = Math.round((x - marginX) / gapX);
    const displayRow = Math.round((y - marginY) / gapY);
    if (displayRow < 0 || displayRow > 9 || displayColumn < 0 || displayColumn > 8) return;
    const [row, column] = modelPoint(displayRow, displayColumn);
    const legal = Array.isArray(room.game.legal_moves) ? room.game.legal_moves : [];
    if (selectedPiece) {
      const move = legal.find((item) => (
        item[0] === selectedPiece[0] && item[1] === selectedPiece[1]
        && item[2] === row && item[3] === column
      ));
      if (move) {
        busy = true;
        pendingMove = {
          kind: "xiangqi", from_row: move[0], from_column: move[1], to_row: move[2], to_column: move[3],
        };
        selectedPiece = null;
        render();
        try {
          const data = await request("POST", "move", { visitor_token: visitorToken, ...pendingMove });
          pendingMove = null;
          setRoom(data.room);
          render();
        } catch (error) {
          pendingMove = null;
          try { await loadState(); } catch (_syncError) { /* polling will retry */ }
          showToast(error?.message || "无法走棋");
        } finally {
          busy = false;
          render();
        }
        return;
      }
    }
    const selectable = legal.some((item) => item[0] === row && item[1] === column);
    selectedPiece = selectable ? [row, column] : null;
    if (!selectable && room.game.board?.[row]?.[column] !== ".") showToast("当前不能移动这枚棋子");
    drawBoard();
  }

  document.querySelectorAll("[data-side]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedSide = button.dataset.side;
      document.querySelectorAll("[data-side]").forEach((item) => {
        item.classList.toggle("is-active", item === button);
      });
    });
  });
  document.querySelectorAll("[data-room-view]").forEach((button) => {
    button.addEventListener("click", () => setRoomView(button.dataset.roomView));
  });
  document.getElementById("seatAction").addEventListener("click", seatAction);
  document.getElementById("rememberIdentity").addEventListener("change", (event) => {
    window.localStorage.setItem(rememberIdentityKey, event.target.checked ? "1" : "0");
  });
  document.getElementById("forgetIdentity").addEventListener("click", forgetTrustedIdentity);
  document.getElementById("chatComposer").addEventListener("submit", submitChat);
  chatInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      document.getElementById("chatComposer").requestSubmit();
    }
  });
  chatInput.addEventListener("input", () => {
    chatInput.style.height = "auto";
    chatInput.style.height = `${Math.min(chatInput.scrollHeight, 126)}px`;
  });
  document.getElementById("diceRollAction").addEventListener("click", () => diceAction("roll"));
  document.getElementById("diceHoldAction").addEventListener("click", () => diceAction("hold"));
  document.getElementById("blackjackHitAction").addEventListener("click", () => blackjackAction("hit"));
  document.getElementById("blackjackStandAction").addEventListener("click", () => blackjackAction("stand"));
  drawCanvas.addEventListener("pointerdown", beginDrawing);
  drawCanvas.addEventListener("pointermove", continueDrawing);
  drawCanvas.addEventListener("pointerup", finishDrawing);
  drawCanvas.addEventListener("pointercancel", finishDrawing);
  document.getElementById("drawUndo").addEventListener("click", () => changeDrawing(drawStrokes.slice(0, -1)));
  document.getElementById("drawClear").addEventListener("click", () => changeDrawing([]));
  document.getElementById("drawGuessAction").addEventListener("click", guessDrawing);
  // 绑定码一键复制
  const copyBindCommand = document.getElementById("copyBindCommand");
  if (copyBindCommand) {
    copyBindCommand.addEventListener("click", async () => {
      const token = (room?.identity_token || "").trim();
      if (!token) {
        showToast("当前没有可复制的绑定码");
        return;
      }
      const text = `/绑定玩家 ${token}`;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        showToast("已复制：" + text);
      } catch (err) {
        showToast("复制失败，请手动选择复制。");
      }
    });
  }
  // 防窥屏绑定码一键复制
  const copyPeekBind = document.getElementById("copyPeekBind");
  if (copyPeekBind) {
    copyPeekBind.addEventListener("click", async () => {
      const token = (room?.identity_token || "").trim();
      if (!token) {
        showToast("当前没有可复制的绑定码");
        return;
      }
      const text = `/绑定玩家 ${token}`;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          document.execCommand("copy");
          document.body.removeChild(ta);
        }
        showToast("已复制：" + text);
      } catch (err) {
        showToast("复制失败，请手动选择复制。");
      }
    });
  }
  const peekRememberInput = document.getElementById("peekRemember");
  if (peekRememberInput) {
    peekRememberInput.addEventListener("change", (event) => {
      window.localStorage.setItem(rememberIdentityKey, event.target.checked ? "1" : "0");
    });
  }
  // 谁是卧底发言字数计数
  const ucSpeechInput = document.getElementById("ucSpeechInput");
  if (ucSpeechInput) {
    ucSpeechInput.addEventListener("input", () => {
      const counter = document.getElementById("ucSpeechCount");
      if (counter) counter.textContent = `${ucSpeechInput.value.length}/500`;
      const btn = document.getElementById("ucSpeechSubmit");
      if (btn) {
        const my = (room?.game?.my) || {};
        const expected = room?.game?.expected_speaker_number;
        const isMyTurn = my.is_player && my.player_number && expected && Number(my.player_number) === Number(expected);
        const canSpeak = room?.game && ["speech", "pk"].includes(room.game.phase) && isMyTurn;
        btn.disabled = !canSpeak || ucSpeechInput.value.trim().length < 1;
      }
    });
  }
  // 提交发言
  const ucSpeechSubmit = document.getElementById("ucSpeechSubmit");
  if (ucSpeechSubmit) {
    ucSpeechSubmit.addEventListener("click", async () => {
      if (!ucSpeechInput || !visitorToken || !accessToken) return;
      const content = ucSpeechInput.value.trim();
      if (!content) return;
      ucSpeechSubmit.disabled = true;
      try {
        const res = await request(
          "POST",
          "undercover/speech",
          { visitor_token: visitorToken, content }
        );
        if (res?.room) {
          setRoom(res.room);
          ucSpeechInput.value = "";
          const counter = document.getElementById("ucSpeechCount");
          if (counter) counter.textContent = "0/500";
          if (res?.notice) showToast(res.notice, 3200);
          render();
        } else if (res?.error) {
          showToast(res.error);
        }
      } catch (error) {
        // 词条拦截 / 相似度拦截等：提示玩家原因，并刷新到当前状态
        showToast(error?.message || "发言提交失败，请换个说法", 3200);
        try { await loadState(); render(); } catch (_syncError) { /* 轮询会重试 */ }
      } finally {
        ucSpeechSubmit.disabled = false;
      }
    });
  }
  // 发言框：仅在自己发言回合按回车提交（Shift+回车换行），其余情况保持默认
  const ucSpeechInputEl = document.getElementById("ucSpeechInput");
  if (ucSpeechInputEl) {
    ucSpeechInputEl.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        const submit = document.getElementById("ucSpeechSubmit");
        const my = (room?.game?.my) || {};
        const expected = room?.game?.expected_speaker_number;
        const isMyTurn = my.is_player && my.player_number && expected && Number(my.player_number) === Number(expected);
        const canSpeak = room?.game && ["speech", "pk"].includes(room.game.phase) && isMyTurn;
        if (canSpeak && submit && !submit.disabled) submit.click();
      }
    });
  }
  // 身份卡关闭
  const ucRevealClose = document.getElementById("ucRevealClose");
  if (ucRevealClose) {
    ucRevealClose.addEventListener("click", () => {
      const overlay = document.getElementById("ucRevealOverlay");
      if (overlay) overlay.hidden = true;
      window.clearTimeout(ucPreheatTimer);
      window.clearInterval(ucPreheatCounter);
      const preheat = document.getElementById("ucPreheat");
      if (preheat) preheat.hidden = true;
      const card = document.getElementById("ucRevealCard");
      if (card) card.hidden = false;
      // 发放身份卡收起后，让「本局身份卡」播放一次飞入动画（信息自然过渡到右侧面板）
      const idCard = document.getElementById("ucIdentityCard");
      if (idCard) {
        idCard.classList.remove("uc-identity-reveal");
        void idCard.offsetWidth; // 强制重排以重放动画
        idCard.classList.add("uc-identity-reveal");
      }
    });
  }
  // 结算卡关闭
  const ucResultClose = document.getElementById("ucResultClose");
  if (ucResultClose) {
    ucResultClose.addEventListener("click", () => {
      const overlay = document.getElementById("ucResultOverlay");
      if (overlay) overlay.hidden = true;
    });
  }
  // 分享战报：把本局结果画成 PNG 卡片并保存/分享
  const ucResultShare = document.getElementById("ucResultShare");
  if (ucResultShare) {
    ucResultShare.addEventListener("click", async () => {
      const btn = ucResultShare;
      btn.disabled = true;
      try {
        const url = await undercoverSharePosterUrl(ucLastResult);
        if (!url) throw new Error("empty");
        savePosterImage(url, `谁是卧底战报_${new Date().toISOString().slice(0, 10)}.png`);
      } catch (error) {
        showToast("生成战报失败：请稍后重试");
      } finally {
        btn.disabled = false;
      }
    });
  }

  // 保存分享图：
  // - 移动端（微信/TIM 内嵌 WebView）：优先系统分享面板，方便存相册/转发；退化为下载、再退化为新标签长按保存。
  // - PC：直接下载图片，并尽量把图片复制进剪贴板，玩家 Ctrl+V 即可粘贴到聊天窗口。
  function savePosterImage(dataUrl, filename) {
    if (isMobileUA() && typeof navigator !== "undefined" && navigator.canShare && navigator.share) {
      try {
        const blob = dataUrlToBlob(dataUrl);
        const file = new File([blob], filename, { type: "image/png" });
        if (navigator.canShare({ files: [file] })) {
          navigator.share({ files: [file], title: "谁是卧底战报" })
            .catch(() => fallbackSavePoster(dataUrl, filename));
          return;
        }
      } catch (_shareError) { /* 走兜底 */ }
    }
    if (!isMobileUA()) {
      // PC：下载 + 复制剪贴板
      downloadPosterFile(dataUrl, filename);
      copyPosterToClipboard(dataUrl).then((copied) => {
        showToast(copied ? "已下载并复制图片，Ctrl+V 即可粘贴" : "已下载战报图片");
      });
      return;
    }
    fallbackSavePoster(dataUrl, filename);
  }
  function fallbackSavePoster(dataUrl, filename) {
    try {
      downloadPosterFile(dataUrl, filename);
    } catch (_dlError) {
      // 极少数 WebView 不支持 a[download]：新标签打开，用户可长按保存
      window.open(dataUrl, "_blank");
    }
  }
  function downloadPosterFile(dataUrl, filename) {
    const a = document.createElement("a");
    a.href = dataUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }
  // 把 PNG 写入剪贴板（需安全上下文 + Chromium ClipboardItem）；失败静默返回 false
  function copyPosterToClipboard(dataUrl) {
    try {
      const blob = dataUrlToBlob(dataUrl);
      if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
        return navigator.clipboard
          .write([new window.ClipboardItem({ "image/png": blob })])
          .then(() => true)
          .catch(() => false);
      }
    } catch (_clipError) { /* 不支持则跳过 */ }
    return Promise.resolve(false);
  }
  function isMobileUA() {
    return /Android|iPhone|iPad|iPod|Mobile|Windows Phone/i.test(
      (typeof navigator !== "undefined" && navigator.userAgent) || ""
    );
  }
  function dataUrlToBlob(dataUrl) {
    const parts = String(dataUrl || "").split(",");
    const meta = (parts[0] || "").match(/data:(.*?)(;|$)/);
    const mime = (meta && meta[1]) || "image/png";
    const bin = window.atob(parts[1] || "");
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }
  // 花火复盘：同局缓存，点击后展示，不重复请求 LLM
  const ucResultRecapBtn = document.getElementById("ucResultRecapBtn");
  if (ucResultRecapBtn) {
    ucResultRecapBtn.addEventListener("click", async () => {
      const btn = ucResultRecapBtn;
      const recapBox = document.getElementById("ucResultRecap");
      const uid = ucShownResultUid || "";
      if (uid && ucRecapCache[uid]) {
        if (recapBox) { recapBox.textContent = ucRecapCache[uid]; recapBox.hidden = false; }
        return;
      }
      btn.disabled = true;
      btn.textContent = "复盘生成中…";
      try {
        const data = await request("POST", "undercover/recap", {});
        const text = String(data?.recap || "").trim() || "暂未生成复盘，稍后再试试。";
        if (uid) ucRecapCache[uid] = text;
        if (recapBox) { recapBox.textContent = text; recapBox.hidden = false; }
      } catch (error) {
        showToast(error?.message || "复盘生成失败");
      } finally {
        btn.disabled = false;
        btn.textContent = "✨ 花火复盘";
      }
    });
  }
  // 通知到群：本局一次性（后端按 game_uid 幂等），携带战报海报图按插件配置发送
  const ucResultAnnounce = document.getElementById("ucResultAnnounce");
  if (ucResultAnnounce) {
    ucResultAnnounce.addEventListener("click", async () => {
      const btn = ucResultAnnounce;
      btn.disabled = true;
      btn.textContent = "通知中…";
      let image = "";
      try {
        // 通知用「战况大字报」风格图（与分享战报完全不同的设计），随通知发给群
        image = await undercoverAnnouncePosterUrl(ucLastResult) || "";
      } catch (_imgError) { image = ""; }
      try {
        const data = await request("POST", "undercover/announce", { image });
        if (data?.announced) {
          btn.textContent = "✅ 已通知（本局一次）";
          showToast(data?.already ? "本局已通知过，未重复发送" : "已通知到群");
        } else {
          btn.textContent = "📢 通知到群";
          showToast("群通知未开启：请在插件配置开启 undercover.group_announce_enabled");
          btn.disabled = false;
        }
      } catch (error) {
        showToast(error?.message || "通知失败");
        btn.textContent = "📢 通知到群";
        btn.disabled = false;
      }
    });
  }

  // 单局「谁是卧底战报」PNG 卡片（纯前端 Canvas 绘制；2x 高清，iOS 相册里也清晰）
  async function undercoverSharePosterUrl(result) {
    if (!result) return "";
    const scale = 2;
    const W = 640, M = 24, rowH = 54, padTop = 252, headerH = 44, footerH = 72;
    const players = result.players || [];
    const H = padTop + headerH + players.length * rowH + footerH;
    // 预载诸位玩家的 QQ 头像：CORS 允许则绘制；否则回退到首字圆标（避免 canvas 被污染）
    const avatars = await loadPosterAvatars(
      players.filter((p) => p.avatar_url).map((p) => ({ number: p.player_number, url: p.avatar_url }))
    );
    const cv = document.createElement("canvas");
    cv.width = W * scale; cv.height = H * scale;
    const c = cv.getContext("2d");
    c.scale(scale, scale); // 画布放大，全部按逻辑坐标绘制，输出为 2x 高清图
    const font = (bold, px) => `${bold ? "bold " : ""}${px}px 'PingFang SC','Microsoft YaHei',sans-serif`;
    const rr = (x, y, w, h, r) => {
      c.beginPath(); c.moveTo(x + r, y);
      c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
      c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
    };
    const campCol = { civilian: "#34d399", undercover: "#c084fc", whiteboard: "#60a5fa" }[result.camp] || "#fbbf24";
    const campCn = { civilian: "平民", undercover: "卧底", whiteboard: "白板" }[result.camp] || result.title || "本局";

    // 背景（深蓝渐变 + 顶部辉光 + 右下装饰圆 + 暗角，避免大片纯色显得呆板）
    const g = c.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, "#17344f"); g.addColorStop(1, "#101d2a");
    c.fillStyle = g; c.fillRect(0, 0, W, H);
    const glow = c.createRadialGradient(W / 2, 40, 10, W / 2, 40, 420);
    glow.addColorStop(0, "rgba(255,255,255,.10)"); glow.addColorStop(1, "rgba(255,255,255,0)");
    c.fillStyle = glow; c.fillRect(0, 0, W, H);
    c.save();
    c.globalAlpha = 0.06;
    c.strokeStyle = campCol; c.lineWidth = 2;
    c.beginPath(); c.arc(W + 60, H - 40, 150, 0, Math.PI * 2); c.stroke();
    c.beginPath(); c.arc(W + 90, H - 10, 90, 0, Math.PI * 2); c.stroke();
    c.restore();
    const vig = c.createRadialGradient(W / 2, H / 2, H * 0.42, W / 2, H / 2, H * 0.78);
    vig.addColorStop(0, "rgba(0,0,0,0)"); vig.addColorStop(1, "rgba(0,0,0,.34)");
    c.fillStyle = vig; c.fillRect(0, 0, W, H);

    // 顶部横幅
    const hg = c.createLinearGradient(0, 0, W, padTop);
    hg.addColorStop(0, "#1d4f86"); hg.addColorStop(1, "#14355a");
    c.fillStyle = hg; c.fillRect(0, 0, W, padTop);
    c.fillStyle = campCol; c.fillRect(0, padTop - 4, W, 4); // 主题色强调线
    c.save();
    c.globalAlpha = 0.08; c.font = font(true, 120); c.textAlign = "right";
    c.fillStyle = "#ffffff"; c.fillText(result.icon || "🕵️", W - 26, padTop - 28);
    c.restore();
    c.textAlign = "left";
    c.font = font(false, 20); c.fillStyle = "#9fc4ec"; c.fillText("🕵️ 谁是卧底 · 单局战报", M, 42);
    // 以“我”的视角生成一句有趣文案（随机多款）
    const headlineLines = wrapPosterLines(c, ucShareHeadline(result), W - 2 * M, font(true, 21), 2);
    headlineLines.forEach((line, li) => {
      c.fillStyle = "#ffffff"; c.font = font(true, 21);
      c.fillText(line, M, 86 + li * 30);
    });
    c.fillStyle = "#ffd9a0"; c.font = font(true, 16);
    c.fillText(`本局结果：${campCn} 获胜`, M, 156);
    c.font = font(false, 13); c.fillStyle = "#9fc4ec";
    c.fillText(truncatePosterText(c, result.message || result.title || "", W - 2 * M), M, 180);
    // 词条药丸
    const pill = (text, x, y, w, col, fg) => {
      rr(x, y, w, 30, 15); c.fillStyle = col; c.fill();
      c.font = font(true, 14); c.fillStyle = fg; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(text, x + w / 2, y + 15);
      c.textBaseline = "alphabetic";
    };
    if (result.civilian_word && result.undercover_word) {
      const cw = result.civilian_word, uw = result.undercover_word;
      const cwt = truncatePosterText(c, `🛡️ ${cw}`, 230);
      pill(cwt, M, padTop - 40, c.measureText(cwt).width + 28, "rgba(52,211,153,.18)", "#8ef0c2");
      const uwt = truncatePosterText(c, `🕵️ ${uw}`, 230);
      pill(uwt, M + 300, padTop - 40, c.measureText(uwt).width + 28, "rgba(192,132,252,.20)", "#e6d5ff");
    }

    // 表头（玩家列带头像空格）
    const yHeader = padTop;
    c.fillStyle = "rgba(255,255,255,.07)"; c.fillRect(0, yHeader, W, headerH);
    c.fillStyle = "#8fa8c4"; c.font = font(true, 14); c.textAlign = "left";
    ["玩家", "身份", "状态"].forEach((t, i) => {
      c.fillText(t, forShareCol(i + 1), yHeader + 27);
    });
    // 数据行（从表头下方开始，避免与表头文字重叠）
    players.forEach((p, i) => {
      const y = yHeader + headerH + i * rowH;
      if (i % 2 === 0) { c.fillStyle = "rgba(255,255,255,.028)"; c.fillRect(0, y, W, rowH); }
      // 头像圆标：优先 QQ 头像，回退首字；本人生成高亮金边
      const ax = forShareCol(0) + 19, ay = y + 27, r = 19;
      const img = avatars.get(p.player_number);
      if (img) {
        c.save();
        c.beginPath(); c.arc(ax, ay, r, 0, Math.PI * 2); c.clip();
        c.drawImage(img, ax - r, ay - r, r * 2, r * 2);
        c.restore();
        c.beginPath(); c.arc(ax, ay, r, 0, Math.PI * 2);
        c.strokeStyle = p.is_me ? "#ffd166" : "rgba(255,255,255,.32)"; c.lineWidth = p.is_me ? 2.5 : 1.5; c.stroke();
      } else {
        c.fillStyle = p.is_me ? "#f0c050" : i < 3 ? "#f0c050" : "rgba(255,255,255,.14)";
        c.beginPath(); c.arc(ax, ay, r, 0, Math.PI * 2); c.fill();
        c.fillStyle = "#14304a"; c.textAlign = "center"; c.fillStyle = p.is_me ? "#1c1c1c" : "#e8eef5";
        c.font = font(true, 16);
        const fc = (p.display_name || `${p.player_number}号`).trim().charAt(0) || "?";
        c.fillText(fc, ax, ay + 6);
      }
      // 名字（随头像列缩进）
      const nm = `${p.player_number}号${p.display_name ? " " + p.display_name : ""}`;
      c.fillStyle = "#e8eef5"; c.font = font(false, 15); c.textAlign = "left";
      c.fillText(truncatePosterText(c, nm, 170), forShareCol(1), y + 33);
      // 身份
      const campT = p.camp === "civilian" ? "平民" : p.camp === "undercover" ? "卧底" : p.camp === "whiteboard" ? "白板" : (p.camp || "—");
      c.fillStyle = { civilian: "#8ef0c2", undercover: "#e6d5ff", whiteboard: "#b9dcff" }[p.camp] || "#8fa8c4";
      c.font = font(true, 15); c.fillText(campT, forShareCol(2), y + 33);
      // 状态
      c.fillStyle = p.is_out ? "#7d8fa3" : "#ffd166";
      c.font = font(false, 14);
      c.fillText(p.is_out ? "✖ 已出局" : "● 存活", forShareCol(3), y + 33);
    });
    // 底部：分隔线 + 左侧标语 + 右下角水印
    c.fillStyle = "rgba(255,255,255,.08)"; c.fillRect(M, H - 42, W - 2 * M, 1);
    c.textAlign = "left";
    c.font = font(false, 13); c.fillStyle = "rgba(160,180,205,.75)";
    c.fillText("🕵️ 谁是卧底 · 花火陪你玩", M, H - 22);
    c.textAlign = "right";
    c.font = font(false, 14); c.fillStyle = "rgba(140,160,185,.65)";
    c.fillText("由 花火 监督生成", W - M, H - 22);
    return cv.toDataURL("image/png");
  }

  // 单局「战况大字报」PNG（通知到群专用）：与分享战报完全独立的设计——
  // 横幅式横版 + 超大获胜阵营字 + 醒目药丸词条，适合群里一眼看清战况
  async function undercoverAnnouncePosterUrl(result) {
    if (!result) return "";
    const scale = 1.5; // 720 -> 1080 宽，群里也足够清晰，同时控制 base64 体积
    const W = 720, H = 660, M = 46;
    const players = result.players || [];
    const camp = result.camp;
    const campCn = { civilian: "平民", undercover: "卧底", whiteboard: "白板" }[camp] || "";
    const campCol = { civilian: "#7dffb0", undercover: "#dda9ff", whiteboard: "#9ccbff" }[camp] || "#ffd166";
    const campName = { civilian: "平民", undercover: "卧底", whiteboard: "白板" };
    const cv = document.createElement("canvas");
    cv.width = W * scale; cv.height = H * scale;
    const c = cv.getContext("2d");
    c.scale(scale, scale); // 画布放大，全部按逻辑坐标绘制
    const font = (bold, px) => `${bold ? "bold " : ""}${px}px 'PingFang SC','Microsoft YaHei',sans-serif`;
    const rr = (x, y, w, h, r) => {
      c.beginPath(); c.moveTo(x + r, y);
      c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
      c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
    };

    // 背景：深红「通知/大字报」暖调渐变（与分享战报的深蓝完全不同）
    const g = c.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, "#5a1c16"); g.addColorStop(1, "#200907");
    c.fillStyle = g; c.fillRect(0, 0, W, H);
    // 斜纹装饰
    c.save();
    c.globalAlpha = 0.05; c.strokeStyle = "#ffffff"; c.lineWidth = 2;
    for (let i = -H; i < W + H; i += 36) {
      c.beginPath(); c.moveTo(i, 0); c.lineTo(i + H, H); c.stroke();
    }
    c.restore();
    // 底部主题色强调条
    c.fillStyle = campCol; c.fillRect(0, H - 10, W, 10);

    // 顶部标题行
    c.textAlign = "left";
    c.font = font(true, 22); c.fillStyle = "#ffd9a0";
    c.fillText("📢 谁是卧底 · 战况通知", M, 64);
    c.font = font(false, 16); c.fillStyle = "rgba(255,255,255,.6)";
    const titleTxt = truncatePosterText(c, result.title || "本局已结束", W - 2 * M - 200);
    c.textAlign = "right"; c.fillText(titleTxt, W - M, 64);
    c.textAlign = "left";

    // 中央大字：获胜阵营（超大字号 + 阵营色描边，大字报的醒目感）
    const bigText = `${campCn || "本局"} 获胜`;
    c.font = font(true, 88);
    let bigW = c.measureText(bigText).width;
    if (bigW > W - 2 * M) { c.font = font(true, 72); bigW = c.measureText(bigText).width; }
    c.textBaseline = "middle";
    c.lineJoin = "round";
    c.strokeStyle = "rgba(0,0,0,.5)"; c.lineWidth = 12;
    c.strokeText(bigText, (W - bigW) / 2, 182);
    c.fillStyle = campCol;
    c.fillText(bigText, (W - bigW) / 2, 182);
    c.textBaseline = "alphabetic";

    // 获胜玩家名单（金色大号，可换行最多 3 行；与大标题保持足够间距）
    const winners = players
      .filter((p) => p.camp === camp)
      .map((p) => `${p.player_number}号${p.display_name || ""}`.trim());
    let wLineCount = 0;
    if (winners.length) {
      const wText = "🎉 " + winners.join(" · ");
      const wLines = wrapPosterLines(c, wText, W - 2 * M, font(true, 26), 3);
      wLineCount = wLines.length;
      c.fillStyle = "#ffe3b0";
      wLines.forEach((ln, i) => c.fillText(ln, (W - c.measureText(ln).width) / 2, 270 + i * 40));
    }

    // 词条双药丸（比分享战报更大更醒目；位置随胜利方行数下移；过宽时缩小字号分两行）
    const pillTop = 296 + Math.max(1, wLineCount) * 40;
    const pill = (text, x, y, w, col, fg, fs) => {
      rr(x, y, w, 46, 23); c.fillStyle = col; c.fill();
      c.font = font(true, fs); c.fillStyle = fg; c.textAlign = "center"; c.textBaseline = "middle";
      c.fillText(text, x + w / 2, y + 23);
      c.textBaseline = "alphabetic";
    };
    if (result.civilian_word || result.undercover_word) {
      const cwT = `🛡️ 平民「${result.civilian_word}」`;
      const uwT = `🕵️ 卧底「${result.undercover_word}」`;
      c.font = font(true, 20);
      const cwW = c.measureText(truncatePosterText(c, cwT, 300)).width + 34;
      const uwW = c.measureText(truncatePosterText(c, uwT, 300)).width + 34;
      const gap = 20;
      const x0 = (W - (cwW + gap + uwW)) / 2;
      if (x0 >= M) {
        pill(truncatePosterText(c, cwT, 300), x0, pillTop, cwW, "rgba(125,255,176,.16)", "#b6ffd6", 20);
        pill(truncatePosterText(c, uwT, 300), x0 + cwW + gap, pillTop, uwW, "rgba(221,169,255,.16)", "#ecc9ff", 20);
      } else {
        // 一行放不下：缩小字号，分上下两行
        c.font = font(true, 18);
        const cw2 = truncatePosterText(c, cwT, 440);
        const uw2 = truncatePosterText(c, uwT, 440);
        const w1 = c.measureText(cw2).width + 34;
        const w2 = c.measureText(uw2).width + 34;
        pill(cw2, (W - w1) / 2, pillTop, w1, "rgba(125,255,176,.16)", "#b6ffd6", 18);
        pill(uw2, (W - w2) / 2, pillTop + 56, w2, "rgba(221,169,255,.16)", "#ecc9ff", 18);
      }
      c.textAlign = "left"; // 药丸内部置 center，画完恢复，避免后续文字错位
    }

    // 底部：失败方（灰白小字，带身份）
    const losers = players.filter((p) => p.camp && p.camp !== camp);
    if (losers.length) {
      const lText = "💔 败方：" + losers.map(
        (p) => `${p.player_number}号${p.display_name || ""}`.trim() + `（${campName[p.camp] || p.camp || "?"}）`
      ).join("、");
      c.font = font(false, 17); c.fillStyle = "rgba(255,255,255,.55)";
      const lLines = wrapPosterLines(c, lText, W - 2 * M - 150, font(false, 17), 2);
      lLines.forEach((ln, i) => c.fillText(ln, M, H - 84 + i * 26));
    }
    // 右下角水印
    c.textAlign = "right";
    c.font = font(false, 14); c.fillStyle = "rgba(255,255,255,.4)";
    c.fillText("由 花火 监督生成", W - M, H - 32);
    return cv.toDataURL("image/png");
  }

  function forShareCol(i) { return [24, 92, 360, 500][i]; }

  // 生成一句「以屏幕前玩家视角」的有趣分享文案（多款随机，丰富多样性）
  function ucShareHeadline(r) {
    const me = r.me || {};
    const ps = r.players || [];
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const t = `${hh}:${mm}`;
    const campCn = (c) => (c === "civilian" ? "平民" : c === "undercover" ? "卧底" : c === "whiteboard" ? "白板" : c || "?");
    const meName = me.name || `${me.number}号玩家`;
    const win = r.camp;
    const meWin = me.camp === win;
    const isSpectator = !me.camp; // 观众/未参与：不套用“我参战”文案
    if (isSpectator) {
      const pool = [
        `旁观了一场${t}的卧底对决：${campCn(win)}笑到了最后！`,
        `${t}围观完毕：这局${campCn(win)}更胜一筹，精彩！`,
        `${t}看完了这场谁是卧底，胜负已分——${campCn(win)}获胜！`,
      ];
      return pool[Math.floor(Math.random() * pool.length)];
    }
    const allies = ps
      .filter((p) => p.camp === win && p.camp !== me.camp && Number(p.player_number) !== me.number && !p.is_out)
      .map((p) => p.display_name || `${p.player_number}号`);
    const ally = allies[0];
    const pool = [];
    if (meWin) {
      if (win === "undercover") {
        pool.push(`在${t}的对局中，${meName}以卧底身份舌战群雄，凭一手演技瞒过全场、赢下本局！`);
        pool.push(`${t}，${meName}化身卧底，在细节与谎言间游走，笑到了最后！`);
      } else if (win === "civilian") {
        pool.push(ally
          ? `在${t}的对局中，${meName}以平民身份与「${ally}」并肩，成功揪出了卧底！`
          : `在${t}的对局中，${meName}以平民身份冷静判案，送卧底出局！`);
      } else {
        pool.push(`在${t}的对局中，${meName}以白板身份深藏不露，顶住压力存活到最后一刻，拿下这场难得的胜利！`);
        pool.push(`${t}，${meName}凭一腔白板身份稳到最后，这局赢得很有含金量！`);
      }
    } else {
      pool.push(`在${t}的对局中，${meName}憾负于${campCn(win)}，技不如人，下次再战！`);
      pool.push(`${t}，${meName}惜败给${campCn(win)}，看来水还是很深，认栽了。`);
    }
    return pool[Math.floor(Math.random() * pool.length)];
  }

  // 按最大宽度把长句折成若干行（最多 maxLines 行，超出补省略号）
  function wrapPosterLines(c, text, maxW, font, maxLines) {
    c.font = font;
    const out = [];
    let cur = "";
    for (const ch of String(text || "")) {
      const next = cur + ch;
      if (cur && c.measureText(next).width > maxW) { out.push(cur); cur = ch; }
      else cur = next;
    }
    if (cur) out.push(cur);
    if (out.length > maxLines) {
      out.splice(maxLines);
      out[maxLines - 1] = ((out[maxLines - 1] || "") + "…");
    }
    return out;
  }

  // 预载分享战报里的 QQ 头像：CORS 允许才返回可绘制图片，否则返回 null
  // （qlogo 未带 Access-Control-Allow-Origin 时会触发 onerror，从而避免污染 canvas）
  function loadPosterAvatars(items, timeoutMs = 5000) {
    return Promise.all(
      (items || []).map(({ number, url }) => loadPosterImage(url, number, timeoutMs))
    ).then((list) => {
      const map = new Map();
      list.forEach(({ number, img }) => map.set(number, img));
      return map;
    });
  }
  function loadPosterImage(url, number, timeoutMs) {
    return new Promise((resolve) => {
      const src = avatarProxyUrl(url);
      if (!src) return resolve({ number, img: null });
      const img = new Image();
      img.crossOrigin = "anonymous";
      let settled = false;
      const done = (value) => { if (!settled) { settled = true; resolve(value); } };
      img.onload = () => done({ number, img });
      img.onerror = () => done({ number, img: null });
      img.referrerPolicy = "no-referrer";
      img.src = src;
      window.setTimeout(() => done({ number, img: null }), timeoutMs);
    });
  }

  // qlogo 不带 CORS 头，Canvas 无法直接绘制；改为经本服务端代理（补 ACAO 头）后加载。
  // 非 qlogo 地址（如自定义头像）保持原样，交给浏览器按原逻辑处理。
  function avatarProxyUrl(url) {
    if (!url || !accessToken || !/^https:\/\/(q1|q2)\.qlogo\.cn\//.test(url)) return url || "";
    return new URL(
      `../../api/room/${accessToken}/avatar?url=${encodeURIComponent(url)}`,
      window.location.href
    ).toString();
  }

  function truncatePosterText(ctx, text, max) {
    if (ctx.measureText(text).width <= max) return text;
    let t = text;
    while (t.length && ctx.measureText(t + "…").width > max) t = t.slice(0, -1);
    return t + "…";
  }
  // 离场提示关闭：收起后让本局身份卡播放一段变暗过渡
  const ucOutClose = document.getElementById("ucOutClose");
  if (ucOutClose) {
    ucOutClose.addEventListener("click", () => {
      const overlay = document.getElementById("ucOutOverlay");
      if (overlay) overlay.hidden = true;
      const idCard = document.getElementById("ucIdentityCard");
      if (idCard) {
        idCard.classList.remove("uc-identity-eliminated");
        void idCard.offsetWidth;
        idCard.classList.add("uc-identity-eliminated");
      }
    });
  }
  // 规则说明 / 战绩排行折叠面板
  const bindUcInfoToggle = (btnId, bodyId) => {
    const btn = document.getElementById(btnId);
    const body = document.getElementById(bodyId);
    if (!btn || !body) return;
    btn.addEventListener("click", () => {
      const open = body.hidden;
      body.hidden = !open;
      btn.setAttribute("aria-expanded", String(open));
      btn.classList.toggle("is-open", open);
    });
  };
  bindUcInfoToggle("ucRulesToggle", "ucRulesBody");
  bindUcInfoToggle("ucBoardToggle", "ucBoardBody");
  // 集结阶段：玩家点击「准备」/「取消准备」，全员就绪自动开局
  const ucReadyButton = document.getElementById("ucReadyButton");
  if (ucReadyButton) {
    ucReadyButton.addEventListener("click", async () => {
      if (!visitorToken || !accessToken) {
        showToast("请先进入玩家席");
        return;
      }
      const next = ucReadyButton.dataset.ready !== "1";
      ucReadyButton.disabled = true;
      try {
        const res = await request("POST", "ready", {
          visitor_token: visitorToken,
          ready: next,
        });
        if (res?.room) {
          setRoom(res.room);
          render();
          showToast(next ? "你已准备，等待其他玩家…" : "已取消准备");
        } else if (res?.error) {
          showToast(res.error);
        }
      } catch (error) {
        showToast(error?.message || "操作失败", 3200);
      } finally {
        ucReadyButton.disabled = false;
      }
    });
  }
  // 玩家退出对局（回到观众席）
  const ucLeaveSeatButton = document.getElementById("ucLeaveSeatButton");
  if (ucLeaveSeatButton) {
    ucLeaveSeatButton.addEventListener("click", leavePlayerSeat);
  }
  // 投票（事件委托）
  const ucVoteGrid = document.getElementById("ucVoteGrid");
  if (ucVoteGrid) {
    ucVoteGrid.addEventListener("click", async (event) => {
      const target = event.target.closest(".uc-vote-card");
      if (!target || target.disabled || !visitorToken || !accessToken) return;
      const num = Number(target.dataset.target_number);
      if (!num) return;
      target.disabled = true;
      try {
        const res = await request(
          "POST",
          "undercover/vote",
          { visitor_token: visitorToken, target_number: num }
        );
        if (res?.room) {
          setRoom(res.room);
          render();
        } else if (res?.error) {
          showToast(res.error);
        }
      } catch (error) {
        showToast(error?.message || "投票失败", 3200);
        try { await loadState(); render(); } catch (_syncError) { /* 轮询会重试 */ }
      }
    });
  }
  board.addEventListener("click", moveAt);
  window.addEventListener("pagehide", (event) => {
    if (!event.persisted) notifyLeave();
  });
  setRoomView("game");
  initAppearance();
  bindAppearanceControls();
  icons();
  join()
    .then(() => { setConnection("online", "已连接"); pollTimer = window.setTimeout(poll, 1000); })
    .catch((error) => {
      setConnection("error", "无法进入");
      document.getElementById("boardOverlay").hidden = false;
      document.getElementById("overlayTitle").textContent = "无法进入房间";
      document.getElementById("overlayText").textContent = error?.message || "链接已经失效";
    });
})();
