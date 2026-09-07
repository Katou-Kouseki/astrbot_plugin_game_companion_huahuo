(() => {
  "use strict";

  const body = document.getElementById("roomsBody");
  const emptyState = document.getElementById("emptyState");
  const assignDialog = document.getElementById("assignDialog");
  const confirmDialog = document.getElementById("confirmDialog");
  const toast = document.getElementById("toast");
  const gameOptions = [
    ["gomoku", "五子棋"],
    ["xiangqi", "中国象棋"],
    ["tictactoe", "井字棋"],
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

  function icons() {
    if (window.lucide?.createIcons) window.lucide.createIcons();
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 2600);
  }

  function resolveConfirmation(accepted) {
    const resolver = confirmResolver;
    confirmResolver = null;
    if (confirmDialog.open) confirmDialog.close();
    if (resolver) resolver(Boolean(accepted));
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
      origin.append(createText("strong", room.source === "group" ? `群聊 ${room.group_id || ""}` : "私聊"));
      origin.append(createText("small", `${room.creator_name || "创建者"} · ${room.creator_qq || "未知 QQ"}`));

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
      }

      const actions = document.createElement("td");
      const actionList = document.createElement("div");
      actionList.className = "row-actions";
      const assign = document.createElement("button");
      assign.type = "button";
      assign.className = "row-button";
      assign.title = "安排玩家";
      assign.setAttribute("aria-label", "安排玩家");
      assign.innerHTML = '<i data-lucide="user-check"></i>';
      assign.addEventListener("click", () => openAssign(room));
      actionList.appendChild(assign);
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
    const privateRooms = rooms.filter((room) => room.source === "private");
    const online = rooms.reduce(
      (count, room) => count + (room.visitors || []).filter((visitor) => visitor.online).length,
      0,
    );
    document.getElementById("roomCount").textContent = rooms.length;
    document.getElementById("groupCount").textContent = groupRooms.length;
    document.getElementById("privateCount").textContent = privateRooms.length;
    document.getElementById("visitorCount").textContent = online;
    document.getElementById("groupLimit").textContent = limitLabel(data.limits?.group);
    document.getElementById("privateLimit").textContent = limitLabel(data.limits?.private);
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
    select.replaceChildren();
    (room.visitors || []).forEach((visitor) => {
      const option = document.createElement("option");
      option.value = visitor.number;
      option.textContent = `${visitor.display_name ? `${visitor.display_name}（${visitor.number}号）` : `${visitor.number}号`}${visitor.online ? " · 在线" : " · 离线"}`;
      select.appendChild(option);
    });
    const syncQq = () => {
      const selected = (room.visitors || []).find((visitor) => String(visitor.number) === select.value);
      document.getElementById("assignQq").value = selected?.player_qq || "";
    };
    select.onchange = syncQq;
    document.getElementById("assignRoomId").value = room.room_id;
    document.getElementById("assignRoomLabel").textContent = `房间 ${room.room_id}`;
    syncQq();
    document.getElementById("confirmAssign").disabled = !(room.visitors || []).length;
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
      showToast(result?.data?.cleared ? "战绩排行榜已清空" : "没有可清空的排行榜数据");
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
        renderUndercoverWords(res?.data?.items || [], tbody, countLabel);
        w1.value = "";
        w2.value = "";
        showToast(res?.status === "ok" ? "已添加" : res?.message || "添加失败");
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
        const items = res?.data?.items || [];
        const skipped = res?.data?.skipped || 0;
        if (tableWrap && !tableWrap.hidden) {
          renderUndercoverWords(items, tbody, countLabel);
        } else {
          loadUndercoverWords(tbody, countLabel);
        }
        showToast(
          res?.status === "ok"
            ? `AI 生成完成：新增 ${res.data.added || 0} 对，跳过重复 ${skipped} 对`
            : res?.message || "生成失败"
        );
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
          renderUndercoverWords(res?.data?.items || [], tbody, countLabel);
        } else {
          loadUndercoverWords(tbody, countLabel);
        }
        const added = res?.data?.added_count || 0;
        const skippedCount = res?.data?.skipped_count || 0;
        const skipped = res?.data?.skipped || [];
        const msg =
          `批量完成：新增 ${added} 对，跳过 ${skippedCount} 对` +
          (skipped.length > 0 ? `\n跳过明细：\n${skipped.slice(0, 5).join("\n")}` : "");
        showToast(res?.status === "ok" ? msg : res?.message || "批量添加失败");
        if (res?.status === "ok") textarea.value = "";
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
      renderUndercoverWords(res?.data?.items || [], tbody, countLabel);
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
        if (!window.confirm(`确认删除词条对「${item.word1} / ${item.word2}」？`)) return;
        try {
          const res = await endpoint("POST", "undercover_words/delete", { id: item.id });
          renderUndercoverWords(res?.data?.items || [], tbody, countLabel);
          showToast(res?.status === "ok" ? "已删除" : res?.message || "删除失败");
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
    document.getElementById("roomsPanel").hidden = settings || stats;
    document.getElementById("settingsPanel").hidden = !settings;
    document.getElementById("statsPanel").hidden = !stats;
    document.querySelectorAll(".manager-tab").forEach((tab) => {
      tab.classList.toggle("active", tab.dataset.panel === panel);
      tab.setAttribute("aria-selected", String(tab.dataset.panel === panel));
    });
    if (settings) loadSettings();
    if (stats) loadStats();
  }

  // ===== 战绩管理 =====
  let statsLeaderboard = {};   // game_type -> [{name,wins}]
  let statsFilter = "";        // 当前玩法筛选

  const GAME_LABELS = {
    gomoku: "五子棋", xiangqi: "中国象棋", tictactoe: "井字棋",
    turtle_soup: "海龟汤", pig_dice: "贪心骰子", draw_guess: "你画我猜",
    blackjack: "二十一点", undercover: "谁是卧底",
  };
  function gameLabel(gtype) { return GAME_LABELS[gtype] || gtype || "未知玩法"; }

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
    try {
      const data = await endpoint("POST", "leaderboard", statsFilter ? { game_type: statsFilter } : {});
      statsLeaderboard = data?.leaderboard || {};
      renderStats();
    } catch (error) {
      showToast(error?.message || "无法读取战绩排行");
    }
    try {
      document.getElementById("operationLogBody").replaceChildren();
      const log = await endpoint("POST", "operation_log", { limit: 200 });
      renderStatsLog(log?.rows || []);
    } catch (error) {
      showToast(error?.message || "无法读取操作日志");
    }
  }

  function renderStats() {
    const body = document.getElementById("leaderboardBody");
    body.replaceChildren();
    const rows = [];
    Object.entries(statsLeaderboard).forEach(([gtype, list]) => {
      (list || []).forEach((row, idx) => {
        rows.push({ game_type: gtype, name: row.name, wins: Number(row.wins) || 0, rank: idx + 1 });
      });
    });
    document.getElementById("leaderboardEmpty").hidden = rows.length > 0;
    document.getElementById("leaderboardSummary").textContent = rows.length
      ? `${rows.length} 条记录`
      : "暂无战绩";
    rows.slice(0, 200).forEach((row) => {
      const tr = document.createElement("tr");
      const rankTd = createText("td", String(row.rank));
      rankTd.className = "rank-cell";
      const nameTd = createText("td", row.name);
      const gameTd = createText("td", gameLabel(row.game_type));
      const winsTd = createText("td", `${row.wins} 胜`);
      winsTd.className = "wins-cell";
      const actTd = document.createElement("td");
      actTd.className = "actions-column";
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "row-button";
      editBtn.title = "修正胜场";
      editBtn.innerHTML = '<i data-lucide="pen-line"></i>';
      editBtn.addEventListener("click", () => editWins(row.game_type, row.name));
      actTd.appendChild(editBtn);
      tr.append(rankTd, nameTd, gameTd, winsTd, actTd);
      body.appendChild(tr);
    });
    icons();
  }

  async function editWins(gameType, name) {
    const value = await promptNumber(`修正「${name}」在《${gameLabel(gameType)}》的胜场数：`);
    if (value === null) return;
    try {
      await endpoint("POST", "leaderboard/set_wins", { game_type: gameType, name, wins: value });
      showToast("胜场已修正");
      await loadStats();
      await loadRooms();
    } catch (error) {
      showToast(error?.message || "修正失败");
    }
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
      tr.appendChild(createText("td", row.detail || ""));
      body.appendChild(tr);
    });
  }

  function actionLabel(action) {
    return {
      clear_leaderboard: "清空排行榜", set_wins: "修正胜场", close_room: "关闭房间",
      assign: "安排玩家", demote: "移出玩家", kick: "踢出", switch_game: "切换游戏",
    }[action] || action || "未知";
  }

  async function promptNumber(message) {
    const raw = window.prompt(message);
    if (raw === null) return null;
    const num = Number(String(raw).trim());
    if (!Number.isFinite(num) || num < 0) {
      showToast("请输入非负整数作为胜场数");
      return promptNumber(message);
    }
    return Math.floor(num);
  }

  function exportPoster() {
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
    const cardW = 720, rowH = 40, headerH = 96, cellPad = 28, cols = 5, footerH = 56;
    const nRows = Math.min(entries.length, 200);
    const height = headerH + nRows * rowH + footerH;
    const canvas = document.createElement("canvas");
    canvas.width = cardW; canvas.height = height;
    const ctx = canvas.getContext("2d");
    // 背景
    const bg = ctx.createLinearGradient(0, 0, cardW, height);
    bg.addColorStop(0, "#1f2b3a"); bg.addColorStop(1, "#14202c");
    ctx.fillStyle = bg; ctx.fillRect(0, 0, cardW, height);
    // 标题
    ctx.fillStyle = "#ffffff";
    ctx.font = "bold 30px 'PingFang SC', 'Microsoft YaHei', sans-serif";
    ctx.textAlign = "left";
    ctx.fillText("🏆 花火 · 游戏战绩排行榜", 28, 56);
    ctx.font = "15px 'PingFang SC', 'Microsoft YaHei', sans-serif";
    ctx.fillStyle = "#9fb3c8";
    ctx.fillText(`导出时间：${new Date().toLocaleString()}`, 28, 82);
    // 表头
    ctx.strokeStyle = "rgba(255,255,255,.18)";
    drawPosterRow(ctx, 0, headerH, ["排名", "玩家", "玩法", "胜场", ""], headerH, { name: 28 });
    // 数据行
    const list = entries.slice(0, nRows);
    list.forEach((entry, i) => {
      const y = headerH + (i + 1) * rowH;
      const bgRow = i % 2 === 0 ? "rgba(255,255,255,.06)" : "rgba(255,255,255,.02)";
      ctx.fillStyle = bgRow; ctx.fillRect(0, y, cardW, rowH);
      ctx.strokeStyle = "rgba(255,255,255,.06)";
      ctx.strokeRect(0, y, cardW, rowH);
      ctx.fillStyle = (i < 3) ? "#ffd166" : "#e8eef5";
      ctx.fillText(String(i + 1), cellPad, y + 28);
      ctx.fillStyle = "#e8eef5";
      ctx.fillText(truncatePosterText(ctx, entry.name, 300), cellPad + 90, y + 28);
      ctx.fillText(gameLabel(entry.game_type), cellPad + 90 + 320, y + 28);
      ctx.fillStyle = "#ffd166";
      ctx.fillText(`${entry.wins} 胜`, cellPad + 90 + 320 + 130, y + 28);
    });
    // 页脚
    ctx.fillStyle = "#7d8fa3";
    ctx.font = "14px 'PingFang SC', 'Microsoft YaHei', sans-serif";
    ctx.textAlign = "right";
    ctx.fillText("由 花火陪你玩 生成", cardW - 28, height - 22);
    return canvas.toDataURL("image/png");
  }

  function truncatePosterText(ctx, text, max) {
    if (ctx.measureText(text).width <= max) return text;
    let t = text;
    while (t.length && ctx.measureText(t + "…").width > max) t = t.slice(0, -1);
    return t + "…";
  }

  function drawPosterRow(ctx, topIndex, headerH, labels, _unused, _opts) {
    ctx.fillStyle = "rgba(255,255,255,.08)";
    ctx.fillRect(0, headerH, 720, headerH);
    ctx.strokeStyle = "rgba(255,255,255,.18)";
    ctx.strokeRect(0, headerH, 720, headerH);
    ctx.fillStyle = "#9fb3c8";
    ctx.font = "bold 15px 'PingFang SC', 'Microsoft YaHei', sans-serif";
    ctx.textAlign = "left";
    labels.forEach((text, i) => {
      const x = i === 0 ? cellStaticCol(28) : i === 1 ? 90 + 28 : i === 2 ? 90 + 320 : 90 + 320 + 130;
      ctx.fillText(text, x, headerH + 28);
    });
  }
  function cellStaticCol(pad) { return pad; }
  // ===== 战绩管理 END =====

  document.getElementById("refreshAction").addEventListener("click", loadRooms);
  document.getElementById("tunnelAction").addEventListener("click", toggleTunnel);
  document.getElementById("engineAction").addEventListener("click", installEngine);
  document.getElementById("cloudflaredAction").addEventListener("click", installCloudflared);
  document.getElementById("clearLeaderboardAction").addEventListener("click", clearLeaderboard);
  document.getElementById("saveSettingsAction").addEventListener("click", saveSettings);
  document.getElementById("reloadSettingsAction").addEventListener("click", loadSettings);
  document.getElementById("posterAction").addEventListener("click", exportPoster);
  document.getElementById("refreshStatsAction").addEventListener("click", loadStats);
  document.getElementById("statsGameSelect").addEventListener("change", (e) => {
    statsFilter = e.target.value;
    loadStats();
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
  icons();
  loadRooms();
})();
