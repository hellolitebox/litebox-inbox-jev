"""scripts/update_psl.py: refreshes the vendored Public Suffix List, never with a broken one."""

import io

import pytest
import update_psl

LIST = update_psl.DEST.read_text(encoding="utf-8")  # the vendored list: a valid one
VERSION, COMMIT = update_psl.validate(LIST)


def with_year(year, text=LIST):
    """The list with its VERSION moved to `year` (still a well-formed VERSION)."""
    return text.replace(f"// VERSION: {VERSION}", f"// VERSION: {year}{VERSION[4:]}", 1)


def bumped(text=LIST):
    return with_year("2099", text)


def run(tmp_path, text, capsys, existing=LIST, args=()):
    dest = tmp_path / "public_suffix_list.dat"
    if existing is not None:
        dest.write_text(existing, encoding="utf-8")
    code = update_psl.main(["--dest", str(dest), *args], fetch=lambda: text)
    out = capsys.readouterr()
    return code, dest, out.out, out.err


def test_validate_reads_version_and_commit():
    assert f"// VERSION: {VERSION}\n" in LIST and f"// COMMIT: {COMMIT}\n" in LIST


def test_a_newer_list_is_written(tmp_path, capsys):
    code, dest, out, _ = run(tmp_path, bumped(), capsys)
    assert code == 0
    assert dest.read_text(encoding="utf-8") == bumped()
    assert out == f"updated: VERSION 2099{VERSION[4:]} COMMIT {COMMIT}\n"
    assert not (tmp_path / "public_suffix_list.dat.tmp").exists()


def test_the_same_list_is_left_alone(tmp_path, capsys):
    code, dest, out, _ = run(tmp_path, LIST, capsys)
    assert code == 0 and out == f"unchanged: VERSION {VERSION} COMMIT {COMMIT}\n"


def test_an_older_list_is_never_written(tmp_path, capsys):
    """A stale mirror or cache serving an earlier list doesn't roll the vendored one back."""
    code, dest, out, _ = run(tmp_path, with_year("2001"), capsys)
    assert code == 0 and out.startswith("unchanged: VERSION 2001-")
    assert dest.read_text(encoding="utf-8") == LIST


@pytest.mark.parametrize(("text", "newer"), [(bumped(), "true"), (LIST, "false")])
def test_check_never_writes_and_reports_the_validated_values(tmp_path, capsys, text, newer):
    """--check is what the weekly workflow runs: it writes only $GITHUB_OUTPUT, with the
    values validate() accepted, never the list."""
    output = tmp_path / "github_output"
    output.write_text("earlier=line\n", encoding="utf-8")
    code, dest, out, _ = run(
        tmp_path, text, capsys, args=["--check", "--github-output", str(output)]
    )
    version = update_psl.validate(text)[0]
    assert code == 0
    assert dest.read_text(encoding="utf-8") == LIST
    status = "newer" if newer == "true" else "current"
    assert out == f"{status}: VERSION {version} COMMIT {COMMIT}\n"
    assert output.read_text(encoding="utf-8") == (
        f"earlier=line\nnewer={newer}\nversion={version}\ncommit={COMMIT}\n"
    )


def test_a_broken_list_writes_no_github_output(tmp_path, capsys):
    output = tmp_path / "github_output"
    code, _, _, _ = run(tmp_path, "", capsys, args=["--check", "--github-output", str(output)])
    assert code == 1 and not output.exists()


def test_a_missing_file_is_created(tmp_path, capsys):
    code, dest, out, _ = run(tmp_path, LIST, capsys, existing=None)
    assert code == 0 and out.startswith("updated") and dest.read_text(encoding="utf-8") == LIST


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "expected one VERSION header line in the list, found 0"),
        (LIST.replace("// COMMIT: ", "// commit: ", 1), "expected one COMMIT header line"),
        (
            LIST + f"// VERSION: {VERSION}\n",
            "expected one VERSION header line in the list, found 2",
        ),
        (LIST + f"// COMMIT: {COMMIT}\n", "expected one COMMIT header line in the list, found 2"),
        # Values that would carry text or markdown into the workflow's issue.
        (LIST.replace(VERSION, VERSION + " [x](https://evil)", 1), "malformed VERSION header"),
        (LIST.replace(VERSION, "latest", 1), "malformed VERSION header"),
        (LIST.replace(COMMIT, COMMIT[:7], 1), "malformed COMMIT header"),
        (LIST.replace(COMMIT, COMMIT.upper(), 1), "malformed COMMIT header"),
        (LIST.replace(f"{COMMIT}\n", f"{COMMIT}\r\n", 1), "malformed COMMIT header"),
        (LIST.replace("// ===BEGIN PRIVATE DOMAINS===", ""), "section markers missing"),
        (
            LIST[: LIST.index("// ===BEGIN ICANN DOMAINS===") + 2000]
            + "\n// ===BEGIN PRIVATE DOMAINS===",
            "rules, expected",
        ),
        (LIST.replace("\nwa.gov.au\n", "\n"), "unexpected answers for: wa.gov.au"),
        (LIST.replace("\n!www.ck\n", "\n"), "unexpected answers for: www.ck"),
    ],
)
def test_a_broken_list_is_never_written(tmp_path, capsys, text, reason):
    code, dest, out, err = run(tmp_path, text, capsys)
    assert code == 1 and out == ""
    assert reason in err
    assert dest.read_text(encoding="utf-8") == LIST


def test_a_download_failure_writes_nothing(tmp_path, capsys):
    def fail():
        raise OSError("network is unreachable")

    dest = tmp_path / "public_suffix_list.dat"
    assert update_psl.main(["--dest", str(dest)], fetch=fail) == 1
    assert "network is unreachable" in capsys.readouterr().err
    assert not dest.exists()


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def test_fetch_reads_the_url_with_a_timeout(monkeypatch):
    calls = []

    def urlopen(url, timeout):
        calls.append((url, timeout))
        return FakeResponse(b"// VERSION: x\ncom\n")

    monkeypatch.setattr(update_psl.urllib.request, "urlopen", urlopen)
    assert update_psl.fetch() == "// VERSION: x\ncom\n"
    assert calls == [(update_psl.URL, update_psl.TIMEOUT_SECONDS)]


def test_fetch_refuses_an_oversized_body(monkeypatch):
    monkeypatch.setattr(update_psl, "MAX_BYTES", 10)
    monkeypatch.setattr(
        update_psl.urllib.request, "urlopen", lambda url, timeout: FakeResponse(b"x" * 11)
    )
    with pytest.raises(ValueError, match="over 10 bytes"):
        update_psl.fetch()
