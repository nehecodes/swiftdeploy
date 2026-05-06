import argparse
from .output import die
from .commands.init import cmd_init
from .commands.validate import cmd_validate
from .commands.deploy import cmd_deploy
from .commands.promote import cmd_promote
from .commands.teardown import cmd_teardown
from .commands.status import cmd_status
from .commands.audit import cmd_audit


def main():
    parser = argparse.ArgumentParser(
        prog="swiftdeploy",
        description="Zero-friction containerised deployment with policy gating.",
    )
    parser.add_argument("--manifest", default="manifest.yaml", metavar="FILE")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    sub.add_parser("init", help="Generate nginx.conf and docker-compose.yaml")
    sub.add_parser("validate", help="Pre-flight checks (manifest, image, ports)")

    p = sub.add_parser(
        "deploy", help="Policy-gated deploy (runs init + validate first)"
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Seconds to wait for the health check to pass",
    )

    p = sub.add_parser("promote", help="Policy-gated mode promotion (canary ↔ stable)")
    p.add_argument("mode", choices=["canary", "stable"])

    p = sub.add_parser("teardown", help="Stop and optionally remove the stack")
    p.add_argument(
        "--clean", action="store_true", help="Also remove volumes and networks"
    )

    sub.add_parser("status", help="Live metrics + policy-compliance dashboard")
    sub.add_parser("audit", help="Generate audit_report.md from history.jsonl")

    args = parser.parse_args()

    dispatch = {
        "init": lambda: cmd_init(args.manifest),
        "validate": lambda: cmd_validate(args.manifest),
        "deploy": lambda: cmd_deploy(args.manifest, args.timeout),
        "promote": lambda: cmd_promote(args.manifest, args.mode),
        "teardown": lambda: cmd_teardown(args.manifest, args.clean),
        "status": lambda: cmd_status(args.manifest),
        "audit": lambda: cmd_audit(args.manifest),
    }

    try:
        dispatch[args.cmd]()
    except KeyboardInterrupt:
        print()
        die("interrupted")


if __name__ == "__main__":
    main()
