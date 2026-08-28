# Multi-Provider LLM Support — Design

Date: 2026-08-28
Status: Approved

## Context

`work_reporter.py` already has an LLM provider seam (added in P2 phase ③): a
provider implements `generate(parts, model, max_tokens) -> (text, usage)`, where
`parts` is a neutral list of `{"type":"text","text":str}` /
`{"type":"image","data":<base64>,"media_type":str}`. `generate_reports` builds
`parts` and calls `provider.generate(...)`; the GUI holds `self.provider`. Today
the only implementation is `AnthropicProvider` (wraps the `anthropic` SDK), so the
tool only supports Claude.

The tool's report data comes from several sources — the window activity log, same-day
git commits, shell history, Chrome history, VSCode recent files, user notes — plus
**sampled screenshots**. The text sources are the primary evidence; screenshots are
explicitly auxiliary ("仅供辅助参考"). Runtime dependencies are only `anthropic` +
`Pillow`, and the app ships as a self-contained PyInstaller `.app`, so keeping the
dependency footprint small matters.

## Goal

Let the user generate reports with any of: **Anthropic (Claude)**, plus any
**OpenAI-compatible** provider — DeepSeek, 通义千问 (Qwen), Kimi (Moonshot),
OpenAI (GPT), Grok (xAI), and third-party relay stations (中转站). A single active
provider at a time, chosen in Settings. No user-visible change to how reports are
generated beyond the new provider choice and a screenshot toggle.

## Non-goals (YAGNI)

- No streaming.
- No multiple saved provider profiles / one-click switching — switching = editing
  Settings; one active provider.
- No automatic vision-capability detection — a global "send screenshots" toggle.
- No native Gemini protocol — Gemini exposes an OpenAI-compatible endpoint, reachable
  via the custom base_url path.
- No per-vendor provider classes — one `OpenAICompatibleProvider` covers every
  OpenAI-compatible vendor and relay station.

## Key fact this design rests on

DeepSeek, Qwen, Kimi, GPT, Grok, and essentially all relay stations speak the same
protocol: OpenAI's `POST /v1/chat/completions`. One provider implementation covers
all of them; the only differences are base_url + api_key + model name.

## Architecture

### `OpenAICompatibleProvider`

Same interface as `AnthropicProvider`: `generate(parts, model, max_tokens) -> (text, usage)`.

- Transport: stdlib `urllib.request` — a single non-streaming `POST` to
  `{base_url}/chat/completions` (base_url is `rstrip('/')`-normalized), header
  `Authorization: Bearer <api_key>`, `Content-Type: application/json`.
- Request mapping — neutral `parts` → OpenAI: one `{"role":"user","content":[...]}`
  message whose `content` array contains, in order:
  - text part → `{"type":"text","text":<text>}`
  - image part → `{"type":"image_url","image_url":{"url":"data:<media_type>;base64,<data>"}}`
  - Body: `{"model": model, "max_tokens": max_tokens, "messages": [...]}`.
- Response parsing: `text = choices[0].message.content`; usage mapped from
  `usage.prompt_tokens` / `usage.completion_tokens` → `{"input_tokens", "output_tokens"}`
  (default to 0 when absent).
- Errors: any non-200, network failure, or unparseable body → raise an exception
  carrying the status/body text, so the existing GUI failure/retry path handles it.
  An empty `content` is left to `generate_reports`' existing empty-response check.

`AnthropicProvider` is unchanged (it keeps its own `/bedrock` base_url handling).

### Provider presets + factory

A module-level preset table maps a stored key → display label, api type, default base_url:

| key | label | api type | default base_url |
| --- | --- | --- | --- |
| `anthropic` | Anthropic (Claude) | anthropic | `""` (official) |
| `deepseek` | DeepSeek | openai | `https://api.deepseek.com` |
| `qwen` | 通义千问 Qwen | openai | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `kimi` | Kimi (Moonshot) | openai | `https://api.moonshot.cn/v1` |
| `openai` | OpenAI (GPT) | openai | `https://api.openai.com/v1` |
| `grok` | Grok (xAI) | openai | `https://api.x.ai/v1` |
| `custom` | 自定义 (OpenAI 兼容/中转站) | openai | `""` (user-filled) |

**Base URLs verified against official docs (2026-08-28), not from memory:**

- **DeepSeek** — `https://api.deepseek.com`; DeepSeek's docs use **no `/v1`** segment (curl example hits `https://api.deepseek.com/chat/completions`). Since the provider appends `/chat/completions` to the stored base, the DeepSeek preset base must NOT include `/v1`. Source: `api-docs.deepseek.com`.
- **Qwen (DashScope)** — `https://dashscope.aliyuncs.com/compatible-mode/v1` is the China (Beijing) endpoint, documented as "fully functional". International: `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`. Newer per-workspace endpoints (`https://{WorkspaceId}.<region>.maas.aliyuncs.com/compatible-mode/v1`) exist but require a WorkspaceId placeholder, so they are not used as a preset default. Source: Alibaba Model Studio OpenAI-compatibility page.
- **Kimi (Moonshot)** — `https://api.moonshot.cn/v1` (China; confirmed via the OpenAPI `servers` entry). The docs site moved to `platform.kimi.com`, but the API base host is unchanged. An international `.ai` base was not confirmed from the official source; users can set it via the editable base_url if they have one. Source: `platform.kimi.com/docs`.
- **OpenAI** — `https://api.openai.com/v1`. Source: official `openai/openai-openapi` spec `servers[0].url`.
- **Grok (xAI)** — `https://api.x.ai/v1`. Source: `docs.x.ai`.

Because the provider builds the endpoint as `base_url.rstrip('/') + "/chat/completions"`, each preset's base_url must be the exact prefix that precedes `/chat/completions` (DeepSeek without `/v1`; the others with the path shown above). The Base URL field stays editable so users can switch to an international/regional endpoint or a relay station.

`make_provider(CONFIG) -> provider`: looks up the api type for `CONFIG.provider`,
resolves the effective base_url (`CONFIG.base_url` if set, else the preset default),
and returns `AnthropicProvider(api_key, base_url)` or
`OpenAICompatibleProvider(api_key, base_url)`.

Protocol coverage for relays: an **Anthropic-protocol** relay = `anthropic` preset +
a custom base_url (existing capability). An **OpenAI-protocol** relay = `custom`
preset + base_url.

### Config schema

Add to the `Config` dataclass (round-trips through `to_dict`/`from_dict`, same
config.json file):

- `provider: str = "anthropic"` — the preset key (so the dropdown re-displays the
  user's choice).
- `send_screenshots: bool = True`.

`api_key`, `base_url`, `model` are reused as the active provider's credentials/target.
`base_url` empty means "use the preset's default base_url".

### `generate_reports` — screenshot gating

When `CONFIG.send_screenshots` is False, do not append any image parts (nor the
"以下是截图" preamble text); the text data sources are sent as usual. When True,
behavior is unchanged (sampled screenshots included, mapped by the active provider).

### GUI (Settings → 模型与参数)

- Add a **服务商 (provider)** dropdown (`ttk.Combobox`, readonly) at the top,
  listing the preset labels. Selecting a preset auto-fills the Base URL field with
  that preset's default (editable afterward, e.g. for a relay).
- Add a **发送截图 (send screenshots)** checkbox (default checked).
- Keep API Key / 模型名称 / Base URL fields.
- `_save` reads the dropdown → `CONFIG.provider`, reads the checkbox →
  `CONFIG.send_screenshots`, then rebuilds `self.provider = make_provider(CONFIG)`.
- Update the beginner guide text to mention provider choice + the screenshot toggle.

### `WorkReporter` wiring

- `__init__` builds `self.provider = make_provider(CONFIG)` instead of hardcoding
  `AnthropicProvider`. The "missing API key" check stays (applies to any provider).
- Env-var fallback: keep the existing Anthropic env fallbacks
  (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BEDROCK_BASE_URL`) for
  the `anthropic` provider. Add an `OPENAI_API_KEY` fallback for openai-type
  providers when `CONFIG.api_key` is empty. (Low-risk convenience; base_url still
  comes from config/preset.)

## Error handling

- Missing api_key for the chosen provider → existing error dialog + `SystemExit(1)`.
- OpenAI provider non-200 / network error → exception with body text →
  GUI FAILED + retry (existing path).
- Empty model response → existing `ValueError("模型返回为空…")` in `generate_reports`.
- Text-only model with screenshots on → the provider/API errors; user turns the
  screenshot toggle off. (Documented behavior, not auto-handled — matches the chosen
  design.)

## Testing

- `TestOpenAICompatibleProvider`: mock `urllib.request.urlopen`; assert request URL
  (`{base_url}/chat/completions`), `Authorization` header, `messages` mapping
  (text + `image_url` data-URI), and response parsing → `(text, usage)` with mapped
  token fields. Include a non-200 → raises case.
- `TestMakeProvider`: `provider="anthropic"` → `AnthropicProvider`; an openai preset
  → `OpenAICompatibleProvider` with the resolved base_url (preset default when
  `base_url` empty; override when set).
- `TestConfig`: extend for `provider` / `send_screenshots` defaults and round-trip.
- `generate_reports`: with `send_screenshots=False`, no image parts reach the
  provider even when screenshot paths are supplied; with `True`, unchanged.
- Existing 73 tests stay green (WorkReporter tests already mock the provider).

## Packaging

No new dependencies (urllib is stdlib), so the PyInstaller build is unaffected.
Rebuild the `.app` after implementation and re-run the fresh-instance smoke test.

## Files touched

- `work_reporter.py` — add `OpenAICompatibleProvider`, `PROVIDERS` preset table,
  `make_provider`; extend `Config`; gate screenshots in `generate_reports`; add the
  provider dropdown + screenshot checkbox in `_open_settings`; wire `__init__`/`_save`
  to the factory.
- `test_work_reporter.py` — new provider/factory/config/screenshot-gating tests.
- `README.md` — document multi-provider support and the screenshot toggle.
- `config_template.json` — add `provider` and `send_screenshots` defaults.
