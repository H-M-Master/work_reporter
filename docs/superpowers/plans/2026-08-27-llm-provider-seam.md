# LLM Provider Seam Implementation Plan (P2 phase ③)

> Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Isolate Anthropic specifics behind an `AnthropicProvider` with `generate(parts, model, max_tokens) -> (text, usage)`, so `generate_reports` and the GUI depend on a provider seam, not the SDK. No behavior change.

**Architecture:** `parts` is a neutral list (`{"type":"text","text":...}` / `{"type":"image","data":b64,"media_type":mt}`). `AnthropicProvider` owns client construction (+ `/bedrock` stripping, de-duplicated from `__init__`/`_save`), maps parts→Anthropic content, calls `messages.create`, extracts text+usage. `generate_reports` builds parts + does the empty-check + `===SPLIT===` split (provider-agnostic). `model`/`max_tokens` are passed per-call (preserves "changing model in Settings takes effect immediately").

---

## Task 1: Add `AnthropicProvider` (+ unit tests). Additive — no wiring yet.

**Files:** `work_reporter.py` (add class immediately before `def generate_reports(`), `test_work_reporter.py`.

- [ ] **Step 1: failing tests** — append a class:

```python
class TestAnthropicProvider:
    def test_maps_parts_and_extracts_text_usage(self):
        with patch("work_reporter.anthropic.Anthropic") as MockA:
            client = MockA.return_value
            resp = MagicMock()
            block = MagicMock(); block.type = "text"; block.text = "hello"
            resp.content = [block]
            resp.usage.input_tokens = 5
            resp.usage.output_tokens = 3
            client.messages.create.return_value = resp

            prov = wr.AnthropicProvider("key")
            text, usage = prov.generate(
                [{"type": "text", "text": "hi"},
                 {"type": "image", "data": "B64", "media_type": "image/jpeg"}],
                "modelX", 123,
            )
        assert text == "hello"
        assert usage == {"input_tokens": 5, "output_tokens": 3}
        sent = client.messages.create.call_args.kwargs
        assert sent["model"] == "modelX" and sent["max_tokens"] == 123
        content = sent["messages"][0]["content"]
        assert content[0] == {"type": "text", "text": "hi"}
        assert content[1] == {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "B64"},
        }

    def test_strips_bedrock_suffix_from_base_url(self):
        with patch("work_reporter.anthropic.Anthropic") as MockA:
            wr.AnthropicProvider("k", "https://proxy.example.io/bedrock")
        assert MockA.call_args.kwargs.get("base_url") == "https://proxy.example.io"

    def test_no_base_url_passes_only_api_key(self):
        with patch("work_reporter.anthropic.Anthropic") as MockA:
            wr.AnthropicProvider("k")
        assert MockA.call_args.kwargs == {"api_key": "k"}
```

- [ ] **Step 2: run, expect FAIL:** `python3 -m pytest test_work_reporter.py::TestAnthropicProvider -q` → `AnthropicProvider` missing.

- [ ] **Step 3: implement** — insert immediately before `def generate_reports(` in `work_reporter.py`:

```python
class AnthropicProvider:
    """LLM 供应商接口（Anthropic 实现）。换/加供应商 = 实现同样的
    generate(parts, model, max_tokens) -> (text, usage)。

    parts: 中性列表，每项 {"type":"text","text":str} 或
           {"type":"image","data":<base64 str>,"media_type":str}。
    """

    def __init__(self, api_key: str, base_url: str = ""):
        kwargs = {"api_key": api_key}
        if base_url:
            # ANTHROPIC_BEDROCK_BASE_URL 含 /bedrock passthrough 路径，标准 SDK 需去掉
            if base_url.endswith("/bedrock"):
                base_url = base_url[: -len("/bedrock")]
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

    def generate(self, parts: list, model: str, max_tokens: int) -> tuple[str, dict]:
        content = []
        for p in parts:
            if p["type"] == "image":
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": p["media_type"],
                        "data": p["data"],
                    },
                })
            else:
                content.append({"type": "text", "text": p["text"]})
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": content}],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        usage = {
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens,
        }
        return text, usage
```

- [ ] **Step 4: run PASS:** `python3 -m pytest test_work_reporter.py::TestAnthropicProvider -q` then full `python3 -m pytest -q` (was 70, now 73).

- [ ] **Step 5: commit:**
```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: add AnthropicProvider LLM seam (P2 phase 3, step 1)"
```

---

## Task 2: Switch `generate_reports` to the provider + wire WorkReporter (one coherent commit)

**Files:** `work_reporter.py` (generate_reports, `__init__`, `_run_generation`, `_save`), `test_work_reporter.py` (4 tests).

- [ ] **Step 1: update the 4 generate_reports tests to mock a provider.**

In `TestGenerateReports.test_includes_all_data_sources`, replace the mock-client setup and call with:
```python
        mock_provider = MagicMock()
        mock_provider.generate.return_value = (
            "详细报告\n===SPLIT===\n总结报告",
            {"input_tokens": 1500, "output_tokens": 800},
        )

        activities = [
            {"timestamp": "09:00", "app": "VSCode", "window": "test.py", "duration_min": 30}
        ]
        detailed, summary, usage = wr.generate_reports(
            mock_provider, activities, "2026-05-12",
            session_start=datetime(2026, 5, 12, 9, 0),
            user_notes="今天修了登录页的 bug",
        )

        # data sources land in the neutral parts passed to the provider
        parts = mock_provider.generate.call_args.args[0]
        full_text = "".join(p["text"] for p in parts if p["type"] == "text")
        assert "窗口活动记录" in full_text
        assert "Git 提交记录" in full_text
        assert "浏览器访问记录" in full_text
        assert "VSCode 最近打开" in full_text
        assert "终端命令历史" in full_text
        assert "用户补充说明" in full_text
        assert "今天修了登录页的 bug" in full_text
        assert detailed == "详细报告"
        assert summary == "总结报告"
        assert usage == {"input_tokens": 1500, "output_tokens": 800}
```
(Delete the old `mock_client`/`mock_resp`/`mock_block` lines and the old `messages.create.call_args` inspection in this test.)

`TestGenerateReports.test_no_split_fallback` — replace mock setup + call:
```python
        mock_provider = MagicMock()
        mock_provider.generate.return_value = (
            "一大段没有分隔符的文本内容", {"input_tokens": 100, "output_tokens": 50})

        activities = [
            {"timestamp": "09:00", "app": "Chrome", "window": "x", "duration_min": 5}
        ]
        detailed, summary, usage = wr.generate_reports(
            mock_provider, activities, "2026-05-12",
            session_start=datetime(2026, 5, 12, 9, 0),
        )
        assert detailed
        assert summary
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
```

`TestGenerateReports.test_no_user_notes` — replace mock setup + call + inspection:
```python
        mock_provider = MagicMock()
        mock_provider.generate.return_value = ("a===SPLIT===b", {"input_tokens": 10, "output_tokens": 5})

        activities = [
            {"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1}
        ]
        detailed, summary, usage = wr.generate_reports(
            mock_provider, activities, "2026-05-12",
            session_start=datetime(2026, 5, 12, 9, 0),
            user_notes="",
        )
        parts = mock_provider.generate.call_args.args[0]
        full_text = "".join(p["text"] for p in parts if p["type"] == "text")
        assert "用户补充说明" not in full_text
```

`TestGenerateReportsEmptyResponse.test_empty_response_raises` — replace mock setup + call:
```python
        mock_provider = MagicMock()
        mock_provider.generate.return_value = ("   \n  ", {"input_tokens": 1, "output_tokens": 0})

        activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1}]
        with pytest.raises(ValueError):
            wr.generate_reports(
                mock_provider, activities, "2026-05-12",
                session_start=datetime(2026, 5, 12, 9, 0),
            )
```
(Keep the 4 `@patch("work_reporter._collect_*")` decorators on each test — the collectors still run inside generate_reports.)

- [ ] **Step 2: run — these 4 now FAIL** (generate_reports still expects an Anthropic client): `python3 -m pytest test_work_reporter.py::TestGenerateReports test_work_reporter.py::TestGenerateReportsEmptyResponse -q` → 4 failed. Red step.

- [ ] **Step 3: refactor `generate_reports`** in `work_reporter.py`:

3a. Change the first parameter. Replace:
```python
    client: anthropic.Anthropic,
    activities: list,
```
with:
```python
    provider,
    activities: list,
```

3b. Replace the content-build block:
```python
    # ── 构建消息内容（文字 + 采样截图）──
    content: list = []

    # 截图（已由调用方采样好）
    sampled = [p for p in (screenshot_paths or []) if p.exists()]
    if sampled:
        content.append({
            "type": "text",
            "text": f"以下是今天工作中采样的 {len(sampled)} 张截图（仅供辅助参考，不要过度解读）：",
        })
        for p in sampled:
            encoded = _encode_image_for_api(p)
            if encoded is None:
                continue  # 跳过损坏/无法编码的截图
            img_data, media_type = encoded
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": img_data,
                },
            })
```
with:
```python
    # ── 构建消息内容（文字 + 采样截图），中性 parts 格式，交给 provider 映射 ──
    parts: list = []

    # 截图（已由调用方采样好）
    sampled = [p for p in (screenshot_paths or []) if p.exists()]
    if sampled:
        parts.append({
            "type": "text",
            "text": f"以下是今天工作中采样的 {len(sampled)} 张截图（仅供辅助参考，不要过度解读）：",
        })
        for p in sampled:
            encoded = _encode_image_for_api(p)
            if encoded is None:
                continue  # 跳过损坏/无法编码的截图
            img_data, media_type = encoded
            parts.append({
                "type": "image",
                "data": img_data,
                "media_type": media_type,
            })
```

3c. Replace the instruction-append + API call block:
```python
    content.append({"type": "text", "text": instruction})

    resp = client.messages.create(
        model=CONFIG.model,
        max_tokens=CONFIG.max_tokens_detailed,
        messages=[{"role": "user", "content": content}],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text.strip():
        # 空响应不是"成功"——上抛让调用方标记失败并显示重试
        raise ValueError("模型返回为空，未生成日报")

    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
```
with:
```python
    parts.append({"type": "text", "text": instruction})

    text, usage = provider.generate(parts, CONFIG.model, CONFIG.max_tokens_detailed)
    if not text.strip():
        # 空响应不是"成功"——上抛让调用方标记失败并显示重试
        raise ValueError("模型返回为空，未生成日报")
```

- [ ] **Step 4: wire WorkReporter.**

4a. In `__init__`, replace:
```python
        client_kwargs = {"api_key": api_key}
        if base_url:
            # ANTHROPIC_BEDROCK_BASE_URL 含 /bedrock passthrough 路径，标准 SDK 需去掉
            if base_url.endswith("/bedrock"):
                base_url = base_url[: -len("/bedrock")]
            client_kwargs["base_url"] = base_url

        self.client = anthropic.Anthropic(**client_kwargs)
```
with:
```python
        self.provider = AnthropicProvider(api_key, base_url)
```

4b. In `_run_generation`, replace `                    self.client, activities, date,` with `                    self.provider, activities, date,`.

4c. In `_save`, replace:
```python
            # 重建客户端（API Key / Base URL 变更时生效）
            if CONFIG.api_key:
                ck = {"api_key": CONFIG.api_key}
                if CONFIG.base_url:
                    url = CONFIG.base_url
                    if url.endswith("/bedrock"):
                        url = url[: -len("/bedrock")]
                    ck["base_url"] = url
                self.client = anthropic.Anthropic(**ck)
```
with:
```python
            # 重建 provider（API Key / Base URL 变更时生效）
            if CONFIG.api_key:
                self.provider = AnthropicProvider(CONFIG.api_key, CONFIG.base_url)
```

- [ ] **Step 5: verify.**
  - `grep -nE "self\.client|\.messages\.create|resp\.usage|resp\.content" work_reporter.py` → the ONLY hits allowed are inside `AnthropicProvider.generate` (the `.messages.create`, `resp.content`, `resp.usage`). No `self.client` anywhere. If `self.client` appears, a wiring site was missed.
  - `python3 -m py_compile work_reporter.py` → OK.
  - `python3 -m pytest -q` → all green (73).

- [ ] **Step 6: commit:**
```bash
git add work_reporter.py test_work_reporter.py
git commit -m "refactor: generate_reports + WorkReporter use AnthropicProvider (P2 phase 3, step 2)"
```

---

## Self-Review
- **Spec coverage:** provider seam with neutral parts (Task 1) ✓; generate_reports depends on provider, GUI holds `self.provider`, `/bedrock` de-dup (Task 2) ✓; model/max_tokens per-call ✓.
- **Placeholders:** none.
- **Parity:** content mapping identical to old nested format; usage dict identical; empty-check + split unchanged; `__init__` env fallback + error dialog unchanged; `import anthropic` still needed (provider uses it).
- **Type consistency:** `AnthropicProvider(api_key, base_url="")`, `.generate(parts, model, max_tokens) -> (text, usage)`; `generate_reports(provider, …)`; `self.provider`.
