"""Print an argon2id hash for TUBARR_ADMIN_PASSWORD_HASH (unattended installs). The password is read without echo
and never stored or printed.

    docker run --rm -it ghcr.io/spongebobmoviept-lab/tubarr:latest python -m tubarr.hashpw

Put the printed line in your .env as TUBARR_ADMIN_PASSWORD_HASH='...' (single quotes: the hash contains $ signs;
in docker-compose.yml double every $ as $$)."""
import getpass
import sys

from argon2 import PasswordHasher

from .auth import MIN_PASSWORD


def main():
    if sys.stdin.isatty():
        pw = getpass.getpass("New admin password: ")
        if pw != getpass.getpass("Repeat it: "):
            sys.exit("The two passwords don't match.")
    else:
        pw = sys.stdin.readline().rstrip("\n")
    if len(pw) < MIN_PASSWORD:
        sys.exit("Use at least %d characters." % MIN_PASSWORD)
    print(PasswordHasher().hash(pw))


if __name__ == "__main__":
    main()
