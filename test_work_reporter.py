"""Tests for work_reporter.py — all external calls mocked."""
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

import work_reporter as wr


# ─── _merge_activities ────────────────────────────────────────────────────────

class TestMergeActivities:
    def test_empty(self):
        assert wr._merge_activities([]) == []

    def test_single(self):
        result = wr._merge_activities([
            {"timestamp": "09:00", "app": "VSCode", "window": "f.py", "duration_min": 5}
        ])
        assert len(result) == 1
        assert result[0]["windows"] == ["f.py"]

    def test_merge_same_app(self):
        activities = [
            {"timestamp": "09:00", "app": "VSCode", "window": "a.py", "duration_min": 5},
            {"timestamp": "09:05", "app": "VSCode", "window": "b.py", "duration_min": 10},
            {"timestamp": "09:15", "app": "VSCode", "window": "a.py", "duration_min": 3},
        ]
        result = wr._merge_activities(activities)
        assert len(result) == 1
        assert result[0]["duration_min"] == 18
        assert result[0]["windows"] == ["a.py", "b.py"]

    def test_different_apps(self):
        activities = [
            {"timestamp": "09:00", "app": "VSCode", "window": "x", "duration_min": 5},
            {"timestamp": "09:05", "app": "Chrome", "window": "y", "duration_min": 3},
            {"timestamp": "09:08", "app": "VSCode", "window": "z", "duration_min": 7},
        ]
        result = wr._merge_activities(activities)
        assert len(result) == 3
        assert [r["app"] for r in result] == ["VSCode", "Chrome", "VSCode"]

    def test_merge_dedup_windows(self):
        activities = [
            {"timestamp": "09:00", "app": "Chrome", "window": "tab1", "duration_min": 2},
            {"timestamp": "09:02", "app": "Chrome", "window": "tab1", "duration_min": 3},
        ]
        result = wr._merge_activities(activities)
        assert result[0]["windows"] == ["tab1"]
        assert result[0]["duration_min"] == 5


# ─── _collect_git_logs ────────────────────────────────────────────────────────

class TestCollectGitLogs:
    @patch("work_reporter.subprocess.run")
    def test_with_commits(self, mock_run):
        # First call: find git dirs
        find_result = MagicMock()
        find_result.stdout = "/Users/test/project/.git\n"
        # Second call: git log
        git_result = MagicMock()
        git_result.stdout = "abc1234 fix: something\ndef5678 feat: another"

        mock_run.side_effect = [find_result, git_result]
        result = wr._collect_git_logs(datetime.now() - timedelta(hours=8))
        assert "project" in result
        assert "fix: something" in result

    @patch("work_reporter.subprocess.run")
    def test_no_repos(self, mock_run):
        find_result = MagicMock()
        find_result.stdout = ""
        mock_run.return_value = find_result
        result = wr._collect_git_logs(datetime.now())
        assert result == ""

    @patch("work_reporter.subprocess.run")
    def test_filters_noise_dirs(self, mock_run):
        find_result = MagicMock()
        find_result.stdout = (
            "/Users/test/Library/something/.git\n"
            "/Users/test/.cache/x/.git\n"
            "/Users/test/real-project/.git\n"
        )
        git_result = MagicMock()
        git_result.stdout = "abc fix"
        mock_run.side_effect = [find_result, git_result]
        result = wr._collect_git_logs(datetime.now() - timedelta(hours=1))
        assert "real-project" in result


# ─── _collect_shell_history ───────────────────────────────────────────────────

class TestCollectShellHistory:
    def test_zsh_format(self, tmp_path):
        ts = int(datetime.now().timestamp())
        hist_content = (
            f": {ts - 100000}:0;old command\n"
            f": {ts}:0;git status\n"
            f": {ts}:0;pytest test.py\n"
            f": {ts}:0;cd /tmp\n"
        )
        hist_file = tmp_path / ".zsh_history"
        hist_file.write_text(hist_content)

        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_shell_history(
                datetime.now() - timedelta(hours=1), max_lines=50
            )
        assert "git status" in result
        assert "pytest test.py" in result
        # cd should be filtered
        assert "cd /tmp" not in result
        # old command should be excluded (before since)
        assert "old command" not in result

    def test_dedup(self, tmp_path):
        ts = int(datetime.now().timestamp())
        hist_content = (
            f": {ts}:0;git status\n"
            f": {ts}:0;git status\n"
            f": {ts}:0;git status\n"
        )
        hist_file = tmp_path / ".zsh_history"
        hist_file.write_text(hist_content)

        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_shell_history(
                datetime.now() - timedelta(hours=1)
            )
        assert result.count("git status") == 1

    def test_no_history_file(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_shell_history(datetime.now())
        assert result == ""


# ─── _collect_browser_history ─────────────────────────────────────────────────

class TestCollectBrowserHistory:
    def test_reads_chrome_history(self, tmp_path):
        chrome_dir = tmp_path / "Library/Application Support/Google/Chrome/Default"
        chrome_dir.mkdir(parents=True)
        hist_db = chrome_dir / "History"

        # Create a real SQLite DB
        conn = sqlite3.connect(str(hist_db))
        conn.execute(
            "CREATE TABLE urls (url TEXT, title TEXT, last_visit_time INTEGER)"
        )
        # Chrome epoch: microseconds since 1601-01-01
        chrome_epoch_offset = 11644473600
        now_chrome = (int(datetime.now().timestamp()) + chrome_epoch_offset) * 1_000_000
        conn.execute(
            "INSERT INTO urls VALUES (?, ?, ?)",
            ("https://example.com/test-plan/123", "Test Plan 123", now_chrome),
        )
        conn.execute(
            "INSERT INTO urls VALUES (?, ?, ?)",
            ("https://google.com/search?q=test", "Google Search", now_chrome),
        )
        conn.commit()
        conn.close()

        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_browser_history(
                datetime.now() - timedelta(hours=1), max_entries=50
            )
        assert "Test Plan 123" in result
        # google search should be filtered
        assert "Google Search" not in result

    def test_no_chrome(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_browser_history(datetime.now())
        assert result == ""


# ─── _collect_vscode_recent_files ─────────────────────────────────────────────

class TestCollectVscodeRecentFiles:
    def test_reads_recent_workspaces(self, tmp_path):
        state_dir = tmp_path / "Library/Application Support/Code/User/globalStorage"
        state_dir.mkdir(parents=True)
        state_db = state_dir / "state.vscdb"

        conn = sqlite3.connect(str(state_db))
        conn.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
        data = json.dumps({
            "entries": [
                {"folderUri": f"file://{tmp_path}/project1"},
                {"folderUri": f"file://{tmp_path}/project2"},
                {"fileUri": f"file://{tmp_path}/script.py"},
            ]
        })
        conn.execute("INSERT INTO ItemTable VALUES (?, ?)", (
            "history.recentlyOpenedPathsList", data
        ))
        conn.commit()
        conn.close()

        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_vscode_recent_files()
        assert "project1" in result
        assert "project2" in result

    def test_no_vscode(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            result = wr._collect_vscode_recent_files()
        assert result == ""


# ─── System detection ─────────────────────────────────────────────────────────

class TestSystemDetection:
    @patch("work_reporter.subprocess.run")
    def test_screen_locked(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='..."CGSSessionScreenIsLocked" = Yes...'
        )
        assert wr._is_screen_locked() is True

    @patch("work_reporter.subprocess.run")
    def test_screen_unlocked(self, mock_run):
        mock_run.return_value = MagicMock(stdout="some normal output")
        assert wr._is_screen_locked() is False

    @patch("work_reporter.subprocess.run")
    def test_idle_seconds(self, mock_run):
        # 5 seconds in nanoseconds
        mock_run.return_value = MagicMock(
            stdout='"HIDIdleTime" = 5000000000'
        )
        assert wr._get_idle_seconds() == pytest.approx(5.0)

    @patch("work_reporter.subprocess.run")
    def test_idle_no_match(self, mock_run):
        mock_run.return_value = MagicMock(stdout="no idle time")
        assert wr._get_idle_seconds() == 0.0

    @patch("work_reporter.subprocess.run")
    def test_get_active_window(self, mock_run):
        r1 = MagicMock(stdout="Google Chrome\n")
        r2 = MagicMock(stdout="GitHub\n")
        mock_run.side_effect = [r1, r2]
        app, win = wr.get_active_window()
        assert app == "Google Chrome"
        assert win == "GitHub"

    @patch("work_reporter.subprocess.run")
    def test_get_active_window_error(self, mock_run):
        mock_run.side_effect = Exception("timeout")
        app, win = wr.get_active_window()
        assert app == ""
        assert win == ""

    @patch("work_reporter.subprocess.run")
    def test_display_count(self, mock_run):
        wr._DISPLAY_COUNT_CACHE = None  # reset cache
        mock_run.return_value = MagicMock(
            stdout=json.dumps({
                "SPDisplaysDataType": [{
                    "spdisplays_ndrvs": [
                        {"_name": "Color LCD"},
                        {"_name": "DELL U2725QE"},
                    ]
                }]
            })
        )
        count = wr._get_display_count()
        assert count == 2
        wr._DISPLAY_COUNT_CACHE = None  # cleanup


# ─── Screenshot grouping and sampling ────────────────────────────────────────

class TestScreenshotSampling:
    def test_group_by_timestamp(self, tmp_path):
        files = [
            tmp_path / "20260512_091500_D1.png",
            tmp_path / "20260512_091500_D2.png",
            tmp_path / "20260512_092000_D1.png",
            tmp_path / "20260512_092000_D2.png",
        ]
        for f in files:
            f.write_bytes(b"fake")

        groups = wr._group_by_timestamp(files)
        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 2

    def test_group_missing_file(self, tmp_path):
        files = [
            tmp_path / "20260512_091500_D1.png",
            tmp_path / "nonexistent_D1.png",
        ]
        (tmp_path / "20260512_091500_D1.png").write_bytes(b"x")
        groups = wr._group_by_timestamp(files)
        assert len(groups) == 1

    def test_hash_distance_same(self):
        h = [1, 0, 1, 0, 1, 0, 1, 0]
        assert wr._hash_distance(h, h) == 0

    def test_hash_distance_different(self):
        h1 = [1, 1, 1, 1]
        h2 = [0, 0, 0, 0]
        assert wr._hash_distance(h1, h2) == 4

    def test_hash_distance_none(self):
        assert wr._hash_distance(None, [1, 2]) == 128
        assert wr._hash_distance([1], None) == 128

    def test_sample_within_limit(self, tmp_path):
        switch = []
        backup = []
        for i in range(5):
            ts = f"20260512_09{i:02d}00"
            p = tmp_path / f"{ts}_D1.png"
            p.write_bytes(b"x" * (i + 1))  # slightly different content
            switch.append(p)
        for i in range(3):
            ts = f"20260512_10{i:02d}00"
            p = tmp_path / f"{ts}_D1.png"
            p.write_bytes(b"y" * (i + 1))
            backup.append(p)

        result = wr._sample_screenshots(switch, backup)
        # All should be included (5 + 3 = 8 < MAX_SAMPLE_GROUPS=50)
        assert len(result) == 8


# ─── _build_instruction (configurable prompt / identity) ──────────────────────

class TestBuildInstruction:
    def test_default_uses_builtin_background_and_split(self):
        instr = wr._build_instruction("## 数据\n某些内容", "2026-05-12", "5月12日")
        assert "工作日报助手" in instr
        assert "软件工程师" in instr          # built-in default identity
        assert "## 数据\n某些内容" in instr    # data sections injected
        assert "===SPLIT===" in instr         # output contract preserved
        assert "# 2026-05-12 工作日报" in instr

    def test_user_background_is_configurable(self):
        with patch.object(wr.CONFIG, "user_background", "我是一名后端开发工程师，负责支付系统"):
            instr = wr._build_instruction("d", "2026-05-12", "5月12日")
        assert "我是一名后端开发工程师，负责支付系统" in instr
        assert "软件工程师" not in instr       # default identity fully replaced

    def test_extra_instructions_are_appended_and_take_effect(self):
        with patch.object(wr.CONFIG, "extra_instructions", "请着重描述踩坑与调试细节"):
            instr = wr._build_instruction("d", "2026-05-12", "5月12日")
        assert "请着重描述踩坑与调试细节" in instr


# ─── generate_reports ─────────────────────────────────────────────────────────

class TestGenerateReports:
    @patch("work_reporter._collect_git_logs", return_value="[my-project]\nabc fix test")
    @patch("work_reporter._collect_shell_history", return_value="pytest test.py")
    @patch("work_reporter._collect_browser_history", return_value="Docs (example.com)")
    @patch("work_reporter._collect_vscode_recent_files", return_value="~/my-project")
    def test_includes_all_data_sources(self, mock_vs, mock_br, mock_sh, mock_git):
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

    @patch("work_reporter._collect_git_logs", return_value="")
    @patch("work_reporter._collect_shell_history", return_value="")
    @patch("work_reporter._collect_browser_history", return_value="")
    @patch("work_reporter._collect_vscode_recent_files", return_value="")
    def test_no_split_fallback(self, mock_vs, mock_br, mock_sh, mock_git):
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
        # Should still return two parts via fallback split
        assert detailed
        assert summary
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50

    @patch("work_reporter._collect_git_logs", return_value="")
    @patch("work_reporter._collect_shell_history", return_value="")
    @patch("work_reporter._collect_browser_history", return_value="")
    @patch("work_reporter._collect_vscode_recent_files", return_value="")
    def test_no_user_notes(self, mock_vs, mock_br, mock_sh, mock_git):
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


# ─── Config security ──────────────────────────────────────────────────────────

class TestConfigSecurity:
    def test_write_config_is_owner_only(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        with patch.object(wr, "DATA_DIR", tmp_path), \
             patch.object(wr, "CONFIG_FILE", cfg_file):
            wr._write_config({"api_key": "secret-key", "model": "x"})
        assert cfg_file.exists()
        # config.json may hold the API key → must be owner read/write only
        assert (cfg_file.stat().st_mode & 0o777) == 0o600
        assert json.loads(cfg_file.read_text("utf-8"))["api_key"] == "secret-key"

    def test_secure_dir_is_owner_only(self, tmp_path):
        d = tmp_path / "sub" / "data"
        wr._secure_dir(d)
        assert d.is_dir()
        assert (d.stat().st_mode & 0o777) == 0o700

    def test_load_config_tightens_existing_perms(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text('{"api_key": "leaked"}', "utf-8")
        cfg_file.chmod(0o644)  # simulate a world-readable legacy config
        with patch.object(wr, "DATA_DIR", tmp_path), \
             patch.object(wr, "CONFIG_FILE", cfg_file):
            cfg = wr._load_config()
        assert cfg["api_key"] == "leaked"
        assert (cfg_file.stat().st_mode & 0o777) == 0o600


# ─── Robustness helpers ───────────────────────────────────────────────────────

class TestRobustness:
    def test_safe_fromisoformat_valid(self):
        assert wr._safe_fromisoformat("2026-05-12T09:00:00") == datetime(2026, 5, 12, 9, 0, 0)

    def test_safe_fromisoformat_garbage(self):
        assert wr._safe_fromisoformat("not-a-date") is None
        assert wr._safe_fromisoformat("") is None
        assert wr._safe_fromisoformat(None) is None

    def test_atomic_write_json_roundtrip_and_no_tmp_left(self, tmp_path):
        p = tmp_path / "state.json"
        wr._atomic_write_json(p, {"a": 1, "中文": "值"})
        assert json.loads(p.read_text("utf-8")) == {"a": 1, "中文": "值"}
        # temp file must not linger
        assert list(tmp_path.glob("*.tmp")) == []

    def test_encode_corrupt_image_returns_none(self, tmp_path):
        bad = tmp_path / "corrupt.png"
        bad.write_bytes(b"this is not a real image")
        assert wr._encode_image_for_api(bad) is None

    def test_encode_good_image(self, tmp_path):
        from PIL import Image
        good = tmp_path / "g.png"
        Image.new("RGB", (60, 40), "blue").save(good, "PNG")
        result = wr._encode_image_for_api(good)
        assert result is not None
        data, media_type = result
        assert media_type == "image/jpeg"
        assert data

    def test_resolve_lark_executor_picks_newest_version(self, tmp_path):
        base = (tmp_path / ".claude/plugins/cache"
                / "my-marketplace/lark-feishu-tools")
        for ver in ["0.1.2", "0.1.10", "0.2.0"]:
            d = base / ver / "skills/feishu-doc/scripts"
            d.mkdir(parents=True)
            (d / "executor.mjs").write_text("//")
        with patch.object(Path, "home", return_value=tmp_path):
            exe = wr._resolve_lark_executor()
        assert exe is not None
        # 0.2.0 > 0.1.10 > 0.1.2 by version, not by string
        assert exe.relative_to(base).parts[0] == "0.2.0"

    def test_resolve_lark_executor_absent(self, tmp_path):
        with patch.object(Path, "home", return_value=tmp_path):
            assert wr._resolve_lark_executor() is None


class TestGenerateReportsEmptyResponse:
    @patch("work_reporter._collect_git_logs", return_value="")
    @patch("work_reporter._collect_shell_history", return_value="")
    @patch("work_reporter._collect_browser_history", return_value="")
    @patch("work_reporter._collect_vscode_recent_files", return_value="")
    def test_empty_response_raises(self, mock_vs, mock_br, mock_sh, mock_git):
        mock_provider = MagicMock()
        mock_provider.generate.return_value = ("   \n  ", {"input_tokens": 1, "output_tokens": 0})

        activities = [{"timestamp": "09:00", "app": "X", "window": "y", "duration_min": 1}]
        with pytest.raises(ValueError):
            wr.generate_reports(
                mock_provider, activities, "2026-05-12",
                session_start=datetime(2026, 5, 12, 9, 0),
            )


# ─── take_screenshot ──────────────────────────────────────────────────────────

class TestTakeScreenshot:
    @patch("work_reporter._get_display_count", return_value=2)
    @patch("work_reporter.subprocess.run")
    def test_dual_display(self, mock_run, mock_count, tmp_path):
        with patch.object(wr, "SCREENSHOT_DIR", tmp_path):
            def fake_screencapture(cmd, **kwargs):
                path = Path(cmd[-1])
                # Create a minimal valid 1x1 PNG
                from PIL import Image
                img = Image.new("RGB", (100, 100), "red")
                img.save(path, "PNG")
                return MagicMock(returncode=0)

            mock_run.side_effect = fake_screencapture
            paths = wr.take_screenshot()
            assert mock_run.call_count == 2
            assert len(paths) == 2
            for call in mock_run.call_args_list:
                cmd = call[0][0]
                assert "-D" in cmd

    @patch("work_reporter._get_display_count", return_value=1)
    @patch("work_reporter.subprocess.run")
    def test_single_display(self, mock_run, mock_count, tmp_path):
        with patch.object(wr, "SCREENSHOT_DIR", tmp_path):
            mock_run.return_value = MagicMock(returncode=0)
            wr.take_screenshot()
            assert mock_run.call_count == 1


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
        assert switch == [str(real)]

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
        assert s.last_app == "VSCode"
        assert r.take_switch_screenshot is False

    def test_poll_debounce_not_elapsed_holds(self):
        s = self._running()
        s.pending_app, s.pending_window = "Chrome", "GitHub"
        s.pending_since = datetime(2026, 5, 12, 9, 1, 0)
        r = s.poll(datetime(2026, 5, 12, 9, 1, 5), ("Chrome", "GitHub"), 0, False)
        assert s.last_app == "VSCode"
        assert s.activities == []
        assert r.take_switch_screenshot is False

    def test_poll_debounce_elapsed_records_and_switches(self):
        s = self._running()
        s.pending_app, s.pending_window = "Chrome", "GitHub"
        s.pending_since = datetime(2026, 5, 12, 9, 1, 0)
        r = s.poll(datetime(2026, 5, 12, 9, 1, 12), ("Chrome", "GitHub"), 0, False)
        assert len(s.activities) == 1
        assert s.activities[0]["app"] == "VSCode"
        assert s.last_app == "Chrome" and s.last_window == "GitHub"
        assert s.last_change_time == datetime(2026, 5, 12, 9, 1, 0)
        assert s.pending_app == "" and s.pending_since is None
        assert r.take_switch_screenshot is True
        assert any("切换 → Chrome — GitHub" in m for m in r.log_messages)

    def test_poll_locked_flushes_and_labels(self):
        s = self._running()
        r = s.poll(datetime(2026, 5, 12, 9, 10, 0), ("VSCode", "a.py"), 0, True)
        assert len(s.activities) == 1
        assert s.last_app == "" and s.last_window == ""
        assert r.count_label == "🔒 锁屏中"
        assert r.take_switch_screenshot is False

    def test_poll_idle_enter_then_exit(self):
        s = self._running()
        r1 = s.poll(datetime(2026, 5, 12, 9, 10, 0), ("VSCode", "a.py"), 300, False)
        assert s.is_idle is True
        assert len(s.activities) == 1
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
        assert r.count_label is None
        assert s.activities == []

    def test_stop_marks_not_running(self):
        s = self._session()
        s.is_paused = True
        s.stop()
        assert s.is_running is False
        assert s.is_paused is False


class TestConfig:
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

    def test_effective_user_background_falls_back_to_default(self):
        assert wr.Config(user_background="").effective_user_background == wr.DEFAULT_USER_BACKGROUND
        assert wr.Config(user_background="   ").effective_user_background == wr.DEFAULT_USER_BACKGROUND
        assert wr.Config(user_background="我是后端").effective_user_background == "我是后端"

    def test_lark_script_dir_path(self):
        assert wr.Config(lark_script_dir="").lark_script_dir_path == Path()
        assert wr.Config(lark_script_dir="~/x").lark_script_dir_path == Path(os.path.expanduser("~/x"))

    def test_module_singleton_exists(self):
        assert isinstance(wr.CONFIG, wr.Config)


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
