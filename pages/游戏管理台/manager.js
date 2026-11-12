(() => {
  "use strict";

  const body = document.getElementById("roomsBody");
  const emptyState = document.getElementById("emptyState");
  const assignDialog = document.getElementById("assignDialog");
  const confirmDialog = document.getElementById("confirmDialog");
  const promptDialog = document.getElementById("promptDialog");
  const toast = document.getElementById("toast");
  const gameOptions = [
    ["gomoku", "五子棋"],
    ["xiangqi", "中国象棋"],
    ["weiqi", "围棋"],
    ["tictactoe", "井字棋"],
    ["othello", "黑白棋"],
    ["turtle_soup", "海龟汤"],
    ["pig_dice", "贪心骰子"],
    ["draw_guess", "你画我猜"],
    ["blackjack", "二十一点"],
    ["undercover", "谁是卧底"],
  ];
  let rooms = [];
  let tunnel = {};
  let cloudflared = {};
  let xiangqiEngine = {};
  let enabledGames = {};
  let gameSettings = [];
  let toastTimer = 0;
  let refreshTimer = 0;
  let confirmResolver = null;
  let promptResolver = null;

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 2600);
  }

  // 复制到剪贴板：插件页面常被嵌在 iframe 里，clipboard API 可能被拒，保留 textarea 兜底
  async function copyText(text) {
    const value = String(text || "");
    if (!value) return false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(value);
        return true;
      }
    } catch (_) { /* 掉到下面的兜底方案 */ }
    try {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.top = "-1000px";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      const ok = document.execCommand("copy");
      area.remove();
      return ok;
    } catch (_) {
      return false;
    }
  }

  function resolveConfirmation(accepted) {
    const resolver = confirmResolver;
    confirmResolver = null;
    if (confirmDialog.open) confirmDialog.close();
    if (resolver) resolver(Boolean(accepted));
  }

  // 页面内数值输入框（管理台页面运行在沙箱 iframe，window.prompt 会被浏览器拦截）
  let promptKind = "number";
  function openPrompt({ title, message, label = "数值", initial = "0", kind = "number" }) {
    if (promptResolver) resolvePromptValue(null);
    promptKind = kind;
    document.getElementById("promptTitle").textContent = title;
    document.getElementById("promptMessage").textContent = message;
    document.getElementById("promptLabel").textContent = label;
    const eyebrow = document.getElementById("promptEyebrow");
    if (eyebrow) eyebrow.textContent = kind === "text" ? "输入文本" : "输入数值";
    const input = document.getElementById("promptInput");
    if (kind === "text") {
      input.type = "text";
      input.removeAttribute("min");
      input.removeAttribute("step");
      input.removeAttribute("inputmode");
    } else {
      input.type = "number";
      input.setAttribute("min", "0");
      input.setAttribute("step", "1");
      input.setAttribute("inputmode", "numeric");
    }
    input.value = initial;
    promptDialog.showModal();
    window.setTimeout(() => input.focus(), 0);
    icons();
    return new Promise((resolve) => { promptResolver = resolve; });
  }

  function resolvePromptValue(value) {
    const resolver = promptResolver;
    promptResolver = null;
    if (promptDialog.open) promptDialog.close();
    if (resolver) resolver(value);
  }

  function confirmAction({ title, message, label = "确认", danger = false }) {
    if (confirmResolver) resolveConfirmation(false);
    document.getElementById("confirmTitle").textContent = title;
    document.getElementById("confirmMessage").textContent = message;
    const proceed = document.getElementById("confirmProceed");
    proceed.querySelector("span").textContent = label;
    proceed.classList.toggle("danger", danger);
    confirmDialog.showModal();
    icons();
    return new Promise((resolve) => { confirmResolver = resolve; });
  }

  async function bridge() {
    for (let index = 0; index < 60; index += 1) {
      const candidate = window.AstrBotPluginPage;
      if (candidate?.apiGet && candidate?.apiPost) {
        if (candidate.ready) await candidate.ready();
        return candidate;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 100));
    }
    throw new Error("请从 AstrBot 插件拓展页打开游戏管理台");
  }

  async function endpoint(method, path, payload = {}) {
    const api = await bridge();
    const result = method === "GET"
      ? await api.apiGet(`page/${path}`)
      : await api.apiPost(`page/${path}`, payload);
    if (result?.status === "error") throw new Error(result.message || "请求失败");
    return result?.data ?? result;
  }

  function statusLabel(status) {
    return {
      waiting: "等待玩家", setup: "等待开局", active: "对局中", paused: "已暂停",
      finished: "本局结束", rematch_pending: "等待回应", closed: "已关闭",
    }[status] || status || "未知";
  }

  function limitLabel(value) {
    return Number(value) === 0 ? "无限制" : `上限 ${value}`;
  }

  function createText(tag, text, className = "") {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }

  function actionButton(icon, title, action, room, className = "") {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `row-button ${className}`;
    button.title = title;
    button.setAttribute("aria-label", title);
    button.innerHTML = `<i data-lucide="${icon}"></i>`;
    button.addEventListener("click", () => runRoomAction(room.room_id, action));
    return button;
  }

  // 右上角「开新房间」玩法下拉：跟着 enabled_games 更新禁用态，全部关闭时按钮置灰
  function renderNewRoomPicker() {
    const select = document.getElementById("newRoomGame");
    const button = document.getElementById("createRoomAction");
    if (!select || !button) return;
    if (!select.dataset.ready) {
      gameOptions.forEach(([value, label]) => {
        const option = document.createElement("option");
        option.value = value;
        option.dataset.label = label;
        option.textContent = label;
        select.appendChild(option);
      });
      select.dataset.ready = "1";
    }
    Array.from(select.options).forEach((option) => {
      const off = enabledGames[option.value] === false;
      option.disabled = off;
      option.textContent = off ? `${option.dataset.label}（已关闭）` : option.dataset.label;
    });
    const current = select.selectedOptions[0];
    if (!current || current.disabled) {
      const usable = Array.from(select.options).find((option) => !option.disabled);
      if (usable) select.value = usable.value;
    }
    const anyEnabled = Array.from(select.options).some((option) => !option.disabled);
    button.disabled = !anyEnabled;
    button.title = anyEnabled ? "在管理台直接开一个链接房间" : "所有玩法都已在配置中关闭";
  }

  async function createRoomFromConsole() {
    const select = document.getElementById("newRoomGame");
    const button = document.getElementById("createRoomAction");
    if (!select || !button) return;
    button.disabled = true;
    try {
      const groupInput = document.getElementById("newRoomGroup");
      const groupId = String(groupInput?.value || "").trim();
      const data = await endpoint("POST", "room/create", {
        game_type: select.value,
        group_id: groupId,
      });
      const url = String(data?.url || "");
      if (url) {
        const copied = await copyText(url);
        showToast(copied ? "房间已创建，访问网址已复制" : `房间已创建：${url}`);
      } else {
        showToast("房间已创建，但外部访问地址还没就绪，请先启动临时访问或配置固定地址");
      }
      if (groupInput) groupInput.value = "";
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "开房失败");
    } finally {
      button.disabled = false;
      renderNewRoomPicker();
    }
  }

  function renderRooms() {
    body.replaceChildren();
    emptyState.hidden = rooms.length > 0;
    rooms.forEach((room) => {
      const row = document.createElement("tr");
      const identity = document.createElement("td");
      identity.append(createText("strong", room.room_id, "room-code"));
      identity.append(createText("small", room.admin_room ? "管理员房间" : "普通房间"));

      const game = document.createElement("td");
      const gameSelect = document.createElement("select");
      gameSelect.className = "game-select";
      gameSelect.title = "切换游戏";
      gameOptions.forEach(([value, label]) => {
        if (enabledGames[value] === false && room.game_type !== value) return;
        const option = document.createElement("option");
        option.value = value;
        option.textContent = enabledGames[value] === false ? `${label}（已关闭）` : label;
        option.selected = room.game_type === value;
        gameSelect.appendChild(option);
      });
      gameSelect.addEventListener("change", async () => {
        const target = gameSelect.value;
        const active = ["active", "paused"].includes(room.status);
        if (active && !await confirmAction({
          title: "放弃当前对局",
          message: "当前对局尚未结束。切换游戏将放弃本局，但会保留房间、玩家和比分。",
          label: "放弃并切换",
          danger: true,
        })) {
          gameSelect.value = room.game_type;
          return;
        }
        gameSelect.disabled = true;
        try {
          await endpoint("POST", "room/action", {
            room_id: room.room_id,
            action: "switch_game",
            game_type: target,
            confirm_abandon: active,
          });
          showToast("房间游戏已切换");
          await loadRooms();
        } catch (error) {
          gameSelect.value = room.game_type;
          gameSelect.disabled = false;
          showToast(error?.message || "切换失败");
        }
      });
      game.appendChild(gameSelect);

      const origin = document.createElement("td");
      // 管理台手动开的房间没有 QQ 会话，别显示成「私聊 · 未知 QQ」
      const consoleRoom = String(room.session_id || "").startsWith("admin:");
      origin.append(createText(
        "strong",
        consoleRoom ? "管理台开房" : (room.source === "group" ? `群聊 ${room.group_id || ""}` : "私聊"),
      ));
      origin.append(createText(
        "small",
        consoleRoom
          ? (room.group_id
            ? `播报群 ${room.group_id} · 访客由管理台安排入座`
            : "未绑定播报群 · 战报发不出去（点右侧喇叭绑定）")
          : `${room.creator_name || "创建者"} · ${room.creator_qq || "未知 QQ"}`,
      ));

      const members = document.createElement("td");
      const visitorList = document.createElement("div");
      visitorList.className = "visitor-list";
      (room.visitors || []).forEach((visitor) => {
        const chip = document.createElement("span");
        chip.className = `visitor ${visitor.online ? "online" : ""} ${visitor.is_player ? "player" : ""}`;
        const dot = document.createElement("i");
        const visitorLabel = visitor.display_name
          ? `${visitor.display_name}（${visitor.number}号）`
          : `${visitor.number}号`;
        chip.append(dot, document.createTextNode(`${visitorLabel}${visitor.player_qq ? ` · QQ ${visitor.player_qq}` : ""}${visitor.is_player ? " · 玩家" : ""}${visitor.is_current_player ? " · 当前" : ""}`));
        if (visitor.is_player) {
          const demote = document.createElement("button");
          demote.type = "button";
          demote.className = "visitor-kick";
          demote.title = `将 ${visitor.number} 号移到观众席`;
          demote.setAttribute("aria-label", demote.title);
          demote.innerHTML = '<i data-lucide="user-round-minus"></i>';
          demote.addEventListener("click", async () => {
            try {
              await endpoint("POST", "room/action", {
                room_id: room.room_id, action: "demote", visitor_number: visitor.number,
              });
              await loadRooms();
            } catch (error) {
              showToast(error?.message || "移出玩家席失败");
            }
          });
          chip.appendChild(demote);
        }
        const kick = document.createElement("button");
        kick.type = "button";
        kick.className = "visitor-kick";
        kick.title = `移出 ${visitor.number} 号`;
        kick.setAttribute("aria-label", `移出 ${visitor.number} 号`);
        kick.innerHTML = '<i data-lucide="x"></i>';
        kick.addEventListener("click", async () => {
          if (!await confirmAction({
            title: "移出访客",
            message: `确认将 ${visitor.number} 号移出房间？多人游戏会在仍有其他玩家时继续。`,
            label: "确认移出",
            danger: true,
          })) return;
          try {
            await endpoint("POST", "room/action", {
              room_id: room.room_id, action: "kick", visitor_number: visitor.number,
            });
            await loadRooms();
          } catch (error) {
            showToast(error?.message || "移出失败");
          }
        });
        chip.appendChild(kick);
        visitorList.appendChild(chip);
      });
      members.appendChild(visitorList);

      const player = document.createElement("td");
      const playerNumbers = Array.isArray(room.player_numbers) ? room.player_numbers : [];
      const playerLabels = Array.isArray(room.player_labels) ? room.player_labels : [];
      player.append(createText("strong", playerLabels.length ? playerLabels.join("、") : playerNumbers.length ? playerNumbers.map((number) => `${number}号`).join("、") : "未安排"));
      player.append(createText("small", room.player_capacity > 1 || room.player_capacity === 0
        ? `${playerNumbers.length} / ${room.player_capacity || "不限"} 席`
        : (room.player_qq ? `QQ ${room.player_qq}` : "尚未绑定 QQ")));

      const state = document.createElement("td");
      state.append(createText("span", statusLabel(room.status), `status ${room.status}`));
      if (room.game_type === "turtle_soup") {
        const progress = room.turtle_soup_progress;
        const phase = progress?.phase === "preparing"
          ? "出题中"
          : (progress?.processing ? "判断中" : `提问 ${progress?.question_count || 0} · 提示 ${progress?.hints_used || 0}`);
        const soupMode = room.turtle_soup_mode === "player_host" ? "玩家出题" : "花火 出题";
        state.append(createText("small", `${soupMode} · 难度：${{ easy: "简单", normal: "普通", hard: "困难" }[room.difficulty] || "普通"} · ${phase}`));
      } else if (room.game_type === "pig_dice") {
        const progress = room.pig_dice_progress;
        const style = { cautious: "稳健", balanced: "均衡", bold: "大胆" }[progress?.risk_style] || "均衡";
        const score = progress
          ? `玩家 ${progress.human_score} · 花火 ${progress.bot_score} · 本回合 ${progress.turn_total}`
          : "等待开局";
        state.append(createText("small", `风格：${style} · ${score}`));
      } else if (room.game_type === "draw_guess") {
        const progress = room.draw_guess_progress;
        const detail = progress
          ? `${progress.processing ? "花火 看图中" : progress.solved ? "已猜中" : progress.timed_out ? "已超时" : `剩余 ${progress.remaining_seconds} 秒`} · 猜测 ${progress.guess_count}/${progress.max_guesses}`
          : "等待开局";
        state.append(createText("small", `难度：${{ easy: "简单", normal: "普通", hard: "困难" }[room.difficulty] || "普通"} · ${detail}`));
      } else if (room.game_type === "blackjack") {
        const progress = room.blackjack_progress;
        const phase = progress?.finished
          ? "已结算"
          : progress?.phase === "dealer_turn"
          ? "庄家补牌中"
          : `闲家 ${progress?.hand_count || 0} 手`;
        state.append(createText("small", `难度：${{ easy: "简单", normal: "普通", hard: "困难" }[room.difficulty] || "普通"} · ${phase}`));
      } else if (room.game_type === "undercover") {
        const progress = room.undercover_progress || {};
        const phaseLabel = {
          idle: "等待入座",
          preparing: "发词准备",
          speech: "发言轮",
          pk: "PK 发言轮",
          voting: "投票轮",
          finished: "已结束",
        }[progress.phase] || (progress.phase ? String(progress.phase) : "等待开局");
        const roundText = Number(progress.round_number)
          ? `第 ${progress.round_number} 轮 · 存活 ${progress.players_live}/${progress.players_total} 人`
          : `存活 ${progress.players_live || 0}/${progress.players_total || 0} 人`;
        state.append(createText("small", `${phaseLabel} · ${roundText}`));
      } else {
        state.append(createText("small", `棋力：${{ easy: "简单", normal: "普通", hard: "困难" }[room.difficulty] || "普通"}`));
        // 只有棋类房间有棋力档位：直接在下拉里切换，下一手 Bot 立即按新棋力走子
        if (room.difficulty_adjustable) {
          const difficultySelect = document.createElement("select");
          difficultySelect.className = "difficulty-select";
          difficultySelect.title = "切换棋力";
          difficultySelect.setAttribute("aria-label", "切换棋力");
          [["easy", "简单"], ["normal", "普通"], ["hard", "困难"]].forEach(([value, label]) => {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = label;
            option.selected = (room.difficulty || "normal") === value;
            difficultySelect.appendChild(option);
          });
          difficultySelect.addEventListener("change", async () => {
            const target = difficultySelect.value;
            difficultySelect.disabled = true;
            try {
              await endpoint("POST", "room/action", {
                room_id: room.room_id,
                action: "set_difficulty",
                difficulty: target,
              });
              showToast("棋力已切换");
              await loadRooms();
            } catch (error) {
              difficultySelect.value = room.difficulty || "normal";
              difficultySelect.disabled = false;
              showToast(error?.message || "切换棋力失败");
            }
          });
          state.appendChild(difficultySelect);
        }
      }

      const actions = document.createElement("td");
      const actionList = document.createElement("div");
      actionList.className = "row-actions";
      if (room.url) {
        // 每行都能直接复制本房访问网址，省得回去翻聊天记录
        const copyLink = document.createElement("button");
        copyLink.type = "button";
        copyLink.className = "row-button";
        copyLink.title = "复制访问网址";
        copyLink.setAttribute("aria-label", "复制访问网址");
        copyLink.innerHTML = '<i data-lucide="link"></i>';
        copyLink.addEventListener("click", async () => {
          const copied = await copyText(room.url);
          showToast(copied ? "访问网址已复制" : room.url);
        });
        actionList.appendChild(copyLink);
      }
      const assign = document.createElement("button");
      assign.type = "button";
      assign.className = "row-button";
      const hasAssignable = (room.visitors || []).some((visitor) => !visitor.is_player);
      assign.disabled = !hasAssignable;
      assign.title = hasAssignable ? "安排玩家" : "房间里还没有可安排的访客";
      assign.setAttribute("aria-label", "安排玩家");
      assign.innerHTML = '<i data-lucide="user-check"></i>';
      assign.addEventListener("click", () => openAssign(room));
      actionList.appendChild(assign);
      if (consoleRoom) {
        // 管理台链接房没有 QQ 会话，战报要发进群只能靠绑一个群号
        const bindGroup = document.createElement("button");
        bindGroup.type = "button";
        bindGroup.className = "row-button";
        bindGroup.title = room.group_id
          ? `播报群：${room.group_id}（点击修改）`
          : "绑定播报群";
        bindGroup.setAttribute("aria-label", "绑定播报群");
        bindGroup.innerHTML = '<i data-lucide="megaphone"></i>';
        bindGroup.addEventListener("click", async () => {
          const value = window.prompt(
            "战报要发到哪个 QQ 群？只填数字群号（如 123456789），留空表示解除绑定",
            room.group_id || "",
          );
          if (value === null) return;
          try {
            await endpoint("POST", "room/action", {
              room_id: room.room_id,
              action: "bind_group",
              group_id: String(value).trim(),
            });
            showToast(String(value).trim() ? `已绑定播报群 ${String(value).trim()}` : "已解除播报群绑定");
            await loadRooms();
          } catch (error) {
            showToast(error?.message || "绑定失败");
          }
        });
        actionList.appendChild(bindGroup);
      }
      if (room.status === "active") actionList.appendChild(actionButton("pause", "暂停", "pause", room));
      if (room.status === "paused") actionList.appendChild(actionButton("play", "继续", "resume", room));
      actionList.appendChild(actionButton("x", "关闭房间", "close", room, "danger"));
      actions.appendChild(actionList);
      row.append(identity, game, origin, members, player, state, actions);
      body.appendChild(row);
    });
    icons();
  }

  function renderMetrics(data) {
    const groupRooms = rooms.filter((room) => room.source === "group");
    // 管理台手动开的房间也归在 private 里，但不受私聊配额约束，单独计数免得看起来「超额」
    const consoleRooms = rooms.filter((room) => String(room.session_id || "").startsWith("admin:"));
    const privateRooms = rooms.filter(
      (room) => room.source === "private" && !consoleRooms.includes(room),
    );
    const online = rooms.reduce(
      (count, room) => count + (room.visitors || []).filter((visitor) => visitor.online).length,
      0,
    );
    document.getElementById("roomCount").textContent = rooms.length;
    document.getElementById("groupCount").textContent = groupRooms.length;
    document.getElementById("privateCount").textContent = privateRooms.length;
    document.getElementById("visitorCount").textContent = online;
    document.getElementById("groupLimit").textContent = limitLabel(data.limits?.group);
    document.getElementById("privateLimit").textContent = consoleRooms.length
      ? `${limitLabel(data.limits?.private)} · 另有 ${consoleRooms.length} 个管理台房间`
      : limitLabel(data.limits?.private);
  }

  function renderService(data) {
    const badge = document.getElementById("serviceBadge");
    const action = document.getElementById("tunnelAction");
    tunnel = data.tunnel || {};
    cloudflared = tunnel;
    if (data.server?.public_base_url || data.server?.external_base_url) {
      badge.textContent = data.server?.public_base_url ? "固定 HTTPS" : "自定义外部地址";
      badge.className = "service-badge online";
      action.disabled = true;
      action.querySelector("span").textContent = "固定地址已配置";
      document.getElementById("tunnelUrl").textContent = data.server.public_base_url || data.server.external_base_url;
    } else if (tunnel.running) {
      badge.textContent = "临时公网已开启";
      badge.className = "service-badge online";
      action.disabled = rooms.length > 0;
      action.dataset.running = "true";
      action.querySelector("span").textContent = rooms.length ? "活动房间使用中" : "停止临时访问";
      document.getElementById("tunnelUrl").textContent = tunnel.url || "";
    } else {
      badge.textContent = data.server?.running ? "仅本机" : "按需启动";
      badge.className = "service-badge";
      action.disabled = !tunnel.installed;
      action.dataset.running = "false";
      action.querySelector("span").textContent = tunnel.installed ? "启动临时访问" : "未安装 cloudflared";
      document.getElementById("tunnelUrl").textContent = "";
    }
    renderCloudflared(cloudflared);
  }

  function renderCloudflared(data) {
    const badge = document.getElementById("cloudflaredBadge");
    const detail = document.getElementById("cloudflaredDetail");
    const action = document.getElementById("cloudflaredAction");
    if (!badge || !detail || !action) return;
    if (data.installed) {
      badge.textContent = data.running ? "运行中" : "已安装";
      badge.className = "service-badge online";
      const source = { configured: "手动路径", system: "系统 PATH", managed: "插件托管", bundled: "插件目录" }[data.source] || "已发现";
      detail.textContent = `${source} · ${data.path || data.platform || ""}`;
    } else {
      badge.textContent = "未安装";
      badge.className = data.error ? "service-badge error" : "service-badge";
      detail.textContent = data.error || `适用版本：${data.platform || "自动检测"}`;
    }
    action.disabled = !data.allow_download || Boolean(data.configured_path);
    action.title = data.configured_path
      ? "当前使用手动配置的 cloudflared 路径"
      : (data.allow_download ? "从 Cloudflare 官方发行版下载" : "插件配置已禁止下载");
  }

  function renderEngine(data) {
    xiangqiEngine = data.xiangqi_engine || {};
    const badge = document.getElementById("engineBadge");
    const detail = document.getElementById("engineDetail");
    const action = document.getElementById("engineAction");
    if (xiangqiEngine.available) {
      badge.textContent = xiangqiEngine.running ? "运行中" : "已安装";
      badge.className = "service-badge online";
      const version = xiangqiEngine.version || "版本未知";
      detail.textContent = `${version} · ${xiangqiEngine.path || xiangqiEngine.platform || ""}`;
    } else {
      badge.textContent = "未安装";
      badge.className = xiangqiEngine.error ? "service-badge error" : "service-badge";
      detail.textContent = xiangqiEngine.error || `适用版本：${xiangqiEngine.platform || "自动检测"}`;
    }
    action.disabled = !xiangqiEngine.allow_download || xiangqiEngine.configured;
    action.title = xiangqiEngine.configured
      ? "当前使用插件配置中指定的引擎"
      : (xiangqiEngine.allow_download ? "从 Pikafish 官方发行版安装或更新" : "插件配置已禁止下载");
  }

  async function loadRooms() {
    window.clearTimeout(refreshTimer);
    try {
      const data = await endpoint("GET", "rooms");
      rooms = Array.isArray(data.rooms) ? data.rooms : [];
      enabledGames = data.enabled_games || {};
      renderNewRoomPicker();
      renderMetrics(data);
      renderService(data);
      renderEngine(data);
      renderRooms();
      document.getElementById("lastUpdated").textContent = `更新于 ${new Date().toLocaleTimeString()}`;
      refreshTimer = window.setTimeout(loadRooms, 2500);
    } catch (error) {
      document.getElementById("serviceBadge").textContent = "读取失败";
      document.getElementById("serviceBadge").className = "service-badge error";
      showToast(error?.message || "无法读取房间状态");
      refreshTimer = window.setTimeout(loadRooms, 5000);
    }
  }

  function openAssign(room) {
    const select = document.getElementById("assignVisitor");
    const visitors = (room.visitors || []).slice().sort((a, b) => a.number - b.number);
    // 没人在网页里就没什么可安排：直接提示，别弹一个空弹窗把人困住
    if (!visitors.length) {
      showToast("房间里还没有访客，先让对方打开房间链接");
      return;
    }
    select.replaceChildren();
    visitors.forEach((visitor) => {
      const option = document.createElement("option");
      option.value = visitor.number;
      const name = (visitor.display_name || "").trim();
      const state = [
        visitor.online ? "在线" : "离线",
        visitor.identity_confirmed ? "已绑定" : "未绑定",
        visitor.is_player ? "在玩家席" : "",
      ].filter(Boolean).join(" · ");
      option.textContent = `${visitor.number} 号${name ? ` · ${name}` : " · 匿名"} · ${state}`;
      option.disabled = Boolean(visitor.is_player);
      select.appendChild(option);
    });
    const firstSelectable = visitors.find((visitor) => !visitor.is_player);
    if (firstSelectable) select.value = firstSelectable.number;
    const syncQq = () => {
      const selected = visitors.find((visitor) => String(visitor.number) === select.value);
      document.getElementById("assignQq").value = selected?.player_qq || selected?.qq || "";
    };
    select.onchange = syncQq;
    document.getElementById("assignRoomId").value = room.room_id;
    document.getElementById("assignRoomLabel").textContent = `房间 ${room.room_id}`;
    syncQq();
    // 全都在玩家席时也没必要再安排，但至少能打开弹窗看状态并正常关闭
    document.getElementById("confirmAssign").disabled = !firstSelectable;
    assignDialog.showModal();
    icons();
  }

  async function confirmAssign() {
    const roomId = document.getElementById("assignRoomId").value;
    const visitorNumber = Number(document.getElementById("assignVisitor").value);
    const playerQq = document.getElementById("assignQq").value.trim();
    if (!/^\d+$/.test(playerQq)) {
      showToast("请输入有效的玩家 QQ 号");
      return;
    }
    try {
      await endpoint("POST", "room/action", {
        action: "assign", room_id: roomId, visitor_number: visitorNumber, player_qq: playerQq,
      });
      assignDialog.close();
      showToast("玩家已安排");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "安排失败");
    }
  }

  async function runRoomAction(roomId, action) {
    if (action === "close" && !await confirmAction({
      title: "关闭房间",
      message: "确认关闭并销毁这个房间？房间链接会立即失效，未完成的对局无法恢复。",
      label: "关闭房间",
      danger: true,
    })) return;
    try {
      await endpoint("POST", "room/action", { room_id: roomId, action });
      showToast("操作已完成");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "操作失败");
    }
  }

  async function toggleTunnel() {
    const action = tunnel.running ? "tunnel/stop" : "tunnel/start";
    try {
      await endpoint("POST", action);
      showToast(tunnel.running ? "临时公网访问已停止" : "临时公网访问已启动");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "无法切换访问通道");
    }
  }

  async function installEngine() {
    if (!await confirmAction({
      title: "安装中国象棋引擎",
      message: "将通过已配置的代理下载 Pikafish 官方发行包，校验后安装到插件数据目录。",
      label: "开始安装",
    })) return;
    const action = document.getElementById("engineAction");
    action.disabled = true;
    action.querySelector("span").textContent = "正在安装";
    try {
      await endpoint("POST", "xiangqi/install");
      showToast("Pikafish 已安装并通过启动检查");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "引擎安装失败");
    } finally {
      action.querySelector("span").textContent = "安装 / 更新";
      action.disabled = !xiangqiEngine.allow_download || xiangqiEngine.configured;
    }
  }

  async function installCloudflared() {
    if (!await confirmAction({
      title: "下载 Cloudflare Tunnel",
      message: "将通过已配置的代理下载 Cloudflare 官方 cloudflared，并安装到插件数据目录。",
      label: "开始下载",
    })) return;
    const action = document.getElementById("cloudflaredAction");
    action.disabled = true;
    action.querySelector("span").textContent = "正在下载";
    try {
      await endpoint("POST", "cloudflared/install");
      showToast("cloudflared 已安装");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "cloudflared 下载失败");
    } finally {
      action.querySelector("span").textContent = "下载 / 更新";
      action.disabled = !cloudflared.allow_download || Boolean(cloudflared.configured_path);
    }
  }

  async function clearLeaderboard() {
    if (!await confirmAction({
      title: "清空战绩排行榜",
      message: "确认清空所有玩法的全局战绩排行榜？此操作不可撤销，房间右侧的“本房战绩排行”也会被清除。",
      label: "确认清空",
      danger: true,
    })) return;
    const action = document.getElementById("clearLeaderboardAction");
    action.disabled = true;
    try {
      const result = await endpoint("POST", "leaderboard/clear", {});
      showToast(result?.cleared ? "战绩排行榜已清空" : "没有可清空的排行榜数据");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "清空排行榜失败");
    } finally {
      action.disabled = false;
    }
  }

  function settingInput(game, field) {
    const wrapper = document.createElement("label");
    wrapper.className = `setting-field setting-${field.type}`;
    const heading = document.createElement("span");
    heading.className = "setting-label";
    heading.textContent = field.label;
    wrapper.appendChild(heading);

    let input;
    if (field.type === "bool") {
      input = document.createElement("input");
      input.type = "checkbox";
      input.checked = Boolean(field.value);
      const track = document.createElement("span");
      track.className = "switch-track";
      wrapper.classList.add("inline-switch");
      wrapper.append(input, track);
    } else if (field.type === "select") {
      input = document.createElement("select");
      (field.options || []).forEach((option) => {
        const node = document.createElement("option");
        node.value = option.value;
        node.textContent = option.label;
        node.selected = String(option.value) === String(field.value);
        input.appendChild(node);
      });
      wrapper.appendChild(input);
    } else {
      const control = document.createElement("div");
      control.className = "setting-control";
      input = document.createElement("input");
      input.type = field.type === "int" ? "number" : "text";
      input.value = field.value ?? "";
      if (field.type === "int") {
        input.step = "1";
        input.required = true;
        input.min = String(field.minimum);
        input.max = String(field.maximum);
      }
      if (field.maximum_length) input.maxLength = Number(field.maximum_length);
      control.appendChild(input);
      if (field.unit) control.appendChild(createText("span", field.unit, "setting-unit"));
      wrapper.appendChild(control);
    }
    input.dataset.game = game.game_type;
    input.dataset.setting = field.key;
    input.dataset.type = field.type;
    input.dataset.default = JSON.stringify(field.default);
    if (field.hint) wrapper.appendChild(createText("small", field.hint, "setting-hint"));
    return wrapper;
  }

  function renderSettings(data) {
    gameSettings = Array.isArray(data.games) ? data.games : [];
    const grid = document.getElementById("gameSettingsGrid");
    grid.replaceChildren();
    gameSettings.forEach((game) => {
      const card = document.createElement("article");
      card.className = `game-setting-card ${game.enabled ? "enabled" : "disabled"}`;
      card.dataset.game = game.game_type;

      const header = document.createElement("header");
      const identity = document.createElement("div");
      identity.append(createText("h3", game.label));
      identity.append(createText("p", game.description));
      const toggle = document.createElement("label");
      toggle.className = "game-toggle";
      const enabled = document.createElement("input");
      enabled.type = "checkbox";
      enabled.checked = Boolean(game.enabled);
      enabled.dataset.game = game.game_type;
      enabled.dataset.setting = "enabled";
      enabled.dataset.type = "bool";
      enabled.addEventListener("change", () => {
        card.classList.toggle("enabled", enabled.checked);
        card.classList.toggle("disabled", !enabled.checked);
        toggle.querySelector("strong").textContent = enabled.checked ? "已开启" : "已关闭";
      });
      toggle.append(enabled, createText("span", "", "switch-track"), createText("strong", enabled.checked ? "已开启" : "已关闭"));
      header.append(identity, toggle);
      card.appendChild(header);

      const fields = document.createElement("div");
      fields.className = "setting-fields";
      (game.fields || []).forEach((field) => fields.appendChild(settingInput(game, field)));
      if (!(game.fields || []).length) {
        fields.appendChild(createText("p", "此游戏当前没有额外数值设置。", "no-settings"));
      }
      card.appendChild(fields);

      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "text-button";
      reset.textContent = "恢复本游戏默认值";
      reset.addEventListener("click", () => resetGameSettings(game.game_type));
      card.appendChild(reset);

      if (game.game_type === "undercover") {
        card.appendChild(buildUndercoverWordsPanel());
      }
      grid.appendChild(card);
    });
    document.getElementById("settingsNotice").textContent = data.notice || "";
    icons();
  }

  function buildUndercoverWordsPanel() {
    const wrap = document.createElement("section");
    wrap.className = "uc-word-panel";
    wrap.style.marginTop = ".9rem";
    wrap.style.borderTop = "1px solid var(--border, #e6e6e6)";
    wrap.style.paddingTop = ".8rem";

    const head = document.createElement("header");
    head.style.display = "flex";
    head.style.justifyContent = "space-between";
    head.style.alignItems = "center";
    head.style.marginBottom = ".6rem";
    const title = document.createElement("h4");
    title.style.margin = "0";
    title.style.color = "var(--text-strong, #222)";
    title.textContent = "词库管理";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "text-button";
    open.textContent = "展开词条";
    open.addEventListener("click", () => {
      tableWrap.hidden = !tableWrap.hidden;
      addRow.hidden = !addRow.hidden;
      open.textContent = tableWrap.hidden ? "展开词条" : "收起词条";
      if (!tableWrap.hidden) loadUndercoverWords(tbody, countLabel);
    });
    head.append(title, open);
    wrap.appendChild(head);

    const intro = document.createElement("p");
    intro.style.margin = "0 0 .55rem";
    intro.style.color = "var(--muted, #666)";
    intro.style.fontSize = ".82rem";
    intro.textContent = "默认内置 30 对常见词条；Bot 也会在开新局时 LLM 生成新词条并自动加入。每条对局开始前系统会自动做去重（最近 10 次不重复）。";
    wrap.appendChild(intro);

    const countLabel = createText("small", "当前共 0 对", "uc-word-count");
    countLabel.style.color = "var(--muted, #666)";
    wrap.appendChild(countLabel);

    const addRow = document.createElement("div");
    addRow.style.display = "flex";
    addRow.style.flexWrap = "wrap";
    addRow.style.alignItems = "center";
    addRow.style.gap = ".4rem";
    addRow.style.margin = ".4rem 0 .7rem";
    addRow.hidden = true;
    const w1 = document.createElement("input");
    w1.type = "text";
    w1.maxLength = 10;
    w1.placeholder = "词条 1";
    w1.style.flex = "1 1 120px";
    w1.style.padding = ".35rem .5rem";
    w1.style.borderRadius = ".5rem";
    w1.style.border = "1px solid var(--border, #ddd)";
    w1.style.font = "inherit";
    const w2 = document.createElement("input");
    w2.type = "text";
    w2.maxLength = 10;
    w2.placeholder = "词条 2";
    w2.style.flex = "1 1 120px";
    w2.style.padding = ".35rem .5rem";
    w2.style.borderRadius = ".5rem";
    w2.style.border = "1px solid var(--border, #ddd)";
    w2.style.font = "inherit";
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "primary-action";
    addBtn.textContent = "添加词条";
    addBtn.style.padding = ".35rem .75rem";
    addBtn.addEventListener("click", async () => {
      const word1 = w1.value.trim();
      const word2 = w2.value.trim();
      if (!word1 || !word2) {
        showToast("两个词条都不能为空");
        return;
      }
      addBtn.disabled = true;
      try {
        const res = await endpoint("POST", "undercover_words/add", { word1, word2 });
        renderUndercoverWords(res?.items || [], tbody, countLabel);
        w1.value = "";
        w2.value = "";
        showToast("已添加");
      } catch (err) {
        showToast(err?.message || "添加失败");
      } finally {
        addBtn.disabled = false;
      }
    });
    addRow.append(w1, w2, addBtn);
    wrap.appendChild(addRow);

    // ========= 批量 & LLM 导入区 =========
    const batchBox = document.createElement("div");
    batchBox.style.margin = ".6rem 0 .9rem";
    batchBox.style.padding = ".65rem";
    batchBox.style.border = "1px dashed var(--border, #ddd)";
    batchBox.style.borderRadius = ".6rem";
    batchBox.style.backgroundColor = "var(--surface-alt, #fafafa)";
    batchBox.hidden = true;

    const batchRow1 = document.createElement("div");
    batchRow1.style.display = "flex";
    batchRow1.style.alignItems = "center";
    batchRow1.style.gap = ".5rem";
    batchRow1.style.marginBottom = ".4rem";

    const llmBtn = document.createElement("button");
    llmBtn.type = "button";
    llmBtn.className = "primary-action";
    llmBtn.textContent = "AI 生成 10 对新词";
    llmBtn.style.padding = ".3rem .75rem";
    llmBtn.addEventListener("click", async () => {
      llmBtn.disabled = true;
      try {
        const res = await endpoint("POST", "undercover_words/llm_generate_batch", {
          count: 10,
        });
        const items = res?.items || [];
        const skipped = res?.skipped || 0;
        if (tableWrap && !tableWrap.hidden) {
          renderUndercoverWords(items, tbody, countLabel);
        } else {
          loadUndercoverWords(tbody, countLabel);
        }
        showToast(`AI 生成完成：新增 ${res?.added || 0} 对，跳过重复 ${skipped} 对`);
      } catch (err) {
        showToast(err?.message || "AI 生成失败，稍后重试");
      } finally {
        llmBtn.disabled = false;
      }
    });

    const batchHint = document.createElement("small");
    batchHint.style.color = "var(--muted, #666)";
    batchHint.textContent =
      "每行一对词条，格式：词1 词2（空格/逗号/冒号均可分隔）。也可直接点上面按钮让 AI 批量生成。";
    batchRow1.append(llmBtn, batchHint);
    batchBox.appendChild(batchRow1);

    const textarea = document.createElement("textarea");
    textarea.rows = 4;
    textarea.placeholder =
      "示例：\n可乐 雪碧\n苹果, 梨\n咖啡：奶茶\n口红 唇釉";
    textarea.style.width = "100%";
    textarea.style.boxSizing = "border-box";
    textarea.style.resize = "vertical";
    textarea.style.padding = ".45rem .55rem";
    textarea.style.borderRadius = ".5rem";
    textarea.style.border = "1px solid var(--border, #ddd)";
    textarea.style.font = "inherit";
    textarea.style.color = "var(--text, #222)";
    textarea.style.background = "var(--surface-solid, #fff)";
    batchBox.appendChild(textarea);

    const batchActRow = document.createElement("div");
    batchActRow.style.display = "flex";
    batchActRow.style.justifyContent = "flex-end";
    batchActRow.style.gap = ".4rem";
    batchActRow.style.marginTop = ".4rem";
    const batchBtn = document.createElement("button");
    batchBtn.type = "button";
    batchBtn.className = "primary-action";
    batchBtn.textContent = "批量添加";
    batchBtn.style.padding = ".3rem .85rem";
    batchBtn.addEventListener("click", async () => {
      const text = textarea.value.trim();
      if (!text) {
        showToast("请先粘贴词条内容");
        return;
      }
      batchBtn.disabled = true;
      try {
        const res = await endpoint("POST", "undercover_words/batch_import", {
          text,
        });
        if (tableWrap && !tableWrap.hidden) {
          renderUndercoverWords(res?.items || [], tbody, countLabel);
        } else {
          loadUndercoverWords(tbody, countLabel);
        }
        const added = res?.added_count || 0;
        const skippedCount = res?.skipped_count || 0;
        const skipped = res?.skipped || [];
        // toast 为单行短提示，跳过多时只带首条原因，避免溢出
        const msg =
          `批量完成：新增 ${added} 对，跳过 ${skippedCount} 对` +
          (skipped.length > 0 ? `（如：${skipped[0]}）` : "");
        showToast(msg);
        textarea.value = "";
      } catch (err) {
        showToast(err?.message || "批量添加失败");
      } finally {
        batchBtn.disabled = false;
      }
    });
    batchActRow.appendChild(batchBtn);
    batchBox.appendChild(batchActRow);
    wrap.appendChild(batchBox);

    // 同步折叠显示
    const originToggle = open.addEventListener; // noop
    // 让 batchBox 和 tableWrap/addRow 同步展开/收起
    (function patchToggle() {
      const prev = open.onclick;
      open.addEventListener("click", () => {
        batchBox.hidden = addRow.hidden;
      });
    })();

    const tableWrap = document.createElement("div");
    tableWrap.style.maxHeight = "340px";
    tableWrap.style.overflow = "auto";
    tableWrap.style.border = "1px solid var(--border, #ddd)";
    tableWrap.style.borderRadius = ".6rem";
    tableWrap.hidden = true;

    const table = document.createElement("table");
    table.className = "uc-word-table";
    table.style.width = "100%";
    table.style.borderCollapse = "collapse";
    table.style.fontSize = ".88rem";

    const thead = document.createElement("thead");
    thead.innerHTML =
      '<tr style="background: var(--surface-alt, #f7f7f7)">' +
      "<th>#</th><th>词条 1</th><th>词条 2</th><th>操作</th></tr>";
    thead.querySelectorAll("th").forEach((th) => {
      th.style.padding = ".4rem .65rem";
      th.style.textAlign = "left";
      th.style.borderBottom = "1px solid var(--border, #ddd)";
      th.style.color = "var(--text-strong, #222)";
    });
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    table.appendChild(tbody);
    tableWrap.appendChild(table);
    wrap.appendChild(tableWrap);

    return wrap;
  }

  async function loadUndercoverWords(tbody, countLabel) {
    try {
      const res = await endpoint("GET", "undercover_words");
      renderUndercoverWords(res?.items || [], tbody, countLabel);
    } catch (err) {
      showToast(err?.message || "读取词库失败");
    }
  }

  function renderUndercoverWords(items, tbody, countLabel) {
    tbody.replaceChildren();
    items.forEach((item) => {
      const tr = document.createElement("tr");
      tr.style.borderBottom = "1px dashed var(--border, #eee)";
      const tdId = document.createElement("td");
      tdId.textContent = String(item.id);
      const td1 = document.createElement("td");
      td1.textContent = String(item.word1 || "");
      const td2 = document.createElement("td");
      td2.textContent = String(item.word2 || "");
      const tdAct = document.createElement("td");
      const del = document.createElement("button");
      del.type = "button";
      del.className = "text-button";
      del.textContent = "删除";
      del.style.color = "var(--red, #c6373a)";
      del.addEventListener("click", async () => {
        const ok = await confirmAction({
          title: "删除词条对",
          message: `确认删除词条对「${item.word1} / ${item.word2}」？`,
          label: "确认删除",
          danger: true,
        });
        if (!ok) return;
        try {
          const res = await endpoint("POST", "undercover_words/delete", { id: item.id });
          renderUndercoverWords(res?.items || [], tbody, countLabel);
          showToast("已删除");
        } catch (err) {
          showToast(err?.message || "删除失败");
        }
      });
      tdAct.appendChild(del);
      [tdId, td1, td2, tdAct].forEach((td) => {
        td.style.padding = ".4rem .65rem";
        td.style.color = "var(--text, #222)";
      });
      tr.append(tdId, td1, td2, tdAct);
      tbody.appendChild(tr);
    });
    if (countLabel) countLabel.textContent = `当前共 ${items.length} 对`;
  }

  function resetGameSettings(gameType) {
    const card = document.querySelector(`.game-setting-card[data-game="${gameType}"]`);
    if (!card) return;
    const enabled = card.querySelector('[data-setting="enabled"]');
    enabled.checked = true;
    enabled.dispatchEvent(new Event("change"));
    card.querySelectorAll("[data-default]").forEach((input) => {
      const value = JSON.parse(input.dataset.default || "null");
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = value ?? "";
    });
    showToast("已恢复默认值，点击保存后生效");
  }

  function collectSettings() {
    const games = {};
    document.querySelectorAll("#gameSettingsGrid [data-game][data-setting]").forEach((input) => {
      const gameType = input.dataset.game;
      games[gameType] ||= {};
      let value = input.value;
      if (input.dataset.type === "bool") value = input.checked;
      if (input.dataset.type === "int") {
        if (!input.reportValidity()) throw new Error("请修正超出范围的数值");
        value = Number(input.value);
      }
      games[gameType][input.dataset.setting] = value;
    });
    return { games };
  }

  async function loadSettings() {
    const action = document.getElementById("reloadSettingsAction");
    action.disabled = true;
    try {
      renderSettings(await endpoint("GET", "settings"));
    } catch (error) {
      showToast(error?.message || "无法读取游戏配置");
    } finally {
      action.disabled = false;
    }
  }

  async function saveSettings() {
    const action = document.getElementById("saveSettingsAction");
    action.disabled = true;
    try {
      const data = await endpoint("POST", "settings/update", collectSettings());
      renderSettings(data);
      showToast("游戏配置已保存");
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "游戏配置保存失败");
    } finally {
      action.disabled = false;
    }
  }

  function showPanel(panel) {
    const settings = panel === "settings";
    const stats = panel === "stats";
    const library = panel === "library";
    document.getElementById("roomsPanel").hidden = settings || stats || library;
    document.getElementById("settingsPanel").hidden = !settings;
    document.getElementById("statsPanel").hidden = !stats;
    document.getElementById("libraryPanel").hidden = !library;
    document.querySelectorAll(".manager-tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.panel === panel);
      tab.setAttribute("aria-selected", String(tab.dataset.panel === panel));
    });
    if (settings) loadSettings();
    if (stats) loadStats();
    if (library) loadLibrary();
  }

  // ===== 战绩管理 =====
  let statsLeaderboard = {};   // game_type -> [{name,qq,wins,games,winrate}]
  let statsFilter = "";        // 当前玩法筛选
  let statsWindow = 0;         // 统计时间范围（天，0 = 全部时间）
  let playerProfiles = [];     // 玩家档案（按 QQ 汇总）
  let playerKeyword = "";      // 玩家搜索词（昵称 / QQ）
  let playerSummary = {};      // 玩家概况（人数 / 总局数 / 今日活跃）
  let playerSort = "games";    // 玩家档案排序：games / wins / winrate / recent / name
  let showFullQQ = false;      // QQ 默认打码，需要时再展开

  const GAME_LABELS = {
    gomoku: "五子棋", xiangqi: "中国象棋", weiqi: "围棋", tictactoe: "井字棋",
    othello: "黑白棋", turtle_soup: "海龟汤", pig_dice: "贪心骰子",
    draw_guess: "你画我猜", blackjack: "二十一点", undercover: "谁是卧底",
  };
  function gameLabel(gtype) { return GAME_LABELS[gtype] || gtype || "未知玩法"; }

  /** QQ 打码：管理台常出现在录屏/截图里，默认不整串露出来 */
  function displayQQ(qq) {
    const value = String(qq || "").trim();
    if (!value) return "—";
    if (showFullQQ || value.length <= 4) return value;
    return `${value.slice(0, 3)}${"*".repeat(Math.max(2, value.length - 5))}${value.slice(-2)}`;
  }

  /** 「x 胜 / y 负 / z 局」，平局非零时补一句 */
  function recordText(row) {
    const wins = Number(row.wins) || 0;
    const losses = Number(row.losses) || 0;
    const games = Number(row.games) || 0;
    const draws = Number(row.draws) || 0;
    const base = `${wins} 胜 / ${losses} 负 / ${games} 局`;
    return draws ? `${base}（含 ${draws} 平）` : base;
  }

  const PLAYER_SORT_KEYS = {
    games: (player) => Number(player.games) || 0,
    wins: (player) => Number(player.wins) || 0,
    winrate: (player) => Number(player.winrate) || 0,
    recent: (player) => Number(player.last_at) || 0,
    name: (player) => 0,
  };

  function sortPlayerRows(rows) {
    const key = PLAYER_SORT_KEYS[playerSort] || PLAYER_SORT_KEYS.games;
    if (playerSort === "name") {
      return rows
        .slice()
        .sort((a, b) => String(a.name || "").localeCompare(String(b.name || ""), "zh-CN"));
    }
    return rows.slice().sort((a, b) => key(b) - key(a));
  }

  function campSummary(camps) {
    if (!camps || typeof camps !== "object") return "";
    const labels = { civilian: "平民", undercover: "卧底", whiteboard: "白板" };
    const parts = [];
    Object.entries(labels).forEach(([key, label]) => {
      const value = Number(camps[key] || 0);
      if (value) parts.push(`${label} ${value}`);
    });
    return parts.length ? `卧底阵营胜场：${parts.join(" · ")}` : "";
  }

  async function loadStats() {
    const select = document.getElementById("statsGameSelect");
    if (select.options.length === 1) {
      // 首次进入：用已知玩法填充筛选下拉
      Object.keys(GAME_LABELS).forEach((g) => {
        const opt = document.createElement("option");
        opt.value = g;
        opt.textContent = gameLabel(g);
        select.appendChild(opt);
      });
    }
    select.value = statsFilter;
    const windowSelect = document.getElementById("statsWindowSelect");
    if (windowSelect) windowSelect.value = String(statsWindow);
    try {
      const payload = {};
      if (statsFilter) payload.game_type = statsFilter;
      if (statsWindow) payload.days = statsWindow;
      const data = await endpoint("POST", "leaderboard", payload);
      statsLeaderboard = data?.leaderboard || {};
      renderStats();
    } catch (error) {
      showToast(error?.message || "无法读取战绩排行");
    }
    try {
      const data = await endpoint("POST", "players", {});
      playerProfiles = data?.players || [];
      renderPlayers(data?.summary || {});
    } catch (error) {
      showToast(error?.message || "无法读取玩家档案");
    }
    try {
      document.getElementById("operationLogBody").replaceChildren();
      const log = await endpoint("POST", "operation_log", { limit: 200 });
      renderStatsLog(log?.rows || []);
    } catch (error) {
      showToast(error?.message || "无法读取操作日志");
    }
  }

  function renderPlayers(summary) {
    if (summary) playerSummary = summary;
    const body = document.getElementById("playerBody");
    body.replaceChildren();
    const setText = (id, text) => {
      const node = document.getElementById(id);
      if (node) node.textContent = text;
    };
    const totalGames = Number(playerSummary.games || 0);
    const totalWins = Number(playerSummary.wins || 0);
    setText("playerTotal", String(playerSummary.players ?? playerProfiles.length));
    setText("playerGames", String(totalGames));
    setText("playerWins", String(totalWins));
    setText("playerActiveToday", String(playerSummary.active_today ?? 0));
    setText("playerActiveDay", playerSummary.today ? `截至 ${playerSummary.today}` : "—");
    setText(
      "playerGamesHint",
      totalGames ? `平均胜率 ${Math.round((totalWins * 100) / totalGames)}%` : "—",
    );
    setText("playerWinsHint", totalGames ? `共 ${totalGames} 局` : "—");

    const keyword = playerKeyword.trim().toLowerCase();
    const filtered = keyword
      ? playerProfiles.filter(
          (p) =>
            String(p.qq || "").includes(keyword) ||
            String(p.name || "").toLowerCase().includes(keyword),
        )
      : playerProfiles;
    const rows = sortPlayerRows(filtered);

    const empty = document.getElementById("playerEmpty");
    if (empty) empty.hidden = rows.length > 0;
    const summaryNode = document.getElementById("playerSummary");
    if (summaryNode) {
      summaryNode.textContent = rows.length
        ? `共 ${rows.length} 位玩家${keyword ? "（已筛选）" : ""} · 点表头可换排序，右侧图标可改名 / 清零 / 合并 / 删除`
        : "按 QQ 汇总所有玩法的战绩";
    }

    rows.slice(0, 300).forEach((player) => {
      const tr = document.createElement("tr");

      const nameTd = document.createElement("td");
      const nameWrap = document.createElement("div");
      nameWrap.className = "player-name";
      nameWrap.appendChild(createText("span", player.name || "未命名"));
      const camp = campSummary(player.camps);
      if (camp) nameWrap.appendChild(createText("small", camp));
      nameTd.appendChild(nameWrap);

      const qqTd = createText("td", displayQQ(player.qq), "qq-cell");
      qqTd.title = showFullQQ ? "" : "已打码，勾选「显示完整 QQ」可查看";
      const statTd = createText("td", recordText(player), "wins-cell");
      const rateTd = createText(
        "td",
        `${Math.round(Number(player.winrate) || 0)}%`,
        "rank-cell",
      );
      const gamesTd = document.createElement("td");
      gamesTd.className = "player-games";
      const played = (player.games_played || []).slice(0, 4);
      played.forEach((gtype) => gamesTd.appendChild(createText("span", gameLabel(gtype))));
      if ((player.games_played || []).length > played.length) {
        gamesTd.appendChild(createText("span", `等 ${player.games_played.length} 种`));
      }
      const activeTd = createText(
        "td",
        player.last_at_text
          ? `${player.last_at_text}${player.active_days ? ` · ${player.active_days} 天` : ""}`
          : player.last_active
            ? `${player.last_active} · ${Number(player.active_days) || 0} 天`
            : "—",
      );

      const actTd = document.createElement("td");
      actTd.className = "actions-column";
      actTd.append(
        rowIcon("pen-line", "修改显示名称", () => renamePlayer(player)),
        rowIcon("eraser", "清零战绩（保留档案）", () => resetPlayerStats(player)),
        rowIcon("git-merge", "把另一个 QQ 合并到这位玩家", () => mergePlayerInto(player)),
        rowIcon("trash-2", "删除这位玩家的战绩记录", () => deletePlayerStats(player), true),
      );
      tr.append(nameTd, qqTd, statTd, rateTd, gamesTd, activeTd, actTd);
      body.appendChild(tr);
    });
    icons();
  }

  async function playerAction(payload, fallback) {
    try {
      const data = await endpoint("POST", "player_action", payload);
      showToast(data?.message || fallback);
      await loadStats();
      await loadRooms();
      return true;
    } catch (error) {
      showToast(error?.message || "操作失败");
      return false;
    }
  }

  async function renamePlayer(player) {
    const name = await openPrompt({
      title: "修改显示名称",
      message: `${player.name || "这位玩家"}（${displayQQ(player.qq)}）在所有玩法里都会改成新名字，胜场仍然跟着这个 QQ 走。`,
      label: "显示名称",
      initial: player.name || "",
      kind: "text",
    });
    if (name === null) return;
    await playerAction({ action: "rename", qq: player.qq, name }, "改名成功");
  }

  async function resetPlayerStats(player) {
    if (!await confirmAction({
      title: "清零这位玩家的战绩",
      message: `把 ${player.name || player.qq} 在所有玩法的胜场与局数清零？档案和昵称会保留，此操作不可撤销。`,
      label: "确认清零",
      danger: true,
    })) return;
    await playerAction({ action: "reset", qq: player.qq }, "已清零");
  }

  async function mergePlayerInto(player) {
    const sourceQQ = await openPrompt({
      title: "合并玩家档案",
      message: `把另一个 QQ 的战绩并入「${player.name || player.qq}」，并入后那个 QQ 的记录会被删除（适合同一个人换号的情况）。`,
      label: "要并入的 QQ",
      initial: "",
      kind: "text",
    });
    if (!sourceQQ) return;
    if (!await confirmAction({
      title: "确认合并",
      message: `把 QQ ${sourceQQ} 的胜场、局数与活跃记录并入 ${displayQQ(player.qq)}？此操作不可撤销。`,
      label: "确认合并",
      danger: true,
    })) return;
    await playerAction({ action: "merge", qq: player.qq, source_qq: sourceQQ }, "已合并");
  }

  async function deletePlayerStats(player) {
    if (!await confirmAction({
      title: "删除玩家战绩",
      message: `彻底删除 ${player.name || player.qq} 的全部战绩记录（含卧底阵营胜场）？档案会从列表里消失，此操作不可撤销。`,
      label: "确认删除",
      danger: true,
    })) return;
    await playerAction({ action: "delete", qq: player.qq }, "已删除");
  }

  async function pruneEmptyPlayers() {
    if (!await confirmAction({
      title: "清理空档案",
      message: "删除所有「0 局 0 胜」的玩家档案？只会清掉空记录，有战绩的不受影响。",
      label: "确认清理",
    })) return;
    await playerAction({ action: "prune" }, "已清理");
  }

  function exportPlayerCsv() {
    if (!playerProfiles.length) {
      showToast("暂无玩家档案可导出");
      return;
    }
    const header = ["昵称", "QQ", "胜场", "负场", "平局", "总局数", "胜率(%)", "玩过玩法", "最近活跃", "活跃天数"];
    const lines = [header.join(",")];
    playerProfiles.forEach((player) => {
      const cells = [
        player.name || "",
        player.qq || "",
        Number(player.wins) || 0,
        Number(player.losses) || 0,
        Number(player.draws) || 0,
        Number(player.games) || 0,
        Number(player.winrate) || 0,
        (player.games_played || []).map(gameLabel).join(" / "),
        player.last_at_text || player.last_active || "",
        Number(player.active_days) || 0,
      ].map((cell) => {
        const text = String(cell ?? "");
        return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
      });
      lines.push(cells.join(","));
    });
    const blob = new Blob([`\ufeff${lines.join("\r\n")}`], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `玩家档案_${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
    showToast(`已导出 ${playerProfiles.length} 位玩家的档案`);
  }

  function renderStats() {
    const body = document.getElementById("leaderboardBody");
    body.replaceChildren();
    const rows = [];
    Object.entries(statsLeaderboard).forEach(([gtype, list]) => {
      (list || []).forEach((row, idx) => {
        rows.push({
          game_type: gtype,
          name: row.name,
          qq: row.qq || "",
          is_ai: Boolean(row.is_ai),
          wins: Number(row.wins) || 0,
          games: Number(row.games) || 0,
          draws: Number(row.draws) || 0,
          losses: Number(row.losses) || 0,
          winrate: Number(row.winrate) || 0,
          rank: idx + 1,
        });
      });
    });
    document.getElementById("leaderboardEmpty").hidden = rows.length > 0;
    document.getElementById("leaderboardSummary").textContent = rows.length
      ? `${rows.length} 条记录${statsWindow ? ` · 最近 ${statsWindow} 天` : ""}`
      : "暂无战绩";
    rows.slice(0, 200).forEach((row) => {
      const tr = document.createElement("tr");
      const rankTd = createText("td", String(row.rank));
      rankTd.className = "rank-cell";
      const nameTd = createText("td", row.name);
      const qqTd = createText("td", row.is_ai ? "—（花火）" : displayQQ(row.qq), "qq-cell");
      const gameTd = createText("td", gameLabel(row.game_type));
      const winsTd = createText(
        "td",
        row.games
          ? `${row.wins} 胜 / ${row.losses} 负 / ${row.games} 局${row.draws ? `（含 ${row.draws} 平）` : ""} · ${row.winrate.toFixed(0)}%`
          : `${row.wins} 胜`,
      );
      winsTd.className = "wins-cell";
      const actTd = document.createElement("td");
      actTd.className = "actions-column";
      if (!row.is_ai) {
        actTd.appendChild(
          rowIcon("sliders-horizontal", "修正胜场 / 局数", () => editWins(row.game_type, row)),
        );
        actTd.appendChild(
          rowIcon("eraser", "只清零这个玩法", () => resetGameStats(row.game_type, row), true),
        );
      }
      tr.append(rankTd, nameTd, qqTd, gameTd, winsTd, actTd);
      body.appendChild(tr);
    });
    icons();
  }

  async function editWins(gameType, row) {
    const games = await openPrompt({
      title: "修正总局数",
      message: `${row.name}（${displayQQ(row.qq)}）在《${gameLabel(gameType)}》当前 ${row.wins} 胜 / ${row.games} 局，先填要修正到多少局。`,
      label: "总局数",
      initial: String(Number(row.games) || 0),
    });
    if (games === null) return;
    const wins = await openPrompt({
      title: "修正胜场",
      message: `总局数按 ${games} 计算，胜场不能超过它。`,
      label: "胜场",
      initial: String(Math.min(Number(row.wins) || 0, games)),
    });
    if (wins === null) return;
    if (wins > games) {
      showToast("胜场不能大于总局数");
      return;
    }
    await playerAction(
      { action: "set_stats", qq: row.qq, game_type: gameType, wins, games },
      "战绩已修正",
    );
  }

  async function resetGameStats(gameType, row) {
    if (!await confirmAction({
      title: "清零该玩法的战绩",
      message: `把 ${row.name}（${displayQQ(row.qq)}）在《${gameLabel(gameType)}》的胜场与局数清零？其它玩法不受影响。`,
      label: "确认清零",
      danger: true,
    })) return;
    await playerAction(
      { action: "reset", qq: row.qq, game_type: gameType },
      "已清零该玩法",
    );
  }

  function renderStatsLog(rows) {
    const body = document.getElementById("operationLogBody");
    body.replaceChildren();
    document.getElementById("logEmpty").hidden = rows.length > 0;
    document.getElementById("logSummary").textContent = rows.length ? `最近 ${rows.length} 条` : "暂无操作日志";
    rows.forEach((row) => {
      const tr = document.createElement("tr");
      tr.appendChild(createText("td", row.time || ""));
      tr.appendChild(createText("td", actionLabel(row.action || "")));
      tr.appendChild(createText("td", row.operator || "—"));
      tr.appendChild(createText("td", row.room_id || "—"));
      tr.appendChild(createText("td", row.detail || ""));
      body.appendChild(tr);
    });
  }

  function actionLabel(action) {
    return {
      // 管理台动作
      create_room: "开房", close_room: "关闭房间", switch_game: "切换游戏",
      assign: "安排玩家", demote: "移出玩家", kick: "踢出",
      set_difficulty: "调整棋力", pause_room: "暂停对局", resume_room: "继续对局",
      clear_leaderboard: "清空排行榜", set_wins: "修正胜场",
      rename_player: "修改玩家昵称", set_player_stats: "修正玩家战绩",
      reset_player: "清零玩家战绩", delete_player: "删除玩家战绩",
      merge_player: "合并玩家档案",
      // 房间内动作
      room_claim: "落座", room_start: "开始对局", room_seat_leave: "离席",
      room_leave: "离开房间", room_seat_swap: "交换座位", room_ai_seat: "花火补位",
      room_rematch: "再来一局", room_resign: "认输",
      room_difficulty: "调整棋力", room_side_rotation: "换先设置",
      room_uc_ai: "花火玩家", room_uc_scales: "阵营比例",
      room_uc_reveal_identity: "告知身份", room_uc_words: "导入词条",
      room_announce: "通知到群",
    }[action] || action || "未知";
  }

  async function clearStatsLeaderboard() {
    const scope = statsFilter ? `《${gameLabel(statsFilter)}》` : "全部玩法";
    if (!await confirmAction({
      title: "清空战绩排行",
      message: `确认清空${scope}的全局战绩排行榜？此操作不可撤销，房间右侧“本房战绩排行”也会同步清除。`,
      label: "确认清空",
      danger: true,
    })) return;
    const action = document.getElementById("statsClearAction");
    action.disabled = true;
    try {
      const result = await endpoint("POST", "leaderboard/clear", statsFilter ? { game_type: statsFilter } : {});
      showToast(result?.cleared ? `已清空${scope}战绩` : "没有可清空的排行榜数据");
      await loadStats();
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "清空排行榜失败");
    } finally {
      action.disabled = false;
    }
  }

  async function exportPoster() {
    const selected = statsFilter ? { [statsFilter]: statsLeaderboard[statsFilter] || [] } : statsLeaderboard;
    const entries = [];
    Object.entries(selected).forEach(([gtype, list]) => {
      (list || []).forEach((r) => entries.push({ name: r.name, wins: Number(r.wins) || 0, game_type: gtype }));
    });
    if (!entries.length) { showToast("暂无战绩可导出"); return; }
    try {
      const url = statsPosterDataUrl(entries);
      const a = document.createElement("a");
      a.href = url;
      a.download = `战绩海报_${new Date().toISOString().slice(0, 10)}.png`;
      a.click();
    } catch (error) {
      showToast(error?.message || "导出海报失败");
    }
  }

  // 用 Canvas 把战绩绘制成一张 PNG 海报（不依赖后端）
  function statsPosterDataUrl(entries) {
    const W = 720, M = 28, rowH = 46, padTop = 150, headerH = 44, footerH = 66;
    const nRows = Math.min(entries.length, 200);
    const height = padTop + headerH + nRows * rowH + footerH;
    const cv = document.createElement("canvas");
    cv.width = W; cv.height = height;
    const c = cv.getContext("2d");
    const font = (bold, px) => `${bold ? "bold " : ""}${px}px 'PingFang SC','Microsoft YaHei',sans-serif`;
    // 背景
    const bg = c.createLinearGradient(0, 0, W, height);
    bg.addColorStop(0, "#1e2c3f"); bg.addColorStop(1, "#121d29");
    c.fillStyle = bg; c.fillRect(0, 0, W, height);
    // 顶部金色横幅
    const hg = c.createLinearGradient(0, 0, W, padTop);
    hg.addColorStop(0, "#3a2f18"); hg.addColorStop(1, "#241d0f");
    c.fillStyle = hg; c.fillRect(0, 0, W, padTop);
    c.fillStyle = "#f0c050"; c.fillRect(0, padTop - 4, W, 4);
    c.textAlign = "left";
    c.font = font(false, 20); c.fillStyle = "#e8c56d"; c.fillText("🏆 花火陪你玩 · 战绩榜", M, 46);
    c.font = font(true, 34); c.fillStyle = "#ffffff";
    c.fillText("游戏战绩排行榜", M, 96);
    c.font = font(false, 15); c.fillStyle = "#b8a878";
    c.fillText(`导出时间：${new Date().toLocaleString()}`, M, 124);
    // 表头
    c.fillStyle = "rgba(255,255,255,.08)"; c.fillRect(0, padTop, W, headerH);
    c.fillStyle = "#8fa8c4"; c.font = font(true, 15); c.textAlign = "left";
    ["排名", "玩家", "玩法", "胜场"].forEach((t, i) => {
      const x = i === 0 ? rankCol() : i === 1 ? nameCol() : i === 2 ? gameCol() : winsCol();
      c.fillText(t, x, padTop + 28);
    });
    // 数据行（从表头下方开始，避免第一行压住表头）
    const list = entries.slice(0, nRows);
    const rowTop = padTop + headerH;
    list.forEach((entry, i) => {
      const y = rowTop + i * rowH;
      if (i % 2 === 0) { c.fillStyle = "rgba(255,255,255,.03)"; c.fillRect(0, y, W, rowH); }
      // 排名徽章
      const isTop = i < 3;
      c.fillStyle = isTop ? "#f0c050" : "rgba(255,255,255,.12)";
      c.beginPath(); c.arc(rankCol() + 15, y + 23, 16, 0, Math.PI * 2); c.fill();
      c.fillStyle = isTop ? "#1c1c1c" : "#dfe9f5";
      c.textAlign = "center"; c.font = font(true, 15);
      c.fillText(isTop ? ["🥇","🥈","🥉"][i] : String(i + 1), rankCol() + 15, y + 28);
      c.textAlign = "left";
      // 玩家名
      c.fillStyle = "#e8eef5"; c.font = font(false, 16);
      c.fillText(truncatePosterText(c, entry.name, 260), nameCol(), y + 30);
      // 玩法
      c.fillStyle = "#9fc4ec"; c.font = font(false, 14);
      c.fillText(gameLabel(entry.game_type), gameCol(), y + 30);
      // 胜场
      c.fillStyle = "#ffd166"; c.font = font(true, 16);
      c.fillText(`${entry.wins} 胜`, winsCol(), y + 30);
    });
    c.fillStyle = "#6d85a3"; c.font = font(false, 14); c.textAlign = "center";
    c.fillText("—— 由 花火陪你玩 生成 ——", W / 2, height - 30);
    return cv.toDataURL("image/png");
  }

  function rankCol() { return 32; }
  function nameCol() { return 96; }
  function gameCol() { return 400; }
  function winsCol() { return 620; }

  function truncatePosterText(ctx, text, max) {
    if (ctx.measureText(text).width <= max) return text;
    let t = text;
    while (t.length && ctx.measureText(t + "…").width > max) t = t.slice(0, -1);
    return t + "…";
  }
  // ===== 题库管理 =====
  const CONTENT_LEVEL_LABELS = { all_ages: "全年龄", normal: "普通", unrestricted: "不限制" };
  const SOUP_SOURCE_LABELS = {
    library: "内置题库", online: "网络题库", llm: "模型生成",
    custom: "玩家投稿", fallback: "兜底",
  };

  function formatStamp(seconds) {
    const value = Number(seconds || 0);
    if (!value) return "—";
    return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  async function loadLibrary() {
    try {
      renderLibrary(await endpoint("GET", "soup_library"));
      // 后台可能还在出题：每次进页面都问一次进度，生成中再开始轮询
      pollSoupGenerate();
    } catch (error) {
      showToast(error?.message || "无法读取题库信息");
    }
  }

  const libraryState = {
    data: null,
    tab: "builtin",
    builtin: { search: "", level: "all", state: "all", page: 1, pageSize: 40 },
    custom: { search: "" },
    generated: { search: "", level: "all", page: 1, pageSize: 40 },
  };

  function textMatch(haystack, needle) {
    return String(haystack || "").toLowerCase().includes(needle);
  }

  function renderLibrary(data) {
    libraryState.data = data;
    const builtin = data?.builtin || {};
    const used = data?.used || {};
    const custom = data?.custom || {};
    const generated = data?.generated || {};
    const levels = builtin.by_level || {};
    const overrides = builtin.overrides || {};

    document.getElementById("libTotal").textContent = String(builtin.available ?? builtin.total ?? 0);
    document.getElementById("libLevels").textContent =
      `全年龄 ${levels.all_ages ?? 0} · 普通 ${levels.normal ?? 0} · 不限制 ${levels.unrestricted ?? 0}`
      + (builtin.disabled ? ` · 停用 ${builtin.disabled}` : "");
    document.getElementById("libGenerated").textContent = String(generated.total ?? 0);
    document.getElementById("libGeneratedHint").textContent = generated.total
      ? `待用 ${generated.unused ?? 0} 道 · 可复用 ${generated.available ?? 0} 道${generated.disabled ? ` · 停用 ${generated.disabled}` : ""}`
      : "模型出题后自动入库";
    document.getElementById("libCustom").textContent = String(custom.total ?? 0);
    document.getElementById("libCustomLimit").textContent = custom.limit
      ? `上限 ${custom.limit} 道`
      : "";
    document.getElementById("libUsed").textContent = String(used.total ?? 0);
    const bySource = used.by_source || {};
    document.getElementById("libUsedSources").textContent = Object.keys(bySource).length
      ? Object.entries(bySource)
          .map(([key, value]) => `${SOUP_SOURCE_LABELS[key] || key} ${value}`)
          .join(" · ")
      : "还没出过题";

    document.getElementById("tabBuiltinCount").textContent = String(builtin.total ?? 0);
    document.getElementById("tabCustomCount").textContent = String(custom.total ?? 0);
    document.getElementById("tabGeneratedCount").textContent = String(generated.total ?? 0);

    const builtinSummary = document.getElementById("builtinSummary");
    if (builtinSummary) {
      const bits = [`可抽 ${builtin.available ?? 0} / 共 ${builtin.total ?? 0} 道`];
      if (overrides.disabled) bits.push(`停用 ${overrides.disabled}`);
      if (overrides.edited) bits.push(`改过 ${overrides.edited}`);
      if (overrides.removed) bits.push(`删除 ${overrides.removed}`);
      builtinSummary.textContent = `${bits.join(" · ")} · 汤底默认打码`;
    }
    const customSummary = document.getElementById("customSummary");
    if (customSummary) {
      customSummary.textContent = custom.total
        ? `共 ${custom.total} 道 · 汤底默认打码，点眼睛查看`
        : "私聊发「添加海龟汤 [全年龄] 汤面|汤底」即可投稿";
    }
    const generatedSummary = document.getElementById("generatedSummary");
    if (generatedSummary) {
      generatedSummary.textContent = generated.total
        ? `共 ${generated.total} 道 · ${Object.keys(generated.by_model || {}).length} 个模型出的题 · 下一局优先复用 · 汤底默认打码`
        : "模型出过并通过校验的题会自动存进这里，下一局优先复用，不必每次重编";
    }

    renderBuiltinTable();
    renderCustomTable(custom);
    renderGeneratedTable(generated);
    renderGenerateBar(data);
  }

  function renderGenerateBar(data) {
    const select = document.getElementById("soupGenerateModel");
    if (!select) return;
    const previous = select.value;
    const models = Array.isArray(data?.models) ? data.models : [];
    select.replaceChildren();
    const fallback = document.createElement("option");
    fallback.value = "";
    fallback.textContent = data?.default_model
      ? `默认（跟随配置：${data.default_model}）`
      : "默认（跟随配置）";
    select.appendChild(fallback);
    models.forEach((item) => {
      const option = document.createElement("option");
      option.value = String(item.id || "");
      option.textContent = String(item.label || item.id || "模型");
      select.appendChild(option);
    });
    select.value = models.some((item) => String(item.id) === previous) ? previous : "";
    const status = document.getElementById("soupGenerateStatus");
    if (!status) return;
    // 服务端状态优先：重进页面时本地还没有 job，靠快照把「生成中」恢复回来，
    // 否则按钮会先闪一下可点，让人以为任务没派出去。
    const restored = jobFromPayload({ job: data?.generate_job });
    if (restored || soupGenerateJob) renderGenerateJob(restored || soupGenerateJob);
    else status.textContent = models.length ? `已载入 ${models.length} 个模型` : "";
  }

  // 后台出题任务：点击后立刻返回，进度靠轮询；页面关掉不影响服务端继续出题
  let soupGenerateJob = null;
  let soupGeneratePolling = false;

  // 页面接口有时给整包 {status,message,data}，有时只给 data，这里都认
  function jobFromPayload(payload) {
    if (!payload) return null;
    if (payload.job) return payload.job;
    if (payload.data?.job) return payload.data.job;
    return payload.state ? payload : null;
  }

  function soupJobRunning(job) {
    return !!job && (job.running === true || job.state === "running");
  }

  function setGenerateBarBusy(running) {
    const bar = document.querySelector(".library-generate");
    if (bar) bar.classList.toggle("is-busy", running);
    ["soupGenerateModel", "soupGenerateLevel", "soupGenerateCount"].forEach((id) => {
      const node = document.getElementById(id);
      // 生成中锁住参数，避免看着像是「改一下就能换一批」
      if (node) node.disabled = running;
    });
  }

  function secondsSince(stamp) {
    const started = Number(stamp || 0);
    if (!started) return 0;
    return Math.max(0, Math.round(Date.now() / 1000 - started));
  }

  function renderGenerateJob(job) {
    soupGenerateJob = job && job.state && job.state !== "idle" ? job : null;
    const button = document.getElementById("soupGenerateBtn");
    const status = document.getElementById("soupGenerateStatus");
    const running = soupJobRunning(job);
    const model = job?.model || "模型";
    if (button) {
      button.disabled = running;
      button.title = running
        ? `${model} 正在后台出题，出完会提示`
        : "让选中的模型立即出一道题并入库";
      const label = button.querySelector("span");
      if (label) label.textContent = running ? "出题中…" : "立即出题";
      button.classList.toggle("is-running", running);
    }
    setGenerateBarBusy(running);
    if (!status) return;
    if (!job || job.state === "idle") {
      status.textContent = "";
      status.classList.remove("is-running", "is-done", "is-failed");
      return;
    }
    status.classList.toggle("is-running", running);
    status.classList.toggle("is-done", !running && job.state === "completed");
    status.classList.toggle("is-failed", !running && job.state === "failed");
    if (running) {
      const total = Math.max(1, Number(job.total || 1));
      const done = Number(job.done || 0);
      const elapsed = secondsSince(job.started_at);
      status.textContent =
        `后台生成中：已入库 ${done}/${total} · ${model}` +
        (elapsed ? ` · 已用 ${elapsed} 秒` : "");
    } else if (job.state === "completed") {
      const elapsed = Math.round(
        Math.max(0, Number(job.finished_at || 0) - Number(job.started_at || 0)),
      );
      status.textContent =
        `上次完成：入库 ${job.done || 0} 道 · ${model}` +
        (elapsed ? ` · 用时 ${elapsed} 秒` : "") +
        (job.error ? `（${job.error}）` : "");
    } else {
      status.textContent = `上次失败：${job.error || "未知原因"} · ${model}`;
    }
  }

  async function fetchSoupGenerateJob() {
    try {
      const api = await bridge();
      const result = await api.apiGet("page/soup_generate_status");
      return jobFromPayload(result);
    } catch (_error) {
      // 网络抖一下不该把页面卡住：下次进页面/切回来再查
      return null;
    }
  }

  async function pollSoupGenerate() {
    if (soupGeneratePolling) return;
    soupGeneratePolling = true;
    let misses = 0;
    try {
      for (;;) {
        const job = await fetchSoupGenerateJob();
        // 偶发网络抖动别把「生成中」误判成结束：本地还认为在跑就一直重试，
        // 只是把间隔拉长；拿到 idle 状态后按钮才会解锁。
        if (!job) {
          misses += 1;
          if (soupJobRunning(soupGenerateJob)) {
            await new Promise((resolve) =>
              window.setTimeout(resolve, misses < 3 ? 2500 : 10000),
            );
            continue;
          }
          break;
        }
        misses = 0;
        const wasRunning = soupJobRunning(soupGenerateJob);
        renderGenerateJob(job);
        if (soupJobRunning(job)) {
          await new Promise((resolve) => window.setTimeout(resolve, 2500));
          continue;
        }
        if (wasRunning) {
          if (job.state === "completed") {
            showToast(
              `出题完成，入库 ${job.done || 0} 道${job.error ? `（${job.error}）` : ""}`,
            );
          } else {
            showToast(job.error || "出题失败");
          }
          loadLibrary();
        }
        break;
      }
    } finally {
      soupGeneratePolling = false;
    }
  }

  // ---------------- 内置题库 ----------------

  function builtinRow(item) {
    const row = document.createElement("tr");
    row.appendChild(createText("td", String(item.index)));
    const surfaceCell = document.createElement("td");
    const surface = document.createElement("span");
    surface.className = "library-row-surface";
    surface.textContent = String(item.surface || "");
    surface.title = String(item.surface || "");
    surfaceCell.appendChild(surface);
    if (item.used) surfaceCell.appendChild(createText("span", "出过", "library-tag muted"));
    if (item.edited) surfaceCell.appendChild(createText("span", "已改", "library-tag"));
    if (item.disabled) surfaceCell.appendChild(createText("span", "已停用", "library-tag muted"));
    row.appendChild(surfaceCell);
    row.appendChild(createText("td", CONTENT_LEVEL_LABELS[item.content_level] || "普通"));
    row.appendChild(createText("td", item.disabled ? "不参与抽题" : "可用"));

    const actions = document.createElement("td");
    actions.className = "actions-column";
    actions.appendChild(rowIcon("eye", "查看答案", () => openSoupEditor("builtin", item.index, "view")));
    actions.appendChild(rowIcon("pencil", "编辑题目", () => openSoupEditor("builtin", item.index, "edit")));
    actions.appendChild(rowIcon(
      item.disabled ? "power" : "power-off",
      item.disabled ? "恢复参与抽题" : "停用（不删除）",
      () => soupLibraryAction("builtin", item.disabled ? "enable" : "disable", item.index),
    ));
    if (item.used) {
      actions.appendChild(rowIcon(
        "rotate-ccw", "单独重置这道题的出题记录（只重置这一条）",
        () => soupLibraryAction("builtin", "reset_used", item.index),
      ));
    }
    actions.appendChild(rowIcon(
      "trash-2", "删除这道题", () => deleteBuiltinSoup(item.index, item.surface), true,
    ));
    row.appendChild(actions);
    return row;
  }

  function renderBuiltinTable() {
    const items = libraryState.data?.builtin?.items || [];
    const state = libraryState.builtin;
    const search = state.search.trim().toLowerCase();
    const filtered = items.filter((item) => {
      if (state.level !== "all" && item.content_level !== state.level) return false;
      if (state.state === "disabled" && !item.disabled) return false;
      if (state.state === "available" && item.disabled) return false;
      if (state.state === "used" && !item.used) return false;
      if (!search) return true;
      return String(item.index) === search || textMatch(item.surface, search);
    });
    const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize));
    state.page = Math.min(Math.max(1, state.page), pages);
    const start = (state.page - 1) * state.pageSize;
    const pageItems = filtered.slice(start, start + state.pageSize);

    const body = document.getElementById("builtinBody");
    body.replaceChildren();
    pageItems.forEach((item) => body.appendChild(builtinRow(item)));
    document.getElementById("builtinEmpty").hidden = pageItems.length > 0;
    document.getElementById("builtinCount").textContent =
      `匹配 ${filtered.length} 道 · 第 ${start + (pageItems.length ? 1 : 0)}-${start + pageItems.length} 道`;
    document.getElementById("builtinPage").textContent = `${state.page} / ${pages}`;
    document.getElementById("builtinPrev").disabled = state.page <= 1;
    document.getElementById("builtinNext").disabled = state.page >= pages;
    icons();
  }

  // ---------------- 玩家投稿 ----------------

  function renderCustomTable(custom) {
    const body = document.getElementById("customBody");
    body.replaceChildren();
    const all = Array.isArray(custom.items) ? custom.items : [];
    const search = libraryState.custom.search.trim().toLowerCase();
    const items = search
      ? all.filter((item) => textMatch(item.surface, search) || textMatch(item.owner, search))
      : all;
    document.getElementById("customEmpty").hidden = items.length > 0;
    document.getElementById("customCountLabel").textContent =
      search ? `匹配 ${items.length} / 共 ${all.length} 道` : `共 ${all.length} 道`;
    items.forEach((item) => {
      const row = document.createElement("tr");
      row.appendChild(createText("td", String(item.index)));
      const surface = String(item.surface || "");
      const cell = document.createElement("td");
      const span = document.createElement("span");
      span.className = "library-row-surface";
      span.textContent = surface.length > 60 ? `${surface.slice(0, 60)}…` : surface;
      span.title = surface;
      cell.appendChild(span);
      if (item.used) cell.appendChild(createText("span", "出过", "library-tag muted"));
      row.appendChild(cell);
      row.appendChild(createText(
        "td", CONTENT_LEVEL_LABELS[item.content_level] || item.content_level || "普通",
      ));
      row.appendChild(createText("td", playerLabel(item.owner)));
      row.appendChild(createText("td", formatStamp(item.created_at)));
      const actions = document.createElement("td");
      actions.className = "actions-column";
      actions.appendChild(rowIcon("eye", "查看答案", () => openSoupEditor("custom", item.index, "view")));
      actions.appendChild(rowIcon("pencil", "编辑这条投稿", () => openSoupEditor("custom", item.index, "edit")));
      if (item.used) {
        actions.appendChild(rowIcon(
          "rotate-ccw", "单独重置这道投稿的出题记录",
          () => soupLibraryAction("custom", "reset_used", item.index),
        ));
      }
      actions.appendChild(rowIcon("trash-2", "删除这条投稿", () => deleteCustomSoup(item.index, surface), true));
      row.appendChild(actions);
      body.appendChild(row);
    });
    icons();
  }

  function playerLabel(owner) {
    const value = String(owner || "").trim();
    if (!value) return "匿名";
    return /^\d+$/.test(value) ? `QQ ${value}` : value;
  }

  // ---------------- 模型出题 ----------------

  function renderGeneratedTable(generated) {
    const items = Array.isArray(generated.items) ? generated.items : [];
    const state = libraryState.generated;
    const search = state.search.trim().toLowerCase();
    const filtered = items.filter((item) => {
      if (state.level !== "all" && item.content_level !== state.level) return false;
      if (!search) return true;
      return String(item.index) === search
        || textMatch(item.title, search)
        || textMatch(item.surface, search);
    });
    const pages = Math.max(1, Math.ceil(filtered.length / state.pageSize));
    state.page = Math.min(Math.max(1, state.page), pages);
    const start = (state.page - 1) * state.pageSize;
    const pageItems = filtered.slice(start, start + state.pageSize);

    const body = document.getElementById("generatedBody");
    body.replaceChildren();
    pageItems.forEach((item) => {
      const row = document.createElement("tr");
      row.appendChild(createText("td", String(item.index)));
      const cell = document.createElement("td");
      const title = document.createElement("span");
      title.className = "library-row-surface";
      title.textContent = String(item.title || item.surface || "");
      title.title = String(item.surface || "");
      cell.appendChild(title);
      if (item.disabled) cell.appendChild(createText("span", "已停用", "library-tag muted"));
      row.appendChild(cell);
      row.appendChild(createText("td", CONTENT_LEVEL_LABELS[item.content_level] || "普通"));
      const modelCell = document.createElement("td");
      const modelText = document.createElement("span");
      modelText.className = "library-model";
      modelText.textContent = String(item.model || "未记录");
      modelText.title = String(item.model || "未记录");
      modelCell.appendChild(modelText);
      row.appendChild(modelCell);
      row.appendChild(createText("td", formatStamp(item.created_at)));
      row.appendChild(createText(
        "td",
        item.disabled ? "不参与复用" : (item.last_used_at ? "已用过" : "待用"),
      ));
      const actions = document.createElement("td");
      actions.className = "actions-column";
      actions.appendChild(rowIcon("eye", "查看答案", () => openSoupEditor("generated", item.index, "view")));
      actions.appendChild(rowIcon("pencil", "编辑这道题", () => openSoupEditor("generated", item.index, "edit")));
      if (item.last_used_at) {
        actions.appendChild(rowIcon(
          "rotate-ccw", "单独重置这道题：恢复成待用",
          () => soupLibraryAction("generated", "reset_used", item.index),
        ));
      }
      actions.appendChild(rowIcon(
        item.disabled ? "power" : "power-off",
        item.disabled ? "恢复复用" : "停用（保留存档）",
        () => soupLibraryAction("generated", item.disabled ? "enable" : "disable", item.index),
      ));
      actions.appendChild(rowIcon(
        "trash-2", "删除这道题", () => deleteGeneratedSoup(item.index, item.title || item.surface), true,
      ));
      row.appendChild(actions);
      body.appendChild(row);
    });
    document.getElementById("generatedEmpty").hidden = pageItems.length > 0;
    document.getElementById("generatedCount").textContent =
      `匹配 ${filtered.length} 道 · 第 ${start + (pageItems.length ? 1 : 0)}-${start + pageItems.length} 道`;
    document.getElementById("generatedPage").textContent = `${state.page} / ${pages}`;
    document.getElementById("generatedPrev").disabled = state.page <= 1;
    document.getElementById("generatedNext").disabled = state.page >= pages;
    icons();
  }

  // ---------------- 通用 ----------------

  function rowIcon(name, title, handler, danger = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = danger ? "row-button danger" : "row-button";
    button.title = title;
    button.setAttribute("aria-label", title);
    button.innerHTML = `<i data-lucide="${name}"></i>`;
    button.addEventListener("click", handler);
    return button;
  }

  async function soupLibraryAction(kind, action, index, extra = {}) {
    try {
      const api = await bridge();
      const result = await api.apiPost("page/soup_library_action", { kind, action, index, ...extra });
      if (result?.status === "error") throw new Error(result.message || "操作失败");
      if (result?.message) showToast(result.message);
      if (result?.data?.builtin) renderLibrary(result.data);
      else loadLibrary();
    } catch (error) {
      showToast(error?.message || "操作失败");
    }
  }

  async function recoverAllBuiltinSoup() {
    const proceed = await confirmAction({
      title: "恢复全部改动",
      message: "把所有停用、删除、编辑过的内置题恢复成随插件分发的样子。玩家投稿与模型出的题不受影响。",
      label: "恢复",
      danger: true,
    });
    if (!proceed) return;
    await soupLibraryAction("builtin", "restore_all", 0);
  }

  async function deleteBuiltinSoup(index, surface) {
    const proceed = await confirmAction({
      title: "删除内置题",
      message: `确定把「${String(surface).slice(0, 40)}…」从抽题里去掉？\n可用「恢复全部改动」整批找回。`,
      label: "删除",
      danger: true,
    });
    if (!proceed) return;
    await soupLibraryAction("builtin", "delete", index);
  }

  async function deleteGeneratedSoup(index, label) {
    const proceed = await confirmAction({
      title: "删除模型出的题",
      message: `确定删除「${String(label || "").slice(0, 40)}」？\n删除后不能再找回，也不参与复用。`,
      label: "删除",
      danger: true,
    });
    if (!proceed) return;
    await soupLibraryAction("generated", "delete", index);
  }

  // 题目详情/编辑：汤底默认打码，点「查看答案」才请求全文
  let soupEditorTarget = null;

  async function openSoupEditor(kind, index, mode) {
    const dialog = document.getElementById("soupEditorDialog");
    if (!dialog) return;
    soupEditorTarget = { kind, index, mode };
    const pool = kind === "custom"
      ? (libraryState.data?.custom?.items || [])
      : kind === "generated"
        ? (libraryState.data?.generated?.items || [])
        : (libraryState.data?.builtin?.items || []);
    const item = pool.find((row) => row.index === index);
    document.getElementById("soupEditorKind").value = kind;
    document.getElementById("soupEditorIndex").value = String(index);
    document.getElementById("soupEditorEyebrow").textContent =
      kind === "custom" ? "玩家投稿"
        : kind === "generated" ? (item?.model ? `模型出题 · ${item.model}` : "模型出题")
          : "内置题库";
    document.getElementById("soupEditorTitle").textContent =
      kind === "generated" && item?.title ? `#${index} ${item.title}` : `#${index}`;
    const surfaceBox = document.getElementById("soupEditorSurface");
    const solutionBox = document.getElementById("soupEditorSolution");
    const levelBox = document.getElementById("soupEditorLevel");
    surfaceBox.value = String(item?.surface || "");
    solutionBox.value = String(item?.solution_preview || "（点「查看答案」展开）");
    levelBox.value = String(item?.content_level || "normal");
    const editing = mode === "edit";
    surfaceBox.readOnly = !editing;
    solutionBox.readOnly = !editing;
    levelBox.disabled = !editing;
    document.getElementById("soupEditorSave").hidden = !editing;
    document.getElementById("soupEditorReveal").hidden = false;
    dialog.showModal();
    icons();
    // 编辑必须先看到全文，否则一保存就会把打码预览写回去
    if (editing) await revealSoupAnswer();
  }

  async function revealSoupAnswer() {
    if (!soupEditorTarget) return;
    const { kind, index } = soupEditorTarget;
    try {
      const data = await endpoint("POST", "soup_reveal", { kind, index });
      document.getElementById("soupEditorSurface").value = String(data?.surface || "");
      document.getElementById("soupEditorSolution").value = String(data?.solution || "");
      const levelBox = document.getElementById("soupEditorLevel");
      if (data?.content_level) levelBox.value = String(data.content_level);
      document.getElementById("soupEditorReveal").textContent = "已展开";
    } catch (error) {
      showToast(error?.message || "读取失败");
    }
  }

  async function saveSoupEditor() {
    if (!soupEditorTarget) return;
    const { kind, index } = soupEditorTarget;
    const payload = {
      kind,
      index,
      surface: document.getElementById("soupEditorSurface").value,
      solution: document.getElementById("soupEditorSolution").value,
      content_level: document.getElementById("soupEditorLevel").value,
    };
    try {
      const api = await bridge();
      const result = kind === "custom"
        ? await api.apiPost("page/soup_custom_update", payload)
        : await api.apiPost("page/soup_library_action", { action: "edit", ...payload });
      if (result?.status === "error") throw new Error(result.message || "保存失败");
      showToast(result?.message || "已保存");
      document.getElementById("soupEditorDialog").close();
      if (result?.data?.builtin) renderLibrary(result.data);
      else loadLibrary();
    } catch (error) {
      showToast(error?.message || "保存失败");
    }
  }

  async function deleteCustomSoup(index, surface) {
    const proceed = await confirmAction({
      title: "删除投稿",
      message: `确定删除第 ${index} 道投稿？\n${surface}`,
      label: "删除",
      danger: true,
    });
    if (!proceed) return;
    try {
      await endpoint("POST", "soup_custom_delete", { index });
      showToast("已删除该投稿");
      loadLibrary();
    } catch (error) {
      showToast(error?.message || "删除失败");
    }
  }

  async function resetSoupLibrary() {
    const proceed = await confirmAction({
      title: "重置出题记录",
      message: "清空「哪些题已经出过」的记录，让所有题重新参与抽题。题库本身不会改动。只想重置某一道题的话，用列表里那一行的 ↺ 按钮。",
      label: "重置",
      danger: true,
    });
    if (!proceed) return;
    try {
      await endpoint("POST", "soup_library_reset", {});
      showToast("出题记录已重置");
      loadLibrary();
    } catch (error) {
      showToast(error?.message || "重置失败");
    }
  }

  async function generateSoupFromModel() {
    const button = document.getElementById("soupGenerateBtn");
    const status = document.getElementById("soupGenerateStatus");
    if (!button || button.disabled) return;
    const modelSelect = document.getElementById("soupGenerateModel");
    const count = Number(document.getElementById("soupGenerateCount").value || 1);
    const payload = {
      provider_id: modelSelect.value,
      content_level: document.getElementById("soupGenerateLevel").value,
      count,
    };
    // 先按「已经在后台跑了」渲染：按钮立刻锁死，不等接口回来，免得连点派多批
    const picked = modelSelect.selectedOptions?.[0]?.textContent || "模型";
    renderGenerateJob({
      state: "running",
      running: true,
      model: picked,
      total: count,
      done: 0,
      titles: [],
      error: "",
      started_at: Date.now() / 1000,
      finished_at: 0,
    });
    if (status) status.textContent = `正在把任务交给后台…（${picked}）`;
    icons();
    try {
      const api = await bridge();
      const result = await api.apiPost("page/soup_generate", payload);
      if (result?.status === "error") throw new Error(result.message || "出题失败");
      const job = jobFromPayload(result);
      if (job) renderGenerateJob(job);
      showToast(
        job?.running === false
          ? result?.message || "出题结束"
          : `${job?.model || picked} 已在后台出题，可以离开这个页面，回来看进度即可`,
      );
    } catch (error) {
      // 网关超时（504）不代表没派出去：先查一次真实状态，别让按钮白解锁
      const job = await fetchSoupGenerateJob();
      if (soupJobRunning(job)) {
        renderGenerateJob(job);
        showToast("后台已经在出题了，出完会提示");
      } else {
        renderGenerateJob(null);
        showToast(error?.message || "出题失败");
      }
    } finally {
      icons();
      pollSoupGenerate();
    }
  }
  // ===== 题库管理 END =====

  document.getElementById("refreshAction").addEventListener("click", loadRooms);
  document.getElementById("createRoomAction").addEventListener("click", createRoomFromConsole);
  document.getElementById("tunnelAction").addEventListener("click", toggleTunnel);
  document.getElementById("engineAction").addEventListener("click", installEngine);
  document.getElementById("cloudflaredAction").addEventListener("click", installCloudflared);
  document.getElementById("clearLeaderboardAction").addEventListener("click", clearLeaderboard);
  document.getElementById("saveSettingsAction").addEventListener("click", saveSettings);
  document.getElementById("reloadSettingsAction").addEventListener("click", loadSettings);
  document.getElementById("posterAction").addEventListener("click", exportPoster);
  document.getElementById("statsClearAction").addEventListener("click", clearStatsLeaderboard);
  document.getElementById("refreshStatsAction").addEventListener("click", loadStats);
  document.getElementById("refreshLibraryAction").addEventListener("click", loadLibrary);
  document.getElementById("resetLibraryAction").addEventListener("click", resetSoupLibrary);
  // 题库页签：一次只看一类，避免三类题目在长页面上互相挤压
  document.querySelectorAll("[data-lib-tab]").forEach((tab) => {
    tab.addEventListener("click", () => {
      const name = tab.getAttribute("data-lib-tab");
      libraryState.tab = name;
      document.querySelectorAll("[data-lib-tab]").forEach((item) => {
        const active = item === tab;
        item.classList.toggle("is-active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
      });
      document.querySelectorAll("[data-lib-pane]").forEach((pane) => {
        pane.hidden = pane.getAttribute("data-lib-pane") !== name;
      });
    });
  });

  document.getElementById("builtinSearch").addEventListener("input", (event) => {
    libraryState.builtin.search = event.target.value;
    libraryState.builtin.page = 1;
    renderBuiltinTable();
  });
  document.getElementById("builtinLevel").addEventListener("change", (event) => {
    libraryState.builtin.level = event.target.value;
    libraryState.builtin.page = 1;
    renderBuiltinTable();
  });
  document.getElementById("builtinState").addEventListener("change", (event) => {
    libraryState.builtin.state = event.target.value;
    libraryState.builtin.page = 1;
    renderBuiltinTable();
  });
  document.getElementById("builtinPrev").addEventListener("click", () => {
    libraryState.builtin.page = Math.max(1, libraryState.builtin.page - 1);
    renderBuiltinTable();
  });
  document.getElementById("builtinNext").addEventListener("click", () => {
    libraryState.builtin.page += 1;
    renderBuiltinTable();
  });
  document.getElementById("builtinRestoreAll").addEventListener("click", recoverAllBuiltinSoup);

  document.getElementById("customSearch").addEventListener("input", (event) => {
    libraryState.custom.search = event.target.value;
    renderCustomTable(libraryState.data?.custom || {});
  });

  document.getElementById("generatedSearch").addEventListener("input", (event) => {
    libraryState.generated.search = event.target.value;
    libraryState.generated.page = 1;
    renderGeneratedTable(libraryState.data?.generated || {});
  });
  document.getElementById("generatedLevel").addEventListener("change", (event) => {
    libraryState.generated.level = event.target.value;
    libraryState.generated.page = 1;
    renderGeneratedTable(libraryState.data?.generated || {});
  });
  document.getElementById("generatedPrev").addEventListener("click", () => {
    libraryState.generated.page = Math.max(1, libraryState.generated.page - 1);
    renderGeneratedTable(libraryState.data?.generated || {});
  });
  document.getElementById("generatedNext").addEventListener("click", () => {
    libraryState.generated.page += 1;
    renderGeneratedTable(libraryState.data?.generated || {});
  });
  document.getElementById("soupGenerateBtn").addEventListener("click", generateSoupFromModel);
  // 切回页面时补查一次：离开期间后台可能已经出完并入库了
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollSoupGenerate();
  });
  document.getElementById("soupEditorReveal").addEventListener("click", revealSoupAnswer);
  document.getElementById("soupEditorSave").addEventListener("click", saveSoupEditor);
  document.getElementById("statsGameSelect").addEventListener("change", (e) => {
    statsFilter = e.target.value;
    loadStats();
  });
  const statsWindowSelect = document.getElementById("statsWindowSelect");
  if (statsWindowSelect) {
    statsWindowSelect.addEventListener("change", (e) => {
      statsWindow = Number(e.target.value) || 0;
      loadStats();
    });
  }
  const playerSearch = document.getElementById("playerSearch");
  if (playerSearch) {
    playerSearch.addEventListener("input", (e) => {
      playerKeyword = e.target.value || "";
      renderPlayers();
    });
  }
  const playerShowQQ = document.getElementById("playerShowQQ");
  if (playerShowQQ) {
    // 记住上次选择：常用「完整 QQ」的人不必每次重新勾
    showFullQQ = localStorage.getItem("huahuo.manager.showFullQQ") === "1";
    playerShowQQ.checked = showFullQQ;
    playerShowQQ.addEventListener("change", () => {
      showFullQQ = playerShowQQ.checked;
      localStorage.setItem("huahuo.manager.showFullQQ", showFullQQ ? "1" : "0");
      renderPlayers();
      renderStats();
    });
  }
  const playerExportAction = document.getElementById("playerExportAction");
  if (playerExportAction) {
    playerExportAction.addEventListener("click", exportPlayerCsv);
  }
  const playerPruneAction = document.getElementById("playerPruneAction");
  if (playerPruneAction) {
    playerPruneAction.addEventListener("click", pruneEmptyPlayers);
  }
  // 表头点击换排序（局数 / 胜场 / 胜率 / 最近活跃 / 昵称）
  document.querySelectorAll("[data-player-sort]").forEach((th) => {
    th.classList.toggle("is-sorted", th.dataset.playerSort === playerSort);
    th.addEventListener("click", () => {
      playerSort = th.dataset.playerSort || "games";
      document.querySelectorAll("[data-player-sort]").forEach((node) => {
        node.classList.toggle("is-sorted", node === th);
      });
      renderPlayers();
    });
  });
  document.querySelectorAll(".manager-tab").forEach((tab) => {
    tab.addEventListener("click", () => showPanel(tab.dataset.panel));
  });
  document.getElementById("confirmAssign").addEventListener("click", confirmAssign);
  document.getElementById("confirmProceed").addEventListener("click", () => resolveConfirmation(true));
  confirmDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    resolveConfirmation(false);
  });
  confirmDialog.addEventListener("close", () => {
    if (confirmResolver) resolveConfirmation(false);
  });
  // 数值输入框：取消（含 Esc / X / 取消按钮）返回 null，确定则校验后返回非负整数
  const promptInput = document.getElementById("promptInput");
  const promptProceed = document.getElementById("promptProceed");
  promptDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    resolvePromptValue(null);
  });
  promptDialog.addEventListener("close", () => {
    if (promptResolver) resolvePromptValue(null);
  });
  promptProceed.addEventListener("click", () => {
    const raw = String(promptInput.value || "").trim();
    if (promptKind === "text") {
      if (!raw) { showToast("请输入内容"); return; }
      resolvePromptValue(raw);
      return;
    }
    if (!raw) { showToast("请输入胜场数"); return; }
    const num = Number(raw);
    if (!Number.isFinite(num) || num < 0) {
      showToast("请输入非负整数作为胜场数");
      return;
    }
    resolvePromptValue(Math.floor(num));
  });
  promptInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      promptProceed.click();
    }
  });
  icons();
  renderNewRoomPicker();
  loadRooms();
})();
