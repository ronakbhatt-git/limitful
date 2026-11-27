"""The MCP server, driven the way an app drives it: started as a program, spoken to over its pipes."""

import asyncio
import json
import sys
from datetime import datetime

import pytest

from limitful import mcp_server
from limitful.store import Store

T0 = int(datetime(2026, 11, 13, 9, 0, 0).timestamp() * 1000)


@pytest.fixture
def lifelog(tmp_path):
    store = Store(tmp_path)
    db = store.db
    db.execute("INSERT INTO speakers (id, name, voice_sum, lines) VALUES (1, 'Karen', x'00', 1)")
    said = [(0, "we should keep the quarterly report under ten pages", 1),
            (5000, "and send the template to Joseph after lunch", None)]
    for offset, text, speaker in said:
        db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text, speaker_id) VALUES ('x', ?, ?, ?, ?)",
                   (T0 + offset, T0 + offset + 4000, text, speaker))
    first, second = [row[0] for row in db.execute("SELECT id FROM utterances ORDER BY start_ms")]
    db.execute("INSERT INTO conversations (id, start_ms, end_ms, status, title, summary, notes) "
               "VALUES (7, ?, ?, 'done', 'Report and template', '[\"Old summary.\"]', ?)",
               (T0, T0 + 9000, json.dumps([{"text": "The report stays under ten pages.", "line": first}])))
    db.execute("INSERT INTO reminders (conversation_id, text, created_ms, utterance_id) VALUES (7, 'Send Joseph the template', ?, ?)",
               (T0, second))
    db.execute("INSERT INTO reminders (conversation_id, text, status, created_ms) VALUES (7, 'Book a room', 'dismissed', ?)", (T0,))
    db.execute("INSERT INTO day_recaps (day, headline, points, engine, signature, made_ms) "
               "VALUES ('2026-11-13', 'A short planning day', '[\"The report was scoped.\"]', 'x', 'x', 0)")
    db.commit()
    store.close()
    return tmp_path, first, second


def _talk(data_dir, calls, extra=()):
    """Start `limitful mcp` as an app would and make these tool calls; returns (tools, replies)."""
    from mcp import Client, StdioServerParameters

    async def run():
        program = StdioServerParameters(command=sys.executable,
                                        args=["-m", "limitful", "mcp", "--data-dir", str(data_dir), *extra])
        async with Client(program) as client:
            tools = (await client.list_tools()).tools
            replies = []
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                replies.append((result.is_error, "\n".join(block.text for block in result.content if block.type == "text")))
            return tools, replies

    return asyncio.run(run())


def test_an_app_can_read_the_lifelog_and_nothing_else(lifelog):
    data_dir, first, second = lifelog
    tools, replies = _talk(data_dir, [
        ("limitful_overview", {}),
        ("limitful_get_day", {"date": "2026-11-13"}),
        ("limitful_search_lines", {"query": "template"}),
        ("limitful_read_conversation", {"conversation_id": 7}),
        ("limitful_list_reminders", {}),
        ("limitful_list_reminders", {"status": "all"}),
        ("limitful_list_people", {}),
        ("limitful_search_meaning", {"query": "what the report should look like"}),
        ("limitful_get_day", {"date": "2026-01-01"}),
        ("limitful_read_conversation", {"conversation_id": 999}),
    ])
    assert sorted(tool.name for tool in tools) == [
        "limitful_get_day", "limitful_list_conversations", "limitful_list_people", "limitful_list_reminders",
        "limitful_overview", "limitful_read_conversation", "limitful_search_lines", "limitful_search_meaning"]
    # Every tool says it only reads, and describes what it is for.
    assert all(tool.annotations.read_only_hint and not tool.annotations.destructive_hint and tool.description
               for tool in tools)
    assert not any(failed for failed, _text in replies)
    overview, day, found, read, owed, every, people, meaning, empty, missing = [text for _failed, text in replies]
    assert "2 lines" in overview and "not set up" in overview
    assert "Recap: A short planning day" in day and "conversation 7" in day and "Report and template" in day
    assert f"The report stays under ten pages. [L{first}]" in day and "Old summary" not in day  # notes, once written, replace the summary
    assert "Send Joseph the template (suggested; conversation 7" in day and "Book a room" not in day
    assert f"[L{second}]" in found and 'conversation 7 "Report and template"' in found
    assert f"[L{first}] 09:00 Karen: we should keep" in read
    assert "1 reminders" in owed and "Send Joseph the template" in owed and "Book a room" not in owed
    assert "2 reminders" in every and "Book a room (dismissed" in every
    assert "Karen: 1 lines, last heard 2026-11-13" in people
    assert "not set up" in meaning
    assert "Nothing was recorded on 2026-01-01" in empty
    assert "There is no conversation 999" in missing


def test_bad_input_is_refused_and_the_snippet_names_this_lifelog(lifelog):
    data_dir, _first, _second = lifelog
    _tools, replies = _talk(data_dir, [("limitful_get_day", {"date": "yesterday"}),
                                       ("limitful_list_reminders", {"limit": 5000})])
    assert all(failed for failed, _text in replies)  # told what is wrong, not answered with a guess
    entry = json.loads(mcp_server.describe(data_dir, ["/x/python", "-m", "limitful"]))["mcpServers"]["limitful"]
    assert entry == {"command": "/x/python", "args": ["-m", "limitful", "mcp", "--data-dir", str(data_dir)]}


def test_a_scheduled_claude_task_can_triage_waiting_conversations(tmp_path):
    store = Store(tmp_path)
    for offset, text in ((0, "the plan is to move the launch to January because the vendor is late "
                             "and we need the security review finished first, so I will email Priya"),
                         (4000, "fine, send it today and copy the whole team on the new date please")):
        store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('x', ?, ?, ?)",
                         (T0 + offset, T0 + offset + 3000, text))
    store.db.commit()
    store.close()

    tools, _ = _talk(tmp_path, [])
    assert "limitful_save_conversation" not in {tool.name for tool in tools}  # read-only unless asked

    tools, replies = _talk(tmp_path, [
        ("limitful_pending_conversations", {"limit": 5}),
        ("limitful_save_conversation", {"conversation_id": 1, "title": "Moving the launch to January",
                                        "tier": "keep", "tags": ["work", "commitments", "nonsense"],
                                        "sensitive": False, "summary": ["The launch moves to January."],
                                        "notes": [{"text": "Launch moves to January: vendor is late.", "line": 1}],
                                        "reminders": [{"text": "Email Priya the new date", "line": 2}]}),
        ("limitful_save_conversation", {"conversation_id": 1, "title": "Again", "tier": "hold",
                                        "tags": [], "sensitive": False}),
        ("limitful_pending_conversations", {}),
    ], extra=["--triage"])
    assert "1 conversations are waiting" in replies[0][1] and "1. [" in replies[0][1]
    saved = json.loads(replies[1][1])
    assert saved["tags"] == ["work", "commitments"] and saved["still_waiting"] == 0
    assert "already has a digest" in replies[2][1]
    assert "No conversations are waiting" in replies[3][1]

    store = Store(tmp_path)
    title, tier, tags, sensitive = store.db.execute(
        "SELECT title, tier, tags, sensitive FROM conversations WHERE id = 1").fetchone()
    assert (title, tier, json.loads(tags), sensitive) == ("Moving the launch to January", "keep", ["work", "commitments"], 0)
    second = store.db.execute("SELECT id FROM utterances ORDER BY start_ms").fetchall()[1][0]
    assert store.db.execute("SELECT text, utterance_id FROM reminders").fetchall() == [("Email Priya the new date", second)]
    store.close()


def test_background_is_held_by_the_free_local_pass(tmp_path):
    from limitful import triage

    store = Store(tmp_path)
    store.db.execute("INSERT INTO utterances (segment, start_ms, end_ms, text) VALUES ('x', ?, ?, 'yeah ok')",
                     (T0, T0 + 2000))
    store.db.commit()
    store.build_conversations(now_ms=T0 + 3_600_000)
    assert triage.prefilter(store) == 1
    assert store.db.execute("SELECT status, tier FROM conversations").fetchone() == ("short", "hold")
    store.close()


def test_the_tag_list_shown_to_claude_matches_the_categories():
    from typing import get_args

    from limitful import triage

    described = get_args(mcp_server.Tags)[1].description
    assert described.endswith(", ".join(triage.CATEGORIES) + ".")
