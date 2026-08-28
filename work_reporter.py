#!/usr/bin/env python3
"""
Work Reporter - 自动工作日报生成器
追踪活跃窗口标题，窗口切换时自动截图，结束时一次性生成日报。
用法：
    python work_reporter.py
"""
from __future__ import annotations

import os
import re
import json
import base64
import threading
import subprocess
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox

import anthropic

__version__ = "3.0"  # 单一版本来源：GUI banner 与 Info.plist 应与此一致

# ─── 配置 ─────────────────────────────────────────────────────────────────────
DATA_DIR = Path.home() / ".work_reporter"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
SESSION_DIR    = DATA_DIR / "sessions"
REPORT_DIR     = DATA_DIR / "reports"
CONFIG_FILE    = DATA_DIR / "config.json"
RUNNING_SESSION_FILE = DATA_DIR / "running_session.json"
CONFIG_TEMPLATE = (
    Path(__file__).parent / "WorkReporter.app" / "Contents" / "Resources" / "config_template.json"
    if (Path(__file__).parent / "WorkReporter.app" / "Contents" / "Resources" / "config_template.json").exists()
    else Path(__file__).parent / "config_template.json"
)


def _secure_dir(path: Path):
    """确保目录存在且仅本人可访问（0700）。"""
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _write_config(cfg: dict):
    """写入 config.json，权限设为仅本人可读写（0600）——里面可能含明文 API Key。"""
    _secure_dir(DATA_DIR)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8"
    )
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _safe_fromisoformat(s) -> datetime | None:
    """宽松解析 ISO 时间字符串，非法/空值返回 None（避免启动时因脏文件崩溃）。"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _atomic_write_json(path: Path, data: dict):
    """原子写 JSON：先写临时文件再 os.replace，避免中断留下半截文件。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, path)


def _resolve_lark_executor() -> Path | None:
    """在插件缓存中查找最新版 lark-feishu-tools 的 feishu-doc executor.mjs。

    不写死插件市场名或版本号——遍历 ~/.claude/plugins/cache 下的任意市场，
    插件升级后自动跟随最新版本。
    """
    cache = Path.home() / ".claude/plugins/cache"
    if not cache.exists():
        return None
    candidates = list(
        cache.glob("*/lark-feishu-tools/*/skills/feishu-doc/scripts/executor.mjs")
    )
    if not candidates:
        return None

    def _ver_key(p: Path):
        # .../lark-feishu-tools/<version>/skills/feishu-doc/scripts/executor.mjs
        ver = p.parents[3].name
        return tuple(int(x) if x.isdigit() else 0 for x in ver.split("."))

    return sorted(candidates, key=_ver_key)[-1]


def _load_config() -> dict:
    """加载用户配置，不存在则从模板复制一份；每次加载都收紧权限到 0600"""
    if not CONFIG_FILE.exists():
        _secure_dir(DATA_DIR)
        if CONFIG_TEMPLATE.exists():
            CONFIG_FILE.write_text(CONFIG_TEMPLATE.read_text("utf-8"), "utf-8")
        else:
            CONFIG_FILE.write_text("{}", "utf-8")
    # 收紧权限：历史上创建的 config.json 可能是 0644（含明文 API Key / 内网 URL）
    try:
        if CONFIG_FILE.exists():
            CONFIG_FILE.chmod(0o600)
    except OSError:
        pass
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# 默认用户背景（身份）——仅是一个示例，用户应在设置里改成自己的真实身份。
DEFAULT_USER_BACKGROUND = (
    "我是一名软件工程师。日常工作主要包括：\n"
    "- 编写和维护项目代码、修复 bug\n"
    "- 编写和评审测试、跑自动化测试并分析结果\n"
    "- 阅读文档、参与需求与技术方案讨论\n"
    "- 使用 IM / 协作工具沟通协作"
)

@dataclass
class Config:
    """全部用户配置的单一来源。取代散落的模块级全局变量。"""
    model: str = "claude-haiku-4-5-20251001"
    api_key: str = ""
    base_url: str = ""
    provider: str = "anthropic"     # 预设键，见 PROVIDERS
    send_screenshots: bool = True
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
            provider=d.get("provider", "anthropic"),
            send_screenshots=d.get("send_screenshots", True),
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
            "provider": self.provider,
            "send_screenshots": self.send_screenshots,
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

# ──────────────────────────────────────────────────────────────────────────────


def ensure_dirs():
    _secure_dir(DATA_DIR)
    for d in [SCREENSHOT_DIR, SESSION_DIR, REPORT_DIR]:
        _secure_dir(d)


SCREENSHOT_RETENTION_DAYS = 30


def _cleanup_old_screenshots(days: int = SCREENSHOT_RETENTION_DAYS) -> int:
    """删除超过指定天数的截图文件，返回删除数量"""
    if not SCREENSHOT_DIR.exists():
        return 0
    cutoff = datetime.now().timestamp() - days * 86400
    deleted = 0
    for f in SCREENSHOT_DIR.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception:
            continue
    return deleted


def _is_screen_locked() -> bool:
    """检测 macOS 是否锁屏或休眠"""
    try:
        r = subprocess.run(
            ["ioreg", "-n", "Root", "-d1", "-w0"],
            capture_output=True, text=True, timeout=5,
        )
        return "CGSSessionScreenIsLocked" in r.stdout
    except Exception:
        return False


def _get_idle_seconds() -> float:
    """获取键鼠空闲秒数"""
    try:
        r = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem", "-d4"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
        if m:
            return int(m.group(1)) / 1_000_000_000  # 纳秒 → 秒
    except Exception:
        pass
    return 0.0


def get_active_window() -> tuple[str, str]:
    """获取当前活跃应用名和窗口标题，返回 (app_name, window_title)"""
    try:
        r1 = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
        app = r1.stdout.strip()

        r2 = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of front window of first application process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
        win = r2.stdout.strip()
        return app, win
    except Exception:
        return "", ""


_DISPLAY_COUNT_CACHE: int | None = None


def _get_display_count() -> int:
    """获取 macOS 显示器数量（启动后缓存）"""
    global _DISPLAY_COUNT_CACHE
    if _DISPLAY_COUNT_CACHE is not None:
        return _DISPLAY_COUNT_CACHE
    try:
        r = subprocess.run(
            ["system_profiler", "SPDisplaysDataType", "-json"],
            capture_output=True, text=True, timeout=10,
        )
        data = json.loads(r.stdout)
        count = sum(
            len(gpu.get("spdisplays_ndrvs", []))
            for gpu in data.get("SPDisplaysDataType", [])
        )
        _DISPLAY_COUNT_CACHE = max(1, count)
    except Exception:
        _DISPLAY_COUNT_CACHE = 1
    return _DISPLAY_COUNT_CACHE


def take_screenshot() -> list[Path]:
    """截取所有显示器的截图（留档），返回路径列表"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    paths = []
    n = _get_display_count()
    try:
        for i in range(1, n + 1):
            path = SCREENSHOT_DIR / f"{ts}_D{i}.png"
            subprocess.run(
                ["screencapture", "-x", "-D", str(i), str(path)],
                check=True, timeout=10,
            )
            # 压缩
            try:
                from PIL import Image
                img = Image.open(path)
                if img.width > CONFIG.screenshot_max_width:
                    ratio = CONFIG.screenshot_max_width / img.width
                    img = img.resize((CONFIG.screenshot_max_width, int(img.height * ratio)), Image.LANCZOS)
                    img.save(path, "PNG", optimize=True)
            except ImportError:
                pass
            paths.append(path)
    except Exception:
        pass
    return paths


def _collect_git_logs(since: datetime, until: datetime | None = None) -> str:
    """收集指定时间范围内所有 git 仓库的 commit 记录"""
    home = Path.home()
    git_dirs = []
    try:
        result = subprocess.run(
            ["find", str(home), "-maxdepth", "3", "-name", ".git", "-type", "d"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            if any(skip in line for skip in ["Library", ".cache", ".local", "node_modules", ".claude"]):
                continue
            git_dirs.append(Path(line).parent)
    except Exception:
        pass

    since_str = since.strftime("%Y-%m-%d %H:%M")
    # 默认 until 为 since 当天的 23:59:59，避免跨天生成时混入第二天的 commit
    if until is None:
        until = since.replace(hour=23, minute=59, second=59)
    until_str = until.strftime("%Y-%m-%d %H:%M")

    logs = []
    for repo in git_dirs[:20]:
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "--since", since_str,
                 "--until", until_str, "--no-merges", "--format=%h %s"],
                capture_output=True, text=True, timeout=5, cwd=str(repo),
            )
            commits = r.stdout.strip()
            if commits:
                repo_name = repo.name
                logs.append(f"[{repo_name}]\n{commits}")
        except Exception:
            continue
    return "\n".join(logs) if logs else ""


def _collect_shell_history(since: datetime, max_lines: int = 100) -> str:
    """收集今天的 shell 命令历史"""
    history_file = Path.home() / ".zsh_history"
    if not history_file.exists():
        history_file = Path.home() / ".bash_history"
    if not history_file.exists():
        return ""

    try:
        lines = history_file.read_bytes().decode("utf-8", errors="replace").splitlines()
        today_cmds = []
        since_ts = since.timestamp()

        for line in reversed(lines):
            # zsh history format: : 1234567890:0;command
            m = re.match(r'^:\s*(\d+):\d+;(.+)', line)
            if m:
                ts = int(m.group(1))
                cmd = m.group(2).strip()
                if ts >= since_ts:
                    if not any(skip in cmd for skip in ["cd ", "ls", "clear", "exit", "pwd"]):
                        today_cmds.append(cmd)
                else:
                    break
            elif line.strip():
                today_cmds.append(line.strip())

        today_cmds.reverse()
        # 去重保留顺序
        seen = set()
        unique = []
        for cmd in today_cmds[-max_lines:]:
            if cmd not in seen:
                seen.add(cmd)
                unique.append(cmd)
        return "\n".join(unique)
    except Exception:
        return ""


def _collect_browser_history(since: datetime, max_entries: int = 80) -> str:
    """收集 Chrome 浏览器历史（复制数据库以避免锁冲突）"""
    import sqlite3 as _sqlite3
    import shutil
    import tempfile

    profiles = []
    chrome_base = Path.home() / "Library/Application Support/Google/Chrome"
    if chrome_base.exists():
        for p in chrome_base.iterdir():
            hist = p / "History"
            if hist.exists():
                profiles.append(hist)

    # Chrome 时间戳: 微秒自 1601-01-01
    chrome_epoch_offset = 11644473600
    since_chrome = (int(since.timestamp()) + chrome_epoch_offset) * 1_000_000

    results = []
    for hist_path in profiles[:3]:
        tmp = None
        try:
            fd, tmp_str = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            tmp = Path(tmp_str)
            shutil.copy2(str(hist_path), str(tmp))
            conn = _sqlite3.connect(str(tmp))
            cursor = conn.execute(
                "SELECT url, title, last_visit_time FROM urls "
                "WHERE last_visit_time > ? ORDER BY last_visit_time DESC LIMIT ?",
                (since_chrome, max_entries),
            )
            for url, title, _ in cursor.fetchall():
                if not url or not title:
                    continue
                # 过滤掉无意义的页面
                if any(skip in url for skip in [
                    "google.com/search", "chrome://", "chrome-extension://",
                    "accounts.google", "oauth", "loginCallback",
                    "blank", "newtab",
                ]):
                    continue
                # 清理标题中的不可见字符
                clean_title = re.sub(r'[​-\u200F- ⁠-⁯﻿]', '', title).strip()
                if clean_title:
                    # 缩短 URL 只保留域名+路径
                    from urllib.parse import urlparse
                    parsed = urlparse(url)
                    short_url = parsed.netloc + parsed.path.rstrip("/")
                    results.append(f"{clean_title} ({short_url})")
            conn.close()
        except Exception:
            pass
        finally:
            if tmp and tmp.exists():
                tmp.unlink(missing_ok=True)

    # 去重
    seen = set()
    unique = []
    for r in results:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return "\n".join(unique[:max_entries])


def _collect_vscode_recent_files() -> str:
    """收集 VSCode 最近打开的工作区和文件"""
    import sqlite3 as _sqlite3
    from urllib.parse import unquote

    state_db = Path.home() / "Library/Application Support/Code/User/globalStorage/state.vscdb"
    if not state_db.exists():
        return ""

    results = []
    try:
        conn = _sqlite3.connect(str(state_db))
        cursor = conn.execute(
            "SELECT value FROM ItemTable WHERE key='history.recentlyOpenedPathsList'"
        )
        row = cursor.fetchone()
        if row:
            data = json.loads(row[0])
            for entry in data.get("entries", [])[:10]:
                uri = entry.get("folderUri", "") or entry.get("fileUri", "")
                if uri and uri.startswith("file://"):
                    path = unquote(uri[7:])
                    # 只保留 home 下的，去掉绝对路径前缀
                    home = str(Path.home())
                    if path.startswith(home):
                        path = "~" + path[len(home):]
                    results.append(path)
        conn.close()
    except Exception:
        pass
    return "\n".join(results)


def _merge_activities(activities: list) -> list:
    """合并相邻同 app 的活动记录，减少条目数和 token 消耗"""
    if not activities:
        return []
    merged = []
    cur = {**activities[0], "windows": [activities[0].get("window", "")]}
    for a in activities[1:]:
        if a["app"] == cur["app"]:
            # 同 app → 累加时长，收集不同窗口标题
            cur["duration_min"] = round(cur.get("duration_min", 0) + a.get("duration_min", 0), 1)
            win = a.get("window", "")
            if win and win not in cur["windows"]:
                cur["windows"].append(win)
        else:
            # 不同 app → 保存当前，开启新段
            merged.append(cur)
            cur = {**a, "windows": [a.get("window", "")]}
    merged.append(cur)
    return merged


MAX_SAMPLE_GROUPS = 50  # 最多采样 50 组（每组含 D1+D2 等多屏截图）


def _group_by_timestamp(paths: list[Path]) -> list[list[Path]]:
    """把截图按时间戳分组（同一秒的 D1/D2 归为一组）"""
    groups: dict[str, list[Path]] = {}
    for p in paths:
        if not p.exists():
            continue
        # 文件名格式: 20260416_091500_D1.png → key = 20260416_091500
        ts_key = p.stem.rsplit("_D", 1)[0]
        groups.setdefault(ts_key, []).append(p)
    # 按时间排序
    return [groups[k] for k in sorted(groups)]


def _image_hash(path: Path, hash_size: int = 8) -> list[int] | None:
    """计算图片感知哈希（dHash），返回比特列表"""
    try:
        from PIL import Image
        img = Image.open(path).convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        pixels = list(img.getdata())
        w = hash_size + 1
        return [1 if pixels[y * w + x] > pixels[y * w + x + 1] else 0
                for y in range(hash_size) for x in range(hash_size)]
    except Exception:
        return None


def _group_hash(group: list[Path]) -> list[int] | None:
    """一组截图（D1+D2）的联合哈希：拼接各屏哈希"""
    combined = []
    for p in sorted(group, key=lambda x: x.name):
        h = _image_hash(p)
        if h:
            combined.extend(h)
    return combined if combined else None


def _hash_distance(h1: list[int] | None, h2: list[int] | None) -> int:
    """汉明距离，长度不同或 None 时返回最大距离"""
    if h1 is None or h2 is None or len(h1) != len(h2):
        return 128
    return sum(a != b for a, b in zip(h1, h2))


def _sample_screenshots(
    switch_paths: list[Path],
    backup_paths: list[Path],
) -> list[Path]:
    """智能采样截图：按时间戳配对分组，优先切换时刻，超限选差异最大的"""
    switch_groups = _group_by_timestamp(switch_paths)
    backup_groups = _group_by_timestamp(backup_paths)

    # 切换组不超限 → 全选，兜底补齐
    if len(switch_groups) <= MAX_SAMPLE_GROUPS:
        selected_groups = list(switch_groups)
        remaining = MAX_SAMPLE_GROUPS - len(selected_groups)
        for g in backup_groups:
            if remaining <= 0:
                break
            selected_groups.append(g)
            remaining -= 1
    else:
        # 切换组超限 → 贪心选差异最大的组
        hashes = {i: _group_hash(g) for i, g in enumerate(switch_groups)}
        # 首尾必选
        selected_idx = [0, len(switch_groups) - 1]
        candidates = list(range(1, len(switch_groups) - 1))
        while len(selected_idx) < MAX_SAMPLE_GROUPS and candidates:
            best_idx = None
            best_min_dist = -1
            for ci in candidates:
                h = hashes[ci]
                min_dist = min(_hash_distance(h, hashes[si]) for si in selected_idx)
                if min_dist > best_min_dist:
                    best_min_dist = min_dist
                    best_idx = ci
            if best_idx is not None:
                selected_idx.append(best_idx)
                candidates.remove(best_idx)
            else:
                break
        selected_idx.sort()
        selected_groups = [switch_groups[i] for i in selected_idx]

    # 展开所有组为平铺路径列表，按文件名排序
    result = []
    for g in selected_groups:
        result.extend(sorted(g, key=lambda p: p.name))
    result.sort(key=lambda p: p.name)
    return result


def _encode_image_for_api(path: Path) -> tuple[str, str] | None:
    """将截图压缩为 JPEG 并 base64 编码（省 token），返回 (base64_data, media_type)。

    图片损坏/无法解码时返回 None（调用方跳过该张），避免一张坏图导致整次生成失败。
    """
    try:
        from PIL import Image
    except ImportError:
        Image = None

    if Image is not None:
        try:
            import io
            img = Image.open(path).convert("RGB")
            # 缩到 1024px 宽，JPEG quality=60，够 Vision 识别
            if img.width > 1024:
                ratio = 1024 / img.width
                img = img.resize((1024, int(img.height * ratio)), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=60)
            return base64.b64encode(buf.getvalue()).decode("utf-8"), "image/jpeg"
        except Exception:
            # 有 Pillow 但图片损坏/无法解码 → 跳过（不要把垃圾字节当图片发出去）
            return None

    # 无 Pillow：发 PNG 原图
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8"), "image/png"
    except OSError:
        return None


def _build_instruction(data_sections: str, date: str, date_title: str) -> str:
    """构建发给模型的完整指令。

    用户背景（身份）读取自 CONFIG.effective_user_background（可在设置里覆盖，
    默认见 DEFAULT_USER_BACKGROUND）；CONFIG.extra_instructions 非空时作为附加规则追加。
    输出格式（===SPLIT=== 两段）固定，以保证下游解析稳定。
    """
    rules = (
        "【重要规则】\n"
        "- 以「窗口活动记录」和「Git 提交记录」为主要依据，这些是确定发生的事实\n"
        "- 截图仅作辅助：只描述你能在截图中明确看到的内容，不要推测截图背后的工作\n"
        "- 绝对不要编造具体数字（金额、百分比、延迟时间等），除非你在截图中清晰看到\n"
        "- 绝对不要编造功能名、变量名、文件名，除非在活动记录或 git log 中明确出现\n"
        "- 不确定的内容宁可不写，也不要猜测\n"
        "- 语言朴实简洁，像写给同事看的工作备忘，不要写得像技术文档或论文\n"
        "- 如果某段时间只能看出在用某个应用但不确定具体做什么，就写「使用 XX 应用」即可\n"
        "- 看到 IM / 聊天软件窗口时，可以推断为「沟通协作」或「查看消息」，不要猜具体聊天内容\n"
        "- 活动时长越长，在报告中应占更多篇幅；短暂切换（<2分钟）可以忽略\n"
        "- 浏览器记录能说明你在哪些平台工作（如项目管理、CI、测试平台），善用它"
    )
    if CONFIG.extra_instructions:
        rules += f"\n- {CONFIG.extra_instructions}"

    return (
        f"你是一个工作日报助手。根据以下数据源生成日报。\n\n"
        f"【用户背景】\n"
        f"{CONFIG.effective_user_background}\n"
        f"请基于这个背景来理解我的活动记录。\n\n"
        f"{rules}\n\n"
        f"【数据源】\n\n"
        f"{data_sections}\n\n"
        f"【输出要求】\n"
        f"一次性生成两份日报，用 ===SPLIT=== 分隔：\n\n"
        f"【第一部分：详细版日报】Markdown 格式：\n"
        f"1. 按时间段分组，描述实际做了什么\n"
        f"2. 末尾附时长统计表（基于活动记录中的分钟数）\n"
        f"3. 一级标题：# {date} 工作日报（详细版）\n\n"
        f"===SPLIT===\n\n"
        f"【第二部分：总结版日报】纯文本，不要用 # 号，格式如下：\n\n"
        f"{date_title}\n"
        f"一、工作总结\n1. （实际完成的工作，每条一句话，基于事实）\n...\n"
        f"二、学习内容\n1. （今日接触到的新技术/知识，没有就写「无」）\n...\n"
        f"三、遇到的问题\n1. （实际遇到的问题，没有就写「无」）\n...\n"
        f"四、明日计划\n1. （基于今天工作的合理延续，简短即可）\n...\n\n"
        f"要求：每部分 2-5 条，基于事实，不编造。"
    )


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
        if not self._base_url:
            raise RuntimeError(
                "未配置 Base URL：请在 ⚙ 设置里为该服务商填写 Base URL"
                "（选“自定义/中转站”时必填）。"
            )
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


def generate_reports(
    provider,
    activities: list,
    date: str,
    screenshot_paths: list[Path] | None = None,
    session_start: datetime | None = None,
    user_notes: str = "",
) -> tuple[str, str, dict]:
    """根据窗口活动日志 + git/shell 历史 + 采样截图，单次 API 调用生成日报。
    返回 (详细版 markdown, 总结版文本, token 用量 dict)。"""
    merged = _merge_activities(activities)

    log_lines = []
    for a in merged:
        ts = a["timestamp"]
        app = a["app"]
        wins = a.get("windows", [])
        dur = a.get("duration_min", 0)
        line = f"[{ts}] {app}"
        valid_wins = [w for w in wins if w]
        if valid_wins:
            shown = valid_wins[:3]
            line += f" — {', '.join(shown)}"
            if len(valid_wins) > 3:
                line += f" 等{len(valid_wins)}个窗口"
        if dur:
            line += f"  ({dur}分钟)"
        log_lines.append(line)

    activity_log = "\n".join(log_lines)
    date_title = datetime.strptime(date, "%Y-%m-%d").strftime("%-m月%-d日")

    # ── 收集辅助数据源（限定在 session 开始那天）──
    since = session_start or datetime.strptime(date, "%Y-%m-%d")
    day_start = since.replace(hour=0, minute=0, second=0)
    day_end = since.replace(hour=23, minute=59, second=59)
    git_log = _collect_git_logs(day_start, day_end)
    shell_history = _collect_shell_history(day_start)
    browser_history = _collect_browser_history(day_start)
    vscode_files = _collect_vscode_recent_files()

    # ── 构建消息内容（文字 + 采样截图），中性 parts 格式，交给 provider 映射 ──
    parts: list = []

    # 截图（已由调用方采样好）
    sampled = (
        [p for p in (screenshot_paths or []) if p.exists()]
        if CONFIG.send_screenshots else []
    )
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

    # ── 组装数据源文本 ──
    data_sections = f"## 窗口活动记录（主要依据，共{len(merged)}条）\n\n{activity_log}"

    if git_log:
        data_sections += f"\n\n## Git 提交记录\n\n{git_log}"

    if browser_history:
        data_sections += f"\n\n## 浏览器访问记录\n\n{browser_history}"

    if vscode_files:
        data_sections += f"\n\n## VSCode 最近打开的项目/文件\n\n{vscode_files}"

    if shell_history:
        data_sections += f"\n\n## 终端命令历史（参考）\n\n{shell_history}"

    if user_notes:
        data_sections += f"\n\n## 用户补充说明（优先参考）\n\n{user_notes}"

    # ── 指令 prompt（用户背景与附加要求均可在设置里配置）──
    instruction = _build_instruction(data_sections, date, date_title)

    parts.append({"type": "text", "text": instruction})

    text, usage = provider.generate(parts, CONFIG.model, CONFIG.max_tokens_detailed)
    if not text.strip():
        # 空响应不是"成功"——上抛让调用方标记失败并显示重试
        raise ValueError("模型返回为空，未生成日报")

    if "===SPLIT===" in text:
        detailed, summary = text.split("===SPLIT===", 1)
    else:
        mid = len(text) * 2 // 3
        detailed, summary = text[:mid], text[mid:]

    return detailed.strip(), summary.strip(), usage


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

    def stop(self):
        """结束录制（标记不再运行）。"""
        self.is_running = False
        self.is_paused = False

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


# ─── GUI ──────────────────────────────────────────────────────────────────────

# 配色：Fallout / Vault-Tec 暖浅色调（长时间使用不疲惫）
COLORS = {
    "bg":          "#F1E9D6",   # 米白（Vault 旧纸质感）
    "card":        "#FAF4E4",   # 卡片更亮一点
    "border":      "#C9BFA8",   # 浅褐边框
    "border_dark": "#8B7E62",   # 深褐重边框
    "text":        "#1A2530",   # 深海军（高对比，长读不刺眼）
    "text_sub":    "#6B5D48",   # 暖灰文本
    "log_bg":      "#FFFCF4",   # 日志区更亮
    "start":       "#1E3A5F",   # Vault 深蓝
    "pause":       "#D9952A",   # 琥珀黄
    "resume":      "#2E7A85",   # 青蓝
    "stop":        "#A8371F",   # 锈红
    "snap":        "#B86A1E",   # 暖橙
    "settings":    "#6B5D48",   # 暖灰
    "history":     "#5C7A2F",   # 橄榄绿（vault 元素）
    "disabled_bg": "#D8CFB8",   # 禁用背景
    "disabled_fg": "#A89E89",   # 禁用文字
    "ok":          "#4F6B27",   # 橄榄绿（成功）
    "warn":        "#B86A1E",   # 暖橙
    "error":       "#A8371F",   # 锈红
}

# Fallout 风字体（macOS 自带等宽字体）
FONT_BTN = ("Menlo", 11, "bold")
FONT_TITLE = ("Menlo", 14, "bold")
FONT_LABEL = ("Menlo", 10, "bold")
FONT_BODY = ("Menlo", 11)


def _darken(hex_color: str, amount: float = 0.85) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"#{int(r*amount):02x}{int(g*amount):02x}{int(b*amount):02x}"


def _lighten(hex_color: str, amount: float = 0.15) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    r = min(255, int(r + (255 - r) * amount))
    g = min(255, int(g + (255 - g) * amount))
    b = min(255, int(b + (255 - b) * amount))
    return f"#{r:02x}{g:02x}{b:02x}"


class RoundedButton(tk.Canvas):
    """Canvas-绘制的圆角按钮，带轻微深度感，适配 Fallout 配色"""

    def __init__(
        self, parent, text: str = "", bg: str = COLORS["snap"],
        fg: str = "white", command=None, font=None,
        padx: int = 16, pady: int = 8, radius: int = 10,
        parent_bg: str | None = None, min_width: int = 0, **kwargs,
    ):
        self._bg = bg
        self._fg = fg
        self._command = command
        self._enabled = True
        self._text = text
        self._font = font or FONT_BTN
        self._padx = padx
        self._pady = pady
        self._radius = radius
        self._parent_bg = parent_bg or COLORS["bg"]
        self._chover = False

        import tkinter.font as tkfont
        f = tkfont.Font(font=self._font)
        text_w = max(int(f.measure(text)), min_width - 2 * padx)
        text_h = int(f.metrics()["linespace"])
        self._cw = int(text_w + 2 * padx)
        self._ch = int(text_h + 2 * pady)

        super().__init__(
            parent, width=self._cw, height=self._ch,
            highlightthickness=0, bd=0, bg=self._parent_bg, **kwargs,
        )
        self.config(cursor="hand2")
        self._render()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _draw_round_rect(self, x1, y1, x2, y2, r, fill, outline=None):
        outline = outline or fill
        x1, y1, x2, y2, r = int(x1), int(y1), int(x2), int(y2), int(r)
        # 4 个圆角 + 中心十字矩形构成圆角矩形
        self.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r,
                        start=90, extent=90, fill=fill, outline=outline)
        self.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r,
                        start=0, extent=90, fill=fill, outline=outline)
        self.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2,
                        start=270, extent=90, fill=fill, outline=outline)
        self.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2,
                        start=180, extent=90, fill=fill, outline=outline)
        self.create_rectangle(x1 + r, y1, x2 - r, y2,
                              fill=fill, outline=fill)
        self.create_rectangle(x1, y1 + r, x2, y2 - r,
                              fill=fill, outline=fill)

    def _render(self):
        self.delete("all")
        if self._enabled:
            base = _darken(self._bg, 0.92) if self._chover else self._bg
            shadow = _darken(self._bg, 0.7)
            fg = self._fg
        else:
            base = COLORS["disabled_bg"]
            shadow = _darken(COLORS["disabled_bg"], 0.85)
            fg = COLORS["disabled_fg"]

        r = self._radius
        # 阴影底层（向下偏移 1px，制造立体感）
        self._draw_round_rect(0, 1, self._cw, self._ch, r, shadow)
        # 主体
        self._draw_round_rect(0, 0, self._cw, self._ch - 1, r, base)
        # 顶部高光（极淡的一条）
        if self._enabled:
            top_hi = _lighten(base, 0.18)
            self.create_arc(2, 1, 2 + 2 * r, 1 + 2 * r,
                            start=90, extent=90, fill=top_hi, outline=top_hi)
            self.create_arc(self._cw - 2 - 2 * r, 1, self._cw - 2, 1 + 2 * r,
                            start=0, extent=90, fill=top_hi, outline=top_hi)
            self.create_rectangle(2 + r, 1, self._cw - 2 - r, 2,
                                  fill=top_hi, outline=top_hi)
        # 文字
        self.create_text(
            self._cw / 2, (self._ch - 1) / 2,
            text=self._text, fill=fg, font=self._font,
        )

    def _on_click(self, _):
        if self._enabled and self._command:
            self._command()

    def _on_enter(self, _):
        if self._enabled:
            self._chover = True
            self._render()

    def _on_leave(self, _):
        if self._enabled:
            self._chover = False
            self._render()

    def set_enabled(self, on: bool):
        self._enabled = on
        self.config(cursor="hand2" if on else "arrow")
        self._render()

    def set_color(self, bg: str, fg: str | None = None):
        self._bg = bg
        if fg is not None:
            self._fg = fg
        self._render()

    def set_text(self, text: str):
        self._text = text
        import tkinter.font as tkfont
        f = tkfont.Font(font=self._font)
        text_w = int(f.measure(text))
        text_h = int(f.metrics()["linespace"])
        self._cw = int(text_w + 2 * self._padx)
        self._ch = int(text_h + 2 * self._pady)
        self.config(width=self._cw, height=self._ch)
        self._render()


# 兼容旧名称
MacButton = RoundedButton


class WorkReporter:
    def __init__(self):
        ensure_dirs()

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
        self.session: RecordingSession | None = None  # 录制领域对象；None=未在录制
        self._poll_timer = None
        self._periodic_timer = None
        # 当前/最近的 session 文件（用于重试）
        self._current_session_file: Path | None = None

        self._build_gui()
        # 启动时清理旧截图
        try:
            n = _cleanup_old_screenshots()
            if n > 0:
                self._log(f"已清理 {n} 张超过 {SCREENSHOT_RETENTION_DAYS} 天的旧截图")
        except Exception:
            pass
        # 检查是否有中断的记录
        self._check_interrupted_session()

    # ── 运行态持久化（崩溃恢复） ────────────────────────────────────────────────

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

    def _delete_running_state(self):
        """正常结束时删除运行态文件"""
        try:
            RUNNING_SESSION_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _check_interrupted_session(self):
        """启动时检查是否有未完成的记录，返回是否已恢复"""
        if not RUNNING_SESSION_FILE.exists():
            return False
        try:
            with open(RUNNING_SESSION_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._delete_running_state()
            return False

        last_update = state.get("last_update")
        activities = state.get("activities", [])
        start_dt = _safe_fromisoformat(state.get("start"))
        if start_dt is None or not activities:
            # start 缺失/损坏或没有活动 → 丢弃这份运行态，不要让脏文件崩溃启动
            self._delete_running_state()
            return False

        last_dt = _safe_fromisoformat(last_update) or start_dt
        interrupted_hours = (datetime.now() - last_dt).total_seconds() / 3600

        start_str = start_dt.strftime("%H:%M")
        last_str = last_dt.strftime("%m-%d %H:%M") if interrupted_hours > 24 else last_dt.strftime("%H:%M")
        act_count = len(activities)

        if interrupted_hours > 48:
            msg = (
                f"检测到未完成的记录（已超过 48 小时）：\n"
                f"开始于 {start_str}，中断于 {last_str}，{act_count} 条活动\n\n"
                f"记录已过期，建议直接生成日报或放弃。\n\n"
                f"是否用已有数据生成日报？\n（选「否」将放弃该记录）"
            )
            if messagebox.askyesno("发现过期记录", msg):
                self._recover_and_stop(state)
            else:
                self._delete_running_state()
            return True

        msg = (
            f"检测到未完成的记录：\n"
            f"开始于 {start_str}，中断于 {last_str}\n"
            f"已记录 {act_count} 条活动，中断了 {interrupted_hours:.1f} 小时\n\n"
            f"请选择操作："
        )
        win = tk.Toplevel(self.root)
        win.title("恢复记录")
        win.geometry("420x200")
        win.resizable(False, False)
        win.configure(bg=COLORS["bg"])
        win.grab_set()
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        tk.Label(
            win, text=msg, bg=COLORS["bg"], fg=COLORS["text"],
            font=("Helvetica", 11), justify=tk.LEFT, wraplength=380,
        ).pack(padx=16, pady=(16, 12))

        btn_frame = tk.Frame(win, bg=COLORS["bg"])
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 16))

        choice = {"value": None}

        def _resume():
            choice["value"] = "resume"
            win.destroy()

        def _stop_gen():
            choice["value"] = "generate"
            win.destroy()

        def _discard():
            choice["value"] = "discard"
            win.destroy()

        RoundedButton(
            btn_frame, text="继续记录", bg=COLORS["start"],
            command=_resume, parent_bg=COLORS["bg"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        RoundedButton(
            btn_frame, text="生成日报", bg=COLORS["resume"],
            command=_stop_gen, parent_bg=COLORS["bg"],
        ).pack(side=tk.LEFT, padx=(0, 8))

        RoundedButton(
            btn_frame, text="放弃", bg=COLORS["settings"],
            command=_discard, parent_bg=COLORS["bg"],
        ).pack(side=tk.LEFT)

        self.root.wait_window(win)

        if choice["value"] == "resume":
            self._recover_and_resume(state)
        elif choice["value"] == "generate":
            self._recover_and_stop(state)
        else:
            self._delete_running_state()

        return True

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

    def _recover_and_stop(self, state: dict):
        """用恢复的数据直接生成日报"""
        self.session = RecordingSession.from_state(state)
        if self.session is None:
            self._delete_running_state()
            return
        self.session.stop()
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

    # ── 界面搭建 ──────────────────────────────────────────────────────────────

    def _build_gui(self):
        root = tk.Tk()
        self.root = root
        root.title("WORK REPORTER")
        root.geometry("680x680")
        root.resizable(False, False)
        root.configure(bg=COLORS["bg"])

        # ── Vault-Tec 风顶部 banner ─────────────────────────────────────
        banner = tk.Frame(root, bg=COLORS["start"], height=44)
        banner.pack(fill=tk.X)
        banner.pack_propagate(False)
        tk.Label(
            banner, text="▼  WORK  REPORTER  ▼",
            bg=COLORS["start"], fg=COLORS["pause"],
            font=("Menlo", 14, "bold"),
        ).pack(side=tk.LEFT, padx=18, pady=8)
        tk.Label(
            banner, text=f"VAULT-TEC  ·  v{__version__}",
            bg=COLORS["start"], fg=_lighten(COLORS["pause"], 0.0),
            font=("Menlo", 9, "bold"),
        ).pack(side=tk.RIGHT, padx=18)

        # 顶部按钮区
        top = tk.Frame(root, bg=COLORS["bg"], pady=14)
        top.pack(fill=tk.X, padx=18)

        self.btn_start = RoundedButton(
            top, text="▶ START", bg=COLORS["start"],
            command=self.start_work, parent_bg=COLORS["bg"],
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_pause = RoundedButton(
            top, text="❚❚ PAUSE", bg=COLORS["pause"],
            command=self._toggle_pause, parent_bg=COLORS["bg"],
        )
        self.btn_pause.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_pause.set_enabled(False)

        self.btn_stop = RoundedButton(
            top, text="■ STOP", bg=COLORS["stop"],
            command=self.stop_work, parent_bg=COLORS["bg"],
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_stop.set_enabled(False)

        self.btn_snap = RoundedButton(
            top, text="◉ SNAP", bg=COLORS["snap"],
            command=self._manual_screenshot, parent_bg=COLORS["bg"],
        )
        self.btn_snap.pack(side=tk.LEFT)
        self.btn_snap.set_enabled(False)

        # 重试按钮（默认隐藏，失败时显示）
        self.btn_retry = RoundedButton(
            top, text="↻ RETRY", bg=COLORS["stop"],
            command=self._retry_generation, parent_bg=COLORS["bg"],
        )

        # 设置按钮
        self.btn_settings = RoundedButton(
            top, text="⚙", bg=COLORS["settings"],
            command=self._open_settings, parent_bg=COLORS["bg"], padx=12,
        )
        self.btn_settings.pack(side=tk.RIGHT)

        # 打开报告目录按钮
        self.btn_open_dir = RoundedButton(
            top, text="▣ FILES", bg=COLORS["resume"],
            command=self._open_report_dir, parent_bg=COLORS["bg"],
        )
        self.btn_open_dir.pack(side=tk.RIGHT, padx=(0, 6))

        # 历史记录按钮
        self.btn_history = RoundedButton(
            top, text="◰ LOG", bg=COLORS["history"],
            command=self._open_history, parent_bg=COLORS["bg"],
        )
        self.btn_history.pack(side=tk.RIGHT, padx=(0, 6))

        # ── 状态行（卡片样式，重边框） ────────────────────────────────────
        status_outer = tk.Frame(root, bg=COLORS["border_dark"])
        status_outer.pack(fill=tk.X, padx=18)
        status_bar = tk.Frame(status_outer, bg=COLORS["card"], pady=10)
        status_bar.pack(fill=tk.X, padx=2, pady=2)

        # 左侧状态指示灯（小圆点）
        self._status_dot = tk.Canvas(
            status_bar, width=14, height=14,
            bg=COLORS["card"], highlightthickness=0,
        )
        self._status_dot.pack(side=tk.LEFT, padx=(12, 6))
        self._draw_status_dot(COLORS["text_sub"])

        self.lbl_status = tk.Label(
            status_bar, text="STANDBY  ·  等待开始工作",
            bg=COLORS["card"], font=("Menlo", 11, "bold"),
            fg=COLORS["text"], anchor=tk.W,
        )
        self.lbl_status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.lbl_count = tk.Label(
            status_bar, text="", bg=COLORS["card"],
            font=("Menlo", 10), fg=COLORS["text_sub"],
        )
        self.lbl_count.pack(side=tk.RIGHT, padx=12)

        # ── 用户备注输入区 ────────────────────────────────────────────────
        tk.Label(
            root, text="▸ NOTES   补充说明（可选）",
            bg=COLORS["bg"], font=FONT_LABEL, fg=COLORS["text_sub"],
        ).pack(anchor=tk.W, padx=20, pady=(14, 4))

        notes_outer = tk.Frame(root, bg=COLORS["border_dark"])
        notes_outer.pack(fill=tk.X, padx=18)
        self.notes_box = scrolledtext.ScrolledText(
            notes_outer, height=3, font=("Menlo", 11),
            bg=COLORS["card"], fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT, bd=0, wrap=tk.WORD,
            highlightthickness=0,
        )
        self.notes_box.pack(fill=tk.X, padx=2, pady=2)
        self.notes_box.insert("1.0", "")

        # ── 日志区 ─────────────────────────────────────────────────────
        tk.Label(
            root, text="▸ ACTIVITY LOG   窗口活动记录（本地追踪）",
            bg=COLORS["bg"], font=FONT_LABEL, fg=COLORS["text_sub"],
        ).pack(anchor=tk.W, padx=20, pady=(10, 4))

        log_outer = tk.Frame(root, bg=COLORS["border_dark"])
        log_outer.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 16))
        self.log_box = scrolledtext.ScrolledText(
            log_outer, height=12, state=tk.DISABLED,
            font=("Menlo", 10), bg=COLORS["log_bg"], fg=COLORS["text"],
            relief=tk.FLAT, bd=0, wrap=tk.WORD,
            highlightthickness=0,
        )
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.createcommand("::tk::mac::ReopenApplication", self._show_window)

    # ── 状态指示灯 ────────────────────────────────────────────────────────────

    def _draw_status_dot(self, color: str):
        self._status_dot.delete("all")
        self._status_dot.create_oval(
            2, 2, 12, 12, fill=color, outline=_darken(color, 0.7),
        )

    # ── 日志工具 ──────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        self.log_box.config(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.insert(tk.END, f"[{ts}]  {msg}\n")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    # ── 开始工作 ──────────────────────────────────────────────────────────────

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

    # ── 结束工作 ──────────────────────────────────────────────────────────────

    def stop_work(self):
        if not self.session or not self.session.is_running:
            return
        now = datetime.now()
        self.session.record_current_activity(now)
        self.session.stop()
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

    def _run_generation(self, session_file: Path):
        """从 session 文件加载并生成日报（首次/重试/历史复用同一入口）"""
        self.btn_retry.pack_forget()
        self._draw_status_dot(COLORS["warn"])
        self.lbl_status.config(text="GENERATING  ·  正在生成日报", fg=COLORS["warn"])

        def _generate():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    sd = json.load(f)

                date = sd["date"]
                session_start = datetime.fromisoformat(sd["start"])
                activities = sd["activities"]
                switch_paths = [Path(p) for p in sd.get("switch_screenshots", [])]
                backup_paths = [Path(p) for p in sd.get("backup_screenshots", [])]
                notes = sd.get("user_notes", "")

                sampled = _sample_screenshots(switch_paths, backup_paths)
                self.root.after(0, lambda n=len(sampled): self._log(
                    f"采样 {n} 张截图，正在收集 git/shell 历史..."
                ))
                detailed, summary, usage = generate_reports(
                    self.provider, activities, date,
                    screenshot_paths=sampled,
                    session_start=session_start,
                    user_notes=notes,
                )

                ts = session_start.strftime("%Y%m%d_%H%M%S")
                detailed_file = REPORT_DIR / f"report_detailed_{ts}.md"
                summary_file  = REPORT_DIR / f"report_summary_{ts}.md"
                detailed_file.write_text(detailed, encoding="utf-8")
                summary_file.write_text(summary, encoding="utf-8")

                # 写入 Lark 文档（URL 为空则跳过）
                lark_ok = False
                lark_err = ""
                lark_skipped = not CONFIG.lark_doc_url
                if not lark_skipped:
                    try:
                        self.root.after(0, lambda: self._log("正在写入 Lark 文档..."))
                        lark_content = (
                            f"---\n\n"
                            f"# {date}\n\n"
                            f"{summary}\n\n"
                            f"{detailed}\n"
                        )
                        lark_tmp = REPORT_DIR / f"lark_tmp_{ts}.md"
                        lark_tmp.write_text(lark_content, encoding="utf-8")
                        feishu_executor = _resolve_lark_executor()
                        if feishu_executor:
                            doc_id = CONFIG.lark_doc_url.rstrip("/").split("/")[-1]
                            params = json.dumps({
                                "doc_id": doc_id,
                                "mode": "append",
                                "file_path": str(lark_tmp),
                            })
                            result = subprocess.run(
                                ["node", str(feishu_executor), "feishu_update_doc", params],
                                capture_output=True, text=True, timeout=120,
                            )
                        else:
                            result = subprocess.run(
                                ["node", "scripts/write-lark-doc.mjs",
                                 "--mode", "append",
                                 "--file", str(lark_tmp),
                                 "--url", CONFIG.lark_doc_url],
                                cwd=str(CONFIG.lark_script_dir_path),
                                capture_output=True, text=True, timeout=60,
                            )
                        lark_tmp.unlink(missing_ok=True)
                        if result.returncode == 0:
                            lark_ok = True
                        else:
                            lark_err = result.stderr.strip() or result.stdout.strip()
                    except Exception as ex:
                        lark_err = str(ex)

                # 更新 session 状态
                sd["status"] = "completed"
                with open(session_file, "w", encoding="utf-8") as f:
                    json.dump(sd, f, ensure_ascii=False, indent=2)

                def _done():
                    self._draw_status_dot(COLORS["ok"])
                    self.lbl_status.config(text="DONE  ·  日报生成完成 ✓", fg=COLORS["ok"])
                    in_tok = usage.get("input_tokens", 0)
                    out_tok = usage.get("output_tokens", 0)
                    self._log(f"Token 用量: 输入 {in_tok:,} + 输出 {out_tok:,} = {in_tok+out_tok:,}")
                    self._log(f"详细版 -> {detailed_file.name}")
                    self._log(f"总结版 -> {summary_file.name}")
                    if lark_skipped:
                        self._log("已跳过 Lark 同步（未配置 URL）")
                    elif lark_ok:
                        self._log("已同步到 Lark 文档 ✓")
                    else:
                        self._log(f"Lark 同步失败: {lark_err}")
                    if messagebox.askyesno(
                        "日报已生成",
                        f"报告已保存至：\n{REPORT_DIR}\n\n"
                        "是否打开本地报告目录？",
                    ):
                        subprocess.run(["open", str(REPORT_DIR)])

                self.root.after(0, _done)

            except Exception as e:
                # 标记失败状态
                try:
                    with open(session_file, "r", encoding="utf-8") as f:
                        sd = json.load(f)
                    sd["status"] = "failed"
                    sd["error"] = str(e)
                    with open(session_file, "w", encoding="utf-8") as f:
                        json.dump(sd, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

                def _on_fail(err=e):
                    self._log(f"生成日报失败: {err}")
                    self._draw_status_dot(COLORS["error"])
                    self.lbl_status.config(
                        text="FAILED  ·  生成失败，点击重试", fg=COLORS["error"],
                    )
                    self.btn_retry.pack(side=tk.LEFT, padx=(0, 6))

                self.root.after(0, _on_fail)

        threading.Thread(target=_generate, daemon=True).start()

    def _retry_generation(self):
        """重试当前失败的 session"""
        if self._current_session_file and self._current_session_file.exists():
            self._log("重试生成日报...")
            self._run_generation(self._current_session_file)
        else:
            messagebox.showinfo("无可重试 session", "请先开始一次工作记录。")

    # ── 打开报告目录 ──────────────────────────────────────────────────────────

    def _open_report_dir(self):
        subprocess.run(["open", str(REPORT_DIR)])

    # ── 历史记录窗口 ──────────────────────────────────────────────────────────

    def _open_history(self):
        win = tk.Toplevel(self.root)
        win.title("历史 Session")
        win.geometry("680x460")
        win.configure(bg=COLORS["bg"])
        win.grab_set()

        tk.Label(
            win, text="选择一个 session 重新生成日报", bg=COLORS["bg"],
            font=("Helvetica", 12, "bold"), fg=COLORS["text"],
        ).pack(anchor=tk.W, padx=14, pady=(12, 6))

        list_frame = tk.Frame(win, bg=COLORS["card"],
                              highlightbackground=COLORS["border"],
                              highlightthickness=1, bd=0)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        listbox = tk.Listbox(
            list_frame, font=("Menlo", 11),
            yscrollcommand=scrollbar.set,
            selectmode=tk.SINGLE, relief=tk.FLAT,
            bg=COLORS["card"], fg=COLORS["text"],
            selectbackground=COLORS["resume"], selectforeground="white",
            highlightthickness=0, bd=0,
        )
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=listbox.yview)

        # 收集 session 文件
        sessions = []
        for f in sorted(SESSION_DIR.glob("session_*.json"), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    sd = json.load(fh)
                date = sd.get("date", "?")
                status = sd.get("status", "?")
                act_count = len(sd.get("activities", []))
                status_emoji = {
                    "completed": "✓", "failed": "✗", "pending": "⏳"
                }.get(status, "?")
                listbox.insert(
                    tk.END,
                    f"{status_emoji}  {date}  ({act_count} 条活动)  [{f.name}]"
                )
                sessions.append(f)
            except Exception:
                continue

        if not sessions:
            listbox.insert(tk.END, "（暂无历史 session）")

        bottom = tk.Frame(win, bg=COLORS["bg"], pady=8)
        bottom.pack(fill=tk.X, padx=14, pady=(0, 10))

        def _regenerate():
            sel = listbox.curselection()
            if not sel or sel[0] >= len(sessions):
                messagebox.showinfo("未选择", "请先选择一个 session")
                return
            session_file = sessions[sel[0]]
            self._current_session_file = session_file
            win.destroy()
            self._log(f"从历史 session 重新生成: {session_file.name}")
            self._run_generation(session_file)

        MacButton(
            bottom, text="重新生成", bg=COLORS["start"],
            command=_regenerate, padx=18,
        ).pack(side=tk.RIGHT, padx=(0, 4))

        MacButton(
            bottom, text="关闭", bg=COLORS["settings"],
            command=win.destroy, padx=18,
        ).pack(side=tk.RIGHT, padx=(0, 8))

    # ── 关闭窗口 ──────────────────────────────────────────────────────────────

    # ── 设置窗口 ──────────────────────────────────────────────────────────────

    def _open_settings(self):
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry("620x640")
        win.resizable(False, False)
        win.configure(bg=COLORS["bg"])
        win.grab_set()

        nb = ttk.Notebook(win)
        nb.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 0))

        # ── Tab 1: 模型与参数 ─────────────────────────────────────────────
        tab1 = tk.Frame(nb, bg=COLORS["bg"], padx=16, pady=12)
        nb.add(tab1, text="模型与参数")

        fields = {}

        def _add_field(parent, label, key, value, row, show=""):
            tk.Label(parent, text=label, bg=COLORS["bg"],
                     fg=COLORS["text"], font=("Helvetica", 11),
                     anchor=tk.W).grid(row=row, column=0, sticky=tk.W, pady=4)
            var = tk.StringVar(value=str(value))
            tk.Entry(parent, textvariable=var, font=("Menlo", 11),
                     bg=COLORS["card"], fg=COLORS["text"],
                     insertbackground=COLORS["text"], relief=tk.SOLID, bd=1,
                     highlightthickness=0, show=show,
                     width=30).grid(row=row, column=1, sticky=tk.EW, padx=(8, 0), pady=4)
            fields[key] = var

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

        # ── Tab 2/3: 用户背景（身份）与附加要求（均会真正生效）──────────────
        prompt_tabs = [
            ("用户背景 (身份)", "user_background", CONFIG.effective_user_background),
            ("附加要求 (可选)", "extra_instructions", CONFIG.extra_instructions),
        ]
        for tab_label, key, current_val in prompt_tabs:
            tab = tk.Frame(nb, bg=COLORS["bg"], padx=10, pady=10)
            nb.add(tab, text=tab_label)
            txt = scrolledtext.ScrolledText(
                tab, font=("Menlo", 10), wrap=tk.WORD, height=20,
                bg=COLORS["card"], fg=COLORS["text"],
                insertbackground=COLORS["text"], relief=tk.SOLID, bd=1,
            )
            txt.pack(fill=tk.BOTH, expand=True)
            txt.insert("1.0", current_val)
            fields[key] = txt

        # ── 底部按钮 ─────────────────────────────────────────────────────
        bottom = tk.Frame(win, bg=COLORS["bg"], pady=8)
        bottom.pack(fill=tk.X, padx=10)

        def _save():
            CONFIG.provider = label_to_key.get(provider_var.get(), "anthropic")
            CONFIG.send_screenshots = bool(send_ss_var.get())
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

            # 重建 provider（服务商 / API Key / Base URL 变更时生效）
            api_key, _ = resolve_credentials(CONFIG)
            if api_key:
                self.provider = make_provider(CONFIG)
            CONFIG.save()
            self._log("配置已保存（config.json 权限 0600）")
            win.destroy()

        MacButton(
            bottom, text="保存", bg=COLORS["start"], command=_save,
        ).pack(side=tk.RIGHT, padx=(0, 4))

        MacButton(
            bottom, text="取消", bg=COLORS["settings"], command=win.destroy,
        ).pack(side=tk.RIGHT, padx=(0, 8))

    def _show_window(self):
        self.root.deiconify()
        self.root.lift()

    def _on_close(self):
        if self.session and self.session.is_running:
            self.root.withdraw()
            self._log("窗口已隐藏，点击 Dock 图标恢复")
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ─── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    WorkReporter().run()
