"""scripts/guard_main.py (ADR 0057): the four refusals, the commands the workflow runs that must
pass, and the cases where it refuses when in doubt. The rulesets enforce the rest on GitHub."""

import io
import json
import subprocess
from pathlib import Path

import pytest
from guard_main import commands, main, reason_to_block


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A git repository with `main` checked out and a `feat/x` branch, in a worktree of its own."""
    root = tmp_path_factory.mktemp("repo")

    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    run("init", "-q", "-b", "main")
    run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    run("branch", "feat/x")
    run("worktree", "add", "-q", str(root.parent / "feat"), "feat/x")
    return root


def blocked(command: str, cwd: Path | None = None) -> str | None:
    return reason_to_block(command, cwd)


# ── 1. Rulesets ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "gh api -X PUT repos/o/r/rulesets/1 --input body.json",
        "gh api --method=DELETE repos/o/r/rulesets/1",
        "gh api -XPATCH repos/o/r/rulesets/1 -f enforcement=disabled",
        "gh api repos/o/r/rulesets -f name=x",  # a body without a method is a POST
        "gh api repos/o/r/rulesets --input=r.json",
        "cd /tmp && gh api --method post orgs/o/rulesets -F x=1",
    ],
)
def test_a_write_to_the_rulesets_is_refused(command):
    assert "rulesets is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/rulesets/25086576 --jq .rules",
        "gh api -X GET repos/o/r/rulesets -f per_page=100",  # an explicit GET with a query
        "gh api repos/o/r/pulls -f title=x",  # a write, but not to the rulesets
        "gh ruleset view 1",
    ],
)
def test_reading_the_rulesets_and_other_writes_pass(command):
    assert blocked(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "gh api graphql -f query='mutation { deleteRepositoryRuleset(input: {}) { id } }'",
        'gh api graphql -f query="mutation{updateRepositoryRuleset(input:{}){ruleset{id}}}"',
        "gh api graphql -F query=@mutation.graphql",  # a file's query can't be seen
        "gh api graphql --input body.json",
        "gh api /graphql -f query='mutation { deleteRepositoryRuleset(input: {}) { id } }'",
        "gh api https://api.github.com/graphql -f query='mutation{createRepositoryRuleset}'",
        'gh api graphql -f query="$(cat mutation.graphql)"',  # a query from a substitution
        'gh api graphql -f query="$Q"',  # or a variable: unseen
        "gh api graphql -f 'query'=\"$Q\"",
        'gh api graphql -f query=""$Q',
        "gh api graphql -f query='mutation { delete'${R}'Ruleset(input:{}) { id } }'",
        'gh api graphql -f query=\'{ repository(owner:"o", name:"r") { rulesets { id } } }\'',
    ],
)
def test_a_ruleset_mutation_over_graphql_is_refused(command):
    assert "rulesets is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "gh api graphql -f query='{ viewer { login } }'",
        # GraphQL's own variables, in single quotes (the review-thread mutation /ship runs)
        "gh api graphql -f query='mutation($id: ID!) { resolveReviewThread(input: {threadId: $id})"
        " { thread { id } } }' -F id=PRRT_x",
    ],
)
def test_a_graphql_read_or_other_mutation_passes(command):
    assert blocked(command) is None


# ── 2. Force pushes ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push --force origin feat/x",
        "git push --force-with-lease",
        "git push --force-with-lease=feat/x:abc origin feat/x",
        "git push --force-if-includes origin feat/x",
        "git push -f",
        "git push -uf origin feat/x",
        "git push origin +feat/x",
        "git push origin +HEAD:feat/x",
        "git fetch && git rebase origin/main && git push -q --force-with-lease 2>&1 | head",
        "git -C ../other push --force origin x",
        "sudo -u root git push -f origin x",
        "FOO=1 timeout 30 git push --force origin x",
        "echo ok; git push --force origin x",
        "git status\ngit push --force origin x",  # a second line
        "x=$(git push --force origin x)",  # a command substitution
        "git push \\\n  --force origin x",  # a line continuation
        "git push --force-w origin x",  # git reads an unambiguous prefix as the long option
        "git push --force-with=x origin x",
        "git push --forc origin x",
    ],
)
def test_a_force_push_is_refused_to_any_branch(command):
    assert "force push is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin feat/x",
        "git push -q origin HEAD",
        "git push origin --delete feat/old",
        "git push -q origin --delete fix/x 2>&1 | grep -v '^remote:'",
        "git push --tags",
        "git push -o ci.skip origin feat/x",  # -o takes a value: not a cluster with f
        "git commit -m 'never git push --force origin x'",  # quoted: one word of a commit
        "echo git push --force",  # not in command position
        "command -v git push --force",  # only looks git up
    ],
)
def test_a_normal_push_and_mentions_pass(command, repo):
    assert blocked(command, repo.parent / "feat") is None


@pytest.mark.parametrize(
    "command",
    [
        "git -c remote.origin.push=+HEAD:refs/heads/x push",
        "git -c remote.origin.mirror=true push origin",
        "git -c alias.p='push -f' p origin x",
        "git --config-env=alias.p=VAR p origin x",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p GIT_CONFIG_VALUE_0='push -f' git p",
        "GIT_CONFIG_PARAMETERS=\"'remote.origin.mirror'='true'\" git push",
        "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.p; git p",
        "set -a; GIT_CONFIG_COUNT=1; GIT_CONFIG_KEY_0=alias.p; git p",  # set -a exports them
        "git --exec-path=/tmp/evil push origin x",
        "GIT_EXEC_PATH=/tmp/evil git push origin x",
        "git -c include.path=/tmp/evil push origin x",
        "git config remote.origin.push +HEAD:refs/heads/x && git push",
        "git config --global alias.p 'push -f'",
    ],
)
def test_a_config_that_can_force_or_mirror_a_push_is_refused(command):
    assert blocked(command) is not None


# ── 3. gh pr merge --admin ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 6 --squash --admin",
        "gh pr merge --admin=true 6",
        "gh pr merge 6 --admin=1",
        "gh pr -R o/r merge 6 --admin",  # the repo flag before the subcommand
        "gh -R o/r pr merge 6 --admin",
    ],
)
def test_an_admin_merge_is_refused(command):
    assert "--admin` is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "gh pr merge 6 --squash",
        "gh pr merge 6 --admin=false",
        "gh pr merge 6 --admin=f",  # false, as gh reads it
        "gh pr view 6 --json mergeable",
    ],
)
def test_a_normal_merge_passes(command):
    assert blocked(command) is None


# ── 4. Pushes to main ───────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin feat/x:refs/heads/main",
        "git push origin x:heads/main",
        "git push --all origin",
        "git push --mirror",
        "git push --mirr origin",  # an abbreviation of --mirror
        "git push origin 'refs/heads/*:refs/heads/*'",  # a glob pushes main too
    ],
)
def test_a_push_to_main_is_refused_from_any_branch(command, repo):
    assert "pushing to `main` is forbidden" in str(blocked(command, repo.parent / "feat"))


@pytest.mark.parametrize("command", ["git push", "git push -u origin HEAD", "git push origin @"])
def test_pushing_the_current_branch_from_main_is_refused(command, repo):
    assert "pushing to `main`" in str(blocked(command, repo))
    assert blocked(command, repo.parent / "feat") is None


def test_cd_and_git_c_move_the_checkout_the_push_is_judged_from(repo):
    feat = repo.parent / "feat"
    assert "pushing to `main`" in str(blocked(f"cd {repo} && git push", feat))
    assert "pushing to `main`" in str(blocked(f"git -C {repo} push", feat))
    assert blocked(f"cd {feat} && git push", repo) is None


# ── When in doubt ───────────────────────────────────────────────────────────────────────────────


def test_a_heredoc_line_that_reads_as_a_forbidden_command_is_refused_with_a_hint():
    command = "gh pr create --body-file - <<'EOF'\nSteps:\ngit push --force origin x\nEOF"
    reason = str(blocked(command))
    assert "force push is forbidden" in reason and "--body-file" in reason


def test_an_unclosed_quote_still_finds_the_command():
    assert "force push" in str(blocked("git push --force origin x 'unterminated"))


def test_operators_split_commands_and_quotes_keep_words_whole():
    assert commands("a && b | c; d & e || (f)") == [["a"], ["b"], ["c"], ["d"], ["e"], ["f"]]
    assert commands("git commit -m 'A & B; C'") == [["git", "commit", "-m", "A & B; C"]]


@pytest.mark.parametrize(
    "command",
    [
        # Forms the bash-reading guard (before ADR 0057) refused, kept refused by reading words
        # several ways: wrappers' options, substitutions in quotes and backquotes, arithmetic,
        # comments, escapes, line continuations, a coproc, a leading redirection, a quoted `>`.
        "sudo -n /usr/bin/git push origin main",
        "sudo -n -u root -g staff env -u HOME git push origin main",
        "time -f %e git push origin main",
        "exec -a name git push origin main",
        "nice --adjustment 5 git push origin main",
        'echo "$(git push origin main)"',
        "echo `git push origin main`",
        "echo $(( `git push origin main` + 1 ))",
        'echo "$(( $(git push origin main) ))"',
        "true # it's fine\ngit push origin main",
        "echo a # b \\\ngit push origin main",
        "echo C:\\\\\ngit push origin main",
        "gh pr merge 6 --subject Fix\\ #12 --admin",
        "echo a\\;# ; gh pr merge 1 --admin",
        "coproc pusher { git push origin main; }",
        "2>/dev/null git push origin main",
        "gh pr merge 1 --body '>' --admin",
    ],
)
def test_the_forms_the_bash_reading_guard_refused_are_still_refused(command):
    assert blocked(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh api -X POST repos/o/r/issues/1/comments -f body='the rulesets stay as they are'",
        "git commit -m 'A & git push origin main later'",
        "gh pr merge 6 --squash --subject 'Sync Q&A'",
        "git -c push.default=current push -u origin feat/x",  # harmless push config
        "git -c user.name=t -c user.email=t@t commit -m x",
        "git log --grep GIT_CONFIG",  # a word, not an assignment
        "git commit -F - <<'EOF'\nfix: refuse `git push --force` (#58, a session's push)\nEOF",
        "gh pr create --title \"fix: x (#61)\" --body-file - <<'EOF'\n"
        "On #58 `git push -f` ran.\nEOF",
        "git commit -m 'docs: link #58'",
    ],
)
def test_a_forbidden_command_quoted_as_text_passes(command):
    assert blocked(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --body-file - <<'EOF'\nIt now refuses `git push --force origin x`.\nEOF",
        "cat > /tmp/review.md <<'EOF'\n1. `gh api -X PUT repos/o/r/rulesets/1` passed.\nEOF",
        # a plain rest of the header line: the workflow's `>/dev/null && echo ok`
        "gh pr comment 61 --body-file - <<'EOF' >/dev/null && echo ok\n"
        "`gh alias set rs 'api -X DELETE …/rulesets/1'` is refused.\nEOF",
    ],
)
def test_a_markdown_code_span_where_bash_expands_nothing_passes(command):
    """Single quotes and a quoted heredoc's body are text to bash: a backquoted command there is
    a Markdown code span, not a substitution."""
    assert blocked(command) is None


@pytest.mark.parametrize(
    "command",
    [
        # Each runs the push in bash (checked with an `echo` in its place): a quoting form the
        # scan doesn't model, so nothing is exempted.
        "echo $'\\'' \"$(git push -f origin x)\" \\'",
        "true # it's\n\"$(git push -f origin x)\" '",
        "cat <<'EOF' >/dev/null; echo \"$(git push -f origin x)\"\nbody\nEOF",
        'cat <<"E"OF\nbody\nEOF\necho "$(git push -f origin x)"',
        # Round 3: a `<<'EOF'` in a comment or a string, a comment after `;`, nested quotes.
        "true # <<'EOF'\necho \"$(git push -f origin x)\"\nEOF",
        "echo \"<<'EOF'\n$(git push -f origin x)\nEOF\"",
        "true;# it's\n\"$(git push -f origin x)\" '",
        'echo "$(echo "\'")" "$(git push -f origin x)" "$(echo "\'")"',
    ],
)
def test_a_quoting_form_the_scan_doesnt_model_exempts_nothing(command):
    assert "force push" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        # Round 4: an earlier unquoted heredoc, two on a line, a comment right after `)`.
        "cat <<EOF\n<<'X'\n$(git push -f origin x)\nX\nEOF",
        "cat <<A <<'B'\n$(git push -f origin x)\nA\nb\nB",
        "(true)# <<'X'\necho \"$(git push -f origin x)\"\nX",
    ],
)
def test_an_earlier_heredoc_or_a_comment_after_a_paren_exempts_nothing(command):
    assert "force push" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        # Round 5: in `((…))` a `<<` is a shift, and bash runs the backquote before its error.
        "(( 1 <<'X'\n`git push -f origin x`\nX",
        "(( 1 <<'X'\n`gh api -X DELETE repos/o/r/rulesets/1`\nX",
    ],
)
def test_a_shift_in_arithmetic_isnt_taken_for_a_heredoc(command):
    assert blocked(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh alias set rs 'api -X DELETE repos/o/r/rulesets/1' && gh rs",
        "gh alias set --shell rs 'gh api -X DELETE repos/o/r/rulesets/1'",
        "gh alias import aliases.yml",
    ],
)
def test_setting_a_gh_alias_is_refused(command):
    assert "alias" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        'gh api graphql -fquery="$Q"',  # gh accepts the attached form
        "gh api graphql -f qu''ery=\"$Q\"",
        'gh api graphql -f qu"ery"="$Q"',
        'gh api graphql -f quer\\y="$Q"',
    ],
)
def test_a_graphql_query_not_written_as_one_literal_is_refused(command):
    assert "rulesets is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "set -a; read GIT_CONFIG_COUNT <<< 1; git p",
        "set -ae",
        "set -o allexport",
    ],
)
def test_set_a_is_refused(command):
    assert blocked(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        # Round 6: a decoy literal can't stand in for an unseen query, nor a `#` in the field.
        "echo -f query='{}' ; gh api graphql -f query=\"$Q\"",
        "gh api graphql -f query=\"$Q # -f query='x' \"",
        "cat <<'EOF'\n-f query='{}'\nEOF\ngh api graphql -f query=\"$Q\"",
        # An endpoint built from a variable, for a write: the hook can't see it's a ruleset.
        'gh api -X DELETE "$E"',
        "E=graphql; gh api \"$E\" -f query='{ viewer { login } }'",
        "builtin set -a",
        "command set -a",
    ],
)
def test_an_unseen_query_or_endpoint_is_refused_whatever_stands_beside_it(command):
    assert blocked(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        # Round 7: an unquoted substitution as the endpoint, a query string on the endpoint,
        # nested quotes merging words. One rule covers them: in a write or a GraphQL call, an
        # expansion may stand only in a plain field (`-F owner="$OWNER"`).
        "gh api $(echo repos/o/r/rulesets) -f name=x -f enforcement=active",
        "gh api $(echo repos/o/r/rulesets/1) --input b.json",
        "gh api $(echo graphql) -f query='mutation { deleteRepositoryRuleset(input:{}) { id } }'",
        "gh api 'graphql?a=1' -f query=\"$Q\"",
        # a nested substitution keeps the call whole
        "gh api $(echo $(echo repos/o/r)/rulesets) -f name=x",
        'gh api graphql -F owner="$(echo "\'")" -f query="$Q" -F x="$(echo "\'")"',
        'gh api -X PATCH "repos/o/r/issues/comments/$ID" -f body="$BODY"',  # a write: literal
    ],
)
def test_an_expansion_outside_a_plain_field_in_a_gh_api_write_is_refused(command):
    assert "rulesets is forbidden" in str(blocked(command))


@pytest.mark.parametrize(
    "command",
    [
        "cd \"$WT\" && gh api graphql -f query='{ viewer { login } }'",  # $WT is another command's
        'gh api "repos/$R/pulls/$N" --jq .head.sha',  # a read from a variable endpoint
        'gh api "repos/$(gh repo view --json nameWithOwner -q .nameWithOwner)/pulls" --jq length',
    ],
)
def test_variables_elsewhere_or_in_a_read_pass(command):
    assert blocked(command) is None


@pytest.mark.parametrize(
    "command",
    [
        # Round 8: quoting the readings misread makes a whole command disappear from them; the
        # quote-blind reading still sees it (bash runs each of these).
        "# it's\ngh api -X DELETE repos/o/r/rulesets/1\n# '",
        "echo $'\\'' ; gh api -X DELETE repos/o/r/rulesets/1 ; echo '",
        'echo "$(echo "\'")"; gh api -X DELETE repos/o/r/rulesets/1; echo "$(echo "\'")"',
        "# it's\ngit push --force origin x\n# '",
        # and `gh api` written with quotes or a backslash is still `gh api`
        "gh 'api' $(echo repos/o/r/rulesets) -f name=x",
        'gh "api" $(echo repos/o/r/rulesets) -f name=x',
        "g\\h a\\pi $(echo repos/o/r/rulesets) -f name=x",
        # Round 9: `$'…'` and `$"…"` are quoting too, `$'\\x67\\x68'` spelling `gh`
        "$'gh' api -X DELETE repos/o/r/rulesets/1",
        '$"gh" api -X DELETE repos/o/r/rulesets/1',
        "$'\\x67\\x68' api -X DELETE repos/o/r/rulesets/1",
        "$'git' push --force origin x",
        "git push $'--force' origin x",
        # Round 10: a NUL ends the word (`$'gh\\0'` is `gh`), however it is written
        "$'gh\\0' api -X DELETE repos/o/r/rulesets/1",
        "$'gh\\x0' api -X DELETE repos/o/r/rulesets/1",
        "$'gh\\x00' api -X DELETE repos/o/r/rulesets/1",
        "gh $'api\\0' -X DELETE repos/o/r/rulesets/1",
        "$'git\\0' push --force origin x",
        "git push $'--force\\0' origin x",
        "$'gh\\c@' api -X DELETE repos/o/r/rulesets/1",
        "$'\\147\\150' api -X DELETE repos/o/r/rulesets/1",  # octal
        "$'\\u0067h' api -X DELETE repos/o/r/rulesets/1",
        # Round 11: zsh ends the word at `\\x` with no digit and at `\\C-@`
        "$'gh\\x' api -X DELETE repos/o/r/rulesets/1",
        "$'gh\\C-@' api -X DELETE repos/o/r/rulesets/1",
        "gh $'api\\x' -X DELETE repos/o/r/rulesets/1",
        "$'git\\x' push --force origin x",
        "git push $'--force\\x' origin x",
        "git push $'--force\\C-@' origin x",
        "$'g\\h' api -X DELETE repos/o/r/rulesets/1",  # zsh drops an unknown escape's backslash
    ],
)
def test_quoting_misread_cant_hide_a_ruleset_write_or_a_force_push(command):
    assert blocked(command) is not None


def test_a_review_reply_that_mentions_a_ruleset_passes():
    """dev-fixer's reply mutation: the word is in a field's text, not in the query."""
    command = (
        "gh api graphql -f query='mutation($id: ID!, $body: String!) "
        "{ addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $id, body: $body})"
        " { comment { id } } }' -F id=PRRT_x -f body='The second ruleset blocks it.'"
    )
    assert blocked(command) is None


def test_nested_quotes_cant_hide_a_ruleset_write_either():
    command = 'echo "$(echo "\'")" "$(gh api -X DELETE repos/o/r/rulesets/1)" "$(echo "\'")"'
    assert "rulesets is forbidden" in str(blocked(command))


def test_a_code_span_in_single_quotes_is_refused_rather_than_guessed():
    """Single quotes aren't exempted (ADR 0057): their reading was the bypass. A text naming a
    forbidden command in a code span goes in a file or a quoted heredoc, as the message says."""
    reason = blocked("git commit -m 'Refuse `git push --force-with-lease` (ADR 0057)'")
    assert reason is not None and "--body-file" in reason


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "see `git push --force origin x`"',  # double quotes expand it
        "cat <<EOF\n$(git push --force origin x)\nEOF",  # an unquoted heredoc expands it
    ],
)
def test_a_substitution_where_bash_expands_it_is_refused(command):
    assert "force push" in str(blocked(command))


# ── The hook's contract ─────────────────────────────────────────────────────────────────────────


def call(command: str, cwd: str | None = None, tool: str = "Bash") -> str:
    return json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": cwd})


def test_a_refusal_exits_2_with_the_reason_on_stderr(capsys):
    assert main(io.StringIO(call("git push --force origin x"))) == 2
    assert capsys.readouterr().err.startswith("scripts/guard_main.py blocked the command")


@pytest.mark.parametrize(
    "stdin", ["not json", "[]", call("git push -f", tool="Edit"), '{"tool_name": "Bash"}']
)
def test_anything_else_exits_0(stdin):
    assert main(io.StringIO(stdin)) == 0


@pytest.mark.parametrize(
    ("body", "word"),
    [
        (r"\x67\x68", "gh"),
        (r"gh\0 api", "gh"),
        (r"\147\150", "gh"),
        (r"\u0067h", "gh"),
        (r"a\tb", "a\tb"),
        (r"\q", r"\q"),  # an unknown escape keeps its backslash
        (r"gh\x api", "gh"),  # cut where bash and zsh may differ
        (r"gh\C-@ api", "gh"),
        (r"gh\c@", "gh"),
    ],
)
def test_ansi_c_strings_decode_as_bash_does(body, word):
    from guard_main import _bash_escapes

    assert _bash_escapes(body) == word


def test_the_hook_runs_with_the_hosts_python_and_isolated(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts" / "guard_main.py"
    result = subprocess.run(
        ["python3", "-I", str(script)],
        input=call("git push --force origin x", str(tmp_path)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2 and "force push" in result.stderr
