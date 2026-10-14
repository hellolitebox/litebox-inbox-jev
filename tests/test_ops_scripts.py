"""The host-side ops scripts: failure alerts, the staleness watchdog, off-site backups and the
deploy's backup rotation. Each runs from a copy of scripts/ in a temporary deployment, with fake
curl, systemctl, journalctl, hostname and rclone first on PATH: no network, no systemd."""

import gzip
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import ops_message
import pytest
from conftest import ROOT

from inbox_jev.logs import JsonFormatter

SCRIPTS = ROOT / "scripts"
SHELL_SCRIPTS = sorted(SCRIPTS.glob("*.sh"))
TOKEN = "xoxb-test-secret-token"
HOST = "test-host"

# A command that records its arguments (one JSON list per line) and stdin, then answers from
# the files the test wrote: <name>.out (stdout) and <name>.exit (exit code).
FAKE = """#!{python}
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
home = Path(os.environ["FAKE_HOME"])
with (home / f"{{name}}.calls").open("a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if name == "curl":
    (home / "curl.stdin").write_text(sys.stdin.read())
if name == "rclone" and sys.argv[1] == "copyto":
    remote = home / "remote"
    remote.mkdir(exist_ok=True)
    (remote / Path(sys.argv[3]).name).write_bytes(Path(sys.argv[2]).read_bytes())
if name == "systemctl" and sys.argv[1] == "show":
    props = json.loads((home / "systemctl.json").read_text())
    print(props.get(sys.argv[-1], {{}}).get(sys.argv[sys.argv.index("-p") + 1], ""))
    sys.exit(0)
out = home / f"{{name}}.out"
if out.exists():
    sys.stdout.write(out.read_text())
code = home / f"{{name}}.exit"
sys.exit(int(code.read_text()) if code.exists() else 0)
"""


@dataclass
class Result:
    code: int
    out: str
    err: str


class Deployment:
    """A deployment checkout with the ops scripts, its .env, state dir and fake commands."""

    def __init__(self, root: Path, env_file: str) -> None:
        self.root = root
        self.bin = root / "bin"
        self.fakes = root / "fakes"
        self.state = root / "state"
        root.mkdir()
        for path in (self.bin, self.fakes, self.state, root / "scripts", root / "data"):
            path.mkdir()
        for script in [*SHELL_SCRIPTS, SCRIPTS / "ops_message.py"]:
            shutil.copy2(script, root / "scripts")
        (root / ".env").write_text(env_file)
        for name in ("curl", "systemctl", "journalctl", "hostname", "rclone"):
            path = self.bin / name
            path.write_text(FAKE.format(python=sys.executable))
            path.chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.answer("hostname", HOST + "\n")
        self.answer("curl", '{"ok":true,"channel":"U1"}')
        self.systemd({})

    def answer(self, command: str, out: str, code: int = 0) -> None:
        (self.fakes / f"{command}.out").write_text(out)
        (self.fakes / f"{command}.exit").write_text(str(code))

    def systemd(self, units: dict[str, dict[str, str]]) -> None:
        (self.fakes / "systemctl.json").write_text(json.dumps(units))

    def calls(self, command: str) -> list[list[str]]:
        path = self.fakes / f"{command}.calls"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def posted(self) -> list[dict[str, object]]:
        """The chat.postMessage bodies sent, in order."""
        return [json.loads(c[c.index("--data-binary") + 1]) for c in self.calls("curl")]

    def run(self, script: str, *args: str, **env: str) -> Result:
        base = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_HOME": str(self.fakes),
            "STATE_DIRECTORY": str(self.state),
            "TMPDIR": str(self.root),
            "HOME": str(self.root),
        }
        proc = subprocess.run(
            ["bash", str(self.root / "scripts" / script), *args],
            cwd=self.root,
            env=base | env,
            capture_output=True,
            text=True,
            check=False,
        )
        return Result(proc.returncode, proc.stdout, proc.stderr)


ENV = f"""SLACK_BOT_TOKEN={TOKEN}
ALERT_SLACK_CHANNEL=U1   # me
"""


@pytest.fixture
def deployment(tmp_path):
    return Deployment(tmp_path / "opt", ENV)


def log(msg, level="INFO", logger="inbox_jev.triage", **fields):
    """A JSON log line as `logs.JsonFormatter` writes it (an app logger unless `logger` says)."""
    line = {"ts": "2026-10-14T12:10:24+00:00", "level": level, "logger": logger, "msg": msg}
    return json.dumps(line | fields)


# --- alert.sh ---------------------------------------------------------------------------------

FAILED_RUN = "\n".join(
    [
        " Container inbox-jev-app-run-0c5f Creating",
        log("starting", revision="abc"),
        log(
            "account run failed",
            level="ERROR",
            account="me@acme.com",
            run_id=7,
            exc="Traceback (most recent call last):\n  ...\n"
            "google.auth.exceptions.RefreshError: Subject 'Your invoice' from boss@x.com",
            exc_type="google.auth.exceptions.RefreshError",
        ),
        log(  # googleapiclient's retry line: its message quotes the sent-mail search
            "Sleeping 1.23 seconds before retry 1 of 2 for request: GET https://gmail.googleapis"
            ".com/gmail/v1/users/me/messages?q=in%3Asent+to%3Aalice%40example.com, after 429",
            level="WARNING",
            logger="googleapiclient.http",
        ),
        log(
            "message failed",
            level="WARNING",
            account="me@acme.com",
            id="18f",
            sender="boss@x.com",
            error="HTTP 422",
        ),
        "me@acme.com: run failed: RefreshError: Subject 'Your invoice'",
    ]
)


def test_alert_posts_unit_host_result_and_safe_log_lines(deployment):
    deployment.answer("journalctl", FAILED_RUN)
    result = deployment.run(
        "alert.sh",
        "inbox-jev-triage.service",
        MONITOR_SERVICE_RESULT="exit-code",
        MONITOR_EXIT_STATUS="1",
        MONITOR_INVOCATION_ID="inv42",
    )

    assert result.code == 0, result.err
    [body] = deployment.posted()
    assert body["channel"] == "U1"  # the inline comment in .env isn't part of the value
    text = str(body["text"])
    assert "inbox-jev-triage.service failed" in text
    assert f"`{HOST}`" in text
    assert "Result: exit-code, exit status 1" in text
    assert "ERROR account run failed account=me@acme.com run_id=7" in text
    assert "error_type=google.auth.exceptions.RefreshError" in text
    assert "WARNING message failed account=me@acme.com" in text
    assert "WARNING [googleapiclient.http] (message not forwarded)" in text
    # Nothing an email or an API said: not the exception's message, the sender, a third-party
    # logger's message or plain stderr.
    assert "invoice" not in text
    assert "boss@x.com" not in text
    assert "alice" not in text
    assert "googleapis.com" not in text
    assert "Container" not in text
    assert deployment.calls("journalctl") == [
        ["-q", "-o", "cat", "--no-pager", "_SYSTEMD_INVOCATION_ID=inv42"]
    ]


def test_alert_never_puts_the_token_on_a_command_line_or_in_its_output(deployment):
    result = deployment.run("alert.sh", "inbox-jev-digest.service", MONITOR_INVOCATION_ID="i")

    assert result.code == 0
    assert TOKEN not in json.dumps(deployment.calls("curl"))
    assert TOKEN not in result.out + result.err
    # curl reads it from its config on stdin.
    stdin = (deployment.fakes / "curl.stdin").read_text()
    assert stdin == f'header = "Authorization: Bearer {TOKEN}"\n'


def test_alert_asks_systemd_when_run_by_hand(deployment):
    deployment.systemd(
        {
            "inbox-jev-digest.service": {
                "Result": "timeout",
                "ExecMainStatus": "0",
                "InvocationID": "",
            }
        }
    )
    result = deployment.run("alert.sh", "inbox-jev-digest.service")

    assert result.code == 0, result.err
    assert "Result: timeout, exit status 0" in str(deployment.posted()[0]["text"])
    assert "(no app log lines in this run)" in str(deployment.posted()[0]["text"])
    # No invocation id: the unit's last journal lines.
    assert deployment.calls("journalctl")[0][-4:] == [
        "-u",
        "inbox-jev-digest.service",
        "-n",
        "500",
    ]


def test_alert_escapes_slack_markup(deployment):
    deployment.answer("journalctl", log("run finished", account="<!channel>&co"))
    deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="i")

    text = str(deployment.posted()[0]["text"])
    assert "&lt;!channel&gt;&amp;co" in text
    assert "<!channel>" not in text


def test_alert_is_sent_once_per_incident(deployment):
    first = deployment.run("alert.sh", "inbox-jev-triage.service", MONITOR_INVOCATION_ID="a")
    again = deployment.run("alert.sh", "inbox-jev-triage.service", MONITOR_INVOCATION_ID="b")
    other = deployment.run("alert.sh", "inbox-jev-digest.service", MONITOR_INVOCATION_ID="c")

    assert (first.code, again.code, other.code) == (0, 0, 0)
    assert len(deployment.posted()) == 2  # triage once, digest once
    assert "already alerted" in again.out
    assert (deployment.state / "alerted-inbox-jev-triage.service").exists()


def test_alert_repeats_after_the_repeat_window(deployment):
    deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="a")
    marker = deployment.state / "alerted-u.service"
    old = time.time() - 7 * 3600
    os.utime(marker, (old, old))

    deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="b")

    assert len(deployment.posted()) == 2
    assert marker.stat().st_mtime > old


def test_alerts_are_off_without_a_channel(tmp_path):
    deployment = Deployment(tmp_path / "opt", f"SLACK_BOT_TOKEN={TOKEN}\n")
    result = deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="a")

    assert result.code == 0
    assert "ALERT_SLACK_CHANNEL is not set" in result.err
    assert deployment.calls("curl") == []
    assert not (deployment.state / "alerted-u.service").exists()  # alerts again once enabled


def test_alert_fails_when_slack_refuses_it(deployment):
    deployment.answer("curl", '{"ok":false,"error":"channel_not_found"}')
    result = deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="a")

    assert result.code == 1
    assert "Slack refused the alert: channel_not_found" in result.err
    assert not (deployment.state / "alerted-u.service").exists()  # retried next failure


def test_alert_fails_when_slack_is_unreachable(deployment):
    deployment.answer("curl", "", code=6)
    result = deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="a")

    assert result.code == 1
    assert "couldn't reach Slack" in result.err


def test_alert_needs_the_token(tmp_path):
    deployment = Deployment(tmp_path / "opt", "ALERT_SLACK_CHANNEL=C1\n")
    result = deployment.run("alert.sh", "u.service", MONITOR_INVOCATION_ID="a")

    assert result.code == 1
    assert "SLACK_BOT_TOKEN is not set" in result.err


def test_alert_rejects_a_bad_setting(deployment):
    result = deployment.run("alert.sh", "u.service", ALERT_REPEAT_HOURS="soon")

    assert result.code == 2
    assert "ALERT_REPEAT_HOURS must be a whole number, got 'soon'" in result.err


# --- watchdog.sh ------------------------------------------------------------------------------

TRIAGE = "inbox-jev-triage.service"
FINISHED = "Finished inbox-jev-triage.service - inbox-jev triage pass.\n"


def test_watchdog_is_quiet_while_passes_succeed(deployment):
    deployment.answer("journalctl", FINISHED * 5)
    result = deployment.run("watchdog.sh")

    assert result.code == 0, result.err
    assert "ok: 5 successful triage passes in the last 60 min" in result.out
    assert deployment.posted() == []
    [query] = deployment.calls("journalctl")
    assert query[query.index("--since") + 1] == "-60min"
    assert "JOB_RESULT=done" in query
    assert f"UNIT={TRIAGE}" in query


def test_watchdog_alerts_once_when_triage_is_stale_then_on_recovery(deployment):
    deployment.answer("journalctl", "")
    deployment.answer("systemctl", "inactive\n", code=3)  # `is-active` of the stopped timer
    deployment.systemd({TRIAGE: {"ActiveState": "inactive", "Result": "success"}})

    first = deployment.run("watchdog.sh", WATCHDOG_MAX_AGE_MINUTES="90")
    second = deployment.run("watchdog.sh", WATCHDOG_MAX_AGE_MINUTES="90")

    assert (first.code, second.code) == (0, 0), first.err
    [alert] = deployment.posted()
    assert "no successful triage pass in the last 90 min" in str(alert["text"])
    assert "Timer: inactive" in str(alert["text"])
    assert "still stale" in second.out

    deployment.answer("journalctl", FINISHED)
    recovered = deployment.run("watchdog.sh")

    assert recovered.code == 0
    assert "triage passes succeed again" in str(deployment.posted()[-1]["text"])
    assert not (deployment.state / f"stale-{TRIAGE}").exists()


def test_watchdog_posts_recovery_of_an_alerted_unit_once_it_succeeds(deployment):
    deployment.answer("journalctl", FINISHED)
    digest, triage = "inbox-jev-digest.service", TRIAGE
    for unit in (digest, triage):
        (deployment.state / f"alerted-{unit}").touch()
    now = int(time.time())
    deployment.systemd(
        {
            digest: {"Result": "success", "ExecMainExitTimestamp": f"@{now + 60}"},
            triage: {"Result": "exit-code", "ExecMainExitTimestamp": f"@{now + 60}"},
        }
    )

    result = deployment.run("watchdog.sh")

    assert result.code == 0, result.err
    [post] = deployment.posted()
    assert f"{digest} succeeds again" in str(post["text"])
    assert not (deployment.state / f"alerted-{digest}").exists()
    assert (deployment.state / f"alerted-{triage}").exists()  # still failing


def test_watchdog_ignores_a_reset_without_a_new_successful_run(deployment):
    """`systemctl reset-failed` sets Result=success; the last exit is still before the alert."""
    deployment.answer("journalctl", FINISHED)
    (deployment.state / "alerted-u.service").touch()
    deployment.systemd({"u.service": {"Result": "success", "ExecMainExitTimestamp": "@1000"}})

    deployment.run("watchdog.sh")

    assert deployment.posted() == []
    assert (deployment.state / "alerted-u.service").exists()


def test_watchdog_posts_its_own_recovery(deployment):
    """While the watchdog runs, systemd has reset its Result and exit timestamp: this run
    reaching the recovery check is the recovery."""
    deployment.answer("journalctl", FINISHED)
    watchdog = "inbox-jev-watchdog.service"
    (deployment.state / f"alerted-{watchdog}").touch()
    deployment.systemd({watchdog: {"Result": "success", "ExecMainExitTimestamp": ""}})

    result = deployment.run("watchdog.sh")

    assert result.code == 0, result.err
    [post] = deployment.posted()
    assert f"{watchdog} succeeds again" in str(post["text"])
    assert not (deployment.state / f"alerted-{watchdog}").exists()


def test_watchdog_does_not_repeat_a_fresh_alert_about_a_failed_pass(deployment):
    """Every pass fails: alert.sh alerted on the first failure; the watchdog stays quiet until
    ALERT_REPEAT_HOURS have passed since, then reminds once."""
    deployment.answer("journalctl", "")
    deployment.systemd({TRIAGE: {"Result": "exit-code"}})
    marker = deployment.state / f"alerted-{TRIAGE}"
    marker.touch()

    quiet = deployment.run("watchdog.sh", ALERT_REPEAT_HOURS="2")

    assert quiet.code == 0, quiet.err
    assert deployment.posted() == []
    assert "a failed pass was already alerted" in quiet.out
    assert not (deployment.state / f"stale-{TRIAGE}").exists()

    three_hours_ago = time.time() - 3 * 3600
    os.utime(marker, (three_hours_ago, three_hours_ago))
    reminded = deployment.run("watchdog.sh", ALERT_REPEAT_HOURS="2")

    assert reminded.code == 0, reminded.err
    [alert] = deployment.posted()
    assert "no successful triage pass" in str(alert["text"])
    assert (deployment.state / f"stale-{TRIAGE}").exists()


def test_watchdog_fails_when_the_journal_cant_be_read(deployment):
    deployment.answer("journalctl", "", code=1)
    result = deployment.run("watchdog.sh")

    assert result.code == 1
    assert deployment.posted() == []  # the service's OnFailure= alert reports it instead


def test_watchdog_rejects_a_too_short_window(deployment):
    result = deployment.run("watchdog.sh", WATCHDOG_MAX_AGE_MINUTES="5")

    assert result.code == 2
    assert "WATCHDOG_MAX_AGE_MINUTES must be at least 15, got 5" in result.err


# --- backup.sh --------------------------------------------------------------------------------

needs_sqlite3 = pytest.mark.skipif(shutil.which("sqlite3") is None, reason="no sqlite3 CLI")


def test_backup_is_off_without_a_remote(deployment):
    result = deployment.run("backup.sh")

    assert result.code == 0
    assert "BACKUP_RCLONE_REMOTE is not set" in result.out
    assert deployment.calls("rclone") == []


@needs_sqlite3
def test_backup_uploads_a_consistent_snapshot_while_the_app_has_the_db_open(deployment):
    db = deployment.root / "data" / "inbox-jev.db"
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")  # the rows stay in the -wal file
        writer.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        writer.executemany("INSERT INTO runs VALUES (?)", [(i,) for i in range(50)])
        writer.commit()
        assert (deployment.root / "data" / "inbox-jev.db-wal").stat().st_size > 0

        result = deployment.run("backup.sh", BACKUP_RCLONE_REMOTE="gdrive:inbox-jev-backups/")
    finally:
        writer.close()

    assert result.code == 0, result.err
    copy, delete = deployment.calls("rclone")
    assert copy[0] == "copyto"
    assert re.fullmatch(r"gdrive:inbox-jev-backups/inbox-jev-\d{8}T\d{6}Z\.db\.gz", copy[2])
    # Top level only, anchored: never a subfolder's (or, for a bare remote, the Drive's) files.
    assert delete == [
        "delete",
        "--max-depth",
        "1",
        "--min-age",
        "30d",
        "--include",
        "/inbox-jev-*.db.gz",
        "gdrive:inbox-jev-backups/",
    ]
    [uploaded] = (deployment.fakes / "remote").iterdir()
    snapshot = deployment.root / "snapshot.db"
    snapshot.write_bytes(gzip.decompress(uploaded.read_bytes()))
    with sqlite3.connect(snapshot) as conn:
        assert conn.execute("SELECT count(*) FROM runs").fetchone() == (50,)
    conn.close()
    assert not list(deployment.root.glob("inbox-jev-backup.*"))  # temp dir removed


@needs_sqlite3
def test_backup_to_a_remote_root_and_custom_retention(deployment):
    sqlite3.connect(deployment.root / "data" / "inbox-jev.db").close()
    result = deployment.run("backup.sh", BACKUP_RCLONE_REMOTE="gdrive:", BACKUP_RETENTION_DAYS="7")

    assert result.code == 0, result.err
    copy, delete = deployment.calls("rclone")
    assert copy[2].startswith("gdrive:inbox-jev-")
    assert delete == [
        "delete",
        "--max-depth",
        "1",
        "--min-age",
        "7d",
        "--include",
        "/inbox-jev-*.db.gz",
        "gdrive:",
    ]


def test_backup_fails_without_the_db(deployment):
    result = deployment.run("backup.sh", BACKUP_RCLONE_REMOTE="gdrive:b")

    assert result.code == 1
    assert "data/inbox-jev.db not found" in result.err


@needs_sqlite3
def test_backup_fails_when_the_upload_fails(deployment):
    sqlite3.connect(deployment.root / "data" / "inbox-jev.db").close()
    deployment.answer("rclone", "", code=1)
    result = deployment.run("backup.sh", BACKUP_RCLONE_REMOTE="gdrive:b")

    assert result.code == 1
    assert len(deployment.calls("rclone")) == 1  # no pruning after a failed upload


def test_backup_fails_without_rclone(deployment):
    (deployment.bin / "rclone").unlink()
    result = deployment.run(
        "backup.sh", BACKUP_RCLONE_REMOTE="gdrive:b", PATH=f"{deployment.bin}:/usr/bin:/bin"
    )

    assert result.code == 1
    assert "rclone is not installed" in result.err


# --- ops_common.sh: settings and backup rotation ----------------------------------------------


def common(deployment: Deployment, command: str, **env: str) -> Result:
    script = deployment.root / "scripts" / "run_common.sh"
    script.write_text(f'. "$(dirname "$0")/ops_common.sh"\n{command}\n')
    return deployment.run("run_common.sh", **env)


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ("X=plain", "plain"),
        ("X=spaced value  # comment", "spaced value"),
        ("X='single # kept'", "single # kept"),
        ('X="double # kept"  # comment', "double # kept"),
        ("X=a#b", "a#b"),
        ("X=", "fallback"),
    ],
)
def test_env_value_reads_env_like_docker_compose(tmp_path, line, value):
    deployment = Deployment(tmp_path / "opt", f"X=earlier\n{line}\r\n")
    assert common(deployment, "env_value X fallback").out == value


def test_env_value_prefers_the_environment(deployment):
    assert common(deployment, "env_value ALERT_SLACK_CHANNEL", ALERT_SLACK_CHANNEL="C9").out == "C9"
    assert common(deployment, "env_value ALERT_SLACK_CHANNEL", ALERT_SLACK_CHANNEL="").out == ""
    assert common(deployment, "env_value MISSING dflt").out == "dflt"


def test_prune_oldest_keeps_the_newest_backups(deployment):
    data = deployment.root / "data"
    names = [f"inbox-jev.db.bak-2026101{i}" for i in range(5)] + ["inbox-jev.db.bak-pr9-x"]
    for age, name in enumerate(reversed(names)):  # the last name is the newest
        path = data / name
        path.touch()
        os.utime(path, (1_000_000 - age * 60, 1_000_000 - age * 60))
    (data / "inbox-jev.db").touch()
    os.utime(data / "inbox-jev.db", (1, 1))  # oldest, but not a backup

    result = common(deployment, f"prune_oldest 3 {data} inbox-jev.db.bak-")

    assert result.code == 0, result.err
    assert sorted(p.name for p in data.iterdir()) == sorted(["inbox-jev.db", *names[-3:]])
    assert result.out.count("removing old backup") == 3


def test_prune_oldest_rotates_the_env_backups_in_the_deployment_root(deployment):
    """deploy.sh's `prune_oldest "$keep" . .env.bak-`: dotfiles are listed too."""
    root = deployment.root
    names = [f".env.bak-20261010{i:02d}00" for i in range(5)]
    for age, name in enumerate(reversed(names)):  # the last name is the newest
        (root / name).touch()
        os.utime(root / name, (1_000_000 - age * 60, 1_000_000 - age * 60))
    os.utime(root / ".env", (1, 1))  # oldest, but not a backup

    result = common(deployment, "prune_oldest 2 . .env.bak-")  # run from the root

    assert result.code == 0, result.err
    assert sorted(p.name for p in root.glob(".env*")) == sorted([".env", *names[-2:]])
    assert result.out.count("removing old backup") == 3


def test_prune_oldest_with_fewer_backups_than_kept(deployment):
    (deployment.root / "data" / "inbox-jev.db.bak-1").touch()
    result = common(deployment, f"prune_oldest 10 {deployment.root / 'data'} inbox-jev.db.bak-")

    assert result.code == 0
    assert (deployment.root / "data" / "inbox-jev.db.bak-1").exists()


def test_deploy_rejects_a_bad_backup_count_before_touching_anything(deployment):
    shutil.copy2(SCRIPTS / "deploy.sh", deployment.root / "scripts")
    result = deployment.run("deploy.sh", DEPLOY_BACKUPS_KEEP="0")

    assert result.code == 2
    assert "DEPLOY_BACKUPS_KEEP must be at least 1, got 0" in result.err
    assert deployment.calls("systemctl") == []


# --- ops_message.py ---------------------------------------------------------------------------


def test_summarize_keeps_only_safe_fields():
    line = log("run finished", account="a@b.c", run_id=3, processed=4, errors=0, failed=False)
    assert ops_message.summarize(line) == (
        "12:10:24 INFO run finished account=a@b.c run_id=3 processed=4 errors=0 failed=False"
    )


@pytest.mark.parametrize(
    "line", ["", "not json", "[1, 2]", json.dumps({"msg": "no level"}), '{"level": 1, "msg": "x"}']
)
def test_summarize_drops_lines_that_are_not_app_logs(line):
    assert ops_message.summarize(line) is None


def test_summarize_without_a_timestamp():
    line = '{"level": "ERROR", "logger": "inbox_jev", "msg": "x"}'
    assert ops_message.summarize(line) == "--:--:-- ERROR x"


@pytest.mark.parametrize("logger", ["googleapiclient.http", "typesafe_sdk", "inbox_jevil", None, 3])
def test_summarize_drops_the_message_of_other_loggers(logger):
    """Only the app's own `msg` values are fixed strings; a library's formats its arguments."""
    line = log("GET /messages?q=to:alice@example.com", level="WARNING", logger=logger, run_id=4)
    name = logger if isinstance(logger, str) else "?"
    assert ops_message.summarize(line) == (
        f"12:10:24 WARNING [{name}] (message not forwarded) run_id=4"
    )


def test_summarize_keeps_only_safe_fields_of_the_expected_type():
    line = log("run finished", account=["x"], run_id="7; alice@x", processed=True, failed=1)
    assert ops_message.summarize(line) == "12:10:24 INFO run finished"


@pytest.mark.parametrize(
    ("exc_type", "expected"),
    [
        ("google.auth.exceptions.RefreshError", " error_type=google.auth.exceptions.RefreshError"),
        ("ValueError", " error_type=ValueError"),
        ("Subject: Your invoice", ""),
        (42, ""),
    ],
)
def test_summarize_forwards_only_a_well_formed_exception_type(exc_type, expected):
    line = log("run failed", exc="Traceback…\nValueError: secret\nSubject: x", exc_type=exc_type)
    assert ops_message.summarize(line) == "12:10:24 INFO run failed" + expected


def test_summarize_reads_the_type_the_app_formatter_writes():
    """End to end with `logs.JsonFormatter`: a multi-line message's last line isn't the type."""
    try:
        raise ValueError("bad request\nSubject: Your invoice")
    except ValueError:
        record = logging.getLogger("inbox_jev.triage").makeRecord(
            "inbox_jev.triage", logging.ERROR, __file__, 1, "account run failed", (), sys.exc_info()
        )
    summary = ops_message.summarize(JsonFormatter().format(record))
    assert summary is not None
    assert summary.endswith("ERROR account run failed error_type=ValueError")
    assert "invoice" not in summary


def test_log_lines_keeps_the_last_n():
    lines = [log(f"m{i}") for i in range(30)]
    assert [s.split()[-1] for s in ops_message.log_lines(lines, 3)] == ["m27", "m28", "m29"]
    assert ops_message.log_lines(lines, 0) == []


@pytest.mark.parametrize(
    ("argv", "code"),
    [([], 2), (["log-lines", "x"], 2), (["payload"], 2), (["payload", ""], 2), (["what"], 2)],
)
def test_ops_message_usage(argv, code, capsys):
    assert ops_message.main(argv) == code
    assert "usage" in capsys.readouterr().err


def test_ops_message_main():
    out = io.StringIO()
    assert ops_message.main(["log-lines"], io.StringIO(log("x") + "\nnoise\n"), out) == 0
    assert out.getvalue() == "12:10:24 INFO x\n"
    out = io.StringIO()
    assert ops_message.main(["payload", "C1"], io.StringIO("a <b>"), out) == 0
    assert json.loads(out.getvalue()) == {
        "channel": "C1",
        "text": "a &lt;b&gt;",
        "unfurl_links": False,
        "unfurl_media": False,
    }


# --- the scripts themselves -------------------------------------------------------------------


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_script_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck():
    subprocess.run(
        ["shellcheck", "-x", "-P", str(SCRIPTS), *map(str, SHELL_SCRIPTS)],
        check=True,
        cwd=SCRIPTS,
    )


def test_every_ops_setting_is_in_env_example():
    """Every setting the ops scripts read (`env_value NAME`) is documented in .env.example."""
    used = {
        name
        for script in SHELL_SCRIPTS
        for name in re.findall(r"\$\(env_value ([A-Z][A-Z0-9_]*)", script.read_text())
    }
    listed = set(re.findall(r"^#?([A-Z][A-Z0-9_]*)=", (ROOT / ".env.example").read_text(), re.M))
    assert used
    assert used - listed == set()


def test_failing_units_alert():
    """Every service but the alert itself names the alert unit in OnFailure=."""
    units = ROOT / "deploy" / "systemd"
    services = {p.name for p in units.glob("*.service")} - {"inbox-jev-alert@.service"}
    assert services == {
        "inbox-jev-triage.service",
        "inbox-jev-digest.service",
        "inbox-jev-backup.service",
        "inbox-jev-watchdog.service",
    }
    for name in services:
        assert "OnFailure=inbox-jev-alert@%n.service" in (units / name).read_text(), name
    assert "OnFailure" not in (units / "inbox-jev-alert@.service").read_text().replace(
        "# No OnFailure=", ""
    )


@pytest.mark.parametrize(
    "unit", ["inbox-jev-alert@.service", "inbox-jev-watchdog.service", "inbox-jev-backup.service"]
)
def test_units_that_post_or_upload_wait_for_the_network(unit):
    """A failure right after boot (Persistent= timers) must not alert before the network is up."""
    text = (ROOT / "deploy" / "systemd" / unit).read_text()
    assert "Wants=network-online.target" in text
    assert re.search(r"^After=.*network-online\.target", text, re.MULTILINE)
