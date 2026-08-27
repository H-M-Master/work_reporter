# P2 Design — Recording domain extraction (and the road to a thin GUI)

Date: 2026-08-27
Status: Approved (approach A), phase ① first

## Context

`work_reporter.py` is a single file whose `WorkReporter` class (~1000 lines, 27
methods) fuses six concerns: the recording state machine, screenshot threads,
crash-recovery persistence, report/Lark orchestration, and all Tkinter GUI. The
pure helper layer (collectors, hashing, sampling, `_build_instruction`) is tested;
the `WorkReporter` class is not (coverage ~34%, and the untested remainder is
almost entirely this class). P0/P1 hardening is done and committed.

## Goal

Shrink `WorkReporter` to a **thin GUI shell** by moving business logic into
independently-testable units. P2 has four independent phases, done in order, each
its own commit + tests, with a pause for user review between phases:

1. **Recording domain extraction** — `RecordingSession` (this doc's focus).
2. **Config object** — replace the mutated module-level globals.
3. **LLM provider seam** — isolate the Anthropic-specific calls behind an interface.
4. **Privacy** — per-source opt-in, secret scrubbing, retention/purge.

## Non-goals

- No change to **user-visible behavior** in phase ①. Same recording rules, same
  reports, same files, same GUI. This is a pure structural refactor.
- No new features in phase ①.
- Phases ②③④ are described only at a high level here; each gets its own design
  pass when we reach it.

## Phase ① — `RecordingSession` (Tk-free domain class)

### Approach (chosen: A)

Extract the recording state and its **pure decision logic** into a class that does
not import `tkinter`, owns no timers, spawns no threads, and does no file I/O. The
GUI drives the 5-second tick, gathers inputs, calls the session, and reacts to a
structured result.

Rejected: (B) extracting only the debounce/idle predicate functions — too little
of the god-class moves, so the untested core stays untested; (C) a full
MVC/observer framework — overkill for a single-window personal tool.

### State that moves into `RecordingSession`

`is_running`, `is_paused`, `activities`, `session_start`, `_last_app`,
`_last_window`, `_last_change_time`, the debounce trio (`_pending_app`,
`_pending_window`, `_pending_since`), `_is_idle`, the two screenshot lists, and the
`_state_lock`.

### Interface (sketch — final names settled during TDD)

| Method | Responsibility (pure, testable) |
| --- | --- |
| `RecordingSession(now)` | begin a recording; initialize state |
| `poll(now, active_window, idle_seconds, is_locked) -> PollResult` | the state machine: lock handling, idle enter/exit, debounce-confirm a window switch, decide whether to record an activity and whether a switch screenshot is due |
| `record_current_activity(now) -> bool` | append one activity with duration (the `<3s` rule preserved) |
| `add_screenshots(paths, kind)` | thread-safe collection into the switch/backup list (holds `_state_lock`) |
| `snapshot_screenshots()` | locked copy of both lists (for serialization) |
| `to_state()` / `from_state(dict) -> RecordingSession \| None` | crash-recovery serialize / deserialize; `from_state` returns `None` on corrupt data (uses `_safe_fromisoformat`) |
| `to_session_data(end)` | build the `session_*.json` dict |

`PollResult` is a small dataclass: `count_label: str`, `log_messages: list[str]`,
`take_switch_screenshot: bool`. The GUI applies it (updates `lbl_count`, appends
each log line, spawns the screenshot thread if requested).

### Responsibility split

- **`RecordingSession` (pure):** all recording decisions and state; serialization
  to/from dicts.
- **`WorkReporter` (GUI):** `root.after` timer loop; gathering inputs each tick via
  the existing module functions `get_active_window()`, `_get_idle_seconds()`,
  `_is_screen_locked()`; spawning screenshot threads (`take_screenshot()`);
  updating labels/log/messagebox; file I/O via the existing `_atomic_write_json`;
  the generation/Lark flow (untouched in ①).

### Known, harmless internal change

The GUI will gather `(app, window)` every tick and pass it in, so `osascript` is
queried once even while locked/idle (the old code skipped it in those branches).
This changes no recorded data and nothing user-visible; it only simplifies the
interface. Documented so it isn't mistaken for a regression.

### Testing strategy

Unit-test `RecordingSession` with **no Tk**, feeding synthetic input sequences:

- same window repeated → no new activity;
- switch that hasn't cleared the 10s debounce → pending, no record;
- switch past the debounce → one activity recorded + `take_switch_screenshot=True`;
- idle ≥ 300s → activity flushed, enters idle; idle < 300s → resumes;
- screen locked → activity flushed, `count_label == "🔒 锁屏中"`;
- `record_current_activity` respects the `<3s` ignore rule;
- `to_state`/`from_state` round-trip; `from_state` returns `None` on corrupt input;
- `to_session_data` shape matches what `_run_generation` reads.

Behavior parity is asserted against the current rules (thresholds: 10s debounce,
300s idle, 3s minimum activity, 5s poll interval kept in the GUI).

### Risk & mitigation

Biggest risk: a subtle behavior regression in recording, which I cannot catch by
running the GUI headlessly. Mitigations: (1) exhaustive unit tests replicating the
current rules; (2) keep the GUI glue mechanical; (3) the full suite + pre-commit
gate must stay green; (4) **user smoke test** after the commit — `open
WorkReporter.app`, do a real record → generate cycle, confirm the activity log and
report look right, and verify crash-recovery still prompts on restart.

## Phases ②③④ (high level, designed later)

- **② Config object:** a `Config` dataclass loaded once, passed where needed,
  replacing `global MODEL/…`; Settings mutates the object and writes via
  `_write_config`. Removes the module-global mutation smell.
- **③ LLM provider seam:** an interface (e.g. `generate(messages) -> (text, usage)`)
  with an `AnthropicProvider`; `generate_reports` depends on the interface, so a
  different provider/proxy is a single new class.
- **④ Privacy:** per-source opt-in flags (screenshots/shell/browser/git), a
  scrubbing denylist applied before data leaves the machine, and retention/purge
  for `sessions/` and `reports/` (mirroring the 30-day screenshot policy). Involves
  product choices — will confirm specifics before implementing.
