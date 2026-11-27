"""The event ladder (lead, confirmed, verified) and the coverage table (tic-coverage/1), kit 0.6.13.

The definitions, the promotion rule and the party and state-media rule are in docs/event-ladder.md and
vocab/event_ladder.json. This module reads an item's state, says whether it rests only on a party's claim or on state
media, and builds coverage-table rows from the items a consumer holds for a place.
"""

from . import vocab

COVERAGE_FORMAT = "tic-coverage/1"


def _v() -> dict:
    return vocab.load("event_ladder")


def rule() -> str:
    """The promotion rule's id and text, as feeds print it in corroboration.rule."""
    return _v()["rule"]["text"]


def url() -> str:
    return _v()["url"]


def _props(item: dict) -> dict:
    return item.get("properties", item) if isinstance(item, dict) else {}


def state_of(item: dict) -> str | None:
    """lead, confirmed or verified for an event item (earlier names read as their new ones); None outside the ladder."""
    c = _props(item).get("corroboration")
    if not isinstance(c, dict):
        return None
    s = c.get("state")
    s = _v()["earlier_names"].get(s, s)
    return s if s in {st["id"] for st in _v()["states"]} else None


def rests_on_claim(item: dict) -> bool:
    """True while an event item rests only on a party's claim or on state media: it raises nothing on its own."""
    c = _props(item).get("corroboration")
    if not isinstance(c, dict):
        return False
    return bool(c.get("party_claim") is True or c.get("state_media") is True) and state_of(item) != "verified"


def coverage_class(independent: int, uncounted: int = 0) -> str:
    """several (3 or more independent groups), few (1 or 2), no_independent (none, but `uncounted` groups were read and
    set aside or are the suite's own, kit 0.6.17) or none (not monitored)."""
    classes = _v()["coverage"]["classes"]
    if independent <= 0 and uncounted > 0:
        return next(cl["id"] for cl in classes if cl.get("uncounted"))
    for cl in classes:
        if not cl.get("uncounted") and independent >= cl["min"] and (cl["max"] is None or independent <= cl["max"]):
            return cl["id"]
    return "none"


def coverage_label(cls: str) -> str:
    return {c["id"]: c["label"] for c in _v()["coverage"]["classes"]}.get(cls, cls)


def is_derived(src: dict) -> bool:
    """True for a source the suite makes itself (a program's own reading, computation, model or machine summary):
    it is not a source about the place, so the coverage table does not count it."""
    kind = str(src.get("source_type") or "").strip().lower()
    return kind in {k.lower() for k in _v()["coverage"]["not_counted_source_types"]}


def _is_state_media(src: dict) -> bool:
    if src.get("owner") != "state":
        return False
    kind = str(src.get("source_type") or "").lower()
    return bool(src.get("perspective")) or any(w in kind for w in ("media", "news", "broadcast", "wire", "press"))


def source_group(src: dict) -> tuple[str, str] | None:
    """(group id, label) for a source: its independence group, else its publisher, else its id."""
    if not isinstance(src, dict):
        return None
    gid = src.get("independence_group") or src.get("publisher") or src.get("id")
    if not gid:
        return None
    return str(gid), str(src.get("publisher") or gid)


def coverage_place(place: str, name: str, items: list[dict]) -> dict:
    """One tic-coverage/1 row from the items held for a place. Pass only items read on time: stale copies do not count.

    Each item is a contract item (or its properties); its program is `program` (or `cop_program` where a consumer
    renames). Sources the suite makes itself (is_derived) are not counted: they are listed under not_counted (kit
    0.6.17). Groups of state media, and every source of an item resting only on a party's claim or on state media, are
    listed under set_aside and never count toward the class. A place with no independent group but some set aside or
    not counted is no_independent, not none: something was read there.
    """
    groups, aside, own, programs = {}, {}, {}, set()
    for it in items:
        p = _props(it)
        prog = p.get("cop_program") or p.get("program")
        claim = rests_on_claim(p)
        why_item = None
        if claim:
            c = p["corroboration"]
            why_item = "party_claim" if c.get("party_claim") is True else "state_media"
        for src in p.get("sources") or []:
            g = source_group(src)
            if not g:
                continue
            why = None if is_derived(src) else why_item or ("state_media" if _is_state_media(src) else None)
            bucket = own if is_derived(src) else aside if why else groups
            row = bucket.setdefault(g[0], {"id": g[0], "label": g[1], "programs": set()} | ({"why": why} if why else {}))
            if prog:
                row["programs"].add(prog)
                programs.add(prog)
    for g in list(aside):
        if g in groups:   # counted elsewhere on its own: it is independent there
            aside.pop(g)

    def rows(b):
        return [dict(r, programs=sorted(r["programs"])) for _, r in sorted(b.items())]

    n = len(groups)
    return {"place": place, "name": name, "class": coverage_class(n, len(aside) + len(own)), "independent": n,
            "groups": rows(groups), "set_aside": rows(aside), "not_counted": rows(own), "programs": sorted(programs)}
