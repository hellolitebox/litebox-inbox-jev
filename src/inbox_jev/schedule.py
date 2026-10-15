"""Run inbox-jev on a schedule on this computer: launchd on macOS, systemd user units on Linux.

A triage pass every 10 minutes and, when Slack is set up, the digest every day at 08:00 local
time, both with `INBOX_JEV_HOME` set to the home they were installed for (ADR 0053). Neither
scheduler starts a job again while its previous run is still going, so passes never overlap. A
server deployment uses Docker and the units in `deploy/systemd/` instead (docs/operations.md):
only one of the two may triage an account, or both would act on the same mail, so `install`
refuses where that server's timer is installed.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .paths import HOME_ENV

TRIAGE_EVERY_SECONDS = 600
DIGEST_HOUR, DIGEST_MINUTE = 8, 0
LAUNCHD_PREFIX = "io.github.inbox-jev"
SYSTEMD_PREFIX = "inbox-jev"

Runner = Callable[[Sequence[str]], None]
# Runs a command and returns its standard output (`loginctl` queries).
Reader = Callable[[Sequence[str]], str]


class Scheduler(StrEnum):
    LAUNCHD = "launchd"
    SYSTEMD = "systemd"


class ScheduleError(RuntimeError):
    """The schedule can't be set up here; the message says why and what to do instead."""


@dataclass(frozen=True)
class Job:
    """One scheduled command: `name` (triage, digest) runs `inbox-jev <args>`, either every
    `every_seconds` or daily at `daily_at` (hour, minute)."""

    name: str
    args: tuple[str, ...]
    every_seconds: int | None = None
    daily_at: tuple[int, int] | None = None


def jobs(*, digest: bool) -> list[Job]:
    """The jobs to schedule: triage always, the digest only when it has somewhere to go."""
    scheduled = [Job("triage", ("run",), every_seconds=TRIAGE_EVERY_SECONDS)]
    if digest:
        scheduled.append(Job("digest", ("digest",), daily_at=(DIGEST_HOUR, DIGEST_MINUTE)))
    return scheduled


def detect(platform: str = sys.platform) -> Scheduler:
    """This computer's scheduler, or ScheduleError (with a cron line to use instead)."""
    if platform == "darwin":
        return Scheduler.LAUNCHD
    if platform.startswith("linux") and shutil.which("systemctl"):
        return Scheduler.SYSTEMD
    raise ScheduleError(
        "only macOS (launchd) and Linux with systemd are supported; schedule it yourself, e.g. "
        f"with cron: */10 * * * * {HOME_ENV}=<home> {executable()} run"
    )


def executable() -> Path:
    """The `inbox-jev` command of the running installation (next to its Python), else on PATH."""
    beside = Path(sys.executable).with_name("inbox-jev")
    if beside.exists():
        return beside
    found = shutil.which("inbox-jev")
    if found is None:
        raise ScheduleError("can't find the inbox-jev command to schedule")
    return Path(found)


# --- launchd ---------------------------------------------------------------------------------


def launchd_label(job: Job) -> str:
    return f"{LAUNCHD_PREFIX}.{job.name}"


def launchd_plist(job: Job, command: Path, root: Path) -> bytes:
    """The LaunchAgent property list for `job`, logging to `root/logs/<name>.log`."""
    log = str(root / "logs" / f"{job.name}.log")
    agent: dict[str, object] = {
        "Label": launchd_label(job),
        "ProgramArguments": [str(command), *job.args],
        "EnvironmentVariables": {HOME_ENV: str(root)},
        "WorkingDirectory": str(root),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }
    if job.every_seconds is not None:
        agent["StartInterval"] = job.every_seconds
    if job.daily_at is not None:
        agent["StartCalendarInterval"] = {"Hour": job.daily_at[0], "Minute": job.daily_at[1]}
    return plistlib.dumps(agent)


# --- systemd ---------------------------------------------------------------------------------


def systemd_unit(job: Job) -> str:
    return f"{SYSTEMD_PREFIX}-{job.name}"


def systemd_units(job: Job, command: Path, root: Path) -> tuple[str, str]:
    """The user service and timer for `job` (its output goes to the journal)."""
    args = " ".join([_quoted(str(command)), *job.args])
    service = (
        "[Unit]\n"
        f"Description=inbox-jev {job.name}\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment={_quoted(f'{HOME_ENV}={root}')}\n"
        f"WorkingDirectory={_quoted(str(root))}\n"
        f"ExecStart={args}\n"
    )
    if job.every_seconds is not None:
        when = f"OnBootSec=2min\nOnUnitActiveSec={job.every_seconds}s\n"
    else:
        hour, minute = job.daily_at or (DIGEST_HOUR, DIGEST_MINUTE)
        when = f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\nPersistent=true\n"
    timer = (
        "[Unit]\n"
        f"Description=inbox-jev {job.name} timer\n\n"
        f"[Timer]\n{when}\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _quoted(value: str) -> str:
    """`value` as one systemd word: double-quoted, with `\\` and `"` escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# --- install / remove ------------------------------------------------------------------------


def _run(args: Sequence[str]) -> None:
    subprocess.run(list(args), check=True, capture_output=True, text=True)


def _read(args: Sequence[str]) -> str:
    return subprocess.run(list(args), check=True, capture_output=True, text=True).stdout


def failure(exc: OSError | subprocess.CalledProcessError) -> str:
    """What went wrong running a scheduler command, with what it printed when it failed."""
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        return f"{exc} ({str(exc.stderr).strip()})"
    return str(exc)


def _quiet(run: Runner, args: Sequence[str]) -> None:
    """`run` that tolerates failure: unloading a job that isn't loaded is fine."""
    with contextlib.suppress(subprocess.CalledProcessError):
        run(args)


@dataclass(frozen=True)
class Places:
    """Where each scheduler's files go (the user's own directories; tests pass temporary ones)."""

    launch_agents: Path
    systemd_user: Path
    # A server deployment's system timer (deploy/systemd/): scheduling here too would triage twice.
    server_timer: Path

    @classmethod
    def default(cls) -> Places:
        config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        user_config = Path(config) if config else Path.home() / ".config"
        return cls(
            launch_agents=Path.home() / "Library" / "LaunchAgents",
            systemd_user=user_config / "systemd" / "user",
            server_timer=Path("/etc/systemd/system/inbox-jev-triage.timer"),
        )


def refuse_if_server(places: Places) -> None:
    """ScheduleError when this computer already runs the server deployment's triage timer."""
    if places.server_timer.exists():
        raise ScheduleError(
            f"{places.server_timer} exists: this server already runs inbox-jev "
            "(docs/operations.md); a second schedule would triage the same mail twice."
        )


def install(
    scheduler: Scheduler,
    scheduled: Sequence[Job],
    root: Path,
    *,
    command: Path,
    places: Places,
    run: Runner | None = None,
) -> list[Path]:
    """Write and load `scheduled` (replacing an earlier install, removing a job no longer in it,
    e.g. the digest after Slack was turned off). The files written. `run` runs each
    `launchctl`/`systemctl` command (default: `subprocess`, failing on a non-zero exit).

    ScheduleError, before anything changes, on a server deployment (`refuse_if_server`). A failing
    command raises CalledProcessError (OSError when the tool is missing) and may leave the
    earlier jobs loaded and the later ones not: `installed` shows what is there and installing
    again starts over.
    """
    refuse_if_server(places)
    run = run or _run
    remove(scheduler, places=places, run=run)
    written: list[Path] = []
    if scheduler is Scheduler.LAUNCHD:
        (root / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
        places.launch_agents.mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        for job in scheduled:
            path = places.launch_agents / f"{launchd_label(job)}.plist"
            path.write_bytes(launchd_plist(job, command, root))
            run(["launchctl", "bootstrap", domain, str(path)])
            written.append(path)
        return written
    places.systemd_user.mkdir(parents=True, exist_ok=True)
    timers = []
    for job in scheduled:
        service, timer = systemd_units(job, command, root)
        for suffix, text in ((".service", service), (".timer", timer)):
            path = places.systemd_user / f"{systemd_unit(job)}{suffix}"
            path.write_text(text)
            written.append(path)
        timers.append(f"{systemd_unit(job)}.timer")
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", *timers])
    return written


def remove(scheduler: Scheduler, *, places: Places, run: Runner | None = None) -> list[Path]:
    """Unload and delete every inbox-jev job this module may have installed. The files removed.

    Unloading stops a job that is running: a triage pass under way ends mid-pass (SIGTERM).
    `install` starts with a removal, so reinstalling during a pass stops it too.
    """
    run = run or _run
    removed: list[Path] = []
    every = [Job(name, ()) for name in ("triage", "digest")]
    if scheduler is Scheduler.LAUNCHD:
        domain = f"gui/{os.getuid()}"
        for job in every:
            path = places.launch_agents / f"{launchd_label(job)}.plist"
            _quiet(run, ["launchctl", "bootout", f"{domain}/{launchd_label(job)}"])
            if path.exists():
                path.unlink()
                removed.append(path)
        return removed
    for job in every:
        _quiet(run, ["systemctl", "--user", "disable", "--now", f"{systemd_unit(job)}.timer"])
        for suffix in (".service", ".timer"):
            path = places.systemd_user / f"{systemd_unit(job)}{suffix}"
            if path.exists():
                path.unlink()
                removed.append(path)
    if removed:
        _quiet(run, ["systemctl", "--user", "daemon-reload"])
    return removed


def installed(scheduler: Scheduler, *, places: Places) -> list[Path]:
    """The inbox-jev job files present for `scheduler`."""
    if scheduler is Scheduler.LAUNCHD:
        return sorted(places.launch_agents.glob(f"{LAUNCHD_PREFIX}.*.plist"))
    return sorted(places.systemd_user.glob(f"{SYSTEMD_PREFIX}-*.timer"))


def linger_warning(scheduler: Scheduler, *, read: Reader | None = None) -> str | None:
    """A warning when systemd won't run this user's timers while they are logged out: without
    lingering, user units stop with the user's last session (a headless machine, after logout).
    None on launchd, with lingering on, or when `loginctl` can't tell."""
    if scheduler is not Scheduler.SYSTEMD:
        return None
    try:
        user = getpass.getuser()
        output = (read or _read)(["loginctl", "show-user", user, "--property=Linger"])
    except (OSError, subprocess.CalledProcessError):
        return None
    if output.strip() != "Linger=no":
        return None
    return (
        "systemd runs your user timers only while you are logged in. To run them after logout "
        f"and on a headless machine: loginctl enable-linger {user}"
    )
