#!/usr/bin/env python3
"""
Transplant cookies + saved passwords from ARC -> BrowserOS.

Decrypts each blob with the ARC Keychain key, re-encrypts with the
BrowserOS Keychain key, merges into BrowserOS's profile (BrowserOS-only
rows are preserved; on conflict, ARC wins).

Requires: BrowserOS CLOSED. ARC may stay open (only read from /tmp copy).
Will trigger 2 macOS Keychain prompts (one per browser's Safe Storage).

Run:  python3 transplant_arc_auth.py            # do it
      python3 transplant_arc_auth.py --dry-run  # preview, no writes
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.padding import PKCS7

HOME = Path.home()
ARC_PROFILE = HOME / "Library/Application Support/Arc/User Data/Default"
BOS_PROFILE = HOME / "Library/Application Support/BrowserOS/Default"
WORK_DIR = Path("/tmp/auth_transplant")
TS = time.strftime("%Y%m%d-%H%M%S")

PBKDF2_SALT = b"saltysalt"
PBKDF2_ITER = 1003
PBKDF2_DKLEN = 16
AES_IV = b" " * 16


def keychain_password(service: str, account: str) -> bytes:
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-s", service, "-a", account]
    ).strip()


def derive_key(password: bytes) -> bytes:
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=PBKDF2_DKLEN,
        salt=PBKDF2_SALT,
        iterations=PBKDF2_ITER,
        backend=default_backend(),
    ).derive(password)


def decrypt(blob: bytes, key: bytes):
    if not blob or len(blob) < 3 or blob[:3] not in (b"v10", b"v11"):
        return None
    ct = blob[3:]
    if len(ct) == 0 or len(ct) % 16 != 0:
        return None
    try:
        cipher = Cipher(algorithms.AES(key), modes.CBC(AES_IV), backend=default_backend())
        pt = cipher.decryptor().update(ct) + cipher.decryptor().finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(pt) + unpadder.finalize()
    except Exception:
        return None


def encrypt(plain: bytes, key: bytes) -> bytes:
    padder = PKCS7(128).padder()
    pt = padder.update(plain) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(AES_IV), backend=default_backend())
    return b"v10" + cipher.encryptor().update(pt) + cipher.encryptor().finalize()


def reencrypt(blob, src_key, dst_key):
    if blob is None:
        return None, "null"
    pt = decrypt(bytes(blob), src_key)
    if pt is None:
        return None, "decrypt_fail"
    try:
        return encrypt(pt, dst_key), "ok"
    except Exception:
        return None, "encrypt_fail"


def is_running(name_pattern: str) -> bool:
    return subprocess.run(["pgrep", "-x", name_pattern], capture_output=True).returncode == 0


def get_cols(db: sqlite3.Connection, table: str):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]


def transplant_table(arc_db_path, bos_db_path, table, encrypted_col, dry_run):
    """Merge encrypted rows from ARC's db into BrowserOS's db. Schema-robust."""
    print(f"\n=== {table} ({encrypted_col}) ===")
    work = WORK_DIR / bos_db_path.name
    arc_snapshot = WORK_DIR / f"arc-{arc_db_path.name}"
    backup = bos_db_path.with_name(bos_db_path.name + f".bak-{TS}")

    # Snapshot ARC's DB (it's live; avoid lock contention by reading a copy)
    shutil.copy2(arc_db_path, arc_snapshot)

    if not dry_run:
        print(f"  backup -> {backup}")
        shutil.copy2(bos_db_path, backup)
        print(f"  work   -> {work}")
        shutil.copy2(bos_db_path, work)
    else:
        print(f"  [dry-run] skipping backup")
        shutil.copy2(bos_db_path, work)  # work copy is always needed for column intersection

    arc_db = sqlite3.connect(f"file:{arc_snapshot}?mode=ro", uri=True)
    bos_db = sqlite3.connect(work)
    arc_db.row_factory = sqlite3.Row
    bos_db.row_factory = sqlite3.Row

    arc_cols = get_cols(arc_db, table)
    bos_cols = get_cols(bos_db, table)
    common = [c for c in arc_cols if c in bos_cols]
    dropped = [c for c in arc_cols if c not in bos_cols]
    if dropped:
        print(f"  dropping ARC-only columns: {dropped}")

    rows = arc_db.execute(f"SELECT {','.join(arc_cols)} FROM {table}").fetchall()
    print(f"  ARC rows: {len(rows)}")

    inserted = updated = failed = skipped = 0
    failures_by_reason = {}
    for r in rows:
        d = {k: r[k] for k in common}
        ev = d.get(encrypted_col)
        if not ev:
            skipped += 1
            continue
        new_ev, reason = reencrypt(ev, ARC_KEY, BOS_KEY)
        if new_ev is None:
            failed += 1
            failures_by_reason[reason] = failures_by_reason.get(reason, 0) + 1
            continue
        d[encrypted_col] = new_ev
        # Clear plaintext value if a parallel column exists (cookies have `value`)
        if "value" in d and encrypted_col != "value":
            d["value"] = ""

        cols_str = ",".join(f'"{c}"' for c in d.keys())
        ph = ",".join(["?"] * len(d))
        try:
            bos_db.execute(f"INSERT OR REPLACE INTO {table} ({cols_str}) VALUES ({ph})", list(d.values()))
            inserted += 1
        except sqlite3.IntegrityError as e:
            failed += 1
            failures_by_reason[f"sql:{e}"] = failures_by_reason.get(f"sql:{e}", 0) + 1
        except Exception as e:
            failed += 1
            failures_by_reason[f"err:{type(e).__name__}"] = failures_by_reason.get(f"err:{type(e).__name__}", 0) + 1

    if not dry_run:
        bos_db.commit()
    bos_db.close()
    arc_db.close()

    print(f"  result: inserted/replaced={inserted}  failed={failed}  skipped(no encrypted_value)={skipped}")
    if failures_by_reason:
        print(f"  failure breakdown: {failures_by_reason}")

    if not dry_run:
        os.replace(work, bos_db_path)
        print(f"  wrote -> {bos_db_path}")
    else:
        work.unlink(missing_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="parse + decrypt but don't write")
    args = ap.parse_args()

    if is_running("BrowserOS"):
        print("ABORT: BrowserOS is running. Quit it (Cmd+Q) and re-run.", file=sys.stderr)
        sys.exit(1)

    WORK_DIR.mkdir(exist_ok=True)

    print("Reading Keychain (you'll see 2 prompts)...")
    global ARC_KEY, BOS_KEY
    ARC_KEY = derive_key(keychain_password("Arc Safe Storage", "Arc"))
    BOS_KEY = derive_key(keychain_password("BrowserOS Safe Storage", "BrowserOS"))
    print(f"  ARC key fp:       {ARC_KEY.hex()[:8]}...")
    print(f"  BrowserOS key fp: {BOS_KEY.hex()[:8]}...")

    transplant_table(ARC_PROFILE / "Cookies", BOS_PROFILE / "Cookies", "cookies", "encrypted_value", args.dry_run)
    transplant_table(ARC_PROFILE / "Login Data", BOS_PROFILE / "Login Data", "logins", "password_value", args.dry_run)

    print("\nDone." + (" (dry-run)" if args.dry_run else ""))
    if not args.dry_run:
        print(f"Backups: {BOS_PROFILE}/Cookies.bak-{TS}  and  Login Data.bak-{TS}")
        print("Reopen BrowserOS and try a site you were logged into in ARC.")


if __name__ == "__main__":
    main()
