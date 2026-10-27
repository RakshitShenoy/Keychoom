# Escape the Haunted House

A Halloween-themed, competitive, multiplayer escape-room game: one host per lobby, 3-25 teams of
3-10 players, eight floors of interconnected puzzles, and the first team to escape wins.

> **Status: Phase 3 of the plan (real-time multiplayer lobby). The game is NOT complete.** Phase 1:
> backend foundation. Phase 2: team slots, leaders, team codes, chat, readiness, lock, kicks.
> Phase 3: the lobby is live over WebSockets (joins, team changes, leaders, formation state,
> presence, reconnects). There is no gameplay, timer or puzzle yet.

## 1. Requirements

- Python 3.11 or newer (developed on 3.12)
- `pip` and the built-in `venv` module
- Nothing else: the database is a local SQLite file

## 2. Installation

```bash
git clone <your-github-url> haunted_house
cd haunted_house
```

## 3. Virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

## 4. Dependencies

```bash
pip install -r requirements-dev.txt     # app + test tools
# production only: pip install -r requirements.txt
```

## 5. Environment configuration

```bash
cp .env.example .env
```

Edit `.env`. At minimum set a private `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`.env` is git-ignored: **never commit it**. Every limit (team/player counts, hint penalties, lock
lease, heartbeat, session lifetimes) is configurable there; see `.env.example` and `app/config.py`.
Settings that contradict each other (for example 3 hint penalties but `MAX_HINTS=4`) stop the
server from starting.

## 6. Database migrations

Run all commands from the project root.

```bash
alembic upgrade head       # creates ./haunted_house.db and all tables
alembic current            # shows the applied revision (0002)
alembic downgrade base     # removes everything (development only)
```

After you change a model, create a new migration with
`alembic revision --autogenerate -m "describe the change"`, **read it**, then `alembic upgrade head`.

## 7. Run the server

```bash
uvicorn app.main:app --reload --port 8000     # development
```

Use **one worker** (never `--workers N`): live game state will be kept in memory in later phases.
Behind a proxy such as Replit's, add `--proxy-headers --forwarded-allow-ips='*'`.

Open <http://localhost:8000>. Health check: <http://localhost:8000/health> returns `{"status":"ok"}`.
Interactive API docs: <http://localhost:8000/docs>.

## 8. Run the tests

```bash
pytest
```

Each test uses its own temporary SQLite file, so tests never touch your real database.

## 9. Project structure

```
app/
  main.py          application factory, router wiring, startup content validation
  config.py        all configuration (env-driven, validated)
  errors.py        AppError (stable error codes)   error_handlers.py  JSON error responses
  db/              base.py (naming convention, engine), session.py, types.py, enums.py, ids.py
    models/        one module per table group (11 tables)
    migrations/    Alembic environment + versions/0001_initial_schema.py
  auth/            tokens (hashing), sessions (cookies), dependencies, /api/me, rate limiter
  lobby/           codes.py, names.py, service.py (create/join rules), router.py, pages.py
  teams/           codes, names, slots, readiness (pure rules), queries, service, router
  host/            /api/host/game (Phase 1 minimal view)
  content/         loader.py + presets/ puzzles/ quizzes/ dares/ (empty on purpose)
  timers/          clock.py, deadline.py (team deadline formula)
  hints/           penalties.py
  game/ engines/ jumpscares/ ws/   empty packages reserved for later phases
  schemas/         Pydantic request/response models
static/            css/ js/ img/ audio/ video/
templates/         landing, host and player development pages
tests/             pytest suite
alembic.ini  requirements*.txt  .env.example  pytest.ini
```

## 10. Phase 2: lobby and team formation

### Lobby state machine

```
WAITING --(host: start)--> TEAM_FORMATION --(host: lock)--> READY_TO_START
                                  ^                               |
                                  +--------(host: reopen)---------+
```

| Public name | Stored `games.status` | Meaning |
|---|---|---|
| `WAITING` | `LOBBY` | Players join, public chat is open. |
| `TEAM_FORMATION` | `TEAM_FORMATION` | Public chat closed. Host approves leaders; players join teams with Team Codes. Late players can still enter the lobby. |
| `READY_TO_START` | `READY` | Formation is **locked**. Locking is only allowed when every readiness requirement holds, so "locked" and "ready to start" are the same state. Phase 4 will start the game from here. |

`TEAM_FORMATION_LOCKED` is not a separate stored state: since the lock itself requires readiness, a
separate state would never be observable. The API reports `team_formation_locked` as a flag too.

### Rules enforced by the server

* Exactly `team_count` team slots are created with the lobby (idempotent; the unique key
  `(game_id, slot_no)` prevents duplicates). `team_count` must satisfy the global limits and
  `min_teams <= team_count <= max_teams` (default: `max_teams`).
* Only the host assigns leaders. A player leads at most one team, a team has one leader, and a
  leader automatically becomes a member of the team. Only an approved leader's team gets a Team Code.
* Team Codes look like `TEAM-7K4P`, are random, globally unique, and never contain database ids.
  Typing `7k4p`, `team 7k4p` or `TEAM-7K4P` all work. A code only resolves **inside the player's own
  game**. Wrong guesses are rate limited per player. Codes are stored in plain text on purpose:
  leaders must be able to see them again after a refresh, exactly like Game Codes.
* A team is `READY` with a leader and at least `min_players`; `FULL` at `max_players`;
  `WAITING_FOR_LEADER` / `WAITING_FOR_PLAYERS` otherwise. The game can be locked only when all teams
  exist and are ready, no team is over capacity, and **nobody is left without a team**.
* Public chat is open only while the game is in `WAITING`; once formation has started it stays closed,
  even if formation is reopened. History stays readable.
* Locked: no joining, no leader changes, no leaving, no kicking (reopen first).
* Kicking deletes the player (their session dies with the row). A kicked leader's team code is revoked.
* Every change is written to `game_events` and announced on `LobbyEventBus`
  (`app/lobby/events.py`). Phase 3 will subscribe the WebSocket layer there.

### Endpoints (all JSON; `{code}` is the Game Code)

| Method and path | Who | Purpose |
|---|---|---|
| `POST /api/games` | anyone | Create lobby (`team_count`, `min/max_players_per_team`, ...). |
| `POST /api/games/join` | anyone | Join the lobby with a Game Code and display name. |
| `GET /api/games/{code}/lobby` | host or player of that game | Server-calculated lobby (host sees codes and ids; players see only what they need). |
| `POST /api/games/{code}/team-formation/start` | host | WAITING to TEAM_FORMATION. |
| `POST /api/games/{code}/team-formation/lock` | host | Lock; refused with details while requirements are unmet. |
| `POST /api/games/{code}/team-formation/reopen` | host | Back to TEAM_FORMATION. |
| `POST /api/games/{code}/teams/{team_id}/leader` | host | Approve a leader (`{"player_id": ...}`). |
| `DELETE /api/games/{code}/teams/{team_id}/leader` | host | Remove the leader and revoke the code. |
| `POST /api/games/{code}/teams/join` | player | Join a team (`{"team_code": ...}`). |
| `POST /api/games/{code}/players/{player_id}/kick` | host | Remove a player. |
| `POST /api/games/{code}/leave` | player | Leave the lobby. |
| `POST /api/games/{code}/chat` | player | Public chat (only while WAITING). |

Phase 1 endpoints (`/api/me`, `/api/host/game`, `/api/player/lobby`) are unchanged; the new `lobby`
endpoint supersedes the two lobby views.

### Authorization

The Game Code in a URL is only a claim: it is compared with the game the session cookie really
belongs to (`403 WRONG_GAME` otherwise). Host-only routes answer `403 HOST_ONLY` to players and `401`
to visitors without a session. Team ids and player ids from the browser are always looked up *inside*
the authenticated game, so ids from another game answer `404`.

### Database changes (migration `0002`)

`games.team_count` (backfilled to `max_teams` for existing rows), `games.team_formation_started_at`,
`games.team_formation_locked_at`, `teams.slot_no` (+ unique `(game_id, slot_no)`), and the foreign key
`teams.leader_player_id -> players.id`. Phase 1 DB tests were updated only to supply the new required
columns.

## 10b. Phase 3: real-time synchronization

```
Browser --REST action--> FastAPI --validate--> DB commit --> LobbyEventBus --> RealtimeHub
Browser <--WebSocket (events + personal LOBBY_STATE)-----------------------------+
```

* **REST stays the only way to change state.** The WebSocket only *announces* committed changes
  (database first, broadcast second). Over the socket a browser may only send `PING`, `PONG` and
  `REQUEST_STATE`; anything else (for example `ASSIGN_LEADER`) answers `ERROR UNSUPPORTED_MESSAGE`.
* **Endpoint:** `GET /ws/games/{game_code}?role=host|player`. It uses the same session cookies and the
  same resolver (`identify_viewer`) as the REST API. `role` can only *narrow* the choice; it never
  grants anything the cookies do not prove. Unauthenticated: `ERROR NOT_AUTHENTICATED` (close 4401).
  Wrong game, wrong role, kicked session or foreign `Origin`: `ERROR NOT_AUTHORIZED` (close 4403).
* **On connect** the server registers the connection, then sends the viewer's own `LOBBY_STATE`
  (hosts see codes and ids, players do not). After every change each connected client receives a
  fresh personal `LOBBY_STATE` (bursts are coalesced within `WS_SNAPSHOT_DEBOUNCE_SECONDS`). Snapshots
  carry a `version`; the browser ignores older ones.
* **Message envelope:** `{"type", "id", "ts", "payload"}`; errors are `{"type":"ERROR","code","message",...}`
  with generic texts only (no stack traces, SQL or paths).

| Event | When |
|---|---|
| `LOBBY_STATE` | on connect, on `REQUEST_STATE`, and after every change (full personal state) |
| `PLAYER_JOINED`, `PLAYER_LEFT`, `PLAYER_KICKED` | lobby membership changed |
| `PLAYER_CONNECTED`, `PLAYER_DISCONNECTED` | a player's first connection opened / last one closed |
| `TEAM_JOINED`, `LEADER_ASSIGNED`, `LEADER_REMOVED`, `TEAM_UPDATED` | team changes |
| `TEAM_FORMATION_STARTED`, `TEAM_FORMATION_LOCKED`, `TEAM_FORMATION_REOPENED` | formation state |
| `CHAT_MESSAGE`, `LOBBY_UPDATED` | chat message stored / "state changed" |
| `PING`, `PONG`, `ERROR` | heartbeat and errors |

Players receive events without row ids; the host receives them with ids.

* **Presence is separate from membership.** `players.is_connected` follows the sockets (two tabs of one
  player count as one). A dropped connection or refresh never deletes the player; reconnecting with the
  same cookie resumes the same player and receives the current state. Flags are cleared at server start.
* **Heartbeat:** the server pings every `WS_HEARTBEAT_INTERVAL_SECONDS`; a connection that sends nothing
  for `WS_HEARTBEAT_TIMEOUT_SECONDS` is closed (`HEARTBEAT_TIMEOUT`).
* **Isolation:** the `ConnectionManager` (`app/ws/manager.py`) is keyed by game, so a broadcast can only
  reach one game. It also supports host-only, player-only, per-team (`send_team`) and per-player delivery,
  the foundation for team-only communication later. Kicking or leaving closes that player's sockets.
* **Concurrency:** team joins run under a process-wide lock *and* `SELECT ... FOR UPDATE` on the team row
  (effective on PostgreSQL), re-reading the member count inside the lock, so a team can never exceed
  its maximum. Tests race up to 11 simultaneous joins.
* **Frontend:** `static/js/realtime.js` connects, applies server state, answers pings, and reconnects with
  capped exponential backoff (1s doubling to 15s, 12 attempts, then a click-to-retry badge). While the
  socket is down the pages fall back to a slow REST poll. Server state always replaces client state.

## 11. Run the tests

```bash
pytest -q
```

## 12. Not implemented yet (later phases)

Gameplay, floors, puzzles, quizzes, dares, hints in play, timers in play, jumpscares, the full host dashboard, the room renderer, the winner logic, team chat, the "start game" action
and the polished haunted UI.

## 13. Known limits

* `games.winner_team_id` still has no foreign key (arrives with the winner logic).
* Rate limiters and the lobby lock are in memory / per process (fine for the single-worker design).
* WebSocket connections and the lobby lock live in one process (single worker). Several workers would
  need a shared pub/sub (for example Redis) behind `LobbyEventBus`.
* A connected socket is not re-validated when its session cookie expires (12h by default) or is
  otherwise invalidated, except for kicks and leaves, which close the socket immediately.
* Every change pushes one snapshot per connected client (coalesced). That is fine for a few hundred
  players; very large lobbies would want delta updates.
* Players who are kicked are deleted, so they simply see the join form again.
* Cookies use `Secure` only when `COOKIE_SECURE=true`; set it when serving over HTTPS.
