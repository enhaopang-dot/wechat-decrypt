#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分析 WCDB 内存转储，寻找 passphrase。
"""
import hashlib
import hmac as hmac_mod
import json
import os
import re
import struct
import sys

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
KDF_ITER = 256000

DB_DIR = ""  # 见 config.example.json，按你的实际路径填


def collect_db_files(db_dir):
    db_files = []
    for root, dirs, files in os.walk(db_dir):
        for name in files:
            if not name.endswith(".db") or name.endswith("-wal") or name.endswith("-shm"):
                continue
            path = os.path.join(root, name)
            size = os.path.getsize(path)
            if size < PAGE_SZ:
                continue
            with open(path, "rb") as f:
                page1 = f.read(PAGE_SZ)
            rel = os.path.relpath(path, db_dir)
            salt = page1[:SALT_SZ].hex()
            db_files.append((rel, path, size, salt, page1))
    return db_files


def verify_passphrase_key(passphrase, db_page1):
    salt = db_page1[:SALT_SZ]
    key_material = hashlib.pbkdf2_hmac("sha512", passphrase, salt, KDF_ITER, dklen=KEY_SZ * 2)
    enc_key = key_material[:KEY_SZ]
    hmac_key = key_material[KEY_SZ:]
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(hmac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def analyze_dump(dump_path, db_files, sample_page1):
    print(f"\n分析: {dump_path}")
    with open(dump_path, "rb") as f:
        data = f.read()
    print(f"  大小: {len(data)} 字节")

    # 1. 找 64 位 hex 字符串
    hex_re = re.compile(rb'[0-9a-fA-F]{64}')
    hex_matches = list(hex_re.finditer(data))
    print(f"  64 位 hex 字符串数: {len(hex_matches)}")
    tested = 0
    for m in hex_matches:
        try:
            passphrase = bytes.fromhex(m.group(0).decode('ascii'))
        except Exception:
            continue
        if len(passphrase) != 32:
            continue
        tested += 1
        if verify_passphrase_key(passphrase, sample_page1):
            print(f"  [FOUND] 有效 passphrase (hex) @ offset {m.start()}: {m.group(0).decode('ascii')}")
            return passphrase
    print(f"  已测试 {tested} 个 hex 候选，无匹配")

    # 2. 滑动窗口测试所有 32 字节
    print("  开始滑动窗口测试 32 字节候选...")
    for i in range(len(data) - 32):
        passphrase = data[i:i+32]
        if verify_passphrase_key(passphrase, sample_page1):
            print(f"  [FOUND] 有效 passphrase (raw) @ offset {i}: {passphrase.hex()}")
            return passphrase
    print("  滑动窗口无匹配")
    return None


def main():
    dump_dir = "wcdb_dumps"
    db_files = collect_db_files(DB_DIR)
    print(f"找到 {len(db_files)} 个数据库")
    sample_page1 = db_files[0][4]

    found = None
    for name in sorted(os.listdir(dump_dir)):
        if not name.endswith(".bin"):
            continue
        dump_path = os.path.join(dump_dir, name)
        passphrase = analyze_dump(dump_path, db_files, sample_page1)
        if passphrase:
            found = passphrase
            break

    if found:
        print(f"\n成功找到 passphrase: {found.hex()}")
        result = {}
        for rel, path, sz, salt_hex, page1 in db_files:
            salt = bytes.fromhex(salt_hex)
            key_material = hashlib.pbkdf2_hmac("sha512", found, salt, KDF_ITER, dklen=KEY_SZ * 2)
            enc_key = key_material[:KEY_SZ]
            hmac_key = key_material[KEY_SZ:]
            if verify_passphrase_key(found, page1):
                result[rel] = {
                    "enc_key": enc_key.hex(),
                    "hmac_key": hmac_key.hex(),
                    "salt": salt_hex,
                    "size_mb": round(sz / 1024 / 1024, 1)
                }
                print(f"  OK: {rel}")
        result["_db_dir"] = DB_DIR
        result["_mode"] = "passphrase"
        out_file = "all_keys.json"
        with open(out_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n密钥已保存到: {out_file}")
    else:
        print("\n未能从转储中找到 passphrase")


if __name__ == "__main__":
    main()
