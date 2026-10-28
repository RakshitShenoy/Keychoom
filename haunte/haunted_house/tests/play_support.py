"""Helpers shared by the Phase 4-6 gameplay tests."""
from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings

CONTENT_DIR = Path(__file__).resolve().parent.parent / "app" / "content"
SOLUTIONS = {"candle": "3", "painting": "maid", "vent": "34"}
CODE = "K7R4"


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        _env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path / 'test.db'}",
        secret_key="test-secret-key", join_failure_limit=3,
        answer_cooldown_seconds=0.0,  # tests resubmit immediately; the cooldown has its own test
        sweeper_interval_seconds=3600,  # the background sweeper never interferes: tests call sweep()
    )
    values.update(overrides)
    return Settings(**values)


def error_code(response) -> str:
    return response.json()["error"]["code"]


class Play:
    """One player's browser, speaking the gameplay API."""

    def __init__(self, lobby, player):
        self.lobby, self.player = lobby, player

    @property
    def client(self):
        return self.player.client

    def call(self, method, suffix, body=None):
        url = self.lobby.url("/play" + suffix)
        if body is None:
            return self.client.request(method, url)
        return self.client.request(method, url, json=body)

    def state(self):
        response = self.call("GET", "/state")
        assert response.status_code == 200, response.text
        return response.json()["state"]

    def obj(self, object_id, state=None):
        return next(o for o in (state or self.state())["room"]["objects"] if o["id"] == object_id)

    def investigate(self, object_id): return self.call("POST", f"/objects/{object_id}/investigate")
    def release(self, object_id): return self.call("POST", f"/objects/{object_id}/release")
    def heartbeat(self, object_id): return self.call("POST", f"/objects/{object_id}/heartbeat")
    def answer(self, object_id, text): return self.call("POST", f"/objects/{object_id}/answer", {"answer": text})
    def vault(self, code): return self.call("POST", "/vault", {"code": code})
    def hint(self, level): return self.call("POST", "/hints", {"level": level})
    def quiz_start(self, object_id): return self.call("POST", f"/objects/{object_id}/quiz/start")
    def quiz_answer(self, object_id, question_id, choice):
        return self.call("POST", f"/objects/{object_id}/quiz/answer", {"question_id": question_id, "choice": choice})
    def dare_confirm(self, object_id): return self.call("POST", f"/objects/{object_id}/dare/confirm")

    def solve(self, object_id, answer=None):
        assert self.investigate(object_id).status_code == 200
        response = self.answer(object_id, answer or SOLUTIONS[object_id])
        assert response.status_code == 200 and response.json()["result"]["result"] == "CORRECT", response.text
        return response


def start_running(make_lobby, team_count=3, per_team=3, **extra):
    lobby = make_lobby(team_count=team_count, min_players=3, max_players=5, **extra)
    lobby.build_full_teams(per_team)
    assert lobby.lock().status_code == 200
    response = lobby.host.post(lobby.url("/start"))
    assert response.status_code == 200, response.text
    return lobby


def team_players(lobby, slot, per_team=3):
    return [Play(lobby, p) for p in lobby.players[(slot - 1) * per_team : slot * per_team]]


def reload(db, model, row_id):
    db.expire_all()
    return db.get(model, row_id)


def quiz_answers() -> dict[str, str]:
    pool = json.loads((CONTENT_DIR / "quizzes" / "core_reasoning.json").read_text())
    return {q["id"]: q["answer"] for q in pool["questions"]}
