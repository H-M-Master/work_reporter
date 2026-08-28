# Multi-Provider LLM Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Work Reporter generate reports via Anthropic (Claude) **or** any OpenAI-compatible provider (DeepSeek, Qwen, Kimi, GPT, Grok, third-party relay stations), selected in Settings.

**Architecture:** Reuse the existing provider seam (`generate(parts, model, max_tokens) -> (text, usage)` with neutral `parts`). Add one `OpenAICompatibleProvider` (stdlib `urllib`, no new deps), a `PROVIDERS` preset table, and a `make_provider(cfg)` factory. Add `provider` + `send_screenshots` to `Config`. GUI gains a provider dropdown + screenshot toggle.

**Tech Stack:** Python 3.13, stdlib `urllib.request`/`urllib.error`, Tkinter/ttk, pytest. Design spec: [docs/superpowers/specs/2026-08-28-multi-provider-llm-design.md](../specs/2026-08-28-multi-provider-llm-design.md).

**Branch:** already on `feat/multi-provider-llm`.

---

## File Structure

- **Modify** `work_reporter.py` — add imports; extend `Config` (fields + `from_dict`/`to_dict`); add `OpenAICompatibleProvider`; add `PROVIDERS` + `resolve_credentials` + `make_provider`; gate screenshots in `generate_reports`; rewire `WorkReporter.__init__` + `_open_settings` (`_save` + provider dropdown + screenshot checkbox + guide text).
- **Modify** `test_work_reporter.py` — extend `TestConfig`; add `TestOpenAICompatibleProvider`, `TestMakeProvider`; add screenshot-gating test.
- **Modify** `config_template.json` — add `provider`, `send_screenshots`.
- **Modify** `README.md` — document multi-provider + screenshot toggle.

Run the whole suite with `python3 -m pytest -q` (pre-commit hook runs it too).

---

## Task 1: `Config` gains `provider` + `send_screenshots`

**Files:**
- Modify: `work_reporter.py:132-181` (`Config` dataclass + `from_dict` + `to_dict`)
- Test: `test_work_reporter.py` (`TestConfig`)

- [ ] **Step 1: Update the three existing `TestConfig` tests + add one.** In `test_work_reporter.py`, replace `test_from_dict_maps_nested_keys`, `test_from_dict_defaults`, `test_to_dict_roundtrip` with these, and add `test_provider_defaults`:

```python
    def test_from_dict_maps_nested_keys(self):
        c = wr.Config.from_dict({
            "model": "m1", "api_key": "k", "base_url": "u",
            "provider": "deepseek", "send_screenshots": False,
            "max_tokens": {"detailed_report": 100, "summary_report": 20},
            "screenshot": {"max_width": 640},
            "user_background": "bg", "extra_instructions": "ex",
            "lark": {"doc_url": "d", "script_dir": "~/s"},
        })
        assert c.model == "m1" and c.api_key == "k" and c.base_url == "u"
        assert c.provider == "deepseek" and c.send_screenshots is False
        assert c.max_tokens_detailed == 100 and c.max_tokens_summary == 20
        assert c.screenshot_max_width == 640
        assert c.user_background == "bg" and c.extra_instructions == "ex"
        assert c.lark_doc_url == "d" and c.lark_script_dir == "~/s"

    def test_from_dict_defaults(self):
        c = wr.Config.from_dict({})
        assert c.model == "claude-haiku-4-5-20251001"
        assert c.provider == "anthropic" and c.send_screenshots is True
        assert c.max_tokens_detailed == 8192 and c.max_tokens_summary == 1024
        assert c.screenshot_max_width == 1280
        assert c.user_background == "" and c.lark_doc_url == ""

    def test_to_dict_roundtrip(self):
        d = {
            "model": "m", "api_key": "k", "base_url": "b",
            "provider": "openai", "send_screenshots": False,
            "max_tokens": {"detailed_report": 1, "summary_report": 2},
            "screenshot": {"max_width": 3},
            "user_background": "bg", "extra_instructions": "ex",
            "lark": {"doc_url": "d", "script_dir": "s"},
        }
        assert wr.Config.from_dict(d).to_dict() == d
```

- [ ] **Step 2: Run to verify fail:** `python3 -m pytest test_work_reporter.py::TestConfig -q` → FAIL (Config has no `provider`).

- [ ] **Step 3: Add the two fields** to the `Config` dataclass (after `base_url`, line 137):

```python
    base_url: str = ""
    provider: str = "anthropic"     # 预设键，见 PROVIDERS
    send_screenshots: bool = True
```

- [ ] **Step 4: Map them in `from_dict`** (add inside the `cls(...)` call, after `base_url=...`):

```python
            base_url=d.get("base_url", ""),
            provider=d.get("provider", "anthropic"),
            send_screenshots=d.get("send_screenshots", True),
```

- [ ] **Step 5: Emit them in `to_dict`** (add after `"base_url": self.base_url,`):

```python
            "base_url": self.base_url,
            "provider": self.provider,
            "send_screenshots": self.send_screenshots,
```

- [ ] **Step 6: Run to verify pass:** `python3 -m pytest test_work_reporter.py::TestConfig -q` then `python3 -m pytest -q`. Both green.

- [ ] **Step 7: Commit:**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "feat: add provider + send_screenshots to Config"
```

---

## Task 2: `OpenAICompatibleProvider`

**Files:**
- Modify: `work_reporter.py` (imports near line 15; new class immediately before `def generate_reports(` at line 747)
- Test: `test_work_reporter.py` (new `TestOpenAICompatibleProvider`)

- [ ] **Step 1: Write failing tests** — append this class to `test_work_reporter.py` (note the top-of-file imports `json`, `pytest`, and `from unittest.mock import patch` already exist):

```python
class TestOpenAICompatibleProvider:
    class _FakeResp:
        def __init__(self, payload):
            self._b = json.dumps(payload).encode("utf-8")
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    @patch("work_reporter.urllib.request.urlopen")
    def test_maps_parts_and_parses(self, mock_urlopen):
        mock_urlopen.return_value = self._FakeResp({
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })
        prov = wr.OpenAICompatibleProvider("KEY", "https://api.deepseek.com")
        text, usage = prov.generate(
            [{"type": "text", "text": "hi"},
             {"type": "image", "data": "B64", "media_type": "image/jpeg"}],
            "deepseek-chat", 100,
        )
        assert text == "hello"
        assert usage == {"input_tokens": 5, "output_tokens": 3}
        req = mock_urlopen.call_args.args[0]
        assert req.full_url == "https://api.deepseek.com/chat/completions"
        assert req.get_header("Authorization") == "Bearer KEY"
        body = json.loads(req.data)
        assert body["model"] == "deepseek-chat" and body["max_tokens"] == 100
        content = body["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "hi"}
        assert content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64,B64"},
        }

    @patch("work_reporter.urllib.request.urlopen")
    def test_strips_trailing_slash(self, mock_urlopen):
        mock_urlopen.return_value = self._FakeResp(
            {"choices": [{"message": {"content": "x"}}], "usage": {}})
        prov = wr.OpenAICompatibleProvider("K", "https://api.openai.com/v1/")
        prov.generate([{"type": "text", "text": "y"}], "gpt-4o", 10)
        assert mock_urlopen.call_args.args[0].full_url == \
            "https://api.openai.com/v1/chat/completions"

    @patch("work_reporter.urllib.request.urlopen")
    def test_http_error_raises(self, mock_urlopen):
        import io
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "u", 401, "Unauthorized", None, io.BytesIO(b'{"error":"bad key"}'))
        prov = wr.OpenAICompatibleProvider("K", "https://api.openai.com/v1")
        with pytest.raises(RuntimeError):
            prov.generate([{"type": "text", "text": "x"}], "gpt-4o", 10)
```

- [ ] **Step 2: Run to verify fail:** `python3 -m pytest test_work_reporter.py::TestOpenAICompatibleProvider -q` → FAIL (`OpenAICompatibleProvider` missing).

- [ ] **Step 3: Add stdlib imports** in `work_reporter.py` right after `import subprocess` (line 15):

```python
import subprocess
import urllib.request
import urllib.error
```

- [ ] **Step 4: Implement the class** — insert immediately before `def generate_reports(` (line 747):

```python
class OpenAICompatibleProvider:
    """OpenAI 兼容供应商（POST {base_url}/chat/completions）。覆盖 DeepSeek /
    Qwen / Kimi / GPT / Grok 及第三方中转站。用标准库 urllib，无额外依赖。

    parts 中性格式同 AnthropicProvider：{"type":"text","text":str} 或
    {"type":"image","data":<base64 str>,"media_type":str}。
    """

    def __init__(self, api_key: str, base_url: str):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def generate(self, parts: list, model: str, max_tokens: int) -> tuple[str, dict]:
        content = []
        for p in parts:
            if p["type"] == "image":
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{p['media_type']};base64,{p['data']}",
                    },
                })
            else:
                content.append({"type": "text", "text": p["text"]})
        body = json.dumps({
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}],
        }).encode("utf-8")
        req = urllib.request.Request(
            self._base_url + "/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"网络错误: {e.reason}") from e

        choices = data.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": usage_raw.get("prompt_tokens", 0),
            "output_tokens": usage_raw.get("completion_tokens", 0),
        }
        return text, usage
```

- [ ] **Step 5: Run to verify pass:** `python3 -m pytest test_work_reporter.py::TestOpenAICompatibleProvider -q` then `python3 -m pytest -q`. Green.

- [ ] **Step 6: Commit:**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "feat: add OpenAICompatibleProvider (urllib, no new deps)"
```

---

## Task 3: `PROVIDERS` presets + `resolve_credentials` + `make_provider`

**Files:**
- Modify: `work_reporter.py` (add after the `OpenAICompatibleProvider` class, before `def generate_reports(`)
- Test: `test_work_reporter.py` (new `TestMakeProvider`)

- [ ] **Step 1: Write failing tests** — append to `test_work_reporter.py`:

```python
class TestMakeProvider:
    def test_anthropic_preset(self):
        cfg = wr.Config(provider="anthropic", api_key="k")
        with patch("work_reporter.anthropic.Anthropic"):
            prov = wr.make_provider(cfg)
        assert isinstance(prov, wr.AnthropicProvider)

    def test_openai_preset_uses_default_base_url(self):
        cfg = wr.Config(provider="deepseek", api_key="k")
        prov = wr.make_provider(cfg)
        assert isinstance(prov, wr.OpenAICompatibleProvider)
        assert prov._base_url == "https://api.deepseek.com"

    def test_base_url_override_wins(self):
        cfg = wr.Config(provider="custom", api_key="k",
                        base_url="https://relay.example.com/v1")
        prov = wr.make_provider(cfg)
        assert isinstance(prov, wr.OpenAICompatibleProvider)
        assert prov._base_url == "https://relay.example.com/v1"

    def test_resolve_credentials_openai_env_fallback(self):
        cfg = wr.Config(provider="openai", api_key="")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "envkey"}, clear=False):
            api_key, base_url = wr.resolve_credentials(cfg)
        assert api_key == "envkey"
        assert base_url == "https://api.openai.com/v1"

    def test_resolve_credentials_anthropic_env_fallback(self):
        cfg = wr.Config(provider="anthropic", api_key="")
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "antkey"}, clear=False):
            api_key, _ = wr.resolve_credentials(cfg)
        assert api_key == "antkey"

    def test_presets_have_required_keys(self):
        for key, p in wr.PROVIDERS.items():
            assert set(p) == {"label", "api", "base_url"}
            assert p["api"] in ("anthropic", "openai")
```

- [ ] **Step 2: Run to verify fail:** `python3 -m pytest test_work_reporter.py::TestMakeProvider -q` → FAIL (`PROVIDERS` / `make_provider` / `resolve_credentials` missing).

- [ ] **Step 3: Implement** — insert after the `OpenAICompatibleProvider` class (before `def generate_reports(`):

```python
# 服务商预设：键 → {显示名, api 类型, 默认 base_url}。
# base_url 已按各家官方文档核实（2026-08-28）——注意 DeepSeek 不带 /v1。
PROVIDERS = {
    "anthropic": {"label": "Anthropic (Claude)", "api": "anthropic", "base_url": ""},
    "deepseek":  {"label": "DeepSeek", "api": "openai",
                  "base_url": "https://api.deepseek.com"},
    "qwen":      {"label": "通义千问 Qwen", "api": "openai",
                  "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "kimi":      {"label": "Kimi (Moonshot)", "api": "openai",
                  "base_url": "https://api.moonshot.cn/v1"},
    "openai":    {"label": "OpenAI (GPT)", "api": "openai",
                  "base_url": "https://api.openai.com/v1"},
    "grok":      {"label": "Grok (xAI)", "api": "openai",
                  "base_url": "https://api.x.ai/v1"},
    "custom":    {"label": "自定义 (OpenAI 兼容/中转站)", "api": "openai", "base_url": ""},
}


def resolve_credentials(cfg) -> tuple[str, str]:
    """按所选服务商解析 (api_key, base_url)，含环境变量兜底。
    base_url 为空时用预设默认；Anthropic 保留原有 /bedrock 相关兜底。"""
    preset = PROVIDERS.get(cfg.provider, PROVIDERS["anthropic"])
    base_url = cfg.base_url.strip() or preset["base_url"]
    if preset["api"] == "anthropic":
        api_key = (
            cfg.api_key.strip()
            or os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        )
        base_url = base_url or os.environ.get("ANTHROPIC_BEDROCK_BASE_URL", "").strip()
    else:
        api_key = cfg.api_key.strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return api_key, base_url


def make_provider(cfg):
    """按 cfg.provider 构造对应 provider（Anthropic 或 OpenAI 兼容）。"""
    preset = PROVIDERS.get(cfg.provider, PROVIDERS["anthropic"])
    api_key, base_url = resolve_credentials(cfg)
    if preset["api"] == "openai":
        return OpenAICompatibleProvider(api_key, base_url)
    return AnthropicProvider(api_key, base_url)
```

- [ ] **Step 4: Run to verify pass:** `python3 -m pytest test_work_reporter.py::TestMakeProvider -q` then `python3 -m pytest -q`. Green.

- [ ] **Step 5: Commit:**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "feat: add PROVIDERS presets + resolve_credentials + make_provider"
```

---

## Task 4: Gate screenshots on `CONFIG.send_screenshots` in `generate_reports`

**Files:**
- Modify: `work_reporter.py:792` (the `sampled = [...]` line inside `generate_reports`)
- Test: `test_work_reporter.py` (`TestGenerateReports`)

- [ ] **Step 1: Write failing test** — append to the existing `TestGenerateReports` class (keep the four `@patch("work_reporter._collect_*")` decorators; they mock the collectors):

```python
    @patch("work_reporter._collect_git_logs", return_value="")
    @patch("work_reporter._collect_shell_history", return_value="")
    @patch("work_reporter._collect_browser_history", return_value="")
    @patch("work_reporter._collect_vscode_recent_files", return_value="")
    def test_send_screenshots_false_skips_images(self, mv, mb, ms, mg, tmp_path):
        from PIL import Image
        img = tmp_path / "shot.png"
        Image.new("RGB", (40, 30), "green").save(img, "PNG")

        mock_provider = MagicMock()
        mock_provider.generate.return_value = ("a===SPLIT===b", {"input_tokens": 1, "output_tokens": 1})

        activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1}]
        with patch.object(wr.CONFIG, "send_screenshots", False):
            wr.generate_reports(
                mock_provider, activities, "2026-05-12",
                screenshot_paths=[img],
                session_start=datetime(2026, 5, 12, 9, 0),
            )
        parts = mock_provider.generate.call_args.args[0]
        assert not any(p["type"] == "image" for p in parts)
        # the "以下是截图" preamble is also skipped
        assert not any("采样的" in p.get("text", "") for p in parts if p["type"] == "text")

    @patch("work_reporter._collect_git_logs", return_value="")
    @patch("work_reporter._collect_shell_history", return_value="")
    @patch("work_reporter._collect_browser_history", return_value="")
    @patch("work_reporter._collect_vscode_recent_files", return_value="")
    def test_send_screenshots_true_includes_images(self, mv, mb, ms, mg, tmp_path):
        from PIL import Image
        img = tmp_path / "shot.png"
        Image.new("RGB", (40, 30), "green").save(img, "PNG")

        mock_provider = MagicMock()
        mock_provider.generate.return_value = ("a===SPLIT===b", {"input_tokens": 1, "output_tokens": 1})

        activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1}]
        with patch.object(wr.CONFIG, "send_screenshots", True):
            wr.generate_reports(
                mock_provider, activities, "2026-05-12",
                screenshot_paths=[img],
                session_start=datetime(2026, 5, 12, 9, 0),
            )
        parts = mock_provider.generate.call_args.args[0]
        assert any(p["type"] == "image" for p in parts)
```

- [ ] **Step 2: Run to verify fail:** `python3 -m pytest test_work_reporter.py::TestGenerateReports::test_send_screenshots_false_skips_images -q` → FAIL (images still present because gating not implemented).

- [ ] **Step 3: Implement** — in `generate_reports`, replace line 792:

```python
    sampled = [p for p in (screenshot_paths or []) if p.exists()]
```

with:

```python
    sampled = (
        [p for p in (screenshot_paths or []) if p.exists()]
        if CONFIG.send_screenshots else []
    )
```

(The existing `if sampled:` block below then adds neither the preamble text nor image parts when `send_screenshots` is False.)

- [ ] **Step 4: Run to verify pass:** `python3 -m pytest test_work_reporter.py::TestGenerateReports -q` then `python3 -m pytest -q`. Green.

- [ ] **Step 5: Commit:**

```bash
git add work_reporter.py test_work_reporter.py
git commit -m "feat: gate screenshots on CONFIG.send_screenshots in generate_reports"
```

---

## Task 5: Wire `WorkReporter.__init__` to the factory

GUI code — not unit-tested (needs Tk). Verified by `py_compile` + full suite staying green.

**Files:**
- Modify: `work_reporter.py:1191-1210` (`WorkReporter.__init__` credential resolution + provider construction)

- [ ] **Step 1: Replace** the block at lines 1191-1210 (from `api_key = (` through `self.provider = AnthropicProvider(api_key, base_url)`):

```python
        api_key, _ = resolve_credentials(CONFIG)
        if not api_key:
            messagebox.showerror(
                "缺少 API Key",
                "请先在 ⚙ 设置里选择服务商并填入 API Key。\n"
                "（Claude 也可用环境变量 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN；"
                "OpenAI 兼容可用 OPENAI_API_KEY。）",
            )
            raise SystemExit(1)

        self.provider = make_provider(CONFIG)
```

- [ ] **Step 2: Verify compile + suite:** `python3 -m py_compile work_reporter.py && python3 -m pytest -q`. Compiles; all green.

- [ ] **Step 3: Commit:**

```bash
git add work_reporter.py
git commit -m "refactor: WorkReporter.__init__ builds provider via make_provider"
```

---

## Task 6: Settings UI — provider dropdown, screenshot toggle, guide, `_save`

GUI code — not unit-tested. Verified by `py_compile` + full suite + a manual smoke test.

**Files:**
- Modify: `work_reporter.py` — `_open_settings`: window geometry (line ~1975), the tab1 field block (lines 2000-2031), and `_save` (lines 2054-2073).

- [ ] **Step 1: Widen the settings window.** Change the geometry line in `_open_settings` (currently `win.geometry("600x520")`):

```python
        win.geometry("620x640")
```

- [ ] **Step 2: Replace the tab1 field block.** Replace lines 2000-2031 (from `tab1.columnconfigure(1, weight=1)` through the guide `.grid(row=9, ...)` Label) with:

```python
        tab1.columnconfigure(1, weight=1)

        # ── 服务商下拉（选预设自动填 Base URL）──
        label_to_key = {v["label"]: k for k, v in PROVIDERS.items()}
        cur_label = PROVIDERS.get(CONFIG.provider, PROVIDERS["anthropic"])["label"]
        provider_var = tk.StringVar(value=cur_label)
        tk.Label(tab1, text="服务商", bg=COLORS["bg"], fg=COLORS["text"],
                 font=("Helvetica", 11), anchor=tk.W).grid(
            row=0, column=0, sticky=tk.W, pady=4)
        provider_combo = ttk.Combobox(
            tab1, textvariable=provider_var, state="readonly",
            values=[v["label"] for v in PROVIDERS.values()], width=28,
        )
        provider_combo.grid(row=0, column=1, sticky=tk.EW, padx=(8, 0), pady=4)

        # 必填/常用项
        _add_field(tab1, "API Key（必填）", "api_key", CONFIG.api_key, 1, show="*")
        _add_field(tab1, "模型名称", "model",
                   CONFIG.model or "claude-haiku-4-5-20251001", 2)

        # 发送截图开关（视觉模型开、纯文本模型关）
        send_ss_var = tk.BooleanVar(value=CONFIG.send_screenshots)
        tk.Checkbutton(
            tab1, text="发送截图给模型（需视觉模型；纯文本模型请取消勾选）",
            variable=send_ss_var, bg=COLORS["bg"], fg=COLORS["text"],
            activebackground=COLORS["bg"], selectcolor=COLORS["card"],
            font=("Helvetica", 10), anchor=tk.W,
        ).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(2, 4))

        # 分隔线：以下都有合理默认，进阶再改
        tk.Label(
            tab1, text="──  以下保持默认即可，进阶再改  ──",
            bg=COLORS["bg"], fg=COLORS["text_sub"], font=("Helvetica", 10),
            anchor=tk.W,
        ).grid(row=4, column=0, columnspan=2, sticky=tk.EW, pady=(12, 4))

        _add_field(tab1, "详细版 max_tokens", "mt_detailed", CONFIG.max_tokens_detailed, 5)
        _add_field(tab1, "总结版 max_tokens", "mt_summary", CONFIG.max_tokens_summary, 6)
        _add_field(tab1, "截图压缩宽度", "max_width", CONFIG.screenshot_max_width, 7)
        _add_field(tab1, "Base URL（预设自动填；中转站手填）", "base_url", CONFIG.base_url, 8)
        _add_field(tab1, "Lark 文档 URL（可选）", "lark_url", CONFIG.lark_doc_url, 9)
        _add_field(tab1, "Lark Script Dir（可选）", "lark_dir", CONFIG.lark_script_dir, 10)

        # 选预设时自动填对应 Base URL（base_url 字段此时已创建；用户仍可手改）
        def _on_provider_change(_event=None):
            key = label_to_key.get(provider_var.get(), "anthropic")
            fields["base_url"].set(PROVIDERS[key]["base_url"])
        provider_combo.bind("<<ComboboxSelected>>", _on_provider_change)

        # 底部小白教程
        guide = (
            "👋 新手三步：①选「服务商」（自动填 Base URL）②填该服务商的 API Key ③填模型名。\n"
            "· Claude→Anthropic；DeepSeek/Qwen/Kimi/GPT/Grok 选对应项；中转站选「自定义」手填 Base URL。\n"
            "· 模型名要填服务商支持的（如 deepseek-chat / qwen-plus / gpt-4o / grok-4 等）。\n"
            "· 纯文本模型不支持看图 → 取消勾选「发送截图」；日报仍用活动记录/git/浏览器历史生成。\n"
            "· 配置只存本机 ~/.work_reporter/config.json（权限 0600，仅你本人可读）。"
        )
        tk.Label(
            tab1, text=guide, bg=COLORS["bg"], fg=COLORS["text_sub"],
            font=("Helvetica", 10), justify=tk.LEFT, anchor=tk.W, wraplength=560,
        ).grid(row=11, column=0, columnspan=2, sticky=tk.W, pady=(18, 0))
```

- [ ] **Step 3: Update `_save`.** In the `def _save():` body, add the two lines below at the top (right after `def _save():`):

```python
        def _save():
            CONFIG.provider = label_to_key.get(provider_var.get(), "anthropic")
            CONFIG.send_screenshots = bool(send_ss_var.get())
            CONFIG.model = fields["model"].get().strip()
```

- [ ] **Step 4: Replace the provider-rebuild block** at the end of `_save` (currently the `# 重建 provider…` block with `if CONFIG.api_key: self.provider = AnthropicProvider(...)`):

```python
            # 重建 provider（服务商 / API Key / Base URL 变更时生效）
            api_key, _ = resolve_credentials(CONFIG)
            if api_key:
                self.provider = make_provider(CONFIG)
            CONFIG.save()
            self._log("配置已保存（config.json 权限 0600）")
            win.destroy()
```

- [ ] **Step 5: Verify compile + suite:** `python3 -m py_compile work_reporter.py && python3 -m pytest -q`. Compiles; all 80 green.

- [ ] **Step 6: Manual smoke test (REQUIRED — GUI can't be unit-tested).** Launch with an isolated HOME so real data is untouched:

```bash
FRESH="$(mktemp -d)"; HOME="$FRESH" ANTHROPIC_API_KEY=dummy \
  python3 work_reporter.py >/tmp/wr_mp.log 2>&1 &
sleep 5; cat /tmp/wr_mp.log
```

Open ⚙ 设置 → confirm: 服务商 dropdown lists all 7 presets; selecting DeepSeek fills Base URL `https://api.deepseek.com`; the 发送截图 checkbox toggles; 保存 doesn't error. Then `pkill -f "python3 work_reporter.py"` and `rm -rf "$FRESH"`.

- [ ] **Step 7: Commit:**

```bash
git add work_reporter.py
git commit -m "feat: provider dropdown + send-screenshots toggle in Settings"
```

---

## Task 7: Config template + README

**Files:**
- Modify: `config_template.json`
- Modify: `README.md`

- [ ] **Step 1: Update `config_template.json`.** Add `provider` and `send_screenshots` (place `provider`/`send_screenshots` right after `base_url`). The file becomes:

```json
{
  "model": "claude-haiku-4-5-20251001",
  "api_key": "",
  "base_url": "",
  "provider": "anthropic",
  "send_screenshots": true,
  "max_tokens": {
    "detailed_report": 8192,
    "summary_report": 1024
  },
  "screenshot": {
    "max_width": 1280
  },
  "user_background": "",
  "extra_instructions": "",
  "lark": {
    "doc_url": "",
    "script_dir": ""
  }
}
```

- [ ] **Step 2: Update `README.md`.** In the "Configure the API key" area, add a short "Model providers" subsection:

```markdown
## Model providers

Work Reporter supports **Anthropic (Claude)** and any **OpenAI-compatible** endpoint —
DeepSeek, 通义千问 (Qwen), Kimi (Moonshot), OpenAI (GPT), Grok (xAI), and third-party
relay stations. Pick one in **⚙ Settings → 服务商**; choosing a preset auto-fills its
Base URL (editable — set your own for a relay or a regional/international endpoint).

| Provider | Base URL |
| --- | --- |
| Anthropic (Claude) | (default, official) |
| DeepSeek | `https://api.deepseek.com` |
| 通义千问 Qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Kimi (Moonshot) | `https://api.moonshot.cn/v1` |
| OpenAI (GPT) | `https://api.openai.com/v1` |
| Grok (xAI) | `https://api.x.ai/v1` |
| 自定义 (OpenAI-compatible / relay) | (you fill it in) |

Fill in the API key for your chosen provider and a model name it supports
(e.g. `deepseek-chat`, `qwen-plus`, `gpt-4o`, `grok-4`). Env-var fallbacks:
`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` for Claude, `OPENAI_API_KEY` for the rest.

**Screenshots need a vision-capable model.** The report's primary evidence is the
activity log + git/browser/shell history; screenshots are auxiliary. If your model is
text-only (e.g. `deepseek-chat`, `qwen-plus`), uncheck **发送截图 (send screenshots)**
in Settings — the report is still generated from the text sources.
```

- [ ] **Step 3: Commit:**

```bash
git add config_template.json README.md
git commit -m "docs: document multi-provider config + screenshot toggle"
```

---

## Task 8: Rebuild the standalone `.app` + smoke test

No new dependencies, so PyInstaller is unaffected — this just refreshes the bundle.

- [ ] **Step 1: Rebuild + re-sign** (icon is tracked in git; restore if the build reports it missing):

```bash
cd /Users/hk00667ml/work_reporter
git checkout HEAD -- "WorkReporter.app/Contents/Resources/WorkReporter.icns" 2>/dev/null || true
python3 -m PyInstaller --name WorkReporter --windowed --noconfirm --clean \
  --icon "WorkReporter.app/Contents/Resources/WorkReporter.icns" \
  --osx-bundle-identifier com.workreporter.app \
  --collect-all anthropic --copy-metadata anthropic \
  --add-data "config_template.json:." \
  work_reporter.py 2>&1 | tail -3
PLIST="dist/WorkReporter.app/Contents/Info.plist"; PB=/usr/libexec/PlistBuddy
$PB -c "Add :NSScreenCaptureUsageDescription string 需要截屏权限以记录工作内容" "$PLIST" 2>/dev/null || $PB -c "Set :NSScreenCaptureUsageDescription 需要截屏权限以记录工作内容" "$PLIST"
$PB -c "Add :NSAppleEventsUsageDescription string 需要自动化权限以读取当前活跃窗口标题" "$PLIST" 2>/dev/null || true
$PB -c "Add :CFBundleShortVersionString string 3.0" "$PLIST" 2>/dev/null || $PB -c "Set :CFBundleShortVersionString 3.0" "$PLIST"
$PB -c "Add :CFBundleVersion string 3.0" "$PLIST" 2>/dev/null || $PB -c "Set :CFBundleVersion 3.0" "$PLIST"
codesign --force --deep -s - "dist/WorkReporter.app" 2>&1 | tail -1
```

- [ ] **Step 2: Smoke test the bundle** with an isolated HOME + dummy key (window flashes ~6s):

```bash
FRESH="$(mktemp -d)"
HOME="$FRESH" ANTHROPIC_API_KEY=dummy \
  "dist/WorkReporter.app/Contents/MacOS/WorkReporter" >/tmp/wr_mp2.log 2>&1 &
PID=$!; sleep 6
kill -0 "$PID" 2>/dev/null && echo "✅ launched OK" || { echo "❌ crashed:"; cat /tmp/wr_mp2.log; }
kill "$PID" 2>/dev/null; rm -rf "$FRESH"
```

Expected: `✅ launched OK`, empty log. (`dist/` is gitignored — nothing to commit.)

---

## Self-Review

- **Spec coverage:** `OpenAICompatibleProvider` (Task 2) ✓; PROVIDERS table with web-verified base_urls incl. DeepSeek-no-`/v1` + `make_provider`/`resolve_credentials` incl. `OPENAI_API_KEY` fallback (Task 3) ✓; `Config.provider`/`send_screenshots` round-trip (Task 1) ✓; screenshot gating (Task 4) ✓; `__init__` factory wiring (Task 5) ✓; Settings dropdown + checkbox + guide + `_save` (Task 6) ✓; config_template + README (Task 7) ✓; rebuild `.app` (Task 8) ✓. Single-active-provider (no multi-profile) — matches spec's non-goals.
- **Placeholder scan:** none — every code step has full content.
- **Type consistency:** `generate(parts, model, max_tokens) -> (text, usage)` matches `AnthropicProvider`; provider `_base_url` attribute asserted in tests matches the class; `PROVIDERS[key]` shape `{label, api, base_url}` used consistently in `resolve_credentials`/`make_provider`/GUI and asserted in `test_presets_have_required_keys`; `resolve_credentials`/`make_provider`/`Config.provider`/`Config.send_screenshots` names consistent across tasks.
- **Existing-test impact:** `TestConfig` roundtrip/defaults updated in Task 1 (to_dict now emits the two new keys); WorkReporter is not unit-tested, so `__init__`/`_save` edits don't break the suite. Final suite ≈ 80 tests.

