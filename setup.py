#!/usr/bin/env python3
"""
setup.py — First-run setup for Kin App.
Run once to set your password.
"""

import sys
import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import auth

def main():
    print("\n=== Echo Bloom — First Run Setup ===\n")

    if auth.is_configured():
        print("Already configured.")
        change = input("Change password? [y/N] ").strip().lower()
        if change != 'y':
            print("Nothing changed.")
            return

    while True:
        pw1 = getpass.getpass("Set password: ")
        if len(pw1) < 8:
            print("Password must be at least 8 characters.")
            continue
        pw2 = getpass.getpass("Confirm password: ")
        if pw1 != pw2:
            print("Passwords don't match. Try again.")
            continue
        break

    auth.set_password(pw1)
    print(f"\nPassword saved to {auth.CONFIG_FILE}")
    print("\nStart the app:")
    print("  uvicorn main:app --host 0.0.0.0 --port 8090\n")

if __name__ == "__main__":
    main()
