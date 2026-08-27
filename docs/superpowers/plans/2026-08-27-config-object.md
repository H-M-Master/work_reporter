# Config Object Implementation Plan (P2 phase ②)

> **For agentic workers:** Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** Replace the 8 mutated module-level config globals (`MODEL`, `SCREENSHOT_MAX_WIDTH`, `MAX_TOKENS_DETAILED`, `MAX_TOKENS_SUMMARY`, `USER_BACKGROUND`, `EXTRA_INSTRUCTIONS`, `LARK_DOC_URL`, `LARK_SCRIPT_DIR`) and the raw `CFG` dict with a single `Config` dataclass singleton `CONFIG`, eliminating the `global MODEL, MAX_TOKENS…` mutation in `_save()`.

**Architecture:** `Config` dataclass with `load()`/`to_dict()`/`save()` + `effective_user_background`/`lark_script_dir_path` properties. Module singleton `CONFIG = Config.load()`. Read sites use `CONFIG.field`. No behavior change (same config.json shape, same values).

**Tech Stack:** Python 3.10+, dataclasses, pytest.

Read sites to migrate (verified): `take_screenshot` (screenshot_max_width), `_build_instruction` (user_background/extra_instructions), `generate_reports` (model/max_tokens_detailed), `_run_generation` Lark block (lark_doc_url/lark_script_dir), `WorkReporter.__init__` (api_key/base_url via CFG), `_open_settings` field defaults, `_save`.

---

## Task 1: Add the `Config` dataclass + `CONFIG` singleton (coexists with old globals)

**Files:** Modify `work_reporter.py` (add class after `DEFAULT_USER_BACKGROUND` / `_load_config`, before the current `CFG = _load_config()` block); Test `test_work_reporter.py`.

- [ ] **Step 1: Write failing tests** (append a new class):

```python
class TestConfig:
    def test_from_dict_maps_nested_keys(self):
        c = wr.Config.from_dict({
            "model": "m1", "api_key": "k", "base_url": "u",
            "max_tokens": {"detailed_report": 100, "summary_report": 20},
            "screenshot": {"max_width": 640},
            "user_background": "bg", "extra_instructions": "ex",
            "lark": {"doc_url": "d", "script_dir": "~/s"},
        })
        assert c.model == "m1" and c.api_key == "k" and c.base_url == "u"
        assert c.max_tokens_detailed == 100 and c.max_tokens_summary == 20
        assert c.screenshot_max_width == 640
        assert c.user_background == "bg" and c.extra_instructions == "ex"
        assert c.lark_doc_url == "d" and c.lark_script_dir == "~/s"

    def test_from_dict_defaults(self):
        c = wr.Config.from_dict({})
        assert c.model == "claude-haiku-4-5-20251001"
        assert c.max_tokens_detailed == 8192 and c.max_tokens_summary == 1024
        assert c.screenshot_max_width == 1280
        assert c.user_background == "" and c.lark_doc_url == ""

    def test_to_dict_roundtrip(self):
        d = {
            "model": "m", "api_key": "k", "base_url": "b",
            "max_tokens": {"detailed_report": 1, "summary_report": 2},
            "screenshot": {"max_width": 3},
            "user_background": "bg", "extra_instructions": "ex",
            "lark": {"doc_url": "d", "script_dir": "s"},
        }
        assert wr.Config.from_dict(d).to_dict() == d

    def test_effective_user_background_falls_back_to_default(self):
        assert wr.Config(user_background="").effective_user_background == wr.DEFAULT_USER_BACKGROUND
        assert wr.Config(user_background="   ").effective_user_background == wr.DEFAULT_USER_BACKGROUND
        assert wr.Config(user_background="我是后端").effective_user_background == "我是后端"

    def test_lark_script_dir_path(self):
        assert wr.Config(lark_script_dir="").lark_script_dir_path == Path()
        assert wr.Config(lark_script_dir="~/x").lark_script_dir_path == Path(os.path.expanduser("~/x"))

    def test_module_singleton_exists(self):
        assert isinstance(wr.CONFIG, wr.Config)
```

Note: the test file needs `import os` at top — add it if missing.

- [ ] **Step 2: Run to verify fail:** `python3 -m pytest test_work_reporter.py::TestConfig -q` → FAIL (`Config` missing).

- [ ] **Step 3: Implement** — add after `DEFAULT_USER_BACKGROUND` is defined and after `_load_config`/`_write_config` exist (i.e. right before the current `CFG = _load_config()` line):

```python
@dataclass
class Config:
    """全部用户配置的单一来源。取代散落的模块级全局变量。"""
    model: str = "claude-haiku-4-5-20251001"
    api_key: str = ""
    base_url: str = ""
    max_tokens_detailed: int = 8192
    max_tokens_summary: int = 1024
    screenshot_max_width: int = 1280
    user_background: str = ""       # 原始值；"" 表示用默认
    extra_instructions: str = ""
    lark_doc_url: str = ""
    lark_script_dir: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        mt = d.get("max_tokens", {}) or {}
        sc = d.get("screenshot", {}) or {}
        lk = d.get("lark", {}) or {}
        return cls(
            model=d.get("model", "claude-haiku-4-5-20251001"),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            max_tokens_detailed=mt.get("detailed_report", 8192),
            max_tokens_summary=mt.get("summary_report", 1024),
            screenshot_max_width=sc.get("max_width", 1280),
            user_background=d.get("user_background", ""),
            extra_instructions=d.get("extra_instructions", ""),
            lark_doc_url=lk.get("doc_url", ""),
            lark_script_dir=lk.get("script_dir", ""),
        )

    @classmethod
    def load(cls) -> "Config":
        return cls.from_dict(_load_config())

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "max_tokens": {
                "detailed_report": self.max_tokens_detailed,
                "summary_report": self.max_tokens_summary,
            },
            "screenshot": {"max_width": self.screenshot_max_width},
            "user_background": self.user_background,
            "extra_instructions": self.extra_instructions,
            "lark": {"doc_url": self.lark_doc_url, "script_dir": self.lark_script_dir},
        }

    def save(self):
        _write_config(self.to_dict())

    @property
    def effective_user_background(self) -> str:
        return self.user_background.strip() or DEFAULT_USER_BACKGROUND

    @property
    def lark_script_dir_path(self) -> Path:
        return Path(os.path.expanduser(self.lark_script_dir)) if self.lark_script_dir else Path()


CONFIG = Config.load()
```

Leave the existing `CFG = _load_config()` and the 8 globals in place for now (removed in Task 3).

- [ ] **Step 4: Run to verify pass:** `python3 -m pytest test_work_reporter.py::TestConfig -q` then full `python3 -m pytest -q`. Both pass.

- [ ] **Step 5: Commit:**
```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: add Config dataclass + CONFIG singleton (P2 phase 2, step 1)"
```

---

## Task 2: Migrate all READ sites from globals to `CONFIG`

**Files:** Modify `work_reporter.py` (read sites) + `test_work_reporter.py` (2 patches).

- [ ] **Step 1: Update the two tests that patch the old globals.** In `test_work_reporter.py`, `TestBuildInstruction`:
  - `test_user_background_is_configurable`: change `with patch.object(wr, "USER_BACKGROUND", "我是一名后端开发工程师，负责支付系统"):` to `with patch.object(wr.CONFIG, "user_background", "我是一名后端开发工程师，负责支付系统"):`
  - `test_extra_instructions_are_appended_and_take_effect`: change `with patch.object(wr, "EXTRA_INSTRUCTIONS", "请着重描述踩坑与调试细节"):` to `with patch.object(wr.CONFIG, "extra_instructions", "请着重描述踩坑与调试细节"):`

- [ ] **Step 2: Run — the two tests now FAIL** (code still reads the globals, so patching CONFIG has no effect): `python3 -m pytest test_work_reporter.py::TestBuildInstruction -q` → 2 fail. This is the red step for the read migration.

- [ ] **Step 3: Migrate read sites** in `work_reporter.py`:
  - `take_screenshot`: replace all three `SCREENSHOT_MAX_WIDTH` with `CONFIG.screenshot_max_width`.
  - `_build_instruction`: replace `if EXTRA_INSTRUCTIONS:` → `if CONFIG.extra_instructions:`, `rules += f"\n- {EXTRA_INSTRUCTIONS}"` → `f"\n- {CONFIG.extra_instructions}"`, and `f"{USER_BACKGROUND}\n"` → `f"{CONFIG.effective_user_background}\n"`.
  - `generate_reports` `client.messages.create(...)`: `model=MODEL` → `model=CONFIG.model`, `max_tokens=MAX_TOKENS_DETAILED` → `max_tokens=CONFIG.max_tokens_detailed`.
  - `_run_generation` Lark block: `not LARK_DOC_URL` → `not CONFIG.lark_doc_url`; `LARK_DOC_URL.rstrip(...)` → `CONFIG.lark_doc_url.rstrip(...)`; `"--url", LARK_DOC_URL` → `"--url", CONFIG.lark_doc_url`; `cwd=str(LARK_SCRIPT_DIR)` → `cwd=str(CONFIG.lark_script_dir_path)`.
  - `WorkReporter.__init__`: `CFG.get("api_key", "")` → `CONFIG.api_key`; `CFG.get("base_url", "")` → `CONFIG.base_url`.
  - `_open_settings` field defaults: `MODEL`→`CONFIG.model`, `MAX_TOKENS_DETAILED`→`CONFIG.max_tokens_detailed`, `MAX_TOKENS_SUMMARY`→`CONFIG.max_tokens_summary`, `SCREENSHOT_MAX_WIDTH`→`CONFIG.screenshot_max_width`, `LARK_DOC_URL`→`CONFIG.lark_doc_url`, `str(LARK_SCRIPT_DIR)`→`str(CONFIG.lark_script_dir_path)`, `CFG.get("api_key", "")`→`CONFIG.api_key`, `CFG.get("base_url", "")`→`CONFIG.base_url`, `USER_BACKGROUND`→`CONFIG.effective_user_background`, `EXTRA_INSTRUCTIONS`→`CONFIG.extra_instructions`.
  - Do NOT touch `_save()` yet (Task 3).

- [ ] **Step 4: Run to verify pass:** `python3 -m pytest -q` → all green.

- [ ] **Step 5: Commit:**
```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: read config via CONFIG object at all read sites (P2 phase 2, step 2)"
```

---

## Task 3: Rewrite `_save()` to mutate CONFIG; delete old globals + `CFG`

**Files:** Modify `work_reporter.py`.

- [ ] **Step 1: Rewrite `_save()`** in `_open_settings`. Replace the whole `def _save():` body with:

```python
        def _save():
            CONFIG.model = fields["model"].get().strip()
            CONFIG.max_tokens_detailed = int(fields["mt_detailed"].get())
            CONFIG.max_tokens_summary = int(fields["mt_summary"].get())
            CONFIG.screenshot_max_width = int(fields["max_width"].get())
            CONFIG.lark_doc_url = fields["lark_url"].get().strip()
            CONFIG.lark_script_dir = os.path.expanduser(fields["lark_dir"].get().strip())
            ub = fields["user_background"].get("1.0", tk.END).strip()
            # 与默认一致时存空串，避免固化默认文案
            CONFIG.user_background = "" if ub == DEFAULT_USER_BACKGROUND else ub
            CONFIG.extra_instructions = fields["extra_instructions"].get("1.0", tk.END).strip()
            CONFIG.api_key = fields["api_key"].get().strip()
            CONFIG.base_url = fields["base_url"].get().strip()

            # 重建客户端（API Key / Base URL 变更时生效）
            if CONFIG.api_key:
                ck = {"api_key": CONFIG.api_key}
                if CONFIG.base_url:
                    url = CONFIG.base_url
                    if url.endswith("/bedrock"):
                        url = url[: -len("/bedrock")]
                    ck["base_url"] = url
                self.client = anthropic.Anthropic(**ck)
            CONFIG.save()
            self._log("配置已保存（config.json 权限 0600）")
            win.destroy()
```

- [ ] **Step 2: Delete the now-unused module globals.** Remove these lines (the block after `CONFIG = Config.load()` — or wherever they sit): `CFG = _load_config()`, `MODEL = …`, `SCREENSHOT_MAX_WIDTH = …`, `MAX_TOKENS_DETAILED = …`, `MAX_TOKENS_SUMMARY = …`, `USER_BACKGROUND = …`, `EXTRA_INSTRUCTIONS = …`, `LARK_DOC_URL = …`, `LARK_SCRIPT_DIR = …`. Keep `DEFAULT_USER_BACKGROUND` and the `# ───` divider comment.

- [ ] **Step 3: Verify nothing references the deleted names.** Run:
```bash
grep -nE "\b(CFG|MODEL|SCREENSHOT_MAX_WIDTH|MAX_TOKENS_DETAILED|MAX_TOKENS_SUMMARY|USER_BACKGROUND|EXTRA_INSTRUCTIONS|LARK_DOC_URL|LARK_SCRIPT_DIR)\b" work_reporter.py
```
Expected remaining hits ONLY: inside `Config`/`Config.from_dict`/`to_dict` (which don't use these names — they use `self.*`), and `DEFAULT_USER_BACKGROUND` (different name, won't match). So expected: NO hits for the bare names above. If any hit remains, migrate it. (`MODEL`/etc. as dict string keys like `"model"` won't match `\bMODEL\b`.)

- [ ] **Step 4: Verify:** `python3 -m py_compile work_reporter.py` (OK) and `python3 -m pytest -q` (all green).

- [ ] **Step 5: Commit:**
```bash
git add work_reporter.py
git commit -m "refactor: settings mutates CONFIG; remove old config module globals (P2 phase 2, step 3)"
```

---

## Self-Review

- **Spec coverage:** Config dataclass (load/to_dict/save/effective_user_background/lark_script_dir_path) — Task 1 ✓. All read sites migrated — Task 2 ✓. `_save` mutates CONFIG, `global`-of-8 removed, old globals + CFG deleted — Task 3 ✓.
- **Placeholders:** none.
- **Type consistency:** `CONFIG.model`, `.max_tokens_detailed`, `.max_tokens_summary`, `.screenshot_max_width`, `.effective_user_background`, `.extra_instructions`, `.lark_doc_url`, `.lark_script_dir_path`, `.api_key`, `.base_url` — used consistently across Tasks 2 & 3.
- **Behavior parity:** config.json shape unchanged (to_dict mirrors old new_cfg); user_background "" ↔ default logic preserved; base_url /bedrock stripping preserved.
