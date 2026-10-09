# Reeve — a tabletop referee server — design (draft 2)

Status: **draft for review**, 2026-10-08. Nothing is built yet.
Basis: the 2026-10-08 snapshot of the AD&D 1e campaign (`artifacts/2026-10-08_snapshot`),
its `tooling_redesign.md`, and the decisions made in conversation (listed in §1).

---

## 1 · Decisions already made

| # | Decision |
|---|---|
| D1 | Rewrite as a proper service; do not wrap the old TSV tools. |
| D2 | Python + FastAPI + SQLite. Plain local HTTP API first; MCP is a thin adapter added later. |
| D3 | **Server is the referee**, not a notebook. A player's `move` is validated, executed and broadcast by the server; the DM does not have to perform it. |
| D4 | Roles are credentials with different views. Priority mode: **Claude DM + human player**. Must not preclude: Claude plays both, human DM + Claude player, human plays both. |
| D5 | Hidden information is **hard-enforced** server-side. |
| D6 | Undo/redo exists. Undone rolls stay consumed unless the DM voids them. A redo may need fewer or extra rolls. |
| D7 | Multi-system by design; AD&D 1e is the first and only real ruleset. |
| D8 | History from the old campaign is imported **read-only as a test corpus**, not to resume play. |
| D9 | Runs on this Mac for development; designed to be hosted and reachable from other devices later. |
| D10 | Narration, dialogue and DM notes are typed events (searchable, visibility-aware), not free-form files. |
| D11 | Design document first, in this directory. Web UI later; CLI + Claude Code first. |
| D12 | A seat (one human) can own several characters. |
| D13 | **Concurrent play is required**: the party may split and several humans act at the same time (§9b). |
| D14 | Store DM narration word for word. |
| D15 | Own repo, living in this directory, entirely separate from the old campaign repo. |
| D16 | Legacy import: whatever best exercises money/inventory/hp/xp/light/position (my call, decided at step 7). |
| D17 | **Decided:** event log as the record, without the purity: events record results (never re-run rules on replay); reference data in plain tables; persisted projections. |
| D18 | **Name: Reeve** (package `reeve`, CLI `reeve`). Runners-up kept for later: Tally, Greyscreen, Tablewright. |
| D19 | Split-party time: world time is driven by the group furthest behind; the DM may override by hand (logged). |
| D20 | **Terminology:** the log's unit of work is a *transaction* (not "turn", which is the 1e 10-minute unit). World time is an integer count of seconds; the minimum tick is 1 second. |

## 2 · Principles (taken from the campaign's own record)

1. **A rule that has failed twice becomes a machine that refuses**, not a sentence. (`tooling_redesign.md` §2.)
   The old campaign's perfect-record mechanisms were all "cannot do the wrong thing" ones.
2. **Events are the source of truth; state is computed.** Never store a running total.
3. **Corrections are appended**, never edited. History stays auditable.
4. **Validate on write, rebuild on read.**
5. **No rules table is written twice.** Rules are data with citations; code reads the data.
6. **The server does not write narration.** It stores it and enforces who may see it.
7. **Prose caches rot.** Anything derivable (position, hp, funds, light, open obligations) is derived on request.

## 3 · Architecture

> **Code style (decided 2026-10-08):** self-documenting names; no abbreviations (`transaction`, not `txn`;
> `connection`, not `conn`; `sequence_number`, not `seq`) and no single-letter variables outside tiny comprehensions.


```
 clients            Claude Code (CLI/Bash, later MCP)   Web UI (later)   scripts/tests
                              │                              │
 transport                    └───────────  HTTP + SSE  ─────┘          (MCP adapter = same calls)
                                              │
 API layer      authn (token → actor + role) · request validation · per-role response shaping
                                              │
 referee        command handlers: validate → ask ruleset → emit events (one transaction, atomic)
                                              │
 ruleset        pluggable package: tables (data) + hooks (resolve checks, combat, encumbrance…)
                                              │
 core           event store · projections · visibility · transactions/undo · roll registry · clocks · scheduler
                                              │
 storage        SQLite (WAL), single writer.  Deterministic JSONL export → git.
```

Rule of thumb: **core knows nothing about D&D** (it knows entities, places, containers, quantities, clocks,
visibility, transactions). **Ruleset knows D&D** (AC, THAC0, segments, encumbrance bands). **Referee** glues them.

## 4 · Event store

### 4.1 Commands vs events
- A **command** is a request: `move(pc=Dave, to=…)`. It may be refused.
- An **event** is a fact that happened: `EntityMoved`, `DamageDealt`, `CoinTransferred`, `RollMade`, `Narrated`.
- Handlers turn a command into 0..n events inside one **transaction**, atomically. Refusals return a structured reason
  (and are themselves logged at low verbosity, because "what was tried" is useful audit).

### 4.2 Tables (sketch)
```
transactions(id, campaign_id, sequence_number, actor_id, role, command, args_json, created_at,
      status[active|retracted], retracted_by_transaction, parent_checkpoint)
events(id, transaction_id, position_in_transaction, type, payload_json, audience, subject_ids_json,
       world_time, prev_hash, hash)
rolls(id, transaction_id, roll_key, dice, result_json, audience, state[used|unused|voided])
checkpoints(id, name, transaction_id, snapshot_ref)         -- for branching/rewind
```
- `hash` chains events (SHA-256 of prev_hash + canonical payload). Cheap tamper-evidence and a stable ID for the
  git export.
- `world_time` is an integer count of **seconds** (the minimum tick is 1 second, D20), so finer-grained play is
  possible later. Units above that are ruleset vocabulary. For 1e: segment = 6 s, round = 10 segments = 60 s,
  **turn** = 10 rounds = 600 s, plus a day/watch calendar the ruleset supplies. In this document and the code, bare
  "turn" only ever means the 1e 10-minute unit; the unit of work in the log is a **transaction**.

### 4.3 Projections
Current state is a fold over non-retracted events. Implemented as **SQL views + incrementally maintained tables**,
all rebuildable (`server rebuild` must reproduce them byte-for-byte; a test enforces it).
Examples: `entity_state`, `location_of`, `container_contents`, `ledger_balance`, `hp`, `light_fuel`,
`open_obligations`, `known_map(party)`.

### 4.4 Export to git
`server export --out dir/` writes canonical, sorted JSONL per campaign (and per-transaction markdown summaries).
Deterministic: same log ⇒ same bytes. Committing it gives `git log`, offsite backup and a diffable audit trail.
The export is **output only**. Hand edits go through an admin command that appends events.

## 5 · Visibility (the hard part, so it comes early)

### 5.1 Model
Every event carries an `audience`:
- `public` — everyone in the campaign sees it.
- `party` — what the player characters know/perceive (the old `cells.seen`, `journal.md`).
- `actor:<id>` — only that character/seat (private thoughts, secret rolls, a hidden class ability).
- `dm` — truth the players must not see (stocking key, hidden monster hp, offscreen results not yet met).

Facts have a *truth* layer and a *knowledge* layer. The truth ("a pit trap at (3,4)") is a `dm` event.
The knowledge ("the party found a trap at (3,4)") is a separate `party` event emitted by a **reveal** command.
Nothing is leaked by an omission in a query because players' queries are run **only against the party/actor
projections** — the truth tables are not joinable from a player token.

### 5.2 Enforcement
- Player and observer credentials are rejected at the API for any `dm` resource, and response shaping is
  allow-list, not deny-list.
- **Process/OS separation:** the server runs as its own OS user (or container) with the DB not readable by the
  user running a player-role Claude. This closes the "agent just reads the sqlite file" hole. Without it the
  "hard" enforcement is cosmetic.
- Even for a *DM-role* Claude, the DM is a seat, not omniscient by default: it can ask for DM views, but the
  server logs every access to `dm`-audience data (useful for debugging "did I leak?").
- Rolls with hidden outcomes (hear noise, find traps) are made by the server and recorded `dm`; the player sees
  only the outcome as the character would perceive it (a `party` event emitted by the DM/ruleset).
- Hidden-until-met news (the old `offscreen.py` rule) is a first-class pattern: **sealed events** with a
  `reveal_on` condition (e.g. `party meets NPC:wend`); the server releases them when the condition fires.

### 5.3 Role matrix (initial)
| capability | DM | Player(char) | Observer |
|---|---|---|---|
| read public/party state | ✔ | ✔ | ✔ |
| read own actor-private | ✔ (all) | own only | ✘ |
| read `dm` truth | ✔ | ✘ | ✘ |
| declare actions for own character | ✔ (override, logged) | ✔ | ✘ |
| declare actions for **another** PC | ✘ unless `override` flag + reason | ✘ | ✘ |
| narrate | ✔ | in-character speech only | ✘ |
| void a roll / force retract | ✔ | ✘ | ✘ |
| undo own last transaction | ✔ | ✔ (until DM acts on it) | ✘ |

Note the old rule "THE DM DOES NOT DECLARE THE PARTY'S ACTIONS" becomes a permission, not a reminder.

## 6 · The referee

The server resolves mechanics. Illustrative command set (first milestone in bold):

| group | commands |
|---|---|
| **movement** | **`move`**, `walk(direction)`, `enter/leave(location)`, `open/close/lock(door)` |
| **inventory** | **`pickup`, `drop`, `give`, `stow`, `equip`**, `buy`, `sell` |
| **time/light** | **`advance(time)`**, `light/extinguish(source)`, `rest` |
| **checks** | **`roll(table|dice)`**, `check(ability|skill)`, `search`, `listen` |
| **combat** | `begin_encounter`, `declare`, `resolve_round`, `morale`, `flee` |
| **world** | `reveal`, `spawn(entity)`, `set_note` (DM) |
| **social** | `say`, `narrate`, `record_interaction` |
| **meta** | `undo`, `redo`, `checkpoint`, `void_roll`, `export` |

Behaviour of a command, e.g. `move(pc, to)`:
1. Authenticate; check the actor may move that PC.
2. Ruleset + world state: reachable? wall/door? encumbrance-adjusted rate? light? time cost?
3. Emit events (`EntityMoved`, `TimeAdvanced`, `CellsSeen` for the party, `dm`-only consequences such as
   wandering-check results).
4. **Push** the results: player gets what its character perceives; the DM seat gets a DM-view delta, including
   anything triggered (an encounter table roll, a sealed event that matured). Delivery by SSE (and polling).
5. If something requires a DM decision (e.g. an encounter starts), the transaction emits a `DecisionRequired` event for the
   DM seat. **The server never narrates the fiction itself; it hands the DM the facts and the pending question.**

Refusals are structured: `{refused: true, code: "WALL", detail: "…", suggestions: [...]}`. This is the generalisation
of `party.py move` and `combat.py round` refusing.

## 7 · Undo / redo

- **Unit:** a transaction. `undo` appends a `TransactionRetracted` marker; projections skip retracted transactions' events. Redo
  appends `TransactionRestored`. Nothing is deleted.
- **Dependencies:** retracting transaction N when later transactions depend on it is refused unless they are retracted
  too (server computes the dependent chain and shows it). No silent cascades.
- **Rolls:** each roll has a `roll_key` (purpose + dice + subject + context hash).
  - On redo/replay with the same key, the original result is reused (`used`).
  - Rolls no longer needed are kept as `unused`, still visible to the DM.
  - New needed rolls are made fresh. Ambiguous matches are surfaced to the DM.
  - `void_roll` (DM only) marks a roll as void with a reason, when the roll itself was the error.
- **Checkpoints/branches:** `checkpoint` names a transaction; `rewind` creates a **branch** from it rather than
  discarding the later log. Useful for "what if" and for recovering from a bad session.
- **Who may undo:** see §5.3. Anything a player undoes after the DM has *narrated on top of it* requires DM
  consent, because narration is an event with dependencies.

## 8 · Rulesets (the multi-system seam)

```
rulesets/
  adnd1e/
    manifest.toml            # id, version, units, calendar
    tables/*.md|*.toml       # data with *source:* citation lines (parsed, never restated in code)
    hooks.py                 # resolve_check, resolve_attack, encumbrance, morale, reaction, xp, light…
    entities.toml            # entity kinds, stats schema, class/race tables
  toy/                       # tiny 2nd ruleset used only by the test suite to prove the seam is real
```
- Core stores stats as **typed JSON documents validated against the ruleset's schema**; core never interprets them.
- Hooks are pure functions `(state_view, command, rng) → events`. RNG is injected by the core roll registry so
  every die is logged and replayable (generalises `dice.py` + `rolls.tsv`).
- Table loading keeps the old rule: renamed heading ⇒ loud failure; every table needs a citation or is flagged
  `unverified`.
- The toy ruleset is the guard against accidentally baking 1e into core. A core change that breaks the toy
  ruleset's tests is a design bug.

## 9 · World model

- **Entities**: PCs, NPCs, monsters (individuals), items, containers, vehicles. Identity-only records; all
  mutable facts are events.
- **Places**: a graph of locations with optional grids (for dungeon squares) and an overland coordinate frame.
  Location kinds and adjacency are data. Wall/door/secret-door and line-of-sight/light-reach logic are
  ruleset-agnostic geometry in a `geometry` module (port of `mapdb` and `dmap.py light/walk`).
- **Map ingestion** (scanned PDF → grid) stays a separate offline tool that emits a *map import file*; it is out
  of scope for the server.
- **Containers & quantities**: one generic ledger model (item, qty, unit, condition, holder) giving the old
  `inventory_log` + `carry` + `money` + `venture` + `accounts` semantics with a single invariant: **located ==
  owned**, and every coin movement has an asset leg. Violations fail the transaction.
- **Clocks & scheduling**: world time, per-source timers (light, spells), and **agendas**: pre-registered
  outcome tables that tick on schedule, with results sealed until met (§5.2).
- **Obligations / loops**: first-class "things that will close" with required evidence on close (old `loops.py`).
- **Narrative layer**: typed events `Narrated`, `Said(npc, text)`, `DmNote`, `JournalEntry`, `InteractionLogged`.
  Entity "recall" (`who is X, everything they did/said`) is a query over these plus all events with that subject.

## 9b · Concurrent play (split parties)

- Unit of play is a **group**: characters sharing a place and a local time cursor. Seats can own characters in
  several groups.
- Commands lock and validate per group (optimistic concurrency on the group's last transaction), so groups act at the same
  time without blocking each other.
- Each group has its own time cursor. Events are ordered by world time. **World time = the earliest cursor that is
  still behind** (nothing global happens until the laggard group catches up). Shared events (wandering checks,
  scheduled agendas) fire when world time passes them, and may land in several groups at once.
- `split` and `merge` are commands; when cursors cross or groups meet, the DM seat receives a `DecisionRequired`.
- Undo dependency checks are per group; retracting a transaction in one group never touches another unless an event
  linked them (an interaction, a transfer, a merge).
- DM narration is per group/scene, so two scenes can be narrated in parallel and interleaved correctly.
- The server may need to tell the DM seat which group is "behind" and should act next, to keep scenes balanced.

## 10 · API sketch (HTTP, JSON; SSE for push)

```
POST /v1/campaigns                                   create
POST /v1/campaigns/{c}/commands/{name}               run a command (idempotency-key header)
GET  /v1/campaigns/{c}/state/{projection}            role-shaped projections
GET  /v1/campaigns/{c}/events?since=&audience=       role-shaped event stream (SSE: /stream)
GET  /v1/campaigns/{c}/recall/{entity}               everything logged about an entity (role-shaped)
GET  /v1/campaigns/{c}/briefing                      session-start briefing (old brief.py)
POST /v1/campaigns/{c}/undo | redo | checkpoint | rewind
GET  /v1/campaigns/{c}/transactions/{id}/diff                "what changed in this transaction"
POST /v1/admin/export | rebuild | import-legacy      admin credential only
```
- Auth: bearer tokens → `(actor, role, campaign)`. Tokens are issued per seat, revocable. TLS and a real
  identity provider come with hosting (D9); the API shape does not change.
- Idempotency keys prevent a retried client call from double-spending coin or double-moving.
- OpenAPI spec is generated and checked in; the CLI client and MCP adapter are generated/derived from it, so
  the three can never disagree.

## 11 · Clients

1. **`tr` CLI** (first): wraps the API for Claude Code via Bash and for humans. One command per API command,
   JSON out, human table on `--pretty`.
2. **MCP adapter** (second): exposes the same commands as typed tools, scoped by the token's role. Added once the
   API settles. Low cost; do not design around it.
3. **Web UI** (later): player view (map, sheet, inventory, log, move controls) and DM view; SSE for live updates.
4. **Autoplayer harness** (for "Claude plays both" and for tests): a script that drives two seats with two tokens,
   enforcing the process separation of §5.2.

## 12 · Mapping from the old tools (so nothing learned is lost)

| old | becomes |
|---|---|
| `tsvlog.py`, `log.py` | event store + command validation |
| `party.py`, `dmap.py` | `move/walk` + geometry module |
| `combat.py`, `encounter.py`, `dice.py`, `rules.py` | adnd1e hooks + roll registry + table loader |
| `hp.py`, `xp.py`, `light.py`, `clock.py` | projections + ruleset hooks + scheduler |
| `carry.py`, `inventory.py`, `ledger.py`, `venture.py`, `account.py` | one container/ledger model + invariants |
| `loops.py` | obligations |
| `offscreen.py`, `agendas.tsv` | scheduled agendas + sealed events |
| `stocking.py`, `treasure.py`, `bestiary.py` | DM-audience world truth + ruleset tables |
| `entities.py`, `recall.py` | recall query |
| `brief.py` | `/briefing` |
| `audit.py`, `auditor` agent | prose-vs-state audit service (second phase) |
| `build_campaign_db.py` | projections + `rebuild` check |
| `mapimg.py` | later: map renderer per role |

## 13 · Build order (each step ends with passing tests)

1. **Core skeleton:** event store, transactions, hash chain, projections, rebuild-equality test, deterministic export.
2. **Roll registry + undo/redo** including the roll-key rules (§7) — before any game logic, because retrofitting
   undo is the expensive mistake.
3. **Visibility + auth + role shaping + process-separation harness.** Tests: a player token can never retrieve a
   `dm` event by any endpoint, including recall and export.
4. **Toy ruleset** + referee command pipeline (`move`, `pickup/give`, `advance`) end-to-end.
5. **AD&D 1e ruleset v0:** characters, encumbrance, light, movement on a small dungeon, one monster fight.
6. **CLI client** and a first real Claude-DM / human-player session on a tiny module.
7. **Legacy importer** (D8) as the stress test: reproduce the old tools' hp/xp/funds/carry numbers from the
   imported TSV history.
8. **Scheduler/agendas, sealed events, obligations, narrative events.**
9. **MCP adapter, SSE, autoplayer harness** (Claude plays both).
10. **Web UI; hosting hardening.**

Acceptance for "ready to play": a human can play a solo-dungeon session with Claude as DM where every money,
inventory, position, hp, light and clock fact comes from the server, and `export` + git shows a readable audit
of the session.

## 14 · Risks

- **Over-generalising the core.** Mitigation: toy ruleset; add abstractions only when the second ruleset needs them.
- **Visibility leaks via side channels:** error messages, timing, counts ("3 news items waiting" is allowed,
  contents never), ID guessability, exports, logs. Needs a dedicated test suite and review.
- **Process separation is only as good as the deployment.** If both agents share a user account, "hard" is not hard.
- **Referee rigidity:** real play involves rulings. Need a first-class **DM override** command (logged, with
  reason) so the server never blocks the fiction, only makes departures visible.
- **Rules fidelity:** transcribing 1e tables is its own large project; port the existing `rules/*.md` and flag
  anything unverified.
- **Concurrency:** single-writer SQLite is fine for a table of humans and a model; revisit for hosted use.
- **Narrative sprawl:** typed narration events must stay lightweight or the DM will bypass them.

## 15 · Open questions

1. **Hosting target** (home server, VPS, container platform): only needed before step 10.

