import argparse
from .output import die
from .commands.init import cmd_init
from .commands.validate import cmd_validate
from .commands.deploy import cmd_deploy
from .commands.promote import cmd_promote
from .commands.teardown import cmd_teardown


def main():
    parser = argparse.ArgumentParser(prog="swiftdeploy", description=__doc__)
    parser.add_argument("--manifest", default="manifest.yaml", metavar="FILE")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND", required=True)

    sub.add_parser("init")
    sub.add_parser("validate")
    p = sub.add_parser("deploy")
    p.add_argument("--timeout", type=int, default=60)
    p = sub.add_parser("promote")
    p.add_argument("mode", choices=["canary", "stable"])
    p = sub.add_parser("teardown")
    p.add_argument("--clean", action="store_true")

    args = parser.parse_args()

    dispatch = {
        "init": lambda: cmd_init(args.manifest),
        "validate": lambda: cmd_validate(args.manifest),
        "deploy": lambda: cmd_deploy(args.manifest, args.timeout),
        "promote": lambda: cmd_promote(args.manifest, args.mode),
        "teardown": lambda: cmd_teardown(args.manifest, args.clean),
    }

    try:
        dispatch[args.cmd]()
    except KeyboardInterrupt:
        print()
        die("interrupted")


if __name__ == "__main__":
    main()
