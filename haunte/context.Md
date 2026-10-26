Escape the Haunted House: Technical Implementation Plan
1. Recommended architecture

A modular monolith: one FastAPI process that serves the HTML/JS/CSS, the REST endpoints and the WebSockets. Replit runs one instance, and the load is small (the maximum is 250 players, about 25 teams × 10). A single process also lets us keep live game state in memory, which makes server-authoritative locks and timers simple and safe.

Core principle:

The database is the durable record.
An in-memory GameRuntime object per active game is the live working copy, written through to SQLite on every meaningful change.
All mutations for a team go through one asyncio.Lock per team, and winning goes through one lock per game. This removes race conditions without needing Redis.
Puzzle content (questions, answers, presets) lives in server-side data files, not in the database. The database stores only references and state.

Constraint: run a single uvicorn worker. Multiple workers would split the in-memory state. This is fine for this scale, and Section 15 covers what changes if you outgrow it.

2. Directory structure
haunted_house/
├── app/
│   ├── main.py                 # app factory, router mounting, startup tasks
│   ├── config.py               # env settings, global limits (3–25 teams, 3–10 players)
│   ├── db/
│   │   ├── base.py             # engine, session (SQLAlchemy 2.x)
│   │   ├── models/             # games, players, teams, objects, hints, events...
│   │   └── migrations/         # Alembic
│   ├── auth/                   # session tokens, host/player identity, dependencies
│   ├── lobby/                  # create/join game, chat, rules
│   ├── teams/                  # leaders, team codes, membership, pawn colors
│   ├── game/
│   │   ├── runtime.py          # in-memory GameRuntime + per-team locks
│   │   ├── state_machine.py    # game & team lifecycle transitions
│   │   ├── floors.py           # floor progression, vault, key fragments
│   │   ├── interactions.py     # object lock leases, investigate/release
│   │   ├── win.py              # final door, winner resolution
│   ├── content/
│   │   ├── loader.py           # loads + validates content at startup
│   │   ├── presets/            # floor1/…floor8/ preset definition files
│   │   ├── puzzles/            # puzzle data (answers server-side only)
│   │   ├── quizzes/            # question pools
│   │   └── dares/
│   ├── engines/
│   │   ├── puzzle/             # base class + one module per puzzle type
│   │   ├── quiz.py
│   │   └── dare.py
│   ├── hints/                  # hint tiers, penalties, secret interactions
│   ├── timers/                 # deadline math, end-of-game sweeper
│   ├── jumpscares/             # scheduler, asset registry
│   ├── ws/
│   │   ├── manager.py          # connections, rooms (game/team/host)
│   │   ├── protocol.py         # message schemas (Pydantic)
│   │   └── handlers.py         # dispatch to domain services
│   ├── host/                   # dashboard API + snapshot builder
│   └── schemas/                # Pydantic request/response models
├── static/
│   ├── css/  js/  img/  audio/  video/
│   │   └── js/modules/         # ws client, room renderer, hud, chat, jumpscare...
├── templates/                  # Jinja2: landing, host, player, room
├── tools/
│   ├── validate_content.py     # preset/puzzle validator
│   └── simulate_preset.py      # bot that solves each preset
├── tests/
└── README.md

Each domain package follows the same split: service.py (rules), router.py (HTTP), and models imported from db/models. The ws/ package only routes messages to services and holds no game rules.

3. Database design

SQLAlchemy 2.x with Alembic. Use only portable types (no SQLite-specific tricks) so the PostgreSQL move is a connection-string change. Use UUIDs or random string IDs, store timestamps as UTC, and avoid SQLite-only JSON functions.

Table	Key fields
games	id, game_code (unique), host_token_hash, status, config (teams min/max, players min/max, base_duration_s), started_at, ended_at, winner_team_id
players	id, game_id, display_name, session_token_hash, team_id (nullable), pawn_color, is_connected, joined_at
teams	id, game_id, name, icon, leader_player_id, team_code (unique), status, current_floor, floors_completed, hint_penalty_s, deadline_at, finished_at
team_floor_state	team_id, floor_no, preset_id, status, vault_pieces (JSON), completed_at, fragment_awarded
object_state	team_id, floor_no, object_id, status (untouched/in_progress/solved), solved_by_player_id, solved_at, progress (JSON)
object_locks	team_id, floor_no, object_id, holder_player_id, acquired_at, expires_at
hint_usage	team_id, floor_no, object_id, hint_index (1–4), penalty_s, used_at, requested_by
quiz_attempts	team_id, floor_no, question_ids (JSON), answers (JSON), started_at, ends_at, score
dare_confirmations	team_id, floor_no, dare_id, confirmed_by, confirmed_at
jumpscare_events	team_id, scheduled_at, fired_at, asset_id, kind, target (team/player)
chat_messages	game_id, player_id, text, created_at
game_events	game_id, team_id, type, payload (JSON), created_at (append-only audit log)

Worth noting:

hint_penalty_s is stored as the accumulated total, and deadline_at is recomputed from it.
object_locks can also live only in memory (locks are short-lived), but persisting them makes crash recovery easier. I recommend memory-only plus lease expiry, and treating a restart as “all locks released.”
Add unique constraints on game_code, team_code, (team_id, floor_no, object_id) and (team_id, hint_index), so the database rejects duplicate hints and double awards even if code has a bug.
4. Core entities and models
Game: lifecycle LOBBY → TEAM_FORMATION → READY → CUTSCENE → RUNNING → FINISHED.
Player: identity, pawn color, connection state, team.
Team: identity, leader, code, status (FORMING / LOCKED / PLAYING / ESCAPED / ELIMINATED).
FloorDefinition and PresetDefinition (content, immutable): an object graph with dependencies and the vault-code mapping.
ObjectDefinition: id, type (puzzle / quiz / dare / trap / decor), prerequisites, which vault characters it yields, hint ladder.
TeamFloorState and ObjectState (runtime, mutable).
Hint, Lock, JumpscareSchedule, GameEvent.

Keeping definitions (static content) apart from state (per-team runtime) is the most important modelling decision. It lets the validator and tests exercise the content on its own.

5. API and WebSocket architecture

REST is used for one-off actions, and WebSocket for live state and gameplay actions.

REST (examples):

POST /api/games (host creates a lobby), POST /api/games/join (game code + name)
POST /api/teams/leaders (host approves leaders), POST /api/teams/join (team code)
POST /api/games/{id}/start, GET /api/host/snapshot, GET /api/me

WebSocket: /ws?token=… (one connection per client; the server resolves game, team and role from the session).

Message envelope: { "type": "...", "req_id": "...", "payload": {...} }.

Client → server (intents only):
object.investigate, object.release, object.submit_answer, vault.submit, hint.request, quiz.answer, dare.confirm, chat.send, pawn.move, final_door.submit, ping.

Server → client (events):
state.snapshot (full state, sent on connect and reconnect), object.locked / unlocked / solved, hint.used, timer.updated, team.updated, vault.opened, floor.completed, jumpscare.fire, game.ended, error (with a reason code the UI can show, such as OBJECT_BUSY).

Rules:

Every message is validated with Pydantic, and every intent returns an ack or an error tied to req_id.
Clients never send state, only intents. The server decides the outcome.
Fan-out has three audiences: the team room (teammates), the host room (dashboard) and the game room (lobby, chat, game end).
Puzzle content is delivered only when a player successfully acquires the lock on that object, and only for that object.
6. State-management strategy
Clients hold no authoritative state, only a render of the last server snapshot plus incremental events.
Each event carries a per-team version counter. If a client sees a gap, it requests a fresh state.snapshot.
Reconnection always begins with a snapshot, so the system never depends on a client having received every event.
The server separates public team state (what teammates see) from secret state (answers, unsent hint content). Only the public part goes in snapshots.
The host dashboard gets a summarised feed, batched about once per second, so 25 busy teams don’t flood it.
7. Authentication and session strategy

There are no accounts. Identity is per-game and anonymous.

Joining creates a players row and issues a long random session token (stored hashed in the database), set in an HttpOnly, SameSite cookie. The WebSocket authenticates with the same token.
The host gets a separate host token at lobby creation. Host-only endpoints and WebSocket messages check it, and a host cannot also be a player.
Reconnection: the same token restores the same player, team and pawn. Closing and reopening the tab must not lose a place in the team.
Game codes are short and unambiguous (for example 6 characters without 0/O/1/I). Team codes are similar but longer, with rate-limited lookups.
Server-side checks: code exists, game status permits joining, team not full, game not started, player not already on a team.
8. Multiplayer synchronization strategy

Object locks (server-authoritative):

A player sends object.investigate.
The server takes the team’s lock, then checks that the object is unsolved, that its prerequisites are met, and that no valid lease exists.
If it is free, it creates a lease (holder, expires_at about 60–90 s), broadcasts object.locked, and sends that player the puzzle payload.
Everyone else receives OBJECT_BUSY plus the holder’s pawn identity.
The holder renews the lease with a heartbeat. The lease is released on explicit leave, on disconnect, or on expiry (a background sweeper handles the last two).
Simultaneous requests are serialised by the team lock, so exactly one wins.

Solved is permanent: once solved, the object status is final for the team, and re-investigating returns the recorded outcome, not the puzzle.

Presence: pawn positions are ephemeral and never saved. Clients send throttled positions (about 5 per second maximum), and the server clamps them to the room bounds and rebroadcasts to teammates only.

9. Timer architecture
team_deadline = game_start_time + base_duration + accumulated_hint_penalties
Computed and stored server-side (teams.deadline_at). A hint adds its penalty and updates the deadline in the same transaction as the hint_usage row.
The server sends {deadline_at, server_now} so each client computes its clock offset once and counts down locally for display only. Resending the offset periodically (for example every 30 s) corrects drift.
Expiry is enforced by a background sweeper that runs every second and marks overdue teams ELIMINATED. Every action handler also checks the deadline first (a team that is out of time cannot submit a late answer).
Treat deadline_at as the single source of truth. Never accept a client-reported time.
Decide the rule for a team that reaches the final door after its own deadline (my recommendation: not allowed).
10. Puzzle architecture

A puzzle type is a class with a small interface:

render_payload(instance) → what the client may see
validate(instance, submission) → result (correct / wrong / partial)
reveal_on_success(instance) → vault characters + narrative text
Types planned: numeric/logic, sequence/pattern, cipher/cryptic, data interpretation (a table or chart generated from data), deduction grid, visual reasoning (SVG built from parameters), observation (spot the clue in the scene), quiz, dare, trap.
Answers live only in server-side data files, are compared with normalisation (case and whitespace), and are never included in any payload. Optionally store them hashed with a per-puzzle salt, which also protects against someone browsing the repo files.
Variation without randomness chaos: parameterised puzzles use a seed fixed to the preset, so the answer is deterministic and testable.
Vault code: each preset maps objects to character positions (for example {candle: [0], painting: [1,2], vent: [3]}). The server reveals characters as objects are solved, and vault.submit checks the full 4-character string.
Quiz engine: draws 5 questions from the floor’s pool using a per-attempt seed, shows one at a time, enforces the time limit server-side, and rejects late answers. It grades server-side, and the result determines the vault characters (for example, one character per correct answer or a threshold, to be decided per floor).
Dare engine: a dare has a description, a safety tag and a confirmation rule (the leader, or a majority of teammates, confirms). The server records it, and dares never expose anything unsafe. Every dare is reviewed as part of content approval.
11. Room preset architecture

Each preset is a declarative file (JSON or YAML) describing the room:

Objects with their id, type, position in the 2D scene, art asset, and puzzle reference
A dependency graph (candle → painting → vent → vault)
The vault-code mapping
The hint ladder per object (including any secret interactions, such as “click the door 3 times”)
A solution walkthrough used only by tests

Validator (tools/validate_content.py) runs on startup and in CI. It checks that:

Every object is reachable from the door and the dependency graph has no cycles
All 4 vault characters are obtainable and no character is unassigned
Every referenced puzzle and asset exists
Every object has a complete hint ladder
No answer is missing or duplicated unsafely

Simulator (tools/simulate_preset.py) plays each preset like a bot, using the walkthrough through the same service layer, and asserts the team reaches the vault, opens it and gets the fragment. A preset that fails cannot be assigned.

Assignment: per floor, shuffle the presets and deal them to teams round-robin, so duplicates only occur when teams outnumber presets. The assignment is stored when the game starts, so reconnects never change a team’s room.

12. Security considerations
Validate on the server: game codes, team codes, team-size limits, leader approval, answers, vault codes, hints, and the winning condition. Never infer any of them from client messages.
Never include answers or unrevealed vault characters in static JS, API responses, snapshots or error messages. Quiz questions are delivered one at a time.
Rate-limit join and code attempts, answer submissions (with a short cool-down after a wrong answer), and chat messages. Log repeated failures.
Escape all user-supplied text (names, team names, chat) in the UI. Restrict team icons to a fixed set of server-defined options, and restrict names by length and characters.
Enforce roles on every message: only the host may start the game, only the leader may confirm certain actions, and only team members may touch their team’s objects.
Origin checks on WebSocket connections, a size limit on messages, and secrets stored in Replit Secrets, not in the repo.
Keep the walkthrough files out of anything served statically.
Anyone can inspect browser traffic, so assume a player can open dev tools. Design so that even a determined player can only see what the server chose to send them.
13. Development phases
Foundation: project skeleton, config, DB models and migrations, session handling, content loader.
Lobby and teams: create/join game, lobby chat, leader approval, team codes, team formation, chat shutdown, start conditions.
Real-time core: WebSocket manager, protocol, snapshots and reconnect, host dashboard skeleton.
Game engine: state machine, timers and sweeper, one floor with a hand-built preset, object locks, vault and fragment.
Puzzle, quiz and dare engines plus the preset validator and simulator.
Hint system with penalties and secret interactions.
Content: Floors 1–8, with 2–4 presets each, difficulty ramp, final door and win logic.
UI polish: room renderer, pawns, HUD, fog, flicker, transitions, cutscene.
Jumpscares: scheduler, assets, trap objects.
Full dashboard, then load testing and a complete playtest, then hardening.

Build a full vertical slice (phases 1–4 with a single floor) before writing more content. That proves the multiplayer foundation before the large part of the work begins.

14. Testing strategy
Unit tests: deadline math, penalty accumulation, puzzle validators, team-size rules, code generation, preset assignment.
Concurrency tests: many simulated clients hit object.investigate at the same moment and exactly one must win. The same applies to two teams reaching the final door at once.
Content tests: the validator and the bot simulator for every preset, run automatically.
WebSocket integration tests: with FastAPI’s test client, covering connect, snapshot, event ordering and reconnect.
Load test: a script that simulates 25 teams × 10 players, with moves and interactions, to confirm the single process holds up.
Manual playtests: timed solve runs per floor to calibrate difficulty, a dare and jumpscare safety review, and cross-device checks (phones in particular).
15. Deployment strategy
Replit: a single instance, uvicorn with one worker, SQLite on persistent storage, secrets in Replit Secrets. Replit’s free tier can sleep, so use a Deployment (Reserved VM or Autoscale with a single instance) for the real event.
GitHub: a main branch with feature branches, CI that runs the validator and tests, and a .env.example file.
Serving assets: serve images, audio and video as static files, compress them, and preload at game start so jumpscares fire without a delay.
Data: back up the SQLite file before each event. Take a snapshot after the game ends and keep the audit log.
Scaling later: the move to PostgreSQL is a connection change, plus an external pub/sub (Redis) if you ever need multiple workers or servers.
16. Risks and likely technical problems
Risk	Mitigation
Server restart mid-game loses in-memory state	Write-through to the DB, rebuild GameRuntime from DB at startup, release all locks
Stale locks from disconnects/closed tabs	Lease expiry and heartbeat, sweeper
WebSocket drops on mobile or flaky Wi-Fi	Automatic reconnect with backoff, snapshot on reconnect
Replit sleeping or resource limits	Reserved/always-on deployment, load test beforehand
Browsers block autoplay audio/video	Require a click at game start (the cutscene button) to unlock audio, then preload assets
Jumpscare overlay breaking puzzle state	Render it in a separate overlay layer that blocks input but never sends game messages
Exactly-5-second flash varying by device	Use a server-sent start and a client timer for display only, and accept minor local jitter
Unsolvable or too-easy presets	Validator, simulator, timed playtests
Players in the same room sharing answers	Different presets per team, parameterised puzzles with per-preset seeds
Content size and authoring workload (8 floors × several presets)	Reusable puzzle types, a content template, build floors incrementally
Dares inappropriate for some audiences	Safety review, leader confirmation, an option for the host to disable the dares
Cheating by inspecting traffic	Send only what’s needed, server validates everything
Scale spike when 250 players move at once	Throttle pawn updates, batch host updates, test early
Decisions I’d like your input on before coding
Whether the quiz’s vault characters come from correct answers or from a pass threshold.
What happens when a team’s timer expires: out of the game immediately, or allowed to keep playing without a chance of winning?
Whether the host can pause the game or kick players.
How many presets per floor are realistic for the first release (I’d suggest 2 per floor to start).

When you’ve reviewed this, tell me what to change, and we’ll start with Phase 1. If you’d like this plan as a shareable document, I can also turn it into a Word or PDF file.