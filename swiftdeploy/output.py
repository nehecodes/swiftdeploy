import sys


def _c(code, text):
    return f"\033[{code}m{text}\033[0m"


def ok(msg):
    print(f"  \033[32m✔\033[0m {msg}")


def err(msg):
    print(f"  \033[31m✘\033[0m {msg}", file=sys.stderr)


def info(msg):
    print(f"  \033[36m→\033[0m {msg}")


def step(msg):
    print(f"  · {msg}")


def check(label, passed, detail=""):
    status = _c(32, "PASS") if passed else _c(31, "FAIL")
    suffix = f"  \033[2m{detail}\033[0m" if detail else ""
    print(f"  [{status}] {label}{suffix}")


def die(msg):
    err(msg)
    sys.exit(1)
