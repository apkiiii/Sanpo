#!/usr/bin/env python3
"""Print a random secret suitable for JWT_SECRET."""

import secrets

print(secrets.token_urlsafe(48))
