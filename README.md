# Work Reporter

A personal **macOS** desktop tool that quietly tracks what you work on during the
day and generates a work report (日报) with the Anthropic Claude API.

While recording it:

- tracks the active app/window (with debounce, idle and screen-lock detection);
- takes screenshots on window switches and every 5 minutes, then de-duplicates
  and samples them with a perceptual hash;
- collects same-day Git commits, shell history, Chrome history and VSCode recent
  files as supporting context;
- sends the activity log + sampled screenshots to Claude in a single call to
  produce a **detailed** report and a **summary** report;
- optionally appends the report to a Lark (Feishu) document.

It is a single Python file (`work_reporter.py`) plus a Tkinter GUI. All data
stays under `~/.work_reporter/` and is only sent to the Claude endpoint you
configure.

> ⚠️ **Platform:** macOS only. It shells out to `screencapture`, `osascript`,
> `ioreg` and `system_profiler`, and reads Chrome/VSCode state from
> `~/Library/Application Support`.

---

## Requirements

- macOS
- Python 3.10+ (developed/tested on 3.13)
- An Anthropic API key (or a compatible proxy `base_url`)
- Node.js — only if you use the Lark document sync
- macOS permissions (grant on first run, in System Settings → Privacy & Security):
  - **Screen Recording** — for screenshots
  - **Accessibility / Automation** — for `osascript` to read the active window title

## Install

```bash
pip3 install -r requirements.txt          # runtime deps
pip3 install -r requirements-dev.txt       # + pytest, for development
```

## Configure the API key

Any one of these works (checked in this order):

1. `api_key` in `~/.work_reporter/config.json` (or via the in-app **⚙ Settings**)
2. `export ANTHROPIC_API_KEY=sk-ant-...`
3. `export ANTHROPIC_AUTH_TOKEN=...`

For a proxy/gateway, set `base_url` in config or `ANTHROPIC_BEDROCK_BASE_URL`
(a trailing `/bedrock` is stripped automatically).

`config.json` may contain the key, so it is created/kept as `0600` (owner-only);
`~/.work_reporter/` is `0700`.

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

## Run

```bash
./run.sh                    # installs deps if needed, then launches
# or
python3 work_reporter.py
```

### Mac app launcher

`WorkReporter.app` is a thin launcher (not a self-contained app): it `cd`s to the
project directory and runs the top-level `work_reporter.py`, so there is **no
duplicated source**.

- **`WorkReporter.app` must sit next to `work_reporter.py`** (inside this repo dir).
- After editing the launcher / icon / `Info.plist`, run `./build_app.sh` to make
  the launcher executable and ad-hoc re-sign the bundle.

## Configuration reference (`~/.work_reporter/config.json`)

| Key | Meaning |
| --- | --- |
| `model` | Claude model id |
| `api_key` / `base_url` | credentials (see above) |
| `max_tokens.detailed_report` / `.summary_report` | output token caps |
| `screenshot.max_width` | screenshot downscale width |
| `user_background` | **your identity/role**, injected into the prompt. Empty → built-in default. Editable in Settings → *用户背景 (身份)*. This is what makes the tool reusable for a different job. |
| `extra_instructions` | optional extra guidance appended to the prompt rules. Editable in Settings → *附加要求 (可选)*. |
| `lark.doc_url` / `lark.script_dir` | Lark document sync target (leave `doc_url` empty to disable) |

Data locations under `~/.work_reporter/`: `screenshots/` (auto-purged after 30
days), `sessions/` (raw recordings, re-generatable from **◰ LOG**), `reports/`
(generated markdown), `config.json`, `running_session.json` (crash recovery).

## Development

```bash
python3 -m pytest -q
```

A pre-commit hook runs the suite and blocks red commits. Activate it once per
clone:

```bash
git config core.hooksPath hooks
```

Bypass intentionally with `git commit --no-verify`.

## Privacy note

This tool captures screenshots of all displays plus shell/browser/Git history and
sends them to the configured LLM endpoint (and optionally to Lark). Use only with
an endpoint and account you trust, and be mindful on shared/work machines.
