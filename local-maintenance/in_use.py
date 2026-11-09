"""in_use.py TARGET LSOF_OUTPUT: exit 0 if a process uses TARGET, 3 if none
does; any other code is no answer.

LSOF_OUTPUT is `lsof -nP -F n`. lsof escapes a name before it prints it:
`\\` for a backslash, `\\t` and the like, `^A` for other controls, and
`\\xNN` for any other byte it cannot print. Under a UTF-8 locale it prints
a multibyte character as is; under C it escapes each byte (sd:8240). So
TARGET is escaped both ways, and a name line that matches either counts.
"""

import os
import sys

_C_FORMS = {0x08: b"\\b", 0x09: b"\\t", 0x0A: b"\\n", 0x0C: b"\\f", 0x0D: b"\\r"}


def escape(raw, high_raw):
    out = bytearray()
    for c in raw:
        if c == 0x5C:
            out += b"\\\\"
        elif c in _C_FORMS:
            out += _C_FORMS[c]
        elif c < 0x20:
            out += b"^" + bytes([c + 0x40])
        elif c < 0x7F or (c >= 0x80 and high_raw):
            out.append(c)
        else:
            out += b"\\x%02x" % c
    return bytes(out)


def in_use(target, lines):
    raw = os.fsencode(target)
    forms = {escape(raw, False), escape(raw, True)}
    for line in lines:
        name = line.rstrip(b"\n")
        if name[:1] != b"n":
            continue
        name = name[1:]
        if any(name == f or name.startswith(f + b"/") for f in forms):
            return True
    return False


if __name__ == "__main__":
    with open(sys.argv[2], "rb") as listing:
        sys.exit(0 if in_use(sys.argv[1], listing) else 3)
