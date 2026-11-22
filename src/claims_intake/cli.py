"""Command line: generate sample data, run the flow, work the review queue."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from .phi import mask_member_id


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claims-intake")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="write the synthetic sample data set")
    s.add_argument("--out", default="data")

    r = sub.add_parser("run", help="run the intake flow once over an inbox")
    r.add_argument("--inbox", default="data/inbox")
    r.add_argument("--out", default="out")
    r.add_argument("--roster", default="data/eligibility.csv")
    r.add_argument("--extractor", choices=["fake", "claude"], default="fake")
    r.add_argument("--truth-dir", default="data/truth", help="answer keys for the fake extractor")
    r.add_argument("--model", default="claude-opus-5-5")
    r.add_argument("--today", help="YYYY-MM-DD; defaults to the real date")
    r.add_argument("--baseline", help="metrics JSON of an earlier run to compare against")
    r.add_argument("--webhook", help="POST alerts here as JSON")

    q = sub.add_parser("review", help="list or decide pending review items")
    q.add_argument("--out", default="out")
    q.add_argument("--today")
    qs = q.add_subparsers(dest="action", required=True)
    qs.add_parser("list")
    for name in ("approve", "reject"):
        a = qs.add_parser(name)
        a.add_argument("id", type=int)
        a.add_argument("--reviewer", required=True)
        a.add_argument("--note", default="")
        if name == "approve":
            a.add_argument(
                "--set",
                action="append",
                default=[],
                metavar="FIELD=VALUE",
                help="correct a field before approving, e.g. --set member_dob=1951-04-02",
            )

    args = p.parse_args(argv)

    if args.cmd == "synth":
        from .synth import generate

        generate(Path(args.out))
        print(f"wrote synthetic data to {args.out}/")
        return 0

    if args.cmd == "run":
        from .flow import intake_flow

        report = intake_flow(
            inbox=args.inbox,
            out_dir=args.out,
            roster_path=args.roster,
            extractor=args.extractor,
            truth_dir=args.truth_dir if args.extractor == "fake" else None,
            model=args.model,
            today=args.today,
            baseline_path=args.baseline,
            webhook_url=args.webhook,
        )
        print(
            json.dumps(
                {k: report[k] for k in ("run_id", "claims_by_decision", "batches_held", "cost_usd", "alerts")}, indent=2
            )
        )
        return 0

    from .models import Claim
    from .store import Store
    from .validate import validate

    store = Store(Path(args.out) / "state.db")
    try:
        if args.action == "list":
            for row in store.pending_reviews():
                c = Claim.model_validate_json(row["claim_json"])
                codes = sorted({i["code"] for i in json.loads(row["issues"])})
                print(
                    f"#{row['id']:<4} {c.claim_id:<8} {c.source:<6} member {mask_member_id(c.member_id)}  issues: {', '.join(codes)}"
                )
            return 0
        if args.action == "reject":
            store.decide(args.id, False, args.reviewer, args.note)
            print(f"review {args.id} rejected")
            return 0
        # approve, optionally with corrections; corrected claims are re-validated before approval
        row = next((r for r in store.pending_reviews() if r["id"] == args.id), None)
        if row is None:
            print(f"review {args.id} is not pending", file=sys.stderr)
            return 1
        data = json.loads(row["claim_json"])
        for kv in args.set:
            k, _, v = kv.partition("=")
            if k not in data or k in {"lines", "source", "source_ref"}:
                print(f"cannot set {k}", file=sys.stderr)
                return 2
            data[k] = v.split(";") if k == "diagnosis_codes" else v
        fixed = {f for f in (kv.partition("=")[0] for kv in args.set)}
        data["unclear_fields"] = [f for f in data["unclear_fields"] if f not in fixed]
        claim = Claim.model_validate(data)
        from .validate import load_roster

        roster = (
            load_roster((Path("data") / "eligibility.csv").read_text())
            if (Path("data") / "eligibility.csv").exists()
            else {}
        )
        today = date.fromisoformat(args.today) if args.today else date.today()
        remaining = [i for i in validate(claim, roster, today) if i.severity == "error"]
        if remaining:
            print("still has errors: " + ", ".join(i.code for i in remaining), file=sys.stderr)
            return 1
        store.decide(args.id, True, args.reviewer, args.note, corrected=claim)
        from .fhir import to_fhir

        out = Path(args.out) / "fhir"
        out.mkdir(parents=True, exist_ok=True)
        resource = to_fhir(claim, datetime.now().astimezone())
        (out / f"{resource['id']}.json").write_text(json.dumps(resource, indent=2))
        print(f"review {args.id} approved; FHIR Claim written")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
