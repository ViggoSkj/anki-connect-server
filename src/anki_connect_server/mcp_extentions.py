"""Read-only MCP tools for review history and learning statistics."""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

from fastmcp import FastMCP

from anki_connect_server.anki_wrapper import AnkiWrapper
from anki_connect_server.tool_metadata import READ_ONLY
from anki_connect_server.types import JsonObject, JsonValue


def _timestamp_ms(value: str) -> int:
    return int(datetime.combine(date.fromisoformat(value), time.min, UTC).timestamp() * 1000)


class MCPExtensions:
    """Resolve the current wrapper on each call, including after sync or test swaps."""

    def __init__(self, get_wrapper: Callable[[], AnkiWrapper]) -> None:
        self.get_wrapper = get_wrapper

    def _scope(
        self,
        wrapper: AnkiWrapper,
        deck: str | None,
        start_date: str | None = None,
        end_date: str | None = None,
        timestamp_column: str = "r.id",
    ) -> tuple[str, list[int]]:
        clauses: list[str] = []
        args: list[int] = []
        if deck is not None:
            decks = wrapper.deck_names_and_ids()
            if deck.casefold() not in {name.casefold() for name in decks}:
                raise ValueError(f"Deck not found: {deck}")
            ids = [
                deck_id
                for name, deck_id in decks.items()
                if name.casefold() == deck.casefold()
                or name.casefold().startswith(deck.casefold() + "::")
            ]
            placeholders = ",".join("?" for _ in ids)
            # Cards in filtered decks retain their home deck in odid.
            clauses.append(
                f"(case when c.odid != 0 then c.odid else c.did end) in ({placeholders})"
            )
            args.extend(ids)
        start = _timestamp_ms(start_date) if start_date is not None else None
        end = _timestamp_ms(end_date) if end_date is not None else None
        if start is not None and end is not None and start > end:
            raise ValueError("start_date must be on or before end_date")
        if start is not None:
            clauses.append(f"{timestamp_column} >= ?")
            args.append(start)
        if end is not None:
            clauses.append(f"{timestamp_column} < ?")
            args.append(end + int(timedelta(days=1).total_seconds() * 1000))
        return " and ".join(clauses) or "1", args

    def get_review_history(
        self,
        deck: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[JsonObject]:
        """Return reviews, newest first, optionally filtered by deck and date.

        Dates are inclusive UTC calendar dates (YYYY-MM-DD). Decks include
        subdecks and use the card's current home deck, not its historical deck.
        Ratings are 1=Again, 2=Hard, 3=Good, 4=Easy. Intervals are Anki's raw
        values: positive days or negative seconds; response times are milliseconds.
        Review types are 0=learning, 1=review, 2=relearning, 3=filtered.
        Manual/rescheduling entries are excluded. Paginate with limit (1-10000)
        and offset; timestamp_ms is also the review's unique ID.
        """
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        wrapper = self.get_wrapper()
        db = wrapper.col.db
        if db is None:
            raise RuntimeError("Anki collection is closed")
        scope, args = self._scope(wrapper, deck, start_date, end_date)
        rows = db.all(
            "select r.id, r.cid, c.nid, r.ease, r.ivl, r.lastIvl, r.time, r.type "  # noqa: S608 - bound values; internal SQL scope
            "from revlog r join cards c on c.id = r.cid "
            f"where r.ease between 1 and 4 and r.type between 0 and 3 and {scope} "
            "order by r.id desc limit ? offset ?",
            *args,
            limit,
            offset,
        )
        return [
            {
                "timestamp_ms": stamp,
                "reviewed_at": datetime.fromtimestamp(stamp / 1000, UTC).isoformat(),
                "card_id": card_id,
                "note_id": note_id,
                "rating": rating,
                "interval": interval,
                "previous_interval": previous_interval,
                "response_time_ms": response_time,
                "review_type": review_type,
            }
            for stamp, card_id, note_id, rating, interval, previous_interval, response_time, review_type in rows
        ]

    def get_learning_summary(
        self,
        deck: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> JsonObject:
        """Return daily reviews, retention, card stages, lapses and new vocabulary.

        Dates are inclusive UTC calendar dates (YYYY-MM-DD); omitted bounds are
        unbounded. Decks include subdecks and refer to current home decks.
        Daily rows cover active days. Retention is the percentage of scheduled
        reviews (type 1) rated Hard/Good/Easy; it is null without such reviews.
        Lapses count Again on scheduled reviews in the selected period.
        Card counts and lifetime_lapses are a current snapshot, unaffected by dates;
        mature cards are review cards with intervals of at least 21 days.
        New vocabulary means distinct notes first studied in the period across
        all sibling cards, with their current fields; it does not infer word counts.
        Manual/rescheduling entries are excluded from all activity statistics.
        """
        wrapper = self.get_wrapper()
        db = wrapper.col.db
        if db is None:
            raise RuntimeError("Anki collection is closed")
        scope, args = self._scope(wrapper, deck, start_date, end_date)
        rows = db.all(
            "select date(r.id / 1000, 'unixepoch'), count(*), "  # noqa: S608 - bound values; internal SQL scope
            "sum(case when r.type = 1 then 1 else 0 end), "
            "sum(case when r.type = 1 and r.ease > 1 then 1 else 0 end), "
            "sum(case when r.type = 1 and r.ease = 1 then 1 else 0 end), sum(r.time) "
            "from revlog r join cards c on c.id = r.cid "
            f"where r.ease between 1 and 4 and r.type between 0 and 3 and {scope} "
            "group by date(r.id / 1000, 'unixepoch') order by r.id",
            *args,
        )
        daily: list[JsonValue] = []
        reviews = scheduled = retained = lapses = response_time = 0
        for day, count, scheduled_count, retained_count, lapse_count, elapsed in rows:
            daily.append(
                {
                    "date": day,
                    "reviews": count,
                    "scheduled_reviews": scheduled_count,
                    "retained_reviews": retained_count,
                    "retention_percent": _retention(retained_count, scheduled_count),
                    "lapses": lapse_count,
                    "response_time_ms": elapsed,
                }
            )
            reviews += count
            scheduled += scheduled_count
            retained += retained_count
            lapses += lapse_count
            response_time += elapsed

        card_scope, card_args = self._scope(wrapper, deck)
        counts: dict[str, int] = {"new": 0, "learning": 0, "relearning": 0, "young": 0, "mature": 0}
        lifetime_lapses = total_cards = 0
        for card_type, interval, count, card_lapses in db.all(
            f"select c.type, c.ivl, count(*), sum(c.lapses) from cards c where {card_scope} "  # noqa: S608 - bound values; internal SQL scope
            "group by c.type, c.ivl",
            *card_args,
        ):
            stage = {0: "new", 1: "learning", 3: "relearning"}.get(
                card_type, "mature" if interval >= 21 else "young"
            )
            counts[stage] += count
            total_cards += count
            lifetime_lapses += card_lapses

        vocabulary_scope, vocabulary_args = self._scope(
            wrapper, deck, start_date, end_date, "first_review.timestamp_ms"
        )
        introduced = db.all(
            "select distinct c.nid, first_review.timestamp_ms from cards c join "  # noqa: S608 - bound values; internal SQL scope
            "(select sibling.nid, min(r.id) as timestamp_ms from revlog r "
            "join cards sibling on sibling.id = r.cid "
            "where r.ease between 1 and 4 and r.type between 0 and 3 group by sibling.nid) "
            "first_review on first_review.nid = c.nid "
            f"where {vocabulary_scope} order by first_review.timestamp_ms, c.nid",
            *vocabulary_args,
        )
        notes = wrapper.notes_info([note_id for note_id, _ in introduced])
        vocabulary: list[JsonValue] = [
            {
                "note_id": note_id,
                "first_review_at": datetime.fromtimestamp(stamp / 1000, UTC).isoformat(),
                "fields": note["fields"],
            }
            for (note_id, stamp), note in zip(introduced, notes, strict=True)
        ]
        return {
            "deck": deck,
            "start_date": start_date,
            "end_date": end_date,
            "timezone": "UTC",
            "total_reviews": reviews,
            "scheduled_reviews": scheduled,
            "retained_reviews": retained,
            "retention_percent": _retention(retained, scheduled),
            "lapses": lapses,
            "response_time_ms": response_time,
            "daily_reviews": daily,
            "total_cards": total_cards,
            "card_counts": dict(counts),
            "lifetime_lapses": lifetime_lapses,
            "new_vocabulary_count": len(vocabulary),
            "new_vocabulary": vocabulary,
        }


def _retention(retained: int, scheduled: int) -> float | None:
    return round(100 * retained / scheduled, 2) if scheduled else None


def register_mcp_extensions(mcp: FastMCP, get_wrapper: Callable[[], AnkiWrapper]) -> None:
    extensions = MCPExtensions(get_wrapper)
    mcp.tool(annotations=READ_ONLY)(extensions.get_review_history)
    mcp.tool(annotations=READ_ONLY)(extensions.get_learning_summary)
