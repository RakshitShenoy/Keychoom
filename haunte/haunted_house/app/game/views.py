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
            "id": oid, "type": obj["type"], "label": obj["label"], "icon": obj.get("icon", ""),
            "x": obj["position"]["x"], "y": obj["position"]["y"], "optional": pr.is_optional(obj),
            "status": status,
            "holder": names.get(lease.player_id) if lease else None,
            "held_by_me": bool(lease and lease.player_id == player.id),
        })

    completed = floor.status == FloorStatus.COMPLETED.value
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
        used_preset = runtime.content.presets.get(floor.preset_id)  # one floor today; per floor later
        ladder = (used_preset.get("hints", {}).get(h.object_id or "") or []) if used_preset else []
        label = vault_def["label"] if h.object_id == pr.VAULT_ID else next(
            (o["label"] for o in preset["objects"] if o["id"] == h.object_id), "")
        revealed.append({
            "level": h.hint_number, "penalty_seconds": h.penalty_seconds, "about": label,
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
        "available": (not expired) and (not completed) and len(used) < settings.max_hints,
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
