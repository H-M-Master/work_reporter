# RecordingSession Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the recording state machine + serialization out of the `WorkReporter` Tk god-class into a Tk-free, unit-tested `RecordingSession` class, with no user-visible behavior change.

**Architecture:** `RecordingSession` owns recording state (debounce/idle/lock/activities/screenshots) and pure decision logic (`poll` returns a `PollResult` the GUI acts on). `WorkReporter` keeps the timer loop, threads, widgets, file I/O, and delegates all recording decisions to the session.

**Tech Stack:** Python 3.10+, dataclasses, threading.Lock, pytest.

---

## File Structure

- Modify: `work_reporter.py` — add `PollResult` + `RecordingSession` near the other module-level helpers (before the `# ─── GUI ───` banner, ~line 700); rewrite the recording methods of `WorkReporter` to delegate.
- Test: `test_work_reporter.py` — add `TestRecordingSession` (pure, no Tk).

Thresholds preserved: 10s debounce, 300s idle, `<3s` (0.05 min) activity ignore, 5s poll interval (stays in GUI).

---

## Task 1: RecordingSession skeleton + record_current_activity

**Files:**
- Modify: `work_reporter.py` (add class before `# ─── GUI` banner)
- Test: `test_work_reporter.py`

- [ ] **Step 1: Write failing tests**

```python
# append near the other test classes
from dataclasses import is_dataclass

class TestRecordingSession:
    def _session(self, start="2026-05-12T09:00:00"):
        return wr.RecordingSession(datetime.fromisoformat(start))

    def test_init_state(self):
        s = self._session()
        assert s.is_running and not s.is_paused
        assert s.activities == []
        assert s.session_start == datetime(2026, 5, 12, 9, 0, 0)
        assert s.last_app == "" and s.last_window == ""
        assert s.last_change_time == s.session_start
        assert s.is_idle is False

    def test_record_activity_appends_with_duration(self):
        s = self._session()
        s.last_app, s.last_window = "VSCode", "a.py"
        s.last_change_time = datetime(2026, 5, 12, 9, 0, 0)
        assert s.record_current_activity(datetime(2026, 5, 12, 9, 10, 0)) is True
        assert s.activities == [{
            "timestamp": "09:00", "app": "VSCode",
            "window": "a.py", "duration_min": 10.0,
        }]

    def test_record_activity_ignores_under_3s(self):
        s = self._session()
        s.last_app = "VSCode"
        s.last_change_time = datetime(2026, 5, 12, 9, 0, 0)
        assert s.record_current_activity(datetime(2026, 5, 12, 9, 0, 2)) is False
        assert s.activities == []

    def test_record_activity_noop_without_last_app(self):
        s = self._session()
        s.last_app = ""
        s.last_change_time = datetime(2026, 5, 12, 9, 0, 0)
        assert s.record_current_activity(datetime(2026, 5, 12, 9, 5, 0)) is False
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: FAIL — `AttributeError: module 'work_reporter' has no attribute 'RecordingSession'`

- [ ] **Step 3: Implement skeleton** — insert before the `# ─── GUI ──` banner in `work_reporter.py`:

```python
from dataclasses import dataclass, field


@dataclass
class PollResult:
    """poll() 的结果：GUI 据此更新界面/起线程。"""
    count_label: str | None = None      # None = 不改 lbl_count
    log_messages: list = field(default_factory=list)
    take_switch_screenshot: bool = False


class RecordingSession:
    """录制领域逻辑：状态机 + 序列化，不依赖 tkinter、无定时器/线程/文件 IO。"""

    def __init__(self, session_start: datetime):
        self.is_running = True
        self.is_paused = False
        self.activities: list = []
        self.session_start = session_start
        self.last_app = ""
        self.last_window = ""
        self.last_change_time: datetime | None = session_start
        self.pending_app = ""
        self.pending_window = ""
        self.pending_since: datetime | None = None
        self.is_idle = False
        self._switch: list = []
        self._backup: list = []
        self._lock = threading.Lock()

    def record_current_activity(self, now: datetime) -> bool:
        """追加一条带时长的活动；无当前窗口或不足 3 秒则忽略。"""
        if not self.last_app or not self.last_change_time:
            return False
        dur = (now - self.last_change_time).total_seconds() / 60
        if dur < 0.05:  # 不足 3 秒忽略
            return False
        self.activities.append({
            "timestamp": self.last_change_time.strftime("%H:%M"),
            "app": self.last_app,
            "window": self.last_window,
            "duration_min": round(dur, 1),
        })
        return True
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: add RecordingSession skeleton + record_current_activity"
```

---

## Task 2: screenshots + serialization (to_state / from_state / to_session_data)

**Files:**
- Modify: `work_reporter.py` (methods on `RecordingSession`)
- Test: `test_work_reporter.py`

- [ ] **Step 1: Write failing tests** (append to `TestRecordingSession`)

```python
    def test_add_and_snapshot_screenshots(self):
        s = self._session()
        s.add_screenshots([Path("/a/1.png"), Path("/a/2.png")], "switch")
        s.add_screenshots([Path("/b/1.png")], "backup")
        switch, backup = s.snapshot_screenshots()
        assert switch == ["/a/1.png", "/a/2.png"]
        assert backup == ["/b/1.png"]

    def test_to_state_roundtrip_via_from_state(self):
        s = self._session()
        s.last_app, s.last_window = "Chrome", "GitHub"
        s.last_change_time = datetime(2026, 5, 12, 9, 5, 0)
        s.activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1.0}]
        s.is_paused = True
        state = s.to_state()
        assert state["status"] == "paused"
        assert state["start"] == "2026-05-12T09:00:00"
        assert state["last_app"] == "Chrome"
        assert state["activities"] == s.activities

        s2 = wr.RecordingSession.from_state(state)
        assert s2 is not None
        assert s2.is_paused is True
        assert s2.session_start == datetime(2026, 5, 12, 9, 0, 0)
        assert s2.last_app == "Chrome"
        assert s2.activities == s.activities
        assert s2.last_change_time == datetime(2026, 5, 12, 9, 5, 0)

    def test_from_state_returns_none_on_corrupt(self):
        assert wr.RecordingSession.from_state({}) is None
        assert wr.RecordingSession.from_state({"start": "garbage", "activities": [1]}) is None
        # valid start but no activities → nothing worth recovering
        assert wr.RecordingSession.from_state(
            {"start": "2026-05-12T09:00:00", "activities": []}) is None

    def test_from_state_drops_missing_screenshot_files(self, tmp_path):
        real = tmp_path / "real.png"
        real.write_bytes(b"x")
        state = {
            "start": "2026-05-12T09:00:00",
            "activities": [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1.0}],
            "switch_screenshots": [str(real), str(tmp_path / "gone.png")],
            "backup_screenshots": [],
        }
        s = wr.RecordingSession.from_state(state)
        switch, _ = s.snapshot_screenshots()
        assert switch == [str(real)]  # missing file filtered out

    def test_to_session_data_shape(self):
        s = self._session()
        s.activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1.0}]
        data = s.to_session_data(datetime(2026, 5, 12, 18, 0, 0), user_notes="备注")
        assert data["date"] == "2026-05-12"
        assert data["start"] == "2026-05-12T09:00:00"
        assert data["end"] == "2026-05-12T18:00:00"
        assert data["activities"] == s.activities
        assert data["user_notes"] == "备注"
        assert data["status"] == "pending"
        assert data["switch_screenshots"] == [] and data["backup_screenshots"] == []
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: FAIL — `AttributeError: 'RecordingSession' object has no attribute 'add_screenshots'`

- [ ] **Step 3: Implement** — add these methods to `RecordingSession`:

```python
    def add_screenshots(self, paths: list, kind: str):
        """线程安全地收集截图路径。kind: 'switch' | 'backup'。"""
        with self._lock:
            (self._switch if kind == "switch" else self._backup).extend(paths)

    def snapshot_screenshots(self) -> tuple[list, list]:
        """加锁快照两个截图列表（返回字符串路径），供序列化。"""
        with self._lock:
            return ([str(p) for p in self._switch],
                    [str(p) for p in self._backup])

    def to_state(self) -> dict:
        """崩溃恢复用的状态 dict（不含 last_update/user_notes，由 GUI 补）。"""
        switch, backup = self.snapshot_screenshots()
        return {
            "status": "paused" if self.is_paused else "recording",
            "start": self.session_start.isoformat() if self.session_start else None,
            "activities": list(self.activities),
            "switch_screenshots": switch,
            "backup_screenshots": backup,
            "last_app": self.last_app,
            "last_window": self.last_window,
            "last_change_time": (
                self.last_change_time.isoformat() if self.last_change_time else None
            ),
        }

    @classmethod
    def from_state(cls, state: dict) -> "RecordingSession | None":
        """从状态 dict 恢复；start 损坏或无活动则返回 None。"""
        start = _safe_fromisoformat(state.get("start"))
        activities = state.get("activities", [])
        if start is None or not activities:
            return None
        s = cls(start)
        s.activities = list(activities)
        s.is_paused = state.get("status") == "paused"
        s.last_app = state.get("last_app", "")
        s.last_window = state.get("last_window", "")
        s.last_change_time = _safe_fromisoformat(state.get("last_change_time"))
        s._switch = [Path(p) for p in state.get("switch_screenshots", []) if Path(p).exists()]
        s._backup = [Path(p) for p in state.get("backup_screenshots", []) if Path(p).exists()]
        return s

    def to_session_data(self, end: datetime, user_notes: str = "") -> dict:
        """生成 session_*.json 的 dict。"""
        switch, backup = self.snapshot_screenshots()
        return {
            "date": self.session_start.strftime("%Y-%m-%d"),
            "start": self.session_start.isoformat(),
            "end": end.isoformat(),
            "activities": list(self.activities),
            "switch_screenshots": switch,
            "backup_screenshots": backup,
            "user_notes": user_notes,
            "status": "pending",
        }
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: RecordingSession screenshot collection + serialization"
```

---

## Task 3: poll() state machine

The heart of the extraction. `poll(now, active_window, idle_seconds, is_locked)` returns a `PollResult`. Behavior mirrors the current `_poll_window` exactly (minus the Tk/timer/thread side effects, which the GUI does based on the result).

**Files:**
- Modify: `work_reporter.py` (add `poll` to `RecordingSession`)
- Test: `test_work_reporter.py`

- [ ] **Step 1: Write failing tests** (append to `TestRecordingSession`)

```python
    def _running(self, start="2026-05-12T09:00:00"):
        s = self._session(start)
        s.last_app, s.last_window = "VSCode", "a.py"
        s.last_change_time = datetime.fromisoformat(start)
        return s

    def test_poll_same_window_no_activity_clears_pending(self):
        s = self._running()
        s.pending_app, s.pending_window = "Chrome", "x"
        s.pending_since = datetime(2026, 5, 12, 9, 0, 30)
        r = s.poll(datetime(2026, 5, 12, 9, 1, 0), ("VSCode", "a.py"), 0, False)
        assert s.activities == []
        assert s.pending_app == "" and s.pending_since is None
        assert r.take_switch_screenshot is False
        assert r.count_label == "已记录 0 条活动"

    def test_poll_new_window_starts_debounce(self):
        s = self._running()
        r = s.poll(datetime(2026, 5, 12, 9, 1, 0), ("Chrome", "GitHub"), 0, False)
        assert s.pending_app == "Chrome" and s.pending_window == "GitHub"
        assert s.pending_since == datetime(2026, 5, 12, 9, 1, 0)
        assert s.last_app == "VSCode"          # not switched yet
        assert r.take_switch_screenshot is False

    def test_poll_debounce_not_elapsed_holds(self):
        s = self._running()
        s.pending_app, s.pending_window = "Chrome", "GitHub"
        s.pending_since = datetime(2026, 5, 12, 9, 1, 0)
        r = s.poll(datetime(2026, 5, 12, 9, 1, 5), ("Chrome", "GitHub"), 0, False)  # 5s < 10s
        assert s.last_app == "VSCode"
        assert s.activities == []
        assert r.take_switch_screenshot is False

    def test_poll_debounce_elapsed_records_and_switches(self):
        s = self._running()
        s.pending_app, s.pending_window = "Chrome", "GitHub"
        s.pending_since = datetime(2026, 5, 12, 9, 1, 0)
        r = s.poll(datetime(2026, 5, 12, 9, 1, 12), ("Chrome", "GitHub"), 0, False)  # 12s >= 10s
        assert len(s.activities) == 1
        assert s.activities[0]["app"] == "VSCode"
        assert s.last_app == "Chrome" and s.last_window == "GitHub"
        assert s.last_change_time == datetime(2026, 5, 12, 9, 1, 0)  # = pending_since
        assert s.pending_app == "" and s.pending_since is None
        assert r.take_switch_screenshot is True
        assert any("切换 → Chrome — GitHub" in m for m in r.log_messages)

    def test_poll_locked_flushes_and_labels(self):
        s = self._running()
        r = s.poll(datetime(2026, 5, 12, 9, 10, 0), ("VSCode", "a.py"), 0, True)
        assert len(s.activities) == 1              # flushed current segment
        assert s.last_app == "" and s.last_window == ""
        assert r.count_label == "🔒 锁屏中"
        assert r.take_switch_screenshot is False

    def test_poll_idle_enter_then_exit(self):
        s = self._running()
        r1 = s.poll(datetime(2026, 5, 12, 9, 10, 0), ("VSCode", "a.py"), 300, False)
        assert s.is_idle is True
        assert len(s.activities) == 1              # flushed on entering idle
        assert s.last_app == ""
        assert r1.count_label == "💤 空闲中"
        assert any("空闲" in m for m in r1.log_messages)

        r2 = s.poll(datetime(2026, 5, 12, 9, 12, 0), ("VSCode", "a.py"), 0, False)
        assert s.is_idle is False
        assert s.last_change_time == datetime(2026, 5, 12, 9, 12, 0)
        assert any("恢复记录" in m for m in r2.log_messages)

    def test_poll_no_app_leaves_count_label_unchanged(self):
        s = self._running()
        r = s.poll(datetime(2026, 5, 12, 9, 1, 0), ("", ""), 0, False)
        assert r.count_label is None               # GUI leaves label as-is
        assert s.activities == []
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: FAIL — `AttributeError: 'RecordingSession' object has no attribute 'poll'`

- [ ] **Step 3: Implement `poll`** — add to `RecordingSession`:

```python
    def poll(self, now: datetime, active_window: tuple,
             idle_seconds: float, is_locked: bool) -> PollResult:
        """5 秒轮询的核心状态机。副作用（起线程/刷界面/落盘）由 GUI 按返回值执行。"""
        logs: list = []

        # ── 锁屏：记完当前段就停 ──
        if is_locked:
            if self.last_app:
                self.record_current_activity(now)
                self.last_app = ""
                self.last_window = ""
            return PollResult(count_label="🔒 锁屏中", log_messages=logs)

        # ── 键鼠空闲进出（≥5 分钟）──
        if idle_seconds >= 300 and not self.is_idle:
            self.is_idle = True
            self.record_current_activity(now)
            self.last_app = ""
            self.last_window = ""
            logs.append("键鼠空闲 ≥5 分钟，暂停记录")
        elif idle_seconds < 300 and self.is_idle:
            self.is_idle = False
            self.last_change_time = now
            logs.append("检测到操作，恢复记录")
        if self.is_idle:
            return PollResult(count_label="💤 空闲中", log_messages=logs)

        app, window = active_window
        if not app:
            # 取不到应用名 → 保持界面不变
            return PollResult(count_label=None, log_messages=logs)

        # ── 窗口切换防抖 ──
        take_shot = False
        if app == self.last_app and window == self.last_window:
            self.pending_app = ""
            self.pending_window = ""
            self.pending_since = None
        elif app == self.pending_app and window == self.pending_window:
            if self.pending_since and (now - self.pending_since).total_seconds() >= 10:
                self.record_current_activity(now)
                take_shot = True
                self.last_app = app
                self.last_window = window
                self.last_change_time = self.pending_since
                self.pending_app = ""
                self.pending_window = ""
                self.pending_since = None
                display = f"{app} — {window}" if window else app
                logs.append(f"切换 → {display}")
        else:
            self.pending_app = app
            self.pending_window = window
            self.pending_since = now

        return PollResult(
            count_label=f"已记录 {len(self.activities)} 条活动",
            log_messages=logs,
            take_switch_screenshot=take_shot,
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest test_work_reporter.py::TestRecordingSession -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: RecordingSession.poll state machine + tests"
```

---

## Task 4: Wire WorkReporter GUI to delegate to RecordingSession

Integration task. `WorkReporter` stops holding recording state directly; it holds
`self.session: RecordingSession | None`. All recording methods delegate. No unit
test (Tk-coupled) — rely on the existing suite staying green + a user smoke test.

**Files:**
- Modify: `work_reporter.py` (`WorkReporter` methods)

### Step 4.1: `__init__` — replace recording-state attrs with `self.session`

- [ ] Replace this block in `__init__`:

```python
        self.client = anthropic.Anthropic(**client_kwargs)
        self.is_running = False
        self.is_paused = False
        self.activities: list = []
        self.session_start: datetime | None = None
        self._poll_timer = None
        self._last_app = ""
        self._last_window = ""
        self._last_change_time: datetime | None = None
        # 防抖：暂存待确认的窗口切换
        self._pending_app = ""
        self._pending_window = ""
        self._pending_since: datetime | None = None
        # 截图路径收集（结束时采样发给 AI）；后台线程会 extend，需加锁
        self._state_lock = threading.Lock()
        self._switch_screenshots: list[Path] = []   # 窗口切换时截的
        self._backup_screenshots: list[Path] = []   # 15分钟兜底截的
        # 定时截图（每 5 分钟）
        self._periodic_timer = None
        # 空闲检测
        self._is_idle = False
        # 当前/最近的 session 文件（用于重试）
        self._current_session_file: Path | None = None
```

with:

```python
        self.client = anthropic.Anthropic(**client_kwargs)
        self.session: RecordingSession | None = None  # 录制领域对象；None=未在录制
        self._poll_timer = None
        self._periodic_timer = None
        # 当前/最近的 session 文件（用于重试）
        self._current_session_file: Path | None = None
```

### Step 4.2: `start_work`

- [ ] Replace `start_work` body up to (and including) the initial-window block:

```python
    def start_work(self):
        now = datetime.now()
        self.session = RecordingSession(now)

        self.btn_start.set_enabled(False)
        self.btn_pause.set_enabled(True)
        self.btn_pause.set_text("❚❚ PAUSE")
        self.btn_pause.set_color(COLORS["pause"])
        self.btn_stop.set_enabled(True)
        self.btn_snap.set_enabled(True)
        self._draw_status_dot(COLORS["ok"])
        self.lbl_status.config(
            text=f"RECORDING  ·  开始于 {now.strftime('%H:%M')}",
            fg=COLORS["text"],
        )

        self._log("开始工作记录（窗口追踪模式）")
        app, win = get_active_window()
        if app:
            self.session.last_app = app
            self.session.last_window = win
            self.session.last_change_time = now
            display = f"{app} — {win}" if win else app
            self._log(f"当前 → {display}")
            threading.Thread(target=self._take_switch_screenshot, daemon=True).start()
        self._save_running_state()
        self._poll_window()
        self._schedule_periodic_screenshot()
```

### Step 4.3: `_toggle_pause`

- [ ] Replace the `is_paused` reads/writes to go through `self.session`. New body:

```python
    def _toggle_pause(self):
        if not self.session or not self.session.is_running:
            return
        now = datetime.now()
        if self.session.is_paused:
            self.session.is_paused = False
            self.session.last_change_time = now
            self.btn_pause.set_text("❚❚ PAUSE")
            self.btn_pause.set_color(COLORS["pause"])
            self.btn_snap.set_enabled(True)
            self._draw_status_dot(COLORS["ok"])
            self.lbl_status.config(
                text=f"RECORDING  ·  开始于 {self.session.session_start.strftime('%H:%M')}",
                fg=COLORS["text"],
            )
            self._log("已继续记录")
            self._save_running_state()
            self._poll_window()
            self._schedule_periodic_screenshot()
        else:
            self.session.is_paused = True
            if self._poll_timer:
                self.root.after_cancel(self._poll_timer)
                self._poll_timer = None
            if self._periodic_timer:
                self.root.after_cancel(self._periodic_timer)
                self._periodic_timer = None
            self.session.record_current_activity(now)
            self.btn_pause.set_text("▶ RESUME")
            self.btn_pause.set_color(COLORS["resume"])
            self.btn_snap.set_enabled(False)
            self._draw_status_dot(COLORS["warn"])
            self.lbl_status.config(text="PAUSED  ·  已暂停", fg=COLORS["warn"])
            self._log("已暂停记录")
            self._save_running_state()
```

- [ ] **Commit checkpoint** (after 4.1–4.3 compile; full wiring finishes in 4.4–4.7 before running):

Deferred — commit once the whole class compiles at end of Task 4. Continue.

### Step 4.4: screenshot methods + periodic guard

- [ ] Replace `_manual_screenshot`, `_do_periodic_screenshot`, `_take_switch_screenshot`, `_take_backup_screenshot`, `_schedule_periodic_screenshot` guards to use `self.session`. Also delete `_record_current_activity` (now on the session):

```python
    def _manual_screenshot(self):
        """手动截图并记录当前窗口"""
        if not self.session or not self.session.is_running or self.session.is_paused:
            return
        paths = take_screenshot()
        if paths:
            self.session.add_screenshots(paths, "switch")
            self._log(f"手动截图 -> {len(paths)} 张")

    def _schedule_periodic_screenshot(self):
        """每 5 分钟定时截图"""
        if not self.session or not self.session.is_running or self.session.is_paused:
            return
        self._periodic_timer = self.root.after(300_000, self._do_periodic_screenshot)

    def _do_periodic_screenshot(self):
        """执行定时截图"""
        if not self.session or not self.session.is_running or self.session.is_paused:
            return
        if not _is_screen_locked() and not self.session.is_idle:
            threading.Thread(target=self._take_backup_screenshot, daemon=True).start()
            self._log("定时截图（5分钟）")
        self._schedule_periodic_screenshot()

    def _take_switch_screenshot(self):
        """窗口切换时截图（后台线程调用）：session 内部加锁，落盘调度回主线程。"""
        paths = take_screenshot()
        if self.session:
            self.session.add_screenshots(paths, "switch")
        self.root.after(0, self._save_running_state)

    def _take_backup_screenshot(self):
        """定时截图（后台线程调用）：session 内部加锁，落盘调度回主线程。"""
        paths = take_screenshot()
        if self.session:
            self.session.add_screenshots(paths, "backup")
        self.root.after(0, self._save_running_state)
```

Delete the whole `_record_current_activity` method (delegated to `self.session.record_current_activity`).

### Step 4.5: `_poll_window` — delegate to `session.poll`

- [ ] Replace the entire `_poll_window` body:

```python
    def _poll_window(self):
        """每 5 秒轮询活跃窗口：收集输入 → 交给 session 决策 → 按结果刷新界面。"""
        if not self.session or not self.session.is_running or self.session.is_paused:
            return
        result = self.session.poll(
            datetime.now(),
            get_active_window(),
            _get_idle_seconds(),
            _is_screen_locked(),
        )
        for msg in result.log_messages:
            self._log(msg)
        if result.count_label is not None:
            self.lbl_count.config(text=result.count_label)
        if result.take_switch_screenshot:
            threading.Thread(target=self._take_switch_screenshot, daemon=True).start()
        self._save_running_state()
        self._poll_timer = self.root.after(5000, self._poll_window)
```

Note (benign, documented in spec): `get_active_window()` is now called every tick,
including while locked/idle. No recorded data changes. `_save_running_state()` also
runs every tick now (was: only on activity/screenshot) — more accurate crash
recovery, still internal-only.

### Step 4.6: `_save_running_state` — build from `session.to_state()`

- [ ] Replace `_save_running_state`:

```python
    def _save_running_state(self):
        """把当前录制状态写盘（崩溃恢复）。始终在主线程执行，可安全读 Tk。"""
        if not self.session or not self.session.is_running:
            return
        state = self.session.to_state()
        state["last_update"] = datetime.now().isoformat()
        state["user_notes"] = ""
        try:
            state["user_notes"] = self.notes_box.get("1.0", tk.END).strip()
        except Exception:
            pass
        try:
            _atomic_write_json(RUNNING_SESSION_FILE, state)
        except Exception:
            pass
```

### Step 4.7: recovery — `_recover_and_resume`, `_recover_and_stop`, `stop_work`

- [ ] `_recover_and_resume(state)`: build the session via `from_state`, then adjust
  for resume (behavior identical to old: `last_change_time = now`, reset debounce/idle):

```python
    def _recover_and_resume(self, state: dict):
        """恢复状态并继续记录"""
        self.session = RecordingSession.from_state(state)
        if self.session is None:
            self._delete_running_state()
            return
        now = datetime.now()
        self.session.is_running = True
        self.session.is_paused = False
        self.session.last_change_time = now
        self.session.pending_app = ""
        self.session.pending_window = ""
        self.session.pending_since = None
        self.session.is_idle = False

        notes = state.get("user_notes", "")
        if notes:
            self.notes_box.delete("1.0", tk.END)
            self.notes_box.insert("1.0", notes)

        self.btn_start.set_enabled(False)
        self.btn_pause.set_enabled(True)
        self.btn_pause.set_text("❚❚ PAUSE")
        self.btn_pause.set_color(COLORS["pause"])
        self.btn_stop.set_enabled(True)
        self.btn_snap.set_enabled(True)
        self._draw_status_dot(COLORS["ok"])
        self.lbl_status.config(
            text=f"RECORDING  ·  恢复记录（开始于 {self.session.session_start.strftime('%H:%M')}）",
            fg=COLORS["text"],
        )
        self.lbl_count.config(text=f"已记录 {len(self.session.activities)} 条活动")
        self._log(f"已恢复上次记录（{len(self.session.activities)} 条活动），继续记录中")
        self._poll_window()
        self._schedule_periodic_screenshot()
```

- [ ] `_recover_and_stop(state)`: build session via `from_state`, write session file
  via `to_session_data`, then generate:

```python
    def _recover_and_stop(self, state: dict):
        """用恢复的数据直接生成日报"""
        self.session = RecordingSession.from_state(state)
        if self.session is None:
            self._delete_running_state()
            return
        notes = state.get("user_notes", "")
        if notes:
            self.notes_box.delete("1.0", tk.END)
            self.notes_box.insert("1.0", notes)

        self._delete_running_state()

        ts = self.session.session_start.strftime("%Y%m%d_%H%M%S")
        session_file = SESSION_DIR / f"session_{ts}.json"
        end = _safe_fromisoformat(state.get("last_update")) or datetime.now()
        _atomic_write_json(session_file, self.session.to_session_data(end, notes))

        self._current_session_file = session_file
        self._log(f"从中断记录生成日报（{len(self.session.activities)} 条活动）...")
        self._run_generation(session_file)
```

- [ ] `stop_work`: delegate. Replace body:

```python
    def stop_work(self):
        if not self.session or not self.session.is_running:
            return
        now = datetime.now()
        self.session.record_current_activity(now)
        self.session.is_running = False
        self.session.is_paused = False
        if self._poll_timer:
            self.root.after_cancel(self._poll_timer)
            self._poll_timer = None
        if self._periodic_timer:
            self.root.after_cancel(self._periodic_timer)
            self._periodic_timer = None

        self.btn_start.set_enabled(True)
        self.btn_pause.set_text("❚❚ PAUSE")
        self.btn_pause.set_color(COLORS["pause"])
        self.btn_pause.set_enabled(False)
        self.btn_stop.set_enabled(False)
        self.btn_snap.set_enabled(False)
        self._draw_status_dot(COLORS["warn"])
        self.lbl_status.config(text="GENERATING  ·  正在生成日报", fg=COLORS["warn"])

        if not self.session.activities:
            self._log("没有任何活动记录，跳过日报生成。")
            self._draw_status_dot(COLORS["text_sub"])
            self.lbl_status.config(text="STANDBY  ·  等待开始工作", fg=COLORS["text_sub"])
            return

        notes = self.notes_box.get("1.0", tk.END).strip()
        if notes:
            self._log(f"已读取用户补充说明（{len(notes)}字）")

        ts = self.session.session_start.strftime("%Y%m%d_%H%M%S")
        session_file = SESSION_DIR / f"session_{ts}.json"
        _atomic_write_json(session_file, self.session.to_session_data(now, notes))
        self._log(f"已保存 session: {session_file.name}")

        self._delete_running_state()
        self._current_session_file = session_file
        self._log(f"共记录 {len(self.session.activities)} 条活动，开始生成日报...")
        self._run_generation(session_file)
```

- [ ] Check `_on_close` (and anywhere else) for `self.is_running`; replace with
  `self.session and self.session.is_running`. Grep: `grep -n "self\.is_running\|self\.is_paused\|self\._last_\|self\._pending_\|self\._switch_screenshots\|self\._backup_screenshots\|self\._is_idle\|self\.activities\|self\.session_start\|self\._state_lock\|_record_current_activity" work_reporter.py` — every hit must be migrated or removed.

### Step 4.8: verify + commit

- [ ] **Compile & full suite:**

Run: `python3 -m py_compile work_reporter.py && python3 -m pytest -q`
Expected: compiles; all tests PASS (existing 47 + new RecordingSession tests).

- [ ] **Grep for leftover old attrs** (must be empty):

Run: `grep -nE "self\.(is_running|is_paused|activities|session_start|_last_app|_last_window|_last_change_time|_pending_|_switch_screenshots|_backup_screenshots|_is_idle|_state_lock)\b|_record_current_activity" work_reporter.py`
Expected: no output.

- [ ] **Commit:**

```bash
git add work_reporter.py
git commit -m "refactor: WorkReporter delegates recording to RecordingSession"
```

- [ ] **User smoke test (REQUIRED — I can't run the GUI):** `open WorkReporter.app`
  (or `./run.sh`), record a few window switches, pause/resume, stop → generate, and
  confirm the activity log + report look correct; then relaunch to confirm the
  crash-recovery prompt still appears if a session was interrupted.

---

## Self-Review

- **Spec coverage:** ① interface (poll/record/add_screenshots/snapshot/to_state/
  from_state/to_session_data) — Tasks 1-3 ✓. Thin GUI delegation — Task 4 ✓.
  Behavior parity tests (debounce 10s / idle 300s / lock / <3s / recovery) —
  Task 3 + Task 2 ✓. Benign osascript-while-locked + per-tick save noted ✓.
- **Placeholders:** none — every step has full code/commands.
- **Type consistency:** `PollResult(count_label, log_messages, take_switch_screenshot)`,
  `poll(now, active_window, idle_seconds, is_locked)`, `add_screenshots(paths, kind)`,
  `to_session_data(end, user_notes)`, `from_state(state)->RecordingSession|None` —
  used consistently across tasks and GUI wiring.
