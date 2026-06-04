#!/usr/bin/env python3
"""Seed the Bitcart store with the folk-hero demo products (run on the host).

Argus copies this script next to a ``manifest.json`` and the card PNGs into
``generated/<net>/bitcart/products/``; the generated ``deploy-bitcart.sh``
wrapper runs it after ``deploy.sh``, with the Bitcart admin credentials and
backend port already in the environment.

It is intentionally dependency-free (stdlib + ``curl``) so it runs on a bare
host. It is idempotent: products already present in the store (matched by
name) are left untouched, so re-running a deploy never duplicates them.

Behaviour (per the agreed design):
  * targets the admin's FIRST existing store (does not create one); if the
    admin has no store yet, it logs a warning and exits 0.
  * prices are in sats — Bitcart has a native ``SATS`` currency (divisibility
    0), so the store's default currency is switched to ``SATS`` when it is
    still at the ``USD`` default. If the operator already chose another
    currency it is left alone (with a warning that prices are interpreted in
    that currency).

Env:
  BITCART_ADMIN_EMAIL, BITCART_ADMIN_PASSWORD   -- admin login (required)
  BITCART_BACKEND_PORT                          -- backend port (default 8000)
  ARGUS_BITCART_API_BASE                        -- override the API base URL
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.json"


def log(msg: str) -> None:
    print(f"[seed] {msg}", flush=True)


def api_base() -> str:
    override = os.environ.get("ARGUS_BITCART_API_BASE")
    if override:
        return override.rstrip("/")
    # With REVERSEPROXY=none the API is served without the /api prefix that
    # nginx would otherwise add (same convention deploy.sh uses).
    port = os.environ.get("BITCART_BACKEND_PORT") or "8000"
    return f"http://localhost:{port}"


def curl(*args: str, timeout: int = 60) -> tuple[int, str]:
    """Run curl, returning (http_status, body). Status 0 means curl failed."""
    try:
        out = subprocess.run(
            ["curl", "-s", "-w", "\n%{http_code}", *args],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"curl error: {exc}")
        return 0, ""
    body, _, code = out.rpartition("\n")
    return (int(code) if code.isdigit() else 0), body


def get_json(code: int, body: str):
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def wait_for_api(base: str, *, attempts: int = 60, delay: int = 5) -> bool:
    for i in range(attempts):
        code, _ = curl(base + "/cryptos", "--max-time", "5")
        if code == 200:
            return True
        if i == 0:
            log("waiting for the Bitcart backend API to accept connections...")
        time.sleep(delay)
    return False


def get_token(base: str, email: str, password: str) -> str | None:
    payload = json.dumps(
        {"email": email, "password": password, "permissions": ["full_control"]}
    )
    for _ in range(12):
        code, body = curl(
            "-X", "POST", base + "/token",
            "-H", "Content-Type: application/json", "--data-binary", payload,
        )
        data = get_json(code, body)
        if code == 200 and isinstance(data, dict):
            tok = data.get("access_token") or data.get("id")
            if tok:
                return tok
        time.sleep(5)
    return None


def first_store(base: str, token: str) -> dict | None:
    code, body = curl(base + "/stores", "-H", f"Authorization: Bearer {token}")
    data = get_json(code, body)
    if code != 200:
        log(f"could not list stores (HTTP {code}): {body[:200]}")
        return None
    result = data.get("result") if isinstance(data, dict) else data
    if not result:
        return None
    return result[0]


def ensure_sats_currency(base: str, token: str, store: dict) -> str:
    """Switch a still-default (USD) store to SATS so prices mean sats."""
    cur = (store.get("default_currency") or "").upper()
    sid = store["id"]
    if cur == "SATS":
        return "SATS"
    if cur and cur != "USD":
        log(f"store currency is {cur}; leaving it. Prices will be in {cur}, "
            f"not sats.")
        return cur
    code, body = curl(
        "-X", "PATCH", f"{base}/stores/{sid}",
        "-H", "Content-Type: application/json",
        "-H", f"Authorization: Bearer {token}",
        "--data-binary", json.dumps({"default_currency": "SATS"}),
    )
    if code == 200:
        log("set store default currency to SATS.")
        return "SATS"
    log(f"WARNING: could not set store currency to SATS (HTTP {code}): "
        f"{body[:200]}. Prices may be interpreted as USD.")
    return cur or "USD"


def existing_names(base: str, token: str, store_id: str) -> set[str]:
    code, body = curl(
        f"{base}/products?store={store_id}&limit=250",
        "-H", f"Authorization: Bearer {token}",
    )
    data = get_json(code, body)
    if code != 200 or not isinstance(data, dict):
        return set()
    return {p.get("name") for p in (data.get("result") or [])}


def create_product(base: str, token: str, store_id: str, item: dict) -> bool:
    img = HERE / str(item["image"])
    if not img.is_file():
        log(f"WARNING: image missing for {item['name']}: {img}")
        return False
    data = json.dumps({
        "name": item["name"],
        "price": item["price_sats"],
        "quantity": -1,  # unlimited stock
        "store_id": store_id,
        "description": item.get("description", ""),
    })
    code, body = curl(
        "-X", "POST", base + "/products",
        "-H", f"Authorization: Bearer {token}",
        "-F", f"data={data}",
        "-F", f"image=@{img};type=image/png",
    )
    if code in (200, 201):
        log(f"created '{item['name']}' ({item['price_sats']} sats).")
        return True
    log(f"WARNING: failed to create '{item['name']}' (HTTP {code}): "
        f"{body[:300]}")
    return False


def main() -> int:
    base = api_base()
    email = os.environ.get("BITCART_ADMIN_EMAIL", "")
    password = os.environ.get("BITCART_ADMIN_PASSWORD", "")
    if not email or not password:
        log("BITCART_ADMIN_EMAIL / BITCART_ADMIN_PASSWORD not set; skipping.")
        return 0
    if not MANIFEST.is_file():
        log(f"manifest not found at {MANIFEST}; skipping.")
        return 0
    items = json.loads(MANIFEST.read_text())

    if not wait_for_api(base):
        log("backend API did not come up; skipping seeding (non-fatal).")
        return 0
    token = get_token(base, email, password)
    if not token:
        log("could not obtain an admin token; skipping seeding (non-fatal).")
        return 0
    store = first_store(base, token)
    if not store:
        log("admin has no store yet; nothing to seed into (non-fatal).")
        return 0
    sid = store["id"]
    log(f"seeding into store '{store.get('name')}' ({sid}).")
    ensure_sats_currency(base, token, store)

    have = existing_names(base, token, sid)
    created = skipped = 0
    for item in items:
        if item["name"] in have:
            skipped += 1
            log(f"'{item['name']}' already present; skipping.")
            continue
        if create_product(base, token, sid, item):
            created += 1
    log(f"done: {created} created, {skipped} already present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
