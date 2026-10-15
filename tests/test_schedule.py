"""Running inbox-jev on a schedule on this computer: launchd and systemd user units (ADR 0053)."""

import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from inbox_jev import cli, schedule
from inbox_jev.schedule import Places, Scheduler

COMMAND = Path("/opt/tools/inbox-jev/bin/inbox-jev")


class Recorder:
    """The commands a schedule change runs; `fail` lists the first words of those that fail."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, args):
        self.calls.append(list(args))
        if any(" ".join(args).startswith(prefix) for prefix in self.fail):
            raise subprocess.CalledProcessError(1, list(args))


@pytest.fixture
def places(tmp_path):
    return Places(
        launch_agents=tmp_path / "LaunchAgents",
        systemd_user=tmp_path / "systemd",
        server_timer=tmp_path / "etc" / "inbox-jev-triage.timer",
    )


def test_triage_is_always_scheduled_and_the_digest_only_with_slack():
    assert [j.name for j in schedule.jobs(digest=False)] == ["triage"]
    triage, digest = schedule.jobs(digest=True)
    assert (triage.args, triage.every_seconds) == (("run",), 600)
    assert (digest.args, digest.daily_at) == (("digest",), (8, 0))


def test_a_launch_agent_runs_the_command_with_the_home(tmp_path):
    triage, digest = schedule.jobs(digest=True)
    agent = plistlib.loads(schedule.launchd_plist(triage, COMMAND, tmp_path))
    assert agent["Label"] == "io.github.inbox-jev.triage"
    assert agent["ProgramArguments"] == [str(COMMAND), "run"]
    assert agent["EnvironmentVariables"] == {"INBOX_JEV_HOME": str(tmp_path)}
    assert agent["StartInterval"] == 600 and "StartCalendarInterval" not in agent
    assert agent["StandardOutPath"] == str(tmp_path / "logs" / "triage.log")
    daily = plistlib.loads(schedule.launchd_plist(digest, COMMAND, tmp_path))
    assert daily["StartCalendarInterval"] == {"Hour": 8, "Minute": 0}
    assert "StartInterval" not in daily


def test_systemd_units_quote_paths_and_set_the_home():
    triage, digest = schedule.jobs(digest=True)
    home = Path('/home/ana/my "inbox"')
    service, timer = schedule.systemd_units(triage, COMMAND, home)
    assert 'Environment="INBOX_JEV_HOME=/home/ana/my \\"inbox\\""' in service
    assert f'ExecStart="{COMMAND}" run' in service
    assert "Type=oneshot" in service
    assert "OnUnitActiveSec=600s" in timer and "WantedBy=timers.target" in timer
    _, daily = schedule.systemd_units(digest, COMMAND, home)
    assert "OnCalendar=*-*-* 08:00:00" in daily and "Persistent=true" in daily


def test_launchd_install_writes_and_loads_each_job(tmp_path, places):
    run = Recorder(fail=("launchctl bootout",))  # nothing loaded yet
    written = schedule.install(
        Scheduler.LAUNCHD,
        schedule.jobs(digest=True),
        tmp_path,
        command=COMMAND,
        places=places,
        run=run,
    )
    assert [p.name for p in written] == [
        "io.github.inbox-jev.triage.plist",
        "io.github.inbox-jev.digest.plist",
    ]
    domain = f"gui/{os.getuid()}"
    assert [c for c in run.calls if c[1] == "bootstrap"] == [
        ["launchctl", "bootstrap", domain, str(p)] for p in written
    ]
    assert (tmp_path / "logs").is_dir()
    assert schedule.installed(Scheduler.LAUNCHD, places=places) == sorted(written)


def test_reinstalling_without_slack_drops_the_digest(tmp_path, places):
    run = Recorder()
    schedule.install(
        Scheduler.LAUNCHD,
        schedule.jobs(digest=True),
        tmp_path,
        command=COMMAND,
        places=places,
        run=run,
    )
    schedule.install(
        Scheduler.LAUNCHD,
        schedule.jobs(digest=False),
        tmp_path,
        command=COMMAND,
        places=places,
        run=run,
    )
    assert [p.name for p in schedule.installed(Scheduler.LAUNCHD, places=places)] == [
        "io.github.inbox-jev.triage.plist"
    ]


def test_systemd_install_enables_the_timers(tmp_path, places):
    run = Recorder()
    written = schedule.install(
        Scheduler.SYSTEMD,
        schedule.jobs(digest=True),
        tmp_path,
        command=COMMAND,
        places=places,
        run=run,
    )
    assert sorted(p.name for p in written) == [
        "inbox-jev-digest.service",
        "inbox-jev-digest.timer",
        "inbox-jev-triage.service",
        "inbox-jev-triage.timer",
    ]
    assert run.calls[-2:] == [
        ["systemctl", "--user", "daemon-reload"],
        [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "inbox-jev-triage.timer",
            "inbox-jev-digest.timer",
        ],
    ]


@pytest.mark.parametrize("scheduler", list(Scheduler))
def test_install_refuses_on_a_server_deployment(tmp_path, places, scheduler):
    """Whoever calls it (the command, `init`): the server's own timer already triages."""
    places.server_timer.parent.mkdir()
    places.server_timer.write_text("")
    run = Recorder()
    with pytest.raises(schedule.ScheduleError, match="triage the same mail twice"):
        schedule.install(
            scheduler, schedule.jobs(digest=True), tmp_path, command=COMMAND, places=places, run=run
        )
    assert run.calls == []
    assert schedule.installed(scheduler, places=places) == []


@pytest.mark.parametrize("scheduler", list(Scheduler))
def test_remove_unloads_and_deletes_everything(tmp_path, places, scheduler):
    run = Recorder()
    schedule.install(
        scheduler, schedule.jobs(digest=True), tmp_path, command=COMMAND, places=places, run=run
    )
    removed = schedule.remove(scheduler, places=places, run=Recorder(fail=("launchctl",)))
    assert removed and not any(p.exists() for p in removed)
    assert schedule.installed(scheduler, places=places) == []
    assert schedule.remove(scheduler, places=places, run=Recorder()) == []


def test_detect_names_the_scheduler_or_suggests_cron(monkeypatch):
    assert schedule.detect("darwin") is Scheduler.LAUNCHD
    monkeypatch.setattr(schedule.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert schedule.detect("linux") is Scheduler.SYSTEMD
    with pytest.raises(schedule.ScheduleError, match="cron"):
        schedule.detect("win32")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    with pytest.raises(schedule.ScheduleError):
        schedule.detect("linux")


def test_the_command_is_the_running_installations(monkeypatch, tmp_path):
    (tmp_path / "inbox-jev").write_text("")
    monkeypatch.setattr(schedule.sys, "executable", str(tmp_path / "python"))
    assert schedule.executable() == tmp_path / "inbox-jev"
    monkeypatch.setattr(schedule.sys, "executable", str(tmp_path / "other" / "python"))
    monkeypatch.setattr(schedule.shutil, "which", lambda name: "/usr/local/bin/inbox-jev")
    assert schedule.executable() == Path("/usr/local/bin/inbox-jev")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: None)
    with pytest.raises(schedule.ScheduleError):
        schedule.executable()


# --- The CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch, places):
    """A home with no Slack, launchd as the scheduler and the given places; `run` records."""
    monkeypatch.setenv("INBOX_JEV_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cli.logs, "setup", lambda level: None)
    monkeypatch.setattr(schedule, "detect", lambda: Scheduler.LAUNCHD)
    monkeypatch.setattr(schedule.Places, "default", classmethod(lambda cls: places))
    monkeypatch.setattr(schedule, "executable", lambda: COMMAND)
    monkeypatch.setattr(schedule, "_run", Recorder())
    return tmp_path


def test_schedule_install_status_and_remove(cli_env):
    runner = CliRunner()
    assert runner.invoke(cli.app, ["schedule", "status"]).exit_code == 1
    result = runner.invoke(cli.app, ["schedule", "install"])
    assert result.exit_code == 0, result.output
    assert "io.github.inbox-jev.triage.plist" in result.output
    assert "digest" not in result.output  # no Slack: no digest job
    status = runner.invoke(cli.app, ["schedule", "status"])
    assert status.exit_code == 0 and "triage" in status.output
    removed = runner.invoke(cli.app, ["schedule", "remove"])
    assert "Removed" in removed.output
    assert runner.invoke(cli.app, ["schedule", "remove"]).output.strip() == (
        "Nothing was scheduled."
    )


def test_schedule_install_refuses_on_a_server_deployment(cli_env, places):
    places.server_timer.parent.mkdir()
    places.server_timer.write_text("")
    result = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert result.exit_code == 2
    assert "triage the same mail twice" in result.output
    assert schedule.installed(Scheduler.LAUNCHD, places=places) == []


def test_a_failing_scheduler_command_says_how_to_recover(cli_env, monkeypatch):
    def bootstrap_fails(args):
        if args[:2] == ["launchctl", "bootstrap"]:
            raise subprocess.CalledProcessError(
                5, list(args), stderr="Bootstrap failed: 5: Input/output error\n"
            )

    monkeypatch.setattr(schedule, "_run", bootstrap_fails)
    result = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Bootstrap failed: 5: Input/output error" in result.output
    assert "`inbox-jev schedule status`" in result.output
    assert "`inbox-jev schedule install` again" in result.output


def test_a_missing_inbox_jev_command_exits_2(cli_env, monkeypatch):
    def missing():
        raise schedule.ScheduleError("can't find the inbox-jev command to schedule")

    monkeypatch.setattr(schedule, "executable", missing)
    result = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert result.exit_code == 2 and "can't find the inbox-jev command" in result.output


def test_systemd_without_lingering_warns_after_installing(cli_env, monkeypatch):
    queries: list[list[str]] = []

    def loginctl(args):
        queries.append(list(args))
        return "Linger=no\n"

    monkeypatch.setattr(schedule, "detect", lambda: Scheduler.SYSTEMD)
    monkeypatch.setattr(schedule, "_read", loginctl)
    result = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert result.exit_code == 0, result.output
    assert "Scheduled with systemd" in result.output
    assert "loginctl enable-linger" in result.output
    assert queries and queries[0][:2] == ["loginctl", "show-user"]


@pytest.mark.parametrize(
    ("scheduler", "answer", "warns"),
    [
        (Scheduler.SYSTEMD, "Linger=no\n", True),
        (Scheduler.SYSTEMD, "Linger=yes\n", False),
        (Scheduler.SYSTEMD, subprocess.CalledProcessError(1, ["loginctl"]), False),
        (Scheduler.SYSTEMD, FileNotFoundError("loginctl"), False),
        (Scheduler.LAUNCHD, "Linger=no\n", False),
    ],
)
def test_the_linger_warning(scheduler, answer, warns):
    def read(args):
        if isinstance(answer, Exception):
            raise answer
        return answer

    warning = schedule.linger_warning(scheduler, read=read)
    assert (warning is not None) == warns
    if warns:
        assert "enable-linger" in warning


def test_a_failure_shows_what_the_command_printed():
    failed = subprocess.CalledProcessError(5, ["launchctl"], stderr="Bootstrap failed: 5\n")
    assert schedule.failure(failed).endswith("(Bootstrap failed: 5)")
    assert schedule.failure(FileNotFoundError(2, "No such file", "launchctl")) == (
        "[Errno 2] No such file: 'launchctl'"
    )


def test_an_unsupported_system_exits_2(cli_env, monkeypatch):
    def unsupported():
        raise schedule.ScheduleError("only macOS (launchd) and Linux with systemd")

    monkeypatch.setattr(schedule, "detect", unsupported)
    result = CliRunner().invoke(cli.app, ["schedule", "status"])
    assert result.exit_code == 2 and "only macOS" in result.output
