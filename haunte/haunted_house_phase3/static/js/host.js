(function () {
  const HH = window.HH;
  const el = HH.el;
  const $ = function (id) { return document.getElementById(id); };
  let gameCode = null;
  let rt = null;
  let pending = null;   // a state that arrived while a dropdown was open
  let lastSnapshot = "";

  const STATUS_TEXT = {
    WAITING_FOR_LEADER: "WAITING FOR LEADER", WAITING_FOR_PLAYERS: "WAITING FOR PLAYERS",
    READY: "READY", FULL: "FULL"
  };

  function lobbyUrl(suffix) { return "/api/games/" + encodeURIComponent(gameCode) + suffix; }

  async function act(method, suffix, body) {
    const res = await HH.api(lobbyUrl(suffix), method, body);
    if (!res.ok) {
      HH.showError($("action-error"), res);
      const problems = res.data && res.data.error && res.data.error.details;
      if (problems && problems.length) {
        const list = $("action-error");
        problems.forEach(function (p) { list.appendChild(el("div", { text: "- " + p.message })); });
      }
      return;
    }
    $("action-error").textContent = "";
    render(res.data, true);
  }

  function leaderCandidates(lobby, team) {
    return lobby.players.filter(function (p) {
      return p.role !== "LEADER" && (p.team_id === null || p.team_id === team.team_id);
    });
  }

  function teamCard(lobby, team) {
    const kids = [
      el("h3", { text: team.name }),
      el("div", { text: "Leader: " + (team.leader_name || "-") }),
      el("div", { text: "Players: " + team.player_count + "/" + team.max_players }),
      el("div", {}, [el("span", { class: "badge " + team.status, text: STATUS_TEXT[team.status] || team.status })])
    ];
    if (team.team_code) { kids.push(el("div", { class: "teamcode", text: team.team_code })); }
    if (team.members.length) { kids.push(el("div", { class: "muted", text: team.members.join(", ") })); }

    if (lobby.lobby_state === "TEAM_FORMATION") {
      if (team.leader_player_id) {
        kids.push(el("button", {
          class: "small danger", type: "button", text: "Remove leader",
          onclick: function () { act("DELETE", "/teams/" + team.team_id + "/leader"); }
        }));
      } else {
        const select = el("select", {}, [el("option", { value: "", text: "Choose leader..." })]);
        leaderCandidates(lobby, team).forEach(function (p) {
          select.appendChild(el("option", { value: p.player_id, text: p.display_name }));
        });
        kids.push(select);
        kids.push(el("button", {
          class: "small", type: "button", text: "Assign leader",
          onclick: function () {
            if (select.value) { act("POST", "/teams/" + team.team_id + "/leader", { player_id: select.value }); }
          }
        }));
      }
    }
    return el("div", { class: "team" }, kids);
  }

  function render(lobby, force) {
    const snapshot = JSON.stringify(lobby);
    if (!force && snapshot === lastSnapshot) { return; }
    const active = document.activeElement;
    if (!force && active && active.tagName === "SELECT") {
      pending = lobby;      // don't close an open dropdown; show the newest state afterwards
      return;
    }
    pending = null;
    lastSnapshot = snapshot;

    $("create-card").classList.add("hidden");
    $("lobby").classList.remove("hidden");
    $("game-code").textContent = lobby.game_code;
    $("lobby-state").textContent = lobby.lobby_state;
    $("player-count").textContent = lobby.counts.player_count + " (need " +
      lobby.counts.min_total_players + "-" + lobby.counts.max_total_players + ", room for " +
      lobby.counts.capacity + ")";

    const a = lobby.host_actions;
    $("btn-start").disabled = !a.can_start_formation;
    $("btn-lock").disabled = !a.can_lock;
    $("btn-reopen").disabled = !a.can_reopen;
    $("btn-startgame").disabled = true;  // the start endpoint is a later phase
    $("btn-startgame").classList.toggle("hidden", !a.can_start_game);

    const problems = $("problems");
    problems.textContent = "";
    if (!lobby.team_formation_locked) {
      lobby.readiness.problems.forEach(function (p) { problems.appendChild(el("li", { text: p.message })); });
    } else {
      problems.appendChild(el("li", { class: "closed", text: "READY TO START - team membership is locked." }));
    }

    const teams = $("teams");
    teams.textContent = "";
    lobby.teams.forEach(function (t) { teams.appendChild(teamCard(lobby, t)); });

    const table = $("players");
    table.textContent = "";
    table.appendChild(el("tr", {}, ["Player", "Team", "Role", ""].map(function (h) { return el("th", { text: h }); })));
    lobby.players.forEach(function (p) {
      table.appendChild(el("tr", {}, [
        el("td", {}, [el("span", { class: "dot", style: "background:" + p.pawn_color }), p.display_name]),
        el("td", { text: p.team_name || "Unassigned" }),
        el("td", { text: p.role }),
        el("td", {}, [el("button", {
          class: "small danger", type: "button", text: "Kick",
          onclick: function () {
            if (window.confirm("Remove " + p.display_name + " from the lobby?")) {
              act("POST", "/players/" + p.player_id + "/kick");
            }
          }
        })])
      ]));
    });

    const chat = $("chat");
    chat.textContent = "";
    lobby.chat.messages.forEach(function (m) {
      chat.appendChild(el("div", {}, [el("strong", { text: m.display_name + ": " }), m.body]));
    });
    $("chat-closed").textContent = lobby.chat.closed_message || "";
  }

  async function poll() {   // fallback only: used while the WebSocket is down
    if (!gameCode) { return; }
    const res = await HH.api(lobbyUrl("/lobby"));
    if (res.ok) { render(res.data, false); }
  }

  function start(code) {
    gameCode = code;
    if (rt) { rt.stop(); }
    rt = HH.connectRealtime({
      gameCode: code,
      role: "host",
      poll: poll,
      onState: function (state) { render(state, false); },
      onEvent: function (type, payload) { HH.logActivity($("activity"), HH.describeEvent(type, payload)); },
      onStatus: function (s) { HH.showConnectionStatus($("conn-status"), s); },
      onFatal: function () {
        $("action-error").textContent = "Your host session is no longer valid. Reload and create a new lobby.";
      }
    });
  }

  document.addEventListener("focusout", function () {
    if (pending) { setTimeout(function () { if (pending) { render(pending, true); } }, 0); }
  });
  $("conn-status").addEventListener("click", function () { if (rt) { rt.retryNow(); } });

  $("btn-start").addEventListener("click", function () { act("POST", "/team-formation/start"); });
  $("btn-lock").addEventListener("click", function () { act("POST", "/team-formation/lock"); });
  $("btn-reopen").addEventListener("click", function () { act("POST", "/team-formation/reopen"); });

  $("create-form").addEventListener("submit", async function (event) {
    event.preventDefault();
    const body = {};
    ["team_count", "min_players_per_team", "max_players_per_team"].forEach(function (field) {
      const raw = $(field).value.trim();
      if (raw !== "") { body[field] = Number(raw); }
    });
    const minutes = $("base_duration_minutes").value.trim();
    if (minutes !== "") { body.base_duration_seconds = Math.round(Number(minutes) * 60); }

    const res = await HH.api("/api/games", "POST", body);
    if (!res.ok) { HH.showError($("create-error"), res); return; }
    $("create-error").textContent = "";
    start(res.data.game_code);
  });

  (async function restoreSession() {
    const res = await HH.api("/api/me");
    if (res.ok && res.data.host) { start(res.data.host.game_code); }
  })();
})();
