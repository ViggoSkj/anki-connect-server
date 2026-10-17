"""Exercise analytics against real Anki collections and through MCP transport."""

from datetime import UTC, datetime

import pytest
from fastmcp import Client

from anki_connect_server.anki_wrapper import AnkiWrapper
from anki_connect_server.mcp_extentions import MCPExtensions


def _stamp(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def _add_card(wrapper: AnkiWrapper, deck: str, front: str, model: str = "Basic") -> int:
    note_id = wrapper.add_note(
        {
            "deckName": deck,
            "modelName": model,
            "fields": {"Front": front, "Back": "translation"},
        }
    )
    assert note_id is not None
    return wrapper.find_cards(f"nid:{note_id}")[0]


def _review(wrapper: AnkiWrapper, card: int, timestamp: str, rating: int, kind: int = 1) -> None:
    assert wrapper.col.db is not None
    wrapper.col.db.execute(
        "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type) "
        "values (?, ?, -1, ?, ?, ?, 2500, 1200, ?)",
        _stamp(timestamp),
        card,
        rating,
        -60 if kind == 0 else 30,
        10,
        kind,
    )


@pytest.fixture
def analytics(anki_wrapper: AnkiWrapper) -> tuple[MCPExtensions, int, int]:
    anki_wrapper.create_deck("Vocabulary")
    anki_wrapper.create_deck("Vocabulary::Child")
    mature = _add_card(anki_wrapper, "Vocabulary", "known")
    learning = _add_card(anki_wrapper, "Vocabulary::Child", "new")
    other = _add_card(anki_wrapper, "Default", "outside")
    assert anki_wrapper.col.db is not None
    anki_wrapper.col.db.execute(
        "update cards set type = 2, ivl = 30, lapses = 2 where id = ?", mature
    )
    assert anki_wrapper.col.db is not None
    anki_wrapper.col.db.execute("update cards set type = 1 where id = ?", learning)
    _review(anki_wrapper, mature, "2026-01-01T23:59:59", 3)
    _review(anki_wrapper, mature, "2026-01-02T00:00:00", 1)
    _review(anki_wrapper, learning, "2026-01-02T23:59:59.999", 3, 0)
    _review(anki_wrapper, mature, "2026-01-02T12:00:00", 0, 4)
    _review(anki_wrapper, mature, "2026-01-02T12:00:01", 3, 4)
    _review(anki_wrapper, other, "2026-01-02T12:00:02", 3)
    _review(anki_wrapper, mature, "2026-01-03T00:00:00", 4)
    return MCPExtensions(lambda: anki_wrapper), mature, learning


def test_history_filters_and_units(analytics):
    tools, mature, learning = analytics
    history = tools.get_review_history("vocabulary", "2026-01-02", "2026-01-02")
    assert [row["card_id"] for row in history] == [learning, mature]
    assert history[0]["interval"] == -60
    assert history[0]["previous_interval"] == 10
    assert history[0]["response_time_ms"] == 1200
    assert history[0]["reviewed_at"] == "2026-01-02T23:59:59.999000+00:00"
    assert history[1]["rating"] == 1
    assert tools.get_review_history("Vocabulary", limit=1, offset=1)[0] == history[0]
    assert len(tools.get_review_history()) == 5
    assert tools.get_review_history(start_date="2030-01-01") == []


def test_summary_period_and_snapshot(analytics):
    tools, _, _ = analytics
    summary = tools.get_learning_summary("Vocabulary", "2026-01-02", "2026-01-02")
    assert summary["total_reviews"] == 2
    assert summary["scheduled_reviews"] == 1
    assert summary["retention_percent"] == 0
    assert summary["lapses"] == 1
    assert summary["lifetime_lapses"] == 2
    assert summary["response_time_ms"] == 2400
    assert summary["total_cards"] == 2
    assert summary["card_counts"] == {
        "new": 0,
        "learning": 1,
        "relearning": 0,
        "young": 0,
        "mature": 1,
    }
    assert summary["new_vocabulary_count"] == 1
    assert summary["new_vocabulary"][0]["fields"]["Front"]["value"] == "new"
    assert summary["daily_reviews"][0]["date"] == "2026-01-02"
    complete = tools.get_learning_summary("Vocabulary")
    assert complete["total_reviews"] == 4
    assert complete["retention_percent"] == 66.67
    assert [row["retention_percent"] for row in complete["daily_reviews"]] == [100, 0, 100]
    assert complete["new_vocabulary_count"] == 2


def test_empty_and_learning_only_retention(anki_wrapper: AnkiWrapper):
    tools = MCPExtensions(lambda: anki_wrapper)
    empty = tools.get_learning_summary()
    assert empty["total_reviews"] == 0
    assert empty["retention_percent"] is None
    assert empty["daily_reviews"] == []
    assert empty["new_vocabulary"] == []
    card = _add_card(anki_wrapper, "Default", "learning")
    _review(anki_wrapper, card, "2026-01-02T12:00:00", 1, 0)
    summary = tools.get_learning_summary()
    assert summary["total_reviews"] == 1
    assert summary["retention_percent"] is None
    assert summary["lapses"] == 0


def test_new_vocabulary_uses_first_review_across_siblings(anki_wrapper: AnkiWrapper):
    anki_wrapper.create_model(
        "Two cards",
        ["Front", "Back"],
        [
            {"Name": "Forward", "Front": "{{Front}}", "Back": "{{Back}}"},
            {"Name": "Reverse", "Front": "{{Back}}", "Back": "{{Front}}"},
        ],
    )
    first = _add_card(anki_wrapper, "Default", "siblings", "Two cards")
    note = anki_wrapper.cards_to_notes([first])[0]
    cards = anki_wrapper.find_cards(f"nid:{note}")
    _review(anki_wrapper, cards[0], "2026-01-01T12:00:00", 3, 0)
    _review(anki_wrapper, cards[1], "2026-01-02T12:00:00", 3, 0)
    tools = MCPExtensions(lambda: anki_wrapper)
    assert tools.get_learning_summary()["new_vocabulary_count"] == 1
    assert tools.get_learning_summary(start_date="2026-01-02")["new_vocabulary_count"] == 0


def test_filtered_cards_use_home_deck(analytics, anki_wrapper: AnkiWrapper):
    tools, mature, _ = analytics
    home_deck = anki_wrapper.deck_names_and_ids()["Vocabulary"]
    temporary_deck = anki_wrapper.create_deck("Temporary")
    assert anki_wrapper.col.db is not None
    anki_wrapper.col.db.execute(
        "update cards set odid = ?, did = ? where id = ?", home_deck, temporary_deck, mature
    )
    assert len(tools.get_review_history("Vocabulary")) == 4
    assert tools.get_review_history("Temporary") == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"deck": "missing"},
        {"deck": 'Vocabulary" OR 1=1 --'},
        {"start_date": "invalid"},
        {"start_date": "2026-02-30"},
        {"start_date": "2026-01-03", "end_date": "2026-01-01"},
    ],
)
def test_invalid_filters(analytics, kwargs):
    tools, _, _ = analytics
    with pytest.raises(ValueError):
        tools.get_review_history(**kwargs)
    with pytest.raises(ValueError):
        tools.get_learning_summary(**kwargs)


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 10001}, {"offset": -1}])
def test_invalid_pagination(analytics, kwargs):
    tools, _, _ = analytics
    with pytest.raises(ValueError):
        tools.get_review_history(**kwargs)


@pytest.mark.asyncio
async def test_tools_registered_and_callable(analytics, anki_wrapper: AnkiWrapper):
    from anki_connect_server import mcp_server

    previous = mcp_server.set_wrapper_for_test(anki_wrapper)
    try:
        async with Client(mcp_server.mcp) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            for name in ("get_review_history", "get_learning_summary"):
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.readOnlyHint is True
                assert "self" not in tools[name].inputSchema["properties"]
                result = await client.call_tool(name, {"deck": "Vocabulary"})
                assert not result.is_error
                assert result.data
    finally:
        mcp_server.set_wrapper_for_test(previous)
