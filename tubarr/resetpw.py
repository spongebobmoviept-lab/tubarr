"""Reset the web UI's admin password (forgotten password). Signs out every browser session.

    docker exec -it tubarr python -m tubarr.resetpw            (asks for the new password, no echo)
    docker exec -it tubarr python -m tubarr.resetpw --user bob  (also renames the admin)

Only someone who can run commands in the container can do this, which is the same trust as owning /data."""
import argparse
import getpass
import sys

from . import auth


def main():
    ap = argparse.ArgumentParser(prog="python -m tubarr.resetpw", description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="new admin username (default: keep the current one, or 'admin')")
    a = ap.parse_args()
    if sys.stdin.isatty():
        pw = getpass.getpass("New admin password: ")
        if pw != getpass.getpass("Repeat it: "):
            sys.exit("The two passwords don't match.")
    else:
        pw = sys.stdin.readline().rstrip("\n")
    try:
        user = auth.set_password(pw, a.user)
    except auth.AuthError as e:
        sys.exit(e.message)
    print("Password reset for '%s'. Every browser session was signed out." % user)


if __name__ == "__main__":
    main()
