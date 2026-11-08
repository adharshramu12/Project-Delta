"""Update notifications (delta.updates): version order, what is asked of GitHub and what is never sent, the banner,
rate limits and offline PCs, the pinned and checked update plan, the dashboard API, `delta update`, the installer's
file check, Telegram once per version, and `delta doctor`. GitHub is a stand-in (fake_github): nothing leaves the PC.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_github import LOCK, REPO, FakeGitHub, pyproject
from fastapi.testclient import TestClient

import delta.updates.checker as checker_mod
from delta.config import Settings, read_user_settings
from delta.updates import install
from delta.updates.checker import (
    Release,
    UpdateChecker,
    UpdateError,
    changelog_section,
    highlights_of,
    resolve_ref,
    sanitize_notes,
)
from delta.updates.service import UpdateService, telegram_text
from delta.updates.version import is_newer, parse


@pytest.fixture(autouse=True)
def checks_on(monkeypatch):
    for k in ("DELTA_NO_UPDATE_CHECK", "DELTA_REPO", "DELTA_REF", "DELTA_UPDATE_API", "DELTA_UPDATE_RAW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(checker_mod, "__version__", "0.3.0")


def make(tmp_path, gh: FakeGitHub, **updates: Any) -> UpdateChecker:
    s = Settings(data_dir=tmp_path)
    for k, v in updates.items():
        setattr(s.updates, k, v)
    return UpdateChecker(s, transport=gh.transport())


# ---- versions --------------------------------------------------------------------------------------------------
def test_semver_order_follows_the_spec():
    order = ["0.9.9", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2",
             "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0", "1.0.1", "1.2.0", "1.10.0", "2.0.0"]
    parsed = [parse(v) for v in order]
    assert all(p is not None for p in parsed)
    assert sorted(parsed, key=lambda p: p.key()) == parsed  # type: ignore[union-attr]
    assert parse("v0.3.0") == parse("0.3.0") and parse("0.3.0+build.7") == parse("0.3.0")
    for bad in ("", "0.3", "01.2.3", "1.2.3.4", "latest", "v1.2.x", "1.2.3-", None):
        assert parse(bad) is None, bad
    assert is_newer("0.10.0", "0.9.0") and not is_newer("0.3.0", "0.3.0") and not is_newer("0.3.0-rc.1", "0.3.0")
    assert not is_newer("nonsense", "0.1.0") and is_newer("0.3.0", "0.0.0+unknown")


# ---- release notes are plain text -----------------------------------------------------------------------------
def test_release_notes_become_plain_safe_text():
    body = ("## What's new\r\n<script>alert(1)</script>\n- **Tour** for new users ([docs](https://evil.example/x))\n"
            "- Update banner &lt;img src=x onerror=alert(1)&gt;\n- ![pic](https://x/y.png)Pictures gone\n"
            "- Copy \u2014 with a dash\u202e reversed\n<!-- hidden -->\n- Fifth\n- Sixth")
    notes = sanitize_notes(body)
    assert "<" not in notes and ">" not in notes and "script" not in notes.replace("alert(1)", "")
    assert "https://" not in notes and "docs" in notes and "\u202e" not in notes and "\u2014" not in notes
    assert "hidden" not in notes and "**" not in notes
    hl = highlights_of(notes)
    assert hl[0] == "Tour for new users (docs)" and len(hl) == 4
    assert all(len(h) <= 160 for h in highlights_of("- " + "x" * 500))


def test_changelog_section_is_found_by_version():
    text = "# Changelog\n\n## Unreleased\n- soon\n\n## 0.3.0 - 2026-11-07\n### Added\n- Tour\n\n## 0.2.0\n- old\n"
    assert changelog_section(text, "0.3.0").strip() == "### Added\n- Tour"
    assert changelog_section(text, "0.9.0") == ""


# ---- what is asked of GitHub ------------------------------------------------------------------------------------
def test_latest_release_and_only_a_plain_get_with_the_user_agent(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0", body="- Faster tasks\n- New tour")
    view = make(tmp_path, gh).check()
    assert view["latest"] == "0.4.0" and view["newer"] and view["show_banner"] and view["source"] == "release"
    assert view["highlights"] == ["Faster tasks", "New tour"]
    assert view["url"] == f"https://github.com/{REPO}/releases/tag/v0.4.0"
    method, path, headers = gh.requests[0]
    assert method == "GET" and path == f"/repos/{REPO}/releases/latest"
    assert headers["user-agent"] == f"Delta/{checker_mod.__version__}" == "Delta/0.3.0"
    allowed = {"user-agent", "accept", "x-github-api-version", "if-none-match", "host", "accept-encoding",
               "connection"}
    assert set(headers) <= allowed, set(headers) - allowed  # no id, no cookie, nothing about this PC


def test_drafts_and_prereleases_are_ignored_unless_beta(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0")
    gh.release("v0.5.0-beta.1", prerelease=True)
    gh.release("v0.6.0", draft=True)
    assert make(tmp_path / "a", gh).check()["latest"] == "0.4.0"
    beta = make(tmp_path / "b", gh, channel="beta").check()
    assert beta["latest"] == "0.5.0-beta.1" and beta["prerelease"] and beta["newer"]


def test_no_release_yet_uses_mains_pyproject_and_changelog(tmp_path):
    gh = FakeGitHub()
    gh.set_main("0.3.1", "# Changelog\n## 0.3.1\n### Fixed\n- A crash on start\n## 0.3.0\n- older\n")
    view = make(tmp_path, gh).check()
    assert view["latest"] == "0.3.1" and view["source"] == "main" and view["highlights"] == ["A crash on start"]
    same = FakeGitHub()
    same.set_main("0.3.0", "## 0.3.0\n- x")
    v2 = make(tmp_path / "s", same).check()
    assert v2["latest"] == "0.3.0" and not v2["newer"] and not v2["show_banner"]
    assert not any(p.endswith("CHANGELOG.md") for _, p, _ in same.requests)  # notes only fetched when newer


def test_etag_makes_an_unchanged_answer_free(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0")
    c = make(tmp_path, gh)
    c.check()
    c.update_state(next_at=0)
    assert c.check()["latest"] == "0.4.0"
    assert "if-none-match" in gh.requests[-1][2]


def test_rate_limit_backs_off_and_does_not_ask_again(tmp_path):
    gh = FakeGitHub(rate_limited=True)
    c = make(tmp_path, gh, )
    view = c.check()
    assert "rate limit" in view["error"] and view["rate_limited_until"] and not view["newer"]
    asked = len(gh.requests)
    c.check(force=True)
    c.check()
    assert len(gh.requests) == asked  # nothing more until GitHub's reset time


def test_offline_is_quiet_and_backs_off(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    c = UpdateChecker(Settings(data_dir=tmp_path), transport=httpx.MockTransport(down))
    view = c.check()
    assert "offline" in view["error"] and not view["newer"]
    state = c.load()
    assert state["failures"] == 1 and state["next_at"] > state["checked_at"] + 600
    c.update_state(next_at=0)
    c.check()
    assert c.load()["failures"] == 2


def test_checks_off_never_ask(tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.release("v0.4.0")
    assert not make(tmp_path, gh, check=False).check(force=True)["enabled"] and not gh.requests
    monkeypatch.setenv("DELTA_NO_UPDATE_CHECK", "1")
    assert not make(tmp_path, gh).check(force=True)["newer"] and not gh.requests


def test_a_redirect_to_another_site_is_refused(tmp_path):
    def away(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://evil.example/releases"})
        return httpx.Response(200, json={"tag_name": "v9.9.9"})

    view = UpdateChecker(Settings(data_dir=tmp_path), transport=httpx.MockTransport(away)).check()
    assert view["latest"] is None and "another site" in view["error"]


def test_test_addresses_only_on_this_pc(monkeypatch):
    monkeypatch.setenv("DELTA_UPDATE_API", "https://evil.example")
    monkeypatch.setenv("DELTA_UPDATE_RAW", "http://127.0.0.1:9999/")
    assert checker_mod.api_base() == checker_mod.API and checker_mod.raw_base() == "http://127.0.0.1:9999"
    monkeypatch.setenv("DELTA_REPO", "../../etc")
    assert checker_mod.repo() == REPO
    monkeypatch.setenv("DELTA_REPO", "someone/Project-Delta")
    assert checker_mod.repo() == "someone/Project-Delta"


def test_banner_later_and_skip(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0")
    c = make(tmp_path, gh)
    assert c.check()["show_banner"]
    c.update_state(skipped="0.4.0")
    assert not c.view()["show_banner"]
    gh.release("v0.4.1")
    c.update_state(next_at=0)
    assert c.check()["show_banner"]  # a newer one than the skipped version shows again
    c.update_state(later_until=c.clock() + 3600)
    assert not c.view()["show_banner"]


# ---- the update plan: one commit, two hashes ----------------------------------------------------------------------
def test_resolve_pins_the_tag_commit_and_hashes_its_files(tmp_path):
    gh = FakeGitHub()
    sha = gh.release("v0.4.0")
    c = make(tmp_path, gh)
    c.check()
    target = c.resolve(c.latest_release())  # type: ignore[arg-type]
    assert target.sha == sha and target.version == "0.4.0" and target.repo == REPO
    assert target.pyproject_sha256 == hashlib.sha256(pyproject("0.4.0").encode()).hexdigest()
    assert target.lock_sha256 == hashlib.sha256(LOCK.encode()).hexdigest()
    assert target.expect_arg() == f"pyproject.toml={target.pyproject_sha256};uv.lock={target.lock_sha256}"


def test_resolve_refuses_a_tag_whose_files_say_another_version(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0", version="0.3.9")
    c = make(tmp_path, gh)
    c.check()
    with pytest.raises(UpdateError) as e:
        c.resolve(c.latest_release())  # type: ignore[arg-type]
    assert e.value.kind == "integrity"


def test_resolve_refuses_a_lock_that_is_not_deltas(tmp_path):
    gh = FakeGitHub()
    sha = gh.release("v0.4.0")
    gh.files[sha]["uv.lock"] = 'version = 1\n[[package]]\nname = "something-else"\n'
    c = make(tmp_path, gh)
    c.check()
    with pytest.raises(UpdateError) as e:
        c.resolve(c.latest_release())  # type: ignore[arg-type]
    assert e.value.kind == "integrity"


def test_resolve_ref_takes_a_commit_without_asking_the_api(tmp_path):
    gh = FakeGitHub()
    sha = gh.set_main("0.4.0")
    t = resolve_ref(make(tmp_path, gh), sha)
    assert t.sha == sha and not any(p.startswith("/repos/") for _, p, _ in gh.requests)
    with pytest.raises(UpdateError):
        resolve_ref(make(tmp_path, gh), "no-such-branch")
    with pytest.raises(UpdateError):
        resolve_ref(make(tmp_path, gh), "../../x")


# ---- the dashboard API ------------------------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path, monkeypatch):
    from engine_helpers import no_registry

    from delta.api.server import create_app, load_token
    from delta.bootstrap import build

    no_registry(monkeypatch, tmp_path)
    gh = FakeGitHub()
    gh.release("v0.4.0", body="- New tour\n- Update banner")
    c = build(Settings(data_dir=tmp_path))
    c.updates.checker.transport = gh.transport()
    launched: list[Any] = []
    monkeypatch.setattr(install, "launch", launched.append)
    monkeypatch.setattr(install, "supported", lambda: True)
    monkeypatch.setattr(install, "is_dev_checkout", lambda root=None: False)
    token = load_token(tmp_path)
    with TestClient(create_app(c, token)) as tc:
        tc.headers["Authorization"] = f"Bearer {token}"
        yield SimpleNamespace(tc=tc, c=c, gh=gh, launched=launched, data=tmp_path)


def test_api_shows_the_banner_and_remembers_skip_and_later(api):
    assert api.tc.get("/api/updates").json()["latest"] is None  # GET never asks GitHub
    assert not api.gh.requests
    v = api.tc.post("/api/updates/check").json()
    assert v["show_banner"] and v["latest"] == "0.4.0" and v["can_install"] and v["highlights"][0] == "New tour"
    assert not api.tc.post("/api/updates/later").json()["show_banner"]
    api.c.updates.checker.update_state(later_until=0)
    assert not api.tc.post("/api/updates/skip", json={"version": "0.4.0"}).json()["show_banner"]


def test_api_update_settings_are_saved(api):
    v = api.tc.put("/api/updates/settings", json={"check": False, "channel": "beta", "telegram": False}).json()
    assert not v["enabled"] and v["channel"] == "beta" and not v["telegram"]
    assert read_user_settings(api.data)["updates"] == {"check": False, "channel": "beta", "telegram": False}
    assert api.tc.put("/api/updates/settings", json={"channel": "nightly"}).status_code == 422


def test_update_now_waits_for_running_tasks_then_installs_the_checked_commit(api):
    api.tc.post("/api/updates/check")

    async def forever() -> None:
        await asyncio.sleep(3600)

    loop = asyncio.new_event_loop()
    try:
        busy = loop.create_task(forever())
        api.c.active["t1"] = busy
        r = api.tc.post("/api/updates/install", json={"version": "0.4.0"})
        assert r.status_code == 409 and r.json()["busy"] == 1 and not api.launched
        busy.cancel()
        loop.run_until_complete(asyncio.gather(busy, return_exceptions=True))
    finally:
        loop.close()
    assert api.tc.post("/api/updates/install", json={"version": "0.3.9"}).status_code == 409  # not what was shown
    r = api.tc.post("/api/updates/install", json={"version": "0.4.0"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    (target,) = api.launched
    assert target.sha == api.gh.refs["v0.4.0"] and target.version == "0.4.0"
    assert api.c.updates.checker.load()["whats_new"]["version"] == "0.4.0"


def test_update_now_refuses_when_already_newest_or_a_dev_copy(api, monkeypatch):
    assert api.tc.post("/api/updates/install", json={}).status_code == 400  # nothing newer known
    api.tc.post("/api/updates/check")
    monkeypatch.setattr(install, "is_dev_checkout", lambda root=None: True)
    assert "git pull" in api.tc.post("/api/updates/install", json={}).json()["detail"]
    assert not api.launched


def test_whats_new_shows_once_after_the_update(api, monkeypatch):
    api.c.updates.checker.update_state(whats_new={"version": "0.4.0", "notes": "- New tour",
                                                  "highlights": ["New tour"]})
    assert api.tc.get("/api/updates").json()["whats_new"] is None  # still 0.3.0: the update has not happened
    monkeypatch.setattr(checker_mod, "__version__", "0.4.0")
    assert api.tc.get("/api/updates").json()["whats_new"]["highlights"] == ["New tour"]
    api.tc.post("/api/updates/whats-new/seen")
    assert api.tc.get("/api/updates").json()["whats_new"] is None


def test_tour_state_is_saved_on_the_pc(api, monkeypatch):
    assert api.tc.get("/api/tour").json() == {"done": False, "setup_completed": False}
    api.tc.post("/api/setup/complete", json={"owner_name": "", "autonomy": "ask-risky"})
    assert api.tc.get("/api/tour").json() == {"done": False, "setup_completed": True}
    assert api.tc.put("/api/tour", json={"done": True}).json() == {"done": True}
    assert read_user_settings(api.data)["tour"] == {"done": True} and api.c.settings.tour.done
    assert api.tc.get("/api/tour").json()["done"]
    from delta.config import load_settings

    monkeypatch.setenv("DELTA_DATA_DIR", str(api.data))
    assert load_settings().tour.done  # read back after a restart


# ---- Telegram: once per version -------------------------------------------------------------------------------
def test_telegram_message_once_per_version(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0", body="- New tour")
    svc = UpdateService(Settings(data_dir=tmp_path), make(tmp_path, gh))
    sent: list[dict[str, Any]] = []

    async def notify(view: dict[str, Any]) -> bool:
        sent.append(view)
        return True

    svc.notify = notify
    view = svc.checker.check()
    asyncio.run(svc.tell(view))
    asyncio.run(svc.tell(svc.checker.view()))
    assert len(sent) == 1
    text = telegram_text(view)
    assert "Delta 0.4.0 is out." in text and "New tour" in text and "Nothing is installed until you do" in text
    assert "\u2014" not in text


# ---- delta update ------------------------------------------------------------------------------------------------
def _target() -> Any:
    from delta.updates.checker import Target

    return Target(REPO, "a" * 40, "0.4.0", "b" * 64, "c" * 64)


def test_update_now_command_is_pinned_and_reopens_delta(tmp_path, monkeypatch):
    import delta.__main__ as cli

    calls: list[Any] = []
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(cli, "running_delta", lambda data_dir: 4242)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a[0]))
    install_dir = tmp_path / "Programs" / "Delta"
    assert cli._update(stop=True, root=install_dir, from_dashboard=True, pin=_target()) == 0
    cmd = calls[0]
    assert cmd[cmd.index("-Branch") + 1] == "a" * 40 and cmd[cmd.index("-Repo") + 1] == REPO
    assert cmd[cmd.index("-ExpectSha256") + 1] == f"pyproject.toml={'b' * 64};uv.lock={'c' * 64}"
    assert cmd[cmd.index("-ExpectVersion") + 1] == "0.4.0"
    assert "-NoLaunch" not in cmd and "-StopRunning" in cmd
    assert cmd[cmd.index("-WaitForPid") + 1] == str(os.getpid())  # never the Delta it is about to close


def test_updater_command_carries_only_checked_values():
    cmd = install.updater_command(_target())
    assert cmd[1:5] == ["-m", "delta", "update", "--stop"] and "--from-dashboard" in cmd
    assert install.parse_expect(cmd[cmd.index("--pin-sha256") + 1]) == ("b" * 64, "c" * 64)
    assert install.parse_expect("pyproject.toml=zz;uv.lock=yy") is None
    from delta.updates.checker import Target

    with pytest.raises(ValueError):
        install.installer_command(install.ROOT, [1], True, Target("x/y; calc", "a" * 40, "1.0.0", "b" * 64, "c" * 64))


def test_pin_arguments_are_checked_again(monkeypatch):
    import delta.__main__ as cli

    good = SimpleNamespace(pin="a" * 40, pin_repo=REPO, pin_version="0.4.0",
                           pin_sha256=f"pyproject.toml={'b' * 64};uv.lock={'c' * 64}")
    assert cli._pin_from_args(good).sha == "a" * 40  # type: ignore[attr-defined]
    assert cli._pin_from_args(SimpleNamespace(pin="")) is None
    with pytest.raises(SystemExit):
        cli._pin_from_args(SimpleNamespace(**{**good.__dict__, "pin_repo": "a/b c"}))


def test_cli_update_target_falls_back_when_github_cannot_be_asked(tmp_path, monkeypatch, capsys):
    import delta.__main__ as cli

    monkeypatch.setattr(cli, "load_settings", lambda: Settings(data_dir=tmp_path))

    def offline(self: UpdateChecker, cache: Any = None) -> Release:
        raise UpdateError("Could not reach GitHub.", "offline")

    monkeypatch.setattr(UpdateChecker, "latest", offline)
    assert cli._update_target() is None and "main branch" in capsys.readouterr().out

    def tampered(self: UpdateChecker, rel: Release) -> Any:
        raise UpdateError("The release says 0.4.0 but its files say 0.3.9.", "integrity")

    monkeypatch.setattr(UpdateChecker, "latest", lambda self, cache=None: Release("0.4.0", "v0.4.0"))
    monkeypatch.setattr(UpdateChecker, "resolve", tampered)
    assert cli._update_target() is False and "Update stopped" in capsys.readouterr().out


def test_doctor_shows_this_and_the_newest_version(tmp_path, monkeypatch):
    import delta.__main__ as cli

    monkeypatch.setattr(UpdateChecker, "check", lambda self, force=False: {
        "current": "0.3.0", "enabled": True, "latest": "0.4.0", "source": "release", "newer": True, "error": ""})
    lines = cli._update_lines(Settings(data_dir=tmp_path))
    assert lines[0] == "this PC  Delta 0.3.0"
    assert "Delta 0.4.0 (GitHub release)" in lines[1] and "Update now" in lines[1]
    monkeypatch.setattr(UpdateChecker, "check", lambda self, force=False: (_ for _ in ()).throw(OSError("x")))
    assert "could not check" in cli._update_lines(Settings(data_dir=tmp_path))[1]


# ---- the installer checks the downloaded files ------------------------------------------------------------------
def test_installer_checks_the_files_it_downloaded(tmp_path):
    from test_fresh_user_minors import _run_ps

    folder = tmp_path / "src"
    folder.mkdir()
    (folder / "pyproject.toml").write_bytes(pyproject("0.4.0").encode())
    (folder / "uv.lock").write_bytes(LOCK.encode())
    good = (f"pyproject.toml={hashlib.sha256(pyproject('0.4.0').encode()).hexdigest()};"
            f"uv.lock={hashlib.sha256(LOCK.encode()).hexdigest()}")
    bad_lock = good.rsplit("=", 1)[0] + "=" + "0" * 64
    script = ("$t = [IO.File]::ReadAllText($env:DELTA_T_INSTALLER)\n. ([scriptblock]::Create($t)) -DefineOnly\n"
              "'A=' + (Test-DeltaFiles $env:F $env:GOOD '0.4.0')\n'B=' + (Test-DeltaFiles $env:F $env:BAD '0.4.0')\n"
              "'C=' + (Test-DeltaFiles $env:F $env:GOOD '0.5.0')\n'D=' + (Test-DeltaFiles $env:F 'uv.lock=x' '')\n")
    got = _run_ps(script, tmp_path, F=str(folder), GOOD=good, BAD=bad_lock)
    out = got.stdout
    assert "A=\n" in out.replace("\r", "") or out.replace("\r", "").rstrip().split("\n")[0] == "A=", out + got.stderr
    assert "B=uv.lock is not the file Delta checked" in out, out + got.stderr
    assert "C=its version is not 0.5.0" in out and "D=the expected file hashes are not valid" in out, out


def test_installer_verifies_before_it_closes_delta():
    text = (install.ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    verify, close = text.index("Test-DeltaFiles $Source $ExpectSha256"), text.index("$running = @(Get-RunningDelta)")
    assert verify < close
    code = [ln.split("#")[0] for ln in text.splitlines()]
    assert not [ln for ln in code if "Get-FileHash" in ln]  # a script-module command: refused under Restricted
    assert text.isascii()


def test_state_file_never_holds_secrets_or_ids(tmp_path):
    gh = FakeGitHub()
    gh.release("v0.4.0")
    c = make(tmp_path, gh)
    c.check()
    raw = (tmp_path / "updates.json").read_text(encoding="utf-8")
    assert set(json.loads(raw)) <= {"latest", "failures", "error", "rate_limited_until", "next_at", "etags",
                                    "checked_at"}
