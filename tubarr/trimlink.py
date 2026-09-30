"""The Tubarr <-> Trimarr link token: created automatically, so nobody has to generate or paste a secret.

docker-compose.yml gives both containers a small shared volume, `trimarr-link` (an in-memory tmpfs owned by
PUID:PGID, mode 750), mounted read-write in Tubarr and read-only in Trimarr at /link. On its first start Tubarr writes
a random 32-byte token (64 hex characters) to /link/trimarr.token (mode 640), atomically and without ever replacing a
valid token that is already there; after that the same token is reused. Trimarr reads it from the same file and sends
nothing without it (fails closed). Only these two containers mount the volume.

TRIMARR_TOKEN in the environment (the same value for both containers) still overrides the file, for advanced setups.
The token is never logged, never shown in the UI and never returned by the API.
"""
import os
import re
import secrets

LINK_DIR = os.environ.get("TRIMARR_LINK_DIR", "/link")
TOKEN_NAME = "trimarr.token"
FILE_MODE = 0o640
MIN_LEN = 16
MAX_LEN = 512
_VALID = re.compile(r"[\x21-\x7e]{%d,%d}" % (MIN_LEN, MAX_LEN))     # printable, no spaces


def token_path(link_dir=None):
    return os.path.join(link_dir or LINK_DIR, TOKEN_NAME)


def read_token(link_dir=None):
    """The token in the link file, or '' if there's none (or it isn't a usable token)."""
    try:
        fd = os.open(token_path(link_dir), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return ""
    try:
        raw = os.read(fd, MAX_LEN + 2)
    except OSError:
        return ""
    finally:
        os.close(fd)
    tok = raw.decode("ascii", "replace").strip()
    return tok if _VALID.fullmatch(tok) else ""


def ensure_token(link_dir=None):
    """Return the link token, creating it first if there's no valid one yet. '' when the link folder isn't there
    (no shared volume: Tubarr running without Trimarr) or can't be written.

    Safe against the web app and a second process starting at the same moment: the token is written to a private
    temporary file, then hard-linked into place, which never replaces an existing file; whoever loses the race reads
    the winner's token."""
    d = link_dir or LINK_DIR
    tok = read_token(d)
    if tok:
        return tok
    if not os.path.isdir(d) or not os.access(d, os.W_OK):
        return ""
    final = token_path(d)
    tmp = os.path.join(d, ".%s.%d.%s" % (TOKEN_NAME, os.getpid(), secrets.token_hex(6)))
    new = secrets.token_hex(32)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
        try:
            os.write(fd, (new + "\n").encode("ascii"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, FILE_MODE)                          # whatever the umask
        if os.path.lexists(final):
            if read_token(d):                             # another process made it just now: use that one
                return read_token(d)
            os.replace(tmp, final)                        # there, but unusable (empty, damaged): replace it
        else:
            try:
                os.link(tmp, final)                       # atomic and never overwrites
            except FileExistsError:
                pass                                      # another process won the race: use its token
            except OSError:
                if not os.path.lexists(final):            # a filesystem without hard links
                    os.replace(tmp, final)
    except OSError:
        return ""
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return read_token(d)
