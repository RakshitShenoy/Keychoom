/* Floor view (Phase 4-6). The browser only RENDERS the state the server sends and posts intents
 * (an answer, a code, a hint level). All text is inserted with textContent via HH.el (no innerHTML). */
(function () {
  const HH = window.HH;
  const el = HH.el;
  const $ = function (id) { return document.getElementById(id); };

  let gameCode = null;
  let state = null;
  let clockOffsetMs = 0;       // server_now - local now, measured on every state: display only
  let tickTimer = null;
  let beatTimer = null;
  let beatObject = null;
  let message = null;          // {kind: "good"|"bad"|"info", text}
  let draft = { answer: "", vault: "" };

  function url(suffix) { return "/api/games/" + encodeURIComponent(gameCode) + "/play" + suffix; }
  function say(kind, text) { message = text ? { kind: kind, text: text } : null; }
  function fmt(seconds) {
    const s = Math.max(0, Math.floor(seconds));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    const two = function (n) { return (n < 10 ? "0" : "") + n; };
    return (h ? h + ":" + two(m) : m) + ":" + two(r);
  }
  function nowServerMs() { return Date.now() + clockOffsetMs; }

  // ------------------------------------------------------------------ talking to the server
  async function act(suffix, body) {
    const res = await HH.api(url(suffix), "POST", body);
    if (!res.ok) {
      say("bad", HH.errorText(res));
      const code = res.data && res.data.error && res.data.error.code;
      if (code === "TEAM_ELIMINATED" || code === "NOT_INVESTIGATING" || code === "OBJECT_BUSY" ||
          code === "OBJECT_SOLVED" || code === "FLOOR_COMPLETED") { await refresh(true); } else { render(); }
      return res;
    }
    const result = res.data.result;
    if (result) {
      const kind = (result.result === "WRONG" || result.result === "INVALID") ? "bad" : "good";
      say(kind, result.message || result.reveal_text || null);
      if (result.result === "HINT") { say("good", "Hint " + result.level + ": " + result.text); }
      if (result.result === "FINISHED" || result.result === "TIME_UP") {
        say(result.passed ? "good" : "bad", "Quiz finished: " + result.score + "/" + (result.total || "?") +
          (result.passed ? " - passed!" : " - not enough correct answers."));
      }
      if (result.result === "CONFIRMED" && result.reveal_text) { say("good", result.reveal_text); }
      if (result.result === "CORRECT" && result.reveal_text) { say("good", "Correct! " + result.reveal_text); }
      if (result.result === "RECORDED") { say(null); }
    } else { say(null); }
    if (res.data.state) { setState(res.data.state); }
    return res;
  }

  async function refresh(keepMessage) {
    if (!gameCode) { return; }
    const res = await HH.api(url("/state"));
    if (res.ok && res.data.state) {
      if (!keepMessage) { say(null); }
      setState(res.data.state);
    }
  }

  function setState(next) {
    state = next;
    if (state.timer && state.timer.server_now) {
      clockOffsetMs = Date.parse(state.timer.server_now) - Date.now();
    }
    render();
  }

  // ------------------------------------------------------------------ small renderers
  function banner() {
    if (!message) { return null; }
    return el("div", { class: "gm-msg " + message.kind, text: message.text });
  }

  function timerBlock() {
    const t = state.timer;
    const box = el("div", { class: "gm-timer" }, [
      el("span", { class: "label", text: "TIME LEFT" }),
      el("strong", { id: "gm-clock", text: t.deadline_at ? fmt((Date.parse(t.deadline_at) - nowServerMs()) / 1000) : "-" })
    ]);
    if (t.penalty_seconds) {
      box.appendChild(el("span", { class: "muted", text: " (hint penalties: -" + fmt(t.penalty_seconds) + ")" }));
    }
    return box;
  }

  function roomCard() {
    const room = state.room;
    const stage = el("div", { class: "gm-room" });
    room.objects.forEach(function (o) {
      let note = "";
      if (o.status === "BEING_INVESTIGATING" || o.status === "BEING_INVESTIGATED") {
        note = o.held_by_me ? " (you)" : " (" + (o.holder || "a teammate") + ")";
      }
      const btn = el("button", {
        type: "button", class: "gm-obj " + o.status + (o.held_by_me ? " mine" : ""),
        style: "left:" + o.x + "%;top:" + o.y + "%",
        title: o.label + " - " + o.status.replace(/_/g, " ").toLowerCase(),
        onclick: function () { investigate(o); }
      }, [el("span", { class: "ico", text: o.icon || "?" }), el("span", { class: "lbl", text: o.label + note })]);
      stage.appendChild(btn);
    });
    stage.appendChild(el("div", {
      class: "gm-vaultpin " + room.vault.status, style: "left:" + room.vault.x + "%;top:" + room.vault.y + "%",
      text: (room.vault.status === "OPEN" ? "\uD83D\uDD13 " : "\uD83D\uDD12 ") + room.vault.label
    }));
    return el("div", { class: "card" }, [
      el("h2", { text: "Floor " + room.floor + ": " + room.title }),
      el("p", { class: "muted", text: room.intro }),
      stage,
      el("p", { class: "muted", text: "Click an object to investigate it. Only one teammate can investigate an object at a time." })
    ]);
  }

  function challengeCard(inv) {
    const kids = [el("h2", { text: inv.label })];
    if (inv.challenge) {
      const c = inv.challenge;
      kids.push(el("h3", { text: c.title + (c.step_count > 1 ? " (part " + c.step + "/" + c.step_count + ")" : "") }));
      kids.push(el("p", { class: "muted", text: (c.category || "").replace(/_/g, " ") + " - " + (c.difficulty || "").replace(/_/g, " ") }));
      kids.push(el("p", { class: "gm-prompt", text: c.prompt }));
      if (c.clues) { kids.push(el("ol", {}, c.clues.map(function (t) { return el("li", { text: t }); }))); }
      if (c.table) {
        const rows = [el("tr", {}, c.table.columns.map(function (h) { return el("th", { text: h }); }))];
        c.table.rows.forEach(function (r) { rows.push(el("tr", {}, r.map(function (v) { return el("td", { text: v }); }))); });
        kids.push(el("table", { class: "plist" }, rows));
      }
      if (c.answer_type === "choice") {
        (c.choices || []).forEach(function (ch) {
          kids.push(el("button", { type: "button", class: "small", text: ch.id.toUpperCase() + ". " + ch.text,
            onclick: function () { submitAnswer(inv.object_id, ch.id); } }));
        });
      } else {
        kids.push(el("div", { class: "row" }, [
          el("input", { id: "gm-answer", value: draft.answer, placeholder: c.placeholder || "your answer",
            maxlength: "64", autocomplete: "off",
            oninput: function (e) { draft.answer = e.target.value; },
            onkeydown: function (e) { if (e.key === "Enter") { submitAnswer(inv.object_id, draft.answer); } } }),
          el("button", { type: "button", text: "Submit", onclick: function () { submitAnswer(inv.object_id, draft.answer); } })
        ]));
      }
    } else if (inv.quiz) {
      kids.push.apply(kids, quizBlock(inv));
    } else if (inv.dare) {
      kids.push(el("h3", { text: "A friendly dare" }));
      kids.push(el("p", { class: "gm-prompt", text: inv.dare.text }));
      kids.push(el("p", { class: "muted", text: "Dares are optional and always safe. Skip any you do not want to do." }));
      kids.push(el("button", { type: "button", text: "I did it", onclick: function () { act("/objects/" + inv.object_id + "/dare/confirm"); } }));
    }
    kids.push(el("button", { type: "button", class: "small danger", text: "Leave this object",
      onclick: function () { draft.answer = ""; act("/objects/" + inv.object_id + "/release"); } }));
    return el("div", { class: "card gm-inv" }, kids);
  }

  function quizBlock(inv) {
    const q = inv.quiz;
    const out = [el("h3", { text: "Quick quiz" })];
    if (q.state === "READY" || q.state === "TIME_UP") {
      out.push(el("p", { text: q.questions + " questions, " + q.time_limit_seconds + " seconds, " + q.pass_score + " correct to pass. The server keeps the clock." }));
      out.push(el("button", { type: "button", text: q.attempts ? "Try again" : "Start quiz",
        onclick: function () { act("/objects/" + inv.object_id + "/quiz/start"); } }));
    } else {
      const left = (Date.parse(q.ends_at) - nowServerMs()) / 1000;
      out.push(el("p", { class: "muted" }, ["Question " + q.question.number + "/" + q.question.total + " - time left ",
        el("strong", { id: "gm-quizclock", "data-ends": q.ends_at, text: fmt(left) })]));
      out.push(el("p", { class: "gm-prompt", text: q.question.prompt }));
      q.question.choices.forEach(function (ch) {
        out.push(el("button", { type: "button", class: "small", text: ch.id.toUpperCase() + ". " + ch.text,
          onclick: function () { act("/objects/" + inv.object_id + "/quiz/answer", { question_id: q.question.question_id, choice: ch.id }); } }));
      });
    }
    return out;
  }

  function vaultCard() {
    const v = state.room.vault;
    const slots = el("div", { class: "gm-slots" }, v.revealed.map(function (ch) {
      return el("span", { class: "gm-slot" + (ch ? " known" : ""), text: ch || "_" });
    }));
    const kids = [el("h2", { text: v.label }), slots];
    if (v.status === "OPEN") {
      kids.push(el("p", { class: "gm-good", text: "The vault is open." }));
    } else if (v.status === "LOCKED") {
      kids.push(el("p", { class: "muted", text: "Sealed. Solve the objects in the room to uncover the code." }));
    } else {
      kids.push(el("div", { class: "row" }, [
        el("input", { id: "gm-vault", value: draft.vault, maxlength: "8", placeholder: "4-character code",
          autocomplete: "off", autocapitalize: "characters",
          oninput: function (e) { draft.vault = e.target.value; },
          onkeydown: function (e) { if (e.key === "Enter") { submitVault(); } } }),
        el("button", { type: "button", text: "Open vault", onclick: submitVault })
      ]));
    }
    const f = state.room.fragment;
    kids.push(el("p", { class: f.awarded ? "gm-good" : "muted",
      text: f.awarded ? "\u2728 " + f.label + " collected!" : f.label + ": not yet collected" }));
    if (state.room.floor_status === "COMPLETED") {
      kids.push(el("p", { class: "gm-good", text: "Floor " + state.room.floor + " complete!" }));
    }
    return el("div", { class: "card" }, kids);
  }

  function cluesCard() {
    const clues = state.room.clues;
    if (!clues.length) { return null; }
    return el("div", { class: "card" }, [el("h2", { text: "Clue notes" }),
      el("ul", {}, clues.map(function (c) { return el("li", {}, [el("strong", { text: c.label + ": " }), c.text]); }))]);
  }

  function hintsCard() {
    const h = state.hints;
    const kids = [el("h2", { text: "Hints" }),
      el("p", { text: h.remaining + " of " + h.max + " team hints left. Each hint costs your whole team time." })];
    h.levels.forEach(function (lv) {
      kids.push(el("div", { class: "row gm-hintrow" }, [
        el("span", { text: "Hint " + lv.level + " (-" + fmt(lv.penalty_seconds) + ")" }),
        el("button", { type: "button", class: "small", text: lv.used ? "Used" : "Use",
          disabled: (lv.used || !h.available) ? "disabled" : null,
          onclick: function () { useHint(lv); } })
      ]));
    });
    if (h.revealed.length) {
      kids.push(el("h3", { text: "Revealed" }));
      h.revealed.forEach(function (r) {
        kids.push(el("p", {}, [el("strong", { text: "Hint " + r.level + " (" + r.about + "): " }), r.text]));
      });
    }
    return el("div", { class: "card" }, kids);
  }

  function membersCard() {
    return el("div", { class: "card" }, [el("h2", { text: "Your team" }),
      el("ul", { class: "players" }, state.members.map(function (m) {
        return el("li", {}, [el("span", { class: "dot", style: "background:" + m.pawn_color }),
          m.name + (m.is_me ? " (you)" : "") + (m.is_leader ? " - leader" : "") + (m.connected ? "" : " - offline")]);
      }))]);
  }

  // ------------------------------------------------------------------ rendering
  function render() {
    const root = $("game");
    if (!root || !state) { return; }
    const hadFocus = document.activeElement && document.activeElement.id;
    root.textContent = "";
    root.classList.remove("hidden");

    if (!state.room) {
      root.appendChild(el("div", { class: "card" }, [el("h2", { text: "Entering the house..." })]));
      return;
    }
    const expired = state.timer.expired || state.team.status === "ELIMINATED";
    root.appendChild(el("div", { class: "card gm-head" }, [
      el("strong", { text: state.team.name }), timerBlock(),
      expired ? el("div", { class: "gm-bad", text: "TIME IS UP. Your team can no longer play." }) : null
    ]));
    const b = banner();
    if (b) { root.appendChild(b); }
    root.appendChild(roomCard());
    if (!expired && state.my_investigation) { root.appendChild(challengeCard(state.my_investigation)); }
    root.appendChild(vaultCard());
    const clues = cluesCard();
    if (clues) { root.appendChild(clues); }
    root.appendChild(hintsCard());
    root.appendChild(membersCard());

    if (hadFocus && $(hadFocus)) { $(hadFocus).focus(); }
    syncHeartbeat();
  }

  function tick() {
    if (!state || !state.timer) { return; }
    const clock = $("gm-clock");
    if (clock && state.timer.deadline_at) {
      clock.textContent = fmt((Date.parse(state.timer.deadline_at) - nowServerMs()) / 1000);
    }
    const qc = $("gm-quizclock");
    if (qc) { qc.textContent = fmt((Date.parse(qc.getAttribute("data-ends")) - nowServerMs()) / 1000); }
  }

  function syncHeartbeat() {
    const inv = state && state.my_investigation;
    const id = inv ? inv.object_id : null;
    if (id === beatObject) { return; }
    if (beatTimer) { clearInterval(beatTimer); beatTimer = null; }
    beatObject = id;
    if (id) {
      beatTimer = setInterval(async function () {
        const res = await HH.api(url("/objects/" + id + "/heartbeat"), "POST");
        if (!res.ok) { say("info", "You are no longer investigating that."); refresh(true); }
      }, 20000);
    }
  }

  // ------------------------------------------------------------------ intents
  function investigate(o) {
    if (o.status === "SOLVED") { say("info", o.label + " is already solved."); render(); return; }
    draft.answer = "";
    act("/objects/" + o.id + "/investigate");
  }
  function submitAnswer(objectId, answer) {
    if (!answer || !answer.trim()) { say("bad", "Type an answer first."); render(); return; }
    act("/objects/" + objectId + "/answer", { answer: answer }).then(function (res) {
      if (res && res.ok && res.data.result && res.data.result.result !== "WRONG") { draft.answer = ""; }
    });
  }
  function submitVault() {
    act("/vault", { code: draft.vault }).then(function (res) {
      if (res && res.ok && res.data.result && res.data.result.result === "CORRECT") { draft.vault = ""; }
    });
  }
  function useHint(lv) {
    const ok = window.confirm("Use hint " + lv.level + "? It costs " + fmt(lv.penalty_seconds) +
      " of your team's time and cannot be undone.");
    if (ok) { act("/hints", { level: lv.level }); }
  }

  // ------------------------------------------------------------------ public API
  HH.game = {
    start: function (code) {
      if (gameCode === code) { return; }
      gameCode = code;
      if (tickTimer) { clearInterval(tickTimer); }
      tickTimer = setInterval(tick, 500);
      refresh(false);
    },
    update: function (floorState) { setState(floorState); },
    refresh: function () { return refresh(true); },
    notify: function (text) { say("info", text); }
  };
})();
