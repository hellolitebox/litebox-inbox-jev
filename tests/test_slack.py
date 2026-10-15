"""The digest's Slack message: Block Kit layout, Block Kit limits, text version and posting."""

import json
import logging

import httpx
import pytest
from conftest import assert_valid_block_kit, assert_valid_cell, cell_style, cell_text

from inbox_jev import slack
from inbox_jev.slack import Digest, Group, Totals, Wording

WORDING = Wording(more="+{n} more", see_all="see all in Gmail", nothing="_Nothing processed._")


def sample(**fields) -> Digest:
    base = {
        "title": "📬 Inbox Jev · Thursday, Oct 15",
        "context": "me@acme.com · last 24h · mode: act",
        "hero": "*2 emails need you*",
        "notification": "2 emails need you · 9 processed",
        "wording": WORDING,
        "needs_you": [Group("*⚡ Needs reply (2)*", ["• a", "• b"])],
        "review": ["*🤔 To review: 1*"],
        "totals_title": "*📊 What Jev did · last 24h*",
        "totals": Totals(
            ("Category", "Emails"),
            [("Jev/Notifications", 6)],
            ("Processed", 9),
            review=("To review", 3),
        ),
        "unsubscribes": [Group("*🚫 Unsubscribed (1)*", ["• news.example _(one-click)_"])],
        "corrections": Group("*🧠 Corrections learned (1)*", ["• a@b.example: X → Y"]),
        "errors": Group("*⚠️ Errors*: 1 email", []),
        "footer": "💡 Fix a label in Gmail.",
    }
    return Digest(**{**base, **fields})


def test_layout_puts_what_needs_you_first_and_the_rest_under_dividers():
    blocks = sample().blocks()
    assert_valid_block_kit(blocks)
    assert [b["type"] for b in blocks] == [
        "header",
        "context",
        "section",  # hero
        "section",  # needs reply
        "section",  # to review
        "divider",
        "section",  # what Jev did
        "table",
        "divider",
        "section",  # unsubscribed
        "section",  # corrections
        "section",  # errors
        "divider",
        "context",  # footer
    ]
    assert blocks[0]["text"]["text"] == "📬 Inbox Jev · Thursday, Oct 15"


def test_table_block_shows_labels_as_code_counts_right_and_bolds_header_and_total():
    totals = Totals(
        ("Category", "Emails"),
        [("Jev/Notifications", 6), ("  a   very long label that goes past the width", 1)],
        ("Processed", 9),
        after=[("Already handled", 2)],
        review=("To review", 2),
    )
    blocks = sample(totals=totals).blocks()
    assert_valid_block_kit(blocks)
    (table,) = [b for b in blocks if b["type"] == "table"]
    assert table["column_settings"] == [{"is_wrapped": True}, {"align": "right"}]
    assert [[(cell_text(c), cell_style(c)) for c in row] for row in table["rows"]] == [
        [("Category", "bold"), ("Emails", "bold")],
        [("Jev/Notifications", "code"), ("6", None)],
        [("a very long label that goes…", "code"), ("1", None)],
        [("To review", None), ("2", None)],  # not a label
        [("Processed", "bold"), ("9", "bold")],
        [("Already handled", None), ("2", None)],  # under the total: not part of it
    ]


def test_a_backtick_in_a_label_is_shown_as_a_quote_in_both_tables():
    """A label is shown as code: a backtick in it would close the text version's code block."""
    totals = Totals(("Category", "Emails"), [("Jev/`Odd`", 1)], ("Processed", 1))
    digest = sample(totals=totals)
    (table,) = [b for b in digest.blocks() if b["type"] == "table"]
    odd = table["rows"][1][0]
    assert (cell_text(odd), cell_style(odd)) == ("Jev/'Odd'", "code")
    assert "```\nJev/'Odd'  1\n" in digest.text


@pytest.mark.parametrize(
    "style", [{"code": True}, {"bold": True}, {"monospace": True}, {"code": "yes"}, None]
)
def test_the_validator_takes_only_block_kit_s_text_styles(style):
    words = {"type": "text", "text": "Jev/Action", **({"style": style} if style else {})}
    cell = {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [words]}]}
    if style in ({"monospace": True}, {"code": "yes"}):
        with pytest.raises(AssertionError):
            assert_valid_cell(cell)
    else:
        assert_valid_cell(cell)


def test_text_version_has_every_part_and_the_table_in_a_code_block():
    text = sample().text
    assert text.startswith("*📬 Inbox Jev · Thursday, Oct 15*\nme@acme.com · last 24h")
    for part in ("*2 emails need you*", "*⚡ Needs reply (2)*\n• a\n• b", "*🤔 To review: 1*"):
        assert part in text
    # Plain in the code block: no backticks around the labels there.
    assert "```\nJev/Notifications  6\nTo review          3\n─" in text
    assert text.endswith("*⚠️ Errors*: 1 email\n\n_💡 Fix a label in Gmail._")


def test_empty_parts_add_no_blocks():
    blocks = sample(
        needs_you=[], review=[], totals=None, unsubscribes=[], corrections=None, errors=None
    ).blocks()
    assert_valid_block_kit(blocks)
    assert [b["type"] for b in blocks] == [
        "header",
        "context",
        "section",
        "divider",
        "section",
        "section",  # "Nothing processed", no table
        "divider",
        "context",
    ]
    assert blocks[5]["text"]["text"] == "_Nothing processed._"


def test_a_group_past_a_section_s_limit_counts_what_it_leaves_out():
    long_lines = [f"• {i} " + "x" * 400 for i in range(10)]
    group = Group("*⚡ Needs reply (12)*", long_lines, hidden=2, link="https://mail.example/s")
    blocks = sample(needs_you=[group]).blocks()
    assert_valid_block_kit(blocks)
    text = blocks[3]["text"]["text"]
    shown = text.count("\n• ")
    assert 0 < shown < 10
    assert text.endswith(f"+{12 - shown} more · <https://mail.example/s|see all in Gmail>")


def test_more_than_fifty_blocks_keep_the_top_and_say_so():
    groups = [Group(f"*🔔 Jev/Cat{i} (1)*", ["• x"]) for i in range(60)]
    blocks = sample(needs_you=groups).blocks()
    assert_valid_block_kit(blocks)
    assert len(blocks) == slack.MAX_BLOCKS
    assert blocks[-1] == {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "…", "verbatim": True}],
    }


def test_a_table_past_slack_s_row_limit_sums_the_rest_in_one_more_row():
    rows = [(f"Jev/Cat{i:03}", 1) for i in range(150)]
    totals = Totals(
        ("Category", "Emails"),
        rows,
        ("Processed", 153),
        [("Already handled", 2)],
        review=("To review", 3),
    )
    blocks = sample(totals=totals).blocks()
    assert_valid_block_kit(blocks)
    (table,) = [b for b in blocks if b["type"] == "table"]
    assert len(table["rows"]) == slack.MAX_TABLE_ROWS
    texts = [[(cell_text(c), cell_style(c)) for c in row] for row in table["rows"][-5:-2]]
    # 1 header + 95 categories + "+N more" + to review + total + already handled = 100
    assert texts == [
        [("Jev/Cat094", "code"), ("1", None)],
        [("+55 more", None), ("55", None)],  # not a label: no code
        [("To review", None), ("3", None)],
    ]
    assert "Jev/Cat149" in sample(totals=totals).text  # the text version has no row limit


def test_every_mrkdwn_text_is_verbatim_so_plain_mentions_stay_text():
    """Slack parses a plain `@here`, `@channel` or `#general` in non-verbatim mrkdwn into a real
    mention or channel link; `esc` can't stop that, `verbatim` does."""
    evil = "Invoice @here @channel #general"
    digest = sample(
        context=evil,
        hero=evil,
        needs_you=[Group(evil, [f"• {evil}"])],
        review=[evil],
        totals_title=evil,
        notes=[evil],
        footer=evil,
    )
    blocks = digest.blocks()
    assert_valid_block_kit(blocks)
    texts = [b["text"] for b in blocks if b["type"] == "section"]
    texts += [e for b in blocks if b["type"] == "context" for e in b["elements"]]
    assert sum(evil in t["text"] for t in texts) == 7
    assert all(t["verbatim"] is True for t in texts)


def test_a_long_title_is_cut_to_the_header_limit():
    blocks = sample(title="x" * 200).blocks()
    assert blocks[0]["text"]["text"] == "x" * 149 + "…"


# --- Posting ------------------------------------------------------------------------------------


def _slack(*answers: dict) -> tuple[httpx.Client, list[dict]]:
    """A client answering each chat.postMessage with the next of `answers`; the posted bodies."""
    posted: list[dict] = []
    replies = iter(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == slack.POST_MESSAGE_URL
        assert request.headers["Authorization"] == "Bearer xoxb-test"
        posted.append(json.loads(request.content))
        return httpx.Response(200, json=next(replies))

    return httpx.Client(transport=httpx.MockTransport(handler)), posted


def test_send_posts_blocks_with_the_notification_as_text():
    http, posted = _slack({"ok": True})
    digest = sample()
    with http:
        slack.send_slack(http, "xoxb-test", "U1", digest)
    assert posted == [
        {
            "channel": "U1",
            "text": "2 emails need you · 9 processed",
            "blocks": digest.blocks(),
            "unfurl_links": False,
            "unfurl_media": False,
        }
    ]


@pytest.mark.parametrize("error", sorted(slack.INVALID_BLOCKS))
def test_rejected_blocks_fall_back_to_the_text_version(caplog, error):
    rejected = {
        "ok": False,
        "error": error,
        "response_metadata": {"messages": ["[ERROR] no more than 50 items [json-pointer:/blocks]"]},
    }
    http, posted = _slack(rejected, {"ok": True})
    digest = sample()
    with http, caplog.at_level(logging.WARNING):
        slack.send_slack(http, "xoxb-test", "U1", digest)
    assert [set(p) for p in posted] == [
        {"channel", "text", "blocks", "unfurl_links", "unfurl_media"},
        {"channel", "text", "unfurl_links", "unfurl_media"},
    ]
    assert posted[1]["text"] == digest.text
    (warning,) = caplog.records
    assert warning.error == error
    assert warning.details == ["[ERROR] no more than 50 items [json-pointer:/blocks]"]


def test_other_slack_errors_raise_without_a_second_post():
    http, posted = _slack({"ok": False, "error": "channel_not_found"})
    with http, pytest.raises(slack.SlackError, match="channel_not_found"):
        slack.send_slack(http, "xoxb-test", "U1", sample())
    assert len(posted) == 1


def test_a_failed_text_fallback_raises():
    http, posted = _slack({"ok": False, "error": "invalid_blocks"}, {"ok": False, "error": "x"})
    with http, pytest.raises(slack.SlackError, match="Slack error: x"):
        slack.send_slack(http, "xoxb-test", "U1", sample())
    assert len(posted) == 2
