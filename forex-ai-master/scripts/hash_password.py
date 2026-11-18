#!/usr/bin/env python3
"""Generate the bcrypt hash for DASHBOARD_ADMIN_PASSWORD_HASH (the password itself is never stored)."""

import getpass

import bcrypt

pw = getpass.getpass("Dashboard admin password (min 12 chars): ")
if len(pw) < 12:
    raise SystemExit("password too short")
if pw != getpass.getpass("Repeat: "):
    raise SystemExit("passwords differ")
print(bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode())
