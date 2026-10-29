"""Builds the private floor state a player receives (REST responses and WebSocket FLOOR_STATE).

Everything shown or enabled in the UI is decided here, on the server. Secrets never appear in it:
* puzzle content only for the player who currently holds the object,
* the vault code only one character at a time, after the object that owns it was solved,
* hint text only for hints the team has already used.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.enums import FloorStatus, GameStatus, ObjectStatus, TeamStatus
from app.db.models import Game, HintUsage, ObjectState, Player, QuizAttempt, Team, TeamFloorState
from app.engines import challenge
from app.engines import dare as dare_engine
from app.engines import quiz as quiz_engine
from app.game import presets as pr
from app.game.runtime import GameRuntime
from app.hints.penalties import penalty_for_hint
from app.timers.clock import utcnow
from app.timers.deadline import seconds_remaining

OBJ_AVAILABLE, OBJ_BUSY, OBJ_SOLVED, OBJ_LOCKED = "AVAILABLE", "BEING_INVESTIGATED", "SOLVED", "LOCKED"


def active_quiz_attempt(db: Session, team_id: str, floor_no: int, object_id: str) -> QuizAttempt | None:
    return db.scalar(
        select(QuizAttempt)
        .where(
            QuizAttempt.team_id == team_id, QuizAttempt.floor_no == floor_no,
            QuizAttempt.object_id == object_id, QuizAttempt.submitted_at.is_(None),
        )
        .order_by(QuizAttempt.started_at.desc())
        .execution_options(populate_existing=True)
    )


def quiz_attempt_count(db: Session, team_id: str, floor_no: int, object_id: str) -> int:
    return db.scalar(
        select(func.count()).select_from(QuizAttempt).where(
            QuizAttempt.team_id == team_id, QuizAttempt.floor_no == floor_no,
            QuizAttempt.object_id == object_id,
        )
    ) or 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _quiz_view(db, runtime, team, floor_no, obj, now) -> dict[str, Any]:
    spec = obj["quiz"]
    pool = runtime.content.quizzes[spec["pool"]]["questions"]
    by_id = {str(q["id"]): q for q in pool}
    count = spec.get("questions", quiz_engine.DEFAULT_QUESTIONS)
    base = {
        "time_limit_seconds": spec.get("time_limit_seconds", quiz_engine.DEFAULT_TIME_LIMIT_SECONDS),
        "questions": count,
        "pass_score": spec.get("pass_score", quiz_engine.DEFAULT_PASS_SCORE),
        "attempts": quiz_attempt_count(db, team.id, floor_no, obj["id"]),
    }
    attempt = active_quiz_attempt(db, team.id, floor_no, obj["id"])
    if attempt is None:
        return {"state": "READY", **base}
    answered = len(attempt.answers or {})
    if quiz_engine.is_over(now, attempt.ends_at) or answered >= len(attempt.question_ids):
        return {"state": "TIME_UP", **base, "ends_at": _iso(attempt.ends_at), "server_now": _iso(now)}
    question = by_id[attempt.question_ids[answered]]
    return {
        "state": "IN_PROGRESS", **base,
        "ends_at": _iso(attempt.ends_at), "server_now": _iso(now),
        "question": quiz_engine.question_public(question, answered + 1, len(attempt.question_ids)),
    }


def build_floor_state(
    db: Session, runtime: GameRuntime, game: Game, player: Player, now: datetime | None = None
) -> dict[str, Any] | None:
    """The personal floor state of ``player`` (None when they are not on a team)."""
    if player.team_id is None:
        return None
    now = now or utcnow()
    settings = runtime.settings
    team = db.scalar(select(Team).where(Team.id == player.team_id).execution_options(populate_existing=True))
    if team is None or team.game_id != game.id:
        return None

    members = list(
        db.scalars(
            select(Player).where(Player.team_id == team.id).order_by(Player.joined_at, Player.id)
            .execution_options(populate_existing=True)
        ).all()
    )
    names = {m.id: m.display_name for m in members}
    leader_id = team.leader_player_id

    expired = team.status == TeamStatus.ELIMINATED.value or (
        team.deadline_at is not None and team.status == TeamStatus.PLAYING.value and now >= team.deadline_at
    )
    state: dict[str, Any] = {
        "game_status": game.status,
        "team": {
            "name": team.name or f"Team {team.slot_no}",
            "status": TeamStatus.ELIMINATED.value if expired else team.status,
            "floor": team.current_floor,
            "floors_completed": team.floors_completed,
        },
        "members": [
            {"name": m.display_name, "pawn_color": m.pawn_color, "connected": m.is_connected,
             "is_me": m.id == player.id, "is_leader": m.id == leader_id}
            for m in members
        ],
        "timer": {
            "deadline_at": _iso(team.deadline_at),
            "server_now": _iso(now),
            "base_duration_seconds": game.base_duration_seconds,
            "penalty_seconds": team.hint_penalty_seconds,
            "seconds_remaining": seconds_remaining(team.deadline_at, now) if team.deadline_at else None,
            "expired": expired,
        },
        "room": None,
    }

    # ---- whole-game progress: fragments, the final door, the winner (all derived from the database)
    floor_rows = {
        r.floor_no: r
        for r in db.scalars(
            select(TeamFloorState).where(TeamFloorState.team_id == team.id).execution_options(populate_existing=True)
        ).all()
    }
    fragments = []
    for n in range(1, pr.TOTAL_FLOORS + 1):
        row = floor_rows.get(n)
        got = bool(row and row.fragment_awarded)
        frag_preset = runtime.content.presets.get(row.preset_id) if (row and got) else None
        fragments.append({
            "floor": n, "collected": got,
            "label": frag_preset["fragment"]["label"] if frag_preset else None,
            "rune": frag_preset["fragment"].get("rune") if frag_preset else None,  # unknown until collected
        })
    all_collected = all(f["collected"] for f in fragments)
    winner_team = db.get(Team, game.winner_team_id, populate_existing=True) if game.winner_team_id else None
    state["game"] = {
        "status": game.status, "total_floors": pr.TOTAL_FLOORS, "ended_at": _iso(game.ended_at),
        "winner": {
            "team_name": winner_team.name or f"Team {winner_team.slot_no}",
            "is_my_team": winner_team.id == team.id,
        } if winner_team else None,
    }
    state["fragments"] = fragments
    state["final_door"] = {
        "status": "OPENED" if team.status == TeamStatus.ESCAPED.value else (
            "AVAILABLE" if (all_collected and game.status == GameStatus.RUNNING.value and not expired) else "LOCKED"),
        "length": pr.TOTAL_FLOORS,
        "collected": sum(1 for f in fragments if f["collected"]),
        "runes": [f["rune"] for f in fragments],
    }
    state["can_advance"] = False

    floor = db.scalar(
        select(TeamFloorState)
        .where(TeamFloorState.team_id == team.id, TeamFloorState.floor_no == team.current_floor)
        .execution_options(populate_existing=True)
    )
    preset = runtime.content.presets.get(floor.preset_id) if floor else None
    if floor is None or preset is None:
        return state

    rows = db.scalars(
        select(ObjectState).where(ObjectState.team_id == team.id, ObjectState.floor_no == floor.floor_no)
        .execution_options(populate_existing=True)
    ).all()
    states = {r.object_id: r for r in rows}
    solved = {oid for oid, r in states.items() if r.status == ObjectStatus.SOLVED.value and oid != pr.VAULT_ID}
    leases = {l.object_id: l for l in runtime.leases.active_for_team(team.id, now)}
    my_lease = next((l for l in leases.values() if l.player_id == player.id), None)

    objects_out = []
    for obj in preset["objects"]:
        oid = obj["id"]
        lease = leases.get(oid)
        if oid in solved:
            status = OBJ_SOLVED
        elif lease is not None:
            status = OBJ_BUSY
        elif not pr.prerequisites_met(obj, solved):
            status = OBJ_LOCKED
        else:
            status = OBJ_AVAILABLE
        objects_out.append({
            "id": oid, "type": "curio" if obj["type"] == "trap" else obj["type"], "label": obj["label"], "icon": obj.get("icon", ""),
            "x": obj["position"]["x"], "y": obj["position"]["y"], "optional": pr.is_optional(obj) and obj["type"] != "trap",  # a trap looks like any other object
            "status": status,
            "holder": names.get(lease.player_id) if lease else None,
            "held_by_me": bool(lease and lease.player_id == player.id),
        })

    completed = floor.status == FloorStatus.COMPLETED.value
    by_player = {l.player_id: l for l in leases.values()}
    label_of = {o["id"]: o["label"] for o in preset["objects"]}
    for member, entry in zip(members, state["members"]):
        held = by_player.get(member.id)
        entry["investigating"] = label_of.get(held.object_id) if held else None
        entry["investigating_id"] = held.object_id if held else None
    required = [o for o in preset["objects"] if not pr.is_optional(o)]
    state["progress"] = {
        "floor": floor.floor_no, "floors_completed": team.floors_completed, "total_floors": pr.TOTAL_FLOORS,
        "required_total": len(required), "required_solved": sum(1 for o in required if o["id"] in solved),
    }
    state["can_advance"] = bool(
        completed and floor.floor_no < pr.TOTAL_FLOORS and team.status == TeamStatus.PLAYING.value
        and game.status == GameStatus.RUNNING.value and not expired
    )
    vault_def = preset["vault"]
    vault_state = states.get(pr.VAULT_ID)
    pieces = floor.vault_pieces or {}
    if completed:
        vault_status = "OPEN"
    elif pr.vault_unlocked(preset, solved):
        vault_status = "AVAILABLE"
    else:
        vault_status = "LOCKED"
    state["room"] = {
        "floor": floor.floor_no,
        "floor_status": floor.status,
        "title": preset["title"],
        "intro": preset["intro"],
        "objects": objects_out,
        "vault": {
            "label": vault_def["label"], "x": vault_def["position"]["x"], "y": vault_def["position"]["y"],
            "length": pr.VAULT_LENGTH, "status": vault_status,
            "revealed": [pieces.get(str(i)) for i in range(pr.VAULT_LENGTH)],
            "attempts": (vault_state.progress or {}).get("attempts", 0) if vault_state else 0,
        },
        "clues": [
            {"object_id": o["id"], "label": o["label"], "text": pr.reward_text(runtime.content, o)}
            for o in preset["objects"] if o["id"] in solved
        ],
        "fragment": {
            "id": preset["fragment"]["id"], "label": preset["fragment"]["label"],
            "awarded": bool(floor.fragment_awarded),
        },
    }

    # ---- hints: levels, penalties, and the text of the hints already used (team-wide)
    used = list(
        db.scalars(
            select(HintUsage).where(HintUsage.team_id == team.id).order_by(HintUsage.used_at, HintUsage.hint_number)
            .execution_options(populate_existing=True)
        ).all()
    )
    used_levels = {h.hint_number for h in used}
    schedule = settings.hint_penalty_schedule
    revealed = []
    for h in used:
        row = floor_rows.get(h.floor_no)
        used_preset = runtime.content.presets.get(row.preset_id) if row else None  # the room the hint was about
        ladder = (used_preset.get("hints", {}).get(h.object_id or "") or []) if used_preset else []
        if used_preset is None:
            label = ""
        elif h.object_id == pr.VAULT_ID:
            label = used_preset["vault"]["label"]
        else:
            label = next((o["label"] for o in used_preset["objects"] if o["id"] == h.object_id), "")
        revealed.append({
            "level": h.hint_number, "penalty_seconds": h.penalty_seconds, "about": label, "floor": h.floor_no,
            "text": ladder[h.hint_number - 1] if len(ladder) >= h.hint_number else "",
            "used_by": names.get(h.requested_by_player_id),
        })
    state["hints"] = {
        "max": settings.max_hints,
        "used": len(used),
        "remaining": max(0, settings.max_hints - len(used)),
        "levels": [
            {"level": n, "penalty_seconds": penalty_for_hint(n, schedule), "used": n in used_levels}
            for n in range(1, settings.max_hints + 1)
        ],
        "revealed": revealed,
        "total_penalty_seconds": team.hint_penalty_seconds,
        "available": (not expired) and (not completed) and game.status == GameStatus.RUNNING.value
        and len(used) < settings.max_hints,
    }

    # ---- what THIS player is investigating (puzzle content goes to the holder only)
    state["my_investigation"] = None
    if my_lease is not None and my_lease.object_id in {o["id"] for o in preset["objects"]}:
        obj = pr.objects_by_id(preset)[my_lease.object_id]
        obj_state = states.get(obj["id"])
        progress = (obj_state.progress if obj_state else None) or {}
        inv: dict[str, Any] = {
            "object_id": obj["id"], "label": obj["label"], "type": obj["type"],
            "expires_at": _iso(my_lease.expires_at),
        }
        if obj["type"] == "puzzle":
            inv["challenge"] = challenge.public_view(runtime.content.puzzles[obj["puzzle"]], progress.get("step", 0))
        elif obj["type"] == "quiz":
            inv["quiz"] = _quiz_view(db, runtime, team, floor.floor_no, obj, now)
        elif obj["type"] == "dare":
            dare_id = progress.get("dare_id")
            pool = runtime.content.dares[obj["dare_pool"]]["dares"]
            chosen = next((d for d in pool if str(d["id"]) == dare_id), None)
            inv["dare"] = dare_engine.dare_public(chosen) if chosen else None
        state["my_investigation"] = inv
    return state


def build_host_dashboard(db: Session, runtime: GameRuntime, game: Game, now: datetime | None = None) -> dict[str, Any]:
    """The host's overview of every team (no puzzle content, no codes): where each team is and who won."""
    now = now or utcnow()
    teams = list(
        db.scalars(select(Team).where(Team.game_id == game.id).order_by(Team.slot_no).execution_options(populate_existing=True)).all()
    )
    hint_counts = dict(
        db.execute(select(HintUsage.team_id, func.count()).where(HintUsage.team_id.in_([t.id for t in teams])).group_by(HintUsage.team_id)).all()
    ) if teams else {}
    fragment_counts = dict(
        db.execute(
            select(TeamFloorState.team_id, func.count()).where(
                TeamFloorState.team_id.in_([t.id for t in teams]), TeamFloorState.fragment_awarded.is_(True)
            ).group_by(TeamFloorState.team_id)
        ).all()
    ) if teams else {}
    members = dict(
        db.execute(select(Player.team_id, func.count()).where(Player.game_id == game.id, Player.team_id.is_not(None)).group_by(Player.team_id)).all()
    )
    rows = []
    for t in teams:
        expired = t.status == TeamStatus.ELIMINATED.value or (
            t.status == TeamStatus.PLAYING.value and t.deadline_at is not None and now >= t.deadline_at)
        rows.append({
            "team_id": t.id, "name": t.name or f"Team {t.slot_no}", "status": TeamStatus.ELIMINATED.value if expired else t.status,
            "floor": t.current_floor, "floors_completed": t.floors_completed,
            "fragments": fragment_counts.get(t.id, 0), "hints_used": hint_counts.get(t.id, 0),
            "players": members.get(t.id, 0),
            "seconds_remaining": seconds_remaining(t.deadline_at, now) if (t.deadline_at and not expired) else 0,
            "finished_at": _iso(t.finished_at),
        })
    winner = next((r for r in rows if r["team_id"] == game.winner_team_id), None)
    return {
        "game_status": game.status, "server_now": _iso(now), "total_floors": pr.TOTAL_FLOORS,
        "winner": {"team_name": winner["name"], "team_id": winner["team_id"]} if winner else None,
        "teams": rows,
    }
