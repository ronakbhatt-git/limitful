"""Name voices from what was said: "I'm Joseph", "thanks, Joseph", "Joseph is late".

Voice grouping (speakers.py) knows that the same voice spoke in many recordings,
but not whose voice it is. Conversations often say so. While triaging, the
scheduled Claude task records each clue as a vote:

- introduction: the speaker of that line says the name is theirs. Strong.
- addressed: the line speaks to someone by name, so the next voice to answer is
  probably them. Weaker.
- mentioned: the name is talked about in the third person, so that person is
  probably not one of the voices in this conversation. A vote against.

Votes are kept per line and counted per voice each time, so they follow lines
when two speakers are merged. A voice is named automatically once at least
AUTO_CONVERSATIONS conversations agree on one name and nothing disagrees: no
conversation offers another name for it, none says that person was not there,
and the owner has not said no. Anything short of that is a suggestion the owner
can accept with one click. A name the owner typed is never changed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .store import Store

KINDS = ("introduction", "addressed", "mentioned")
AUTO_CONVERSATIONS = 3  # conversations that must agree before a name is applied without asking
NAMED_BY_CLUES = "clues"
# An address is answered by one of the next few lines; past that the guess is too loose.
ANSWER_WITHIN = 3
MAX_NAME = 60


def clean_name(name: str) -> str | None:
    """The name as it will be kept, or None if it does not look like a person's name."""
    name = " ".join(str(name or "").split()).strip(" .,:;!?\"'")
    if not name or len(name) > MAX_NAME or not any(ch.isalpha() for ch in name):
        return None
    if name.lower().startswith("speaker ") or len(name.split()) > 4:
        return None
    return name


def record(store: Store, conversation_id: int, line_ids: list[int], clues) -> int:
    """Save the clues Claude found in one conversation. Line numbers refer to its numbered
    transcript. Returns how many were kept; malformed ones are dropped."""
    now = int(time.time() * 1000)
    rows = []
    for clue in (clues or [])[:30]:
        if not isinstance(clue, dict):
            continue
        line, kind, name = clue.get("line"), clue.get("clue"), clean_name(clue.get("name"))
        if kind not in KINDS or name is None or not isinstance(line, int) or not 1 <= line <= len(line_ids):
            continue
        rows.append((conversation_id, line_ids[line - 1], name, kind, now))
    store.db.executemany(
        "INSERT INTO name_votes (conversation_id, utterance_id, name, kind, created_ms) VALUES (?, ?, ?, ?, ?)",
        rows)
    store.db.commit()
    return len(rows)


def reject(store: Store, speaker_id: int, name: str) -> None:
    """The owner says this voice is not that person: never suggest it again."""
    name = clean_name(name)
    if name is None:
        raise ValueError("that is not a name")
    if store.db.execute("SELECT 1 FROM speakers WHERE id = ?", (speaker_id,)).fetchone() is None:
        raise ValueError(f"there is no speaker {speaker_id}")
    store.db.execute("INSERT INTO name_votes (speaker_id, name, kind, created_ms) VALUES (?, ?, 'rejected', ?)",
                     (speaker_id, name, int(time.time() * 1000)))
    store.db.commit()


@dataclass
class Evidence:
    name: str
    agree: set[int] = field(default_factory=set)     # conversations voting for this name
    strong: set[int] = field(default_factory=set)    # of those, ones where they introduced themselves
    against: set[int] = field(default_factory=set)   # conversations where this person was only talked about
    rejected: bool = False


def tally(store: Store) -> dict[int, dict[str, Evidence]]:
    """For every voice, the names offered for it and how strongly. Keys of the inner dict
    are names in lower case."""
    out: dict[int, dict[str, Evidence]] = {}

    def entry(speaker_id: int, name: str) -> Evidence:
        return out.setdefault(speaker_id, {}).setdefault(name.lower(), Evidence(name))

    votes = store.db.execute(
        "SELECT v.conversation_id, v.utterance_id, v.name, v.kind, c.start_ms, c.end_ms FROM name_votes v "
        "JOIN conversations c ON c.id = v.conversation_id WHERE v.utterance_id IS NOT NULL "
        "ORDER BY v.conversation_id, v.id").fetchall()
    lines_of: dict[int, list[tuple[int, int | None]]] = {}
    mentions: list[tuple[int, str]] = []
    for cid, uid, name, kind, start_ms, end_ms in votes:
        if cid not in lines_of:
            lines_of[cid] = store.db.execute(
                "SELECT id, speaker_id FROM utterances WHERE start_ms >= ? AND start_ms <= ? ORDER BY start_ms, id",
                (start_ms, end_ms)).fetchall()
        lines = lines_of[cid]
        at = next((i for i, (line_id, _s) in enumerate(lines) if line_id == uid), None)
        if at is None:
            continue
        if kind == "mentioned":
            mentions.append((cid, name))
            continue
        target = None
        if kind == "introduction":
            target = lines[at][1]
        elif kind == "addressed":
            speaking = lines[at][1]
            for _line_id, sid in lines[at + 1 : at + 1 + ANSWER_WITHIN]:
                if sid is not None and sid != speaking:
                    target = sid
                    break
        if target is None:
            continue
        evidence = entry(target, name)
        evidence.agree.add(cid)
        if kind == "introduction":
            evidence.strong.add(cid)

    # Talked about in the third person: probably none of the voices in that conversation,
    # unless the same conversation also put the name on one of them.
    for cid, name in mentions:
        named_here = {sid for sid, names in out.items()
                      if name.lower() in names and cid in names[name.lower()].agree}
        for sid in {sid for _line_id, sid in lines_of[cid] if sid is not None} - named_here:
            entry(sid, name).against.add(cid)

    for sid, name in store.db.execute(
            "SELECT speaker_id, name FROM name_votes WHERE kind = 'rejected' AND speaker_id IS NOT NULL"):
        entry(sid, name).rejected = True
    return out


def _unnamed(store: Store) -> set[int]:
    """Voices still carrying a number, so free to be named from clues."""
    return {row[0] for row in store.db.execute("SELECT id FROM speakers WHERE name GLOB 'Speaker [0-9]*'")}


def review(store: Store) -> tuple[list[tuple[int, str]], list[dict]]:
    """What to apply now, as (speaker id, name), and what to suggest to the owner."""
    taken = {row[0].lower() for row in store.db.execute(
        "SELECT name FROM speakers WHERE name NOT GLOB 'Speaker [0-9]*'")}
    apply, suggest = [], []
    open_voices = _unnamed(store)
    for sid, names in tally(store).items():
        if sid not in open_voices:
            continue
        offered = [e for e in names.values() if e.agree and not e.rejected]
        for evidence in sorted(offered, key=lambda e: (-len(e.agree), -len(e.strong), e.name)):
            others = set().union(*(e.agree for e in names.values() if e is not evidence)) - evidence.agree
            disagree = len(others | evidence.against)
            clean = (len(evidence.agree) >= AUTO_CONVERSATIONS and not disagree
                     and evidence.name.lower() not in taken and len(offered) == 1)
            if clean:
                apply.append((sid, evidence.name))
                taken.add(evidence.name.lower())
                break
            suggest.append({"speaker": sid, "name": evidence.name, "conversations": sorted(evidence.agree),
                            "introductions": len(evidence.strong), "against": disagree})
    return apply, suggest


def apply(store: Store) -> list[tuple[int, str]]:
    """Name every voice the clues agree on. Returns (speaker id, name) for each one named."""
    done, _suggest = review(store)
    for sid, name in done:
        store.rename_speaker(sid, name)
        store.db.execute("UPDATE speakers SET named_by = ? WHERE id = ?", (NAMED_BY_CLUES, sid))
        store.db.commit()
    return done


def suggestions(store: Store) -> list[dict]:
    """Names the clues point to but do not settle, strongest first, each with the speaker's
    current label, how many lines they have, and a conversation to check it in."""
    _apply, found = review(store)
    labels = dict(store.db.execute("SELECT id, name FROM speakers").fetchall())
    out = []
    for item in found:
        cid = item["conversations"][-1]
        start, title = store.db.execute("SELECT start_ms, title FROM conversations WHERE id = ?", (cid,)).fetchone()
        lines = store.db.execute("SELECT COUNT(*) FROM utterances WHERE speaker_id = ?", (item["speaker"],)).fetchone()[0]
        out.append({**item, "label": labels.get(item["speaker"], ""), "lines": lines,
                    "example": {"id": cid, "start": start, "title": title}})
    return sorted(out, key=lambda s: (-len(s["conversations"]), -s["introductions"], s["against"], s["name"]))
