"""
One-time setup: generate Polymarket API credentials from your wallet private key.

Usage:
  1. Set POLY_PRIVATE_KEY in .env to your wallet's private key (0x...)
  2. Run:  python setup_credentials.py
  3. The script will generate API key/secret/passphrase and update .env

Only needs to be run once. After that, the bot uses the saved credentials.
"""

import os
import sys
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENV_PATH = Path(__file__).parent / ".env"

POLY_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def update_env(key: str, value: str):
    """Update a single key in .env, preserving all other content."""
    text = ENV_PATH.read_text()
    pattern = rf"^{re.escape(key)}=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, text, flags=re.MULTILINE):
        text = re.sub(pattern, replacement, text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{replacement}\n"
    ENV_PATH.write_text(text)


def main():
    private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if not private_key:
        print("ERROR: Set POLY_PRIVATE_KEY in .env first.")
        print("  Open .env and paste your wallet private key (0x...)")
        sys.exit(1)

    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    print(f"Connecting to Polymarket CLOB ({POLY_HOST})...")

    from py_clob_client.client import ClobClient

    client = ClobClient(
        host=POLY_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
    )

    print(f"Wallet address: {client.get_address()}")

    # Generate (or derive existing) API credentials
    print("Generating API credentials...")
    creds = client.create_or_derive_api_creds()

    print(f"  API Key:        {creds.api_key}")
    print(f"  API Secret:     {creds.api_secret[:8]}...  (truncated)")
    print(f"  API Passphrase: {creds.api_passphrase[:8]}...  (truncated)")

    # Write to .env
    update_env("POLY_API_KEY", creds.api_key)
    update_env("POLY_API_SECRET", creds.api_secret)
    update_env("POLY_PASSPHRASE", creds.api_passphrase)

    print("\n.env updated with API credentials.")

    # Verify connectivity
    print("\nVerifying API connection...")
    ok = client.get_ok()
    print(f"  Server health: {ok}")

    # Set up API auth and check balance
    client.set_api_creds(creds)
    try:
        bal = client.get_balance_allowance()
        print(f"  Balance info: {bal}")
    except Exception as e:
        print(f"  Balance check skipped: {e}")

    print("\nSetup complete! You can now run the bot with PAPER_TRADING=false.")


if __name__ == "__main__":
    main()
