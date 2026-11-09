"""``prooft`` command line.

    prooft run LANE -- CMD...        run CMD; on success stamp HEAD for LANE's gates
    prooft verify [REV]              show every gate's state on REV (default HEAD)
    prooft plan --base REV [--head REV] [--title T] [--fork] [--github-output]
    prooft queue --pr-head REV --queue-head REV --base REV
    prooft lanes                     list lanes and the gates each proves
    prooft soldr [--host T] [-- CMD]  run soldr lint (porcelain); stamp the Rust gates whose steps passed
    prooft hook install [--force]    install the pre-push hook
    prooft check-push                what the hook runs (git pre-push stdin)

Exit codes: 0 ok, 1 a check failed / something must run, 2 usage or policy error.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
import sys

import prooft
from prooft import hook
from prooft.policy import PolicyError
from prooft.verify import Plan


def _plan_report(plan: Plan, as_json: bool) -> str:
    if as_json:
        return json.dumps(
            {
                "sha": plan.verdict.sha,
                "skipped": list(plan.skipped),
                "run": list(plan.run),
                "jobs": [{"job": j.job, "skip": j.skip, "reason": j.reason} for j in plan.jobs],
            },
            indent=2,
        )
    return "\n".join(f"{'skip' if j.skip else 'run '} {j.job}  ({j.reason})" for j in plan.jobs)


def _write_outputs(plan: Plan) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in plan.outputs().items():
            handle.write(f"{key}={value}\n")


def _cmd_run(repo: prooft.Repo, args: argparse.Namespace) -> int:
    cmd = [str(c) for c in args.cmd]
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        raise ValueError("run needs a command after --")
    sha = repo.lane(args.lane).run(cmd)
    print(f"attested {args.lane} on {sha[:12]}")
    return 0


def _cmd_verify(repo: prooft.Repo, args: argparse.Namespace) -> int:
    verdict = repo.verify(args.rev)
    if args.json:
        rows = [{"gate": s.gate, "state": str(s.state), "detail": s.detail} for s in verdict.statuses]
        print(json.dumps(rows, indent=2))
    else:
        for status in verdict.statuses:
            print(f"{status.state!s:13} {status.gate}  ({status.detail})")
        for problem in verdict.malformed:
            print(f"malformed     {problem}")
    return 1 if verdict.errors or verdict.malformed else 0


def _cmd_lanes(repo: prooft.Repo, _: argparse.Namespace) -> int:
    policy = repo.policy()
    for lane in policy.lanes:
        print(f"{lane}: {', '.join(g.path for g in policy.lane_gates(lane))}")
    return 0


def _cmd_plan(repo: prooft.Repo, args: argparse.Namespace) -> int:
    if args.command == "plan":
        plan = repo.plan(args.base, args.head, title=args.title, fork=args.fork)
    else:
        plan = repo.queue(pr_head=args.pr_head, queue_head=args.queue_head, base=args.base)
    print(_plan_report(plan, args.json))
    if args.github_output:
        _write_outputs(plan)
    return 0


def _cmd_soldr(repo: prooft.Repo, args: argparse.Namespace) -> int:
    cmd = [str(c) for c in args.cmd]
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    result = repo.soldr(cmd or ("soldr", "lint", "rust", "--check"), host=args.host)
    for gate in result.gates:
        print(f"attested {gate}")
    if not result.gates:
        print("no declared gate passed; nothing attested")
    return 0 if result.ok else 1


def _cmd_hook(repo: prooft.Repo, args: argparse.Namespace) -> int:
    print(f"installed {hook.install(repo, force=args.force)}")
    return 0


def _cmd_check_push(repo: prooft.Repo, _: argparse.Namespace) -> int:
    result = hook.check_push(repo, sys.stdin)
    for line in result.lines:
        print(line, file=sys.stderr)
    return 0 if result.ok else 1


COMMANDS = {
    "run": _cmd_run,
    "verify": _cmd_verify,
    "lanes": _cmd_lanes,
    "plan": _cmd_plan,
    "queue": _cmd_plan,
    "hook": _cmd_hook,
    "check-push": _cmd_check_push,
    "soldr": _cmd_soldr,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prooft", description="Proof marks for client builds.")
    parser.add_argument("-C", dest="path", default=".", help="repository path (default: .)")
    parser.add_argument("--version", action="version", version=f"prooft {prooft.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run a lane and stamp HEAD on success")
    p_run.add_argument("lane")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER)

    p_verify = sub.add_parser("verify", help="show every gate's state")
    p_verify.add_argument("rev", nargs="?", default="HEAD")
    p_verify.add_argument("--json", action="store_true")

    for name in ("plan", "queue"):
        p = sub.add_parser(name, help=f"decide which jobs a {name} run may skip")
        p.add_argument("--base", required=True)
        p.add_argument("--json", action="store_true")
        p.add_argument("--github-output", action="store_true", help="append skip_<job>=true|false to $GITHUB_OUTPUT")
        if name == "plan":
            p.add_argument("--head", default="HEAD")
            p.add_argument("--title", default="")
            p.add_argument("--fork", action="store_true")
        else:
            p.add_argument("--pr-head", required=True)
            p.add_argument("--queue-head", default="HEAD")

    sub.add_parser("lanes", help="list lanes and their gates")

    p_soldr = sub.add_parser("soldr", help="run soldr lint (porcelain) and stamp the Rust gates it proved")
    p_soldr.add_argument("--host", help="host triple for host-only steps (default: detected)")
    p_soldr.add_argument("cmd", nargs=argparse.REMAINDER, help="default: soldr lint rust --check")

    p_hook = sub.add_parser("hook", help="install the pre-push hook")
    p_hook.add_argument("action", choices=["install"])
    p_hook.add_argument("--force", action="store_true", help="replace a custom hook (saved as pre-push.pre-prooft)")

    p_check = sub.add_parser("check-push", help="pre-push check (reads git pre-push lines on stdin)")
    p_check.add_argument("remote", nargs="?")
    p_check.add_argument("url", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return COMMANDS[args.command](prooft.open(args.path), args)
    except prooft.LaneFailed as exc:
        print(f"prooft: {exc}", file=sys.stderr)
        return 1
    except (PolicyError, RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"prooft: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
