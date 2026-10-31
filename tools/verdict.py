"""Verdict: register, check, resolve and score forecasts. Standard library only.

    python tools/verdict.py check [--base <git ref>]     ledger valid, linted, append-only since <ref>
    python tools/verdict.py register drafts/<file>.json  move approved drafts into the ledger, timestamped now
    python tools/verdict.py resolve <id> <0|1> --url <evidence> [--note "..."] [--by "..."]
    python tools/verdict.py score [--out scores.json]    Brier score, Murphy decomposition, skill, by program

The ledger is two append-only JSON-lines files in git: ledger/forecasts.jsonl (feed contract tic-feed/2 forecast
items, each with its resolution rule written at issue time) and ledger/resolutions.jsonl. A registered line is never
edited or removed; a correction is a new line (msg_type update or cancel, with supersedes). Git commit history is
the timestamp lock.
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doctrine" / "kit" / "python"))
from tic_doctrine import contract, registers  # noqa: E402

FORECASTS = Path("ledger") / "forecasts.jsonl"
RESOLUTIONS = Path("ledger") / "resolutions.jsonl"
BINS = 10


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise SystemExit(f"{path}:{n}: not JSON ({e})")
    return out


def append_lines(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n")


def forecast_problems(item: dict, regs: dict) -> list[str]:
    """Contract errors plus Verdict's own rules for one ledger forecast."""
    errs = [f"{std}: {m}" for std, sev, m in contract.check(item, regs) if sev == "error"]
    if item.get("kind") != "forecast":
        errs.append("verdict: only kind=forecast items go in the ledger")
    if item.get("status") != "actual":
        errs.append("verdict: a registered forecast has status 'actual'")
    if not item.get("approved_by"):
        errs.append("verdict: approved_by missing (who set the final probability)")
    res = item.get("resolution") or {}
    known = res.get("known_at")
    if not known:
        errs.append("verdict: resolution.known_at missing (when the outcome first becomes knowable)")
    elif item.get("published_at") and _t(item["published_at"]) >= _t(known):
        errs.append("verdict: registered at or after the outcome became knowable (not a pre-registration)")
    return errs


def check(root: Path = ROOT, base: str | None = None) -> list[str]:
    regs = registers.ids(root / "doctrine")
    errs = []
    forecasts = read_lines(root / FORECASTS)
    seen = set()
    for i, f in enumerate(forecasts, 1):
        fid = f.get("id")
        if fid in seen:
            errs.append(f"forecasts line {i}: duplicate id {fid!r}")
        seen.add(fid)
        errs += [f"forecasts line {i} ({fid}): {m}" for m in forecast_problems(f, regs)]
        if f.get("supersedes") and f["supersedes"] not in seen:
            errs.append(f"forecasts line {i} ({fid}): supersedes unknown or later id {f['supersedes']!r}")
    by_id = {f.get("id"): f for f in forecasts}
    resolved = set()
    for i, r in enumerate(read_lines(root / RESOLUTIONS), 1):
        rid = r.get("id")
        if rid not in by_id:
            errs.append(f"resolutions line {i}: unknown forecast id {rid!r}")
            continue
        if rid in resolved:
            errs.append(f"resolutions line {i}: {rid} resolved twice")
        resolved.add(rid)
        if r.get("outcome") not in (0, 1):
            errs.append(f"resolutions line {i}: outcome must be 0 or 1")
        if not (r.get("evidence") or {}).get("url"):
            errs.append(f"resolutions line {i}: evidence.url missing")
        if r.get("resolved_at") and _t(r["resolved_at"]) < _t(by_id[rid]["published_at"]):
            errs.append(f"resolutions line {i}: resolved before the forecast was registered")
    if base:
        errs += append_only(root, base)
    return errs


def append_only(root: Path, base: str) -> list[str]:
    """Every ledger line present at <base> must still be there, unchanged and in order."""
    errs = []
    for rel in (FORECASTS, RESOLUTIONS):
        old = subprocess.run(["git", "show", f"{base}:{rel.as_posix()}"], cwd=root, capture_output=True, text=True,
                             encoding="utf-8")
        if old.returncode != 0:
            continue  # the file did not exist at base
        before = [ln for ln in old.stdout.splitlines() if ln.strip()]
        now_path = root / rel
        after = [ln for ln in now_path.read_text(encoding="utf-8").splitlines() if ln.strip()] if now_path.exists() else []
        if after[:len(before)] != before:
            errs.append(f"{rel}: a registered line was edited, removed or reordered since {base} (append only)")
    return errs


def register(draft_path: Path, root: Path = ROOT, when: str | None = None) -> list[str]:
    """Append every approved draft to the ledger with status actual and published_at = now; return their ids."""
    doc = json.loads(draft_path.read_text(encoding="utf-8"))
    regs = registers.ids(root / "doctrine")
    when = when or now_utc()
    existing = {f.get("id") for f in read_lines(root / FORECASTS)}
    ready, problems = [], []
    for d in doc["drafts"]:
        if not d.get("approved") or d.get("registered_at"):
            continue
        item = {k: v for k, v in d["item"].items()}
        item.update({"status": "actual", "published_at": when, "approved_by": d["approved"]})
        if item["id"] in existing:
            problems.append(f"{item['id']}: already in the ledger")
            continue
        errs = forecast_problems(item, regs)
        if errs:
            problems += [f"{item['id']}: {e}" for e in errs]
            continue
        ready.append((d, item))
    if problems:
        raise SystemExit("not registered:\n  " + "\n  ".join(problems))
    append_lines(root / FORECASTS, [it for _, it in ready])
    for d, _ in ready:
        d["registered_at"] = when
    draft_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return [it["id"] for _, it in ready]


def resolve(fid: str, outcome: int, url: str, note: str = "", by: str = "", root: Path = ROOT,
            when: str | None = None) -> dict:
    if fid not in {f.get("id") for f in read_lines(root / FORECASTS)}:
        raise SystemExit(f"{fid}: not in the ledger")
    if fid in {r.get("id") for r in read_lines(root / RESOLUTIONS)}:
        raise SystemExit(f"{fid}: already resolved")
    rec = {"id": fid, "outcome": int(outcome), "resolved_at": when or now_utc(),
           "evidence": {"url": url, "accessed_at": when or now_utc()}, "note": note, "resolved_by": by}
    append_lines(root / RESOLUTIONS, [rec])
    return rec


def _brier(pairs):
    """Brier score with Murphy's decomposition (reliability - resolution + uncertainty) over BINS bins."""
    n = len(pairs)
    bs = sum((p - o) ** 2 for p, o in pairs) / n
    base = sum(o for _, o in pairs) / n
    bins = {}
    for p, o in pairs:
        bins.setdefault(min(int(p * BINS), BINS - 1), []).append((p, o))
    rel = sum(len(v) * (sum(p for p, _ in v) / len(v) - sum(o for _, o in v) / len(v)) ** 2 for v in bins.values()) / n
    res = sum(len(v) * (sum(o for _, o in v) / len(v) - base) ** 2 for v in bins.values()) / n
    unc = base * (1 - base)
    return {"n": n, "brier": round(bs, 4), "base_rate": round(base, 4), "reliability": round(rel, 4),
            "resolution": round(res, 4), "uncertainty": round(unc, 4),
            "skill_vs_base_rate": round(1 - bs / unc, 4) if unc > 0 else None,
            "note": "decomposition is approximate within bins; skill_vs_base_rate uses the sample's own base rate"}


def score(root: Path = ROOT) -> dict:
    fc = {f["id"]: f for f in read_lines(root / FORECASTS)}
    live = {fid for fid, f in fc.items() if f.get("msg_type") != "cancel"}
    superseded = {f.get("supersedes") for f in fc.values() if f.get("supersedes")}
    pairs = {}
    for r in read_lines(root / RESOLUTIONS):
        f = fc.get(r["id"])
        if not f or r["id"] not in live or r["id"] in superseded:
            continue
        pairs.setdefault(f["program"], []).append((float(f["probability"]), int(r["outcome"])))
        pairs.setdefault("all", []).append((float(f["probability"]), int(r["outcome"])))
    return {"generated_at": now_utc(), "registered": len(fc), "resolved": len(pairs.get("all", [])),
            "by_program": {k: _brier(v) for k, v in sorted(pairs.items())}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base")
    r = sub.add_parser("register")
    r.add_argument("drafts")
    s = sub.add_parser("resolve")
    s.add_argument("id")
    s.add_argument("outcome", type=int, choices=(0, 1))
    s.add_argument("--url", required=True)
    s.add_argument("--note", default="")
    s.add_argument("--by", default="")
    sc = sub.add_parser("score")
    sc.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        errs = check(ROOT, a.base)
        for e in errs:
            print("  -", e)
        n = len(read_lines(ROOT / FORECASTS))
        print(f"Verdict ledger: {n} forecasts, {len(read_lines(ROOT / RESOLUTIONS))} resolutions: "
              f"{'PASS' if not errs else f'FAIL ({len(errs)} problems)'}")
        return 1 if errs else 0
    if a.cmd == "register":
        ids = register(Path(a.drafts))
        print("registered:", ", ".join(ids) if ids else "nothing (no approved, unregistered drafts)")
        return 0
    if a.cmd == "resolve":
        print(json.dumps(resolve(a.id, a.outcome, a.url, a.note, a.by)))
        return 0
    out = score()
    text = json.dumps(out, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8", newline="\n")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
