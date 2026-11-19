#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
查找微信数据目录路径字符串并转储附近内存。
"""
import ctypes
import os
from ctypes import wintypes

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}

kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE
ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wintypes.BOOL
CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wintypes.DWORD), ("_pad1", wintypes.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD), ("_pad2", wintypes.DWORD),
    ]


def get_pids():
    import subprocess
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True)
    pids = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        p = line.strip('"').split('","')
        if len(p) >= 5:
            pid = int(p[1])
            mem = int(p[4].replace(',', '').replace(' K', '').strip() or '0')
            pids.append((pid, mem))
    if not pids:
        raise RuntimeError("Weixin.exe 未运行")
    pids.sort(key=lambda x: x[1], reverse=True)
    return pids


def enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)) != 0:
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE and 0 < mbi.RegionSize < 500 * 1024 * 1024:
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if ReadProcessMemory(h, ctypes.c_void_p(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def find_and_dump(pid, regions, needle, output_dir, prefix):
    h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return 0
    count = 0
    try:
        for base, size in regions:
            data = read_mem(h, base, size)
            if not data:
                continue
            start = 0
            while True:
                idx = data.find(needle, start)
                if idx == -1:
                    break
                addr = base + idx
                dump_addr = max(0, addr - 4096)
                dump_size = 8192 + len(needle)
                chunk = read_mem(h, dump_addr, dump_size)
                if chunk:
                    out_path = os.path.join(output_dir, f"{prefix}_pid{pid}_addr{addr:016X}.bin")
                    with open(out_path, 'wb') as f:
                        f.write(chunk)
                    print(f"  dumped {out_path}")
                    count += 1
                start = idx + 1
    finally:
        CloseHandle(h)
    return count


def main():
    output_dir = "path_dumps"
    os.makedirs(output_dir, exist_ok=True)
    print(f"转储目录: {output_dir}")

    pids = get_pids()
    print(f"找到 {len(pids)} 个 Weixin.exe 进程")

    # 微信数据目录的多个片段
    needles = [
        b"wxid_example",
        b"xwechat_files",
        b"db_storage",
        b"D:\\RJ\\Weixinjilu",
    ]

    for pid, mem in pids:
        print(f"\n[*] PID={pid} ({mem//1024}MB)")
        h = OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not h:
            continue
        regions = enum_regions(h)
        CloseHandle(h)
        for needle in needles:
            count = find_and_dump(pid, regions, needle, output_dir, needle.decode('utf-8', errors='ignore').replace('\\', '_').replace(':', '_'))
            print(f"  '{needle.decode('utf-8', errors='ignore')}' 转储数: {count}")


if __name__ == "__main__":
    main()
