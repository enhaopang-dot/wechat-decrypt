import ctypes
import os
import sys
import time

WORK_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(WORK_DIR, "dump_wcdb_cipher.py")
DONE_FLAG = os.path.join(WORK_DIR, ".wcdb_dump_done")
RESULT_FILE = os.path.join(WORK_DIR, "wcdb_dump_result.txt")

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def main():
    for f in (DONE_FLAG, RESULT_FILE):
        if os.path.exists(f):
            os.remove(f)

    if is_admin():
        import subprocess
        with open(RESULT_FILE, "wb") as f:
            proc = subprocess.Popen(
                [sys.executable, SCRIPT],
                cwd=WORK_DIR,
                stdout=f,
                stderr=subprocess.STDOUT,
            )
            proc.wait()
        with open(DONE_FLAG, "w", encoding="utf-8") as f:
            f.write(str(proc.returncode))
    else:
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, f'"{__file__}"', WORK_DIR, 0
        )
        for i in range(300):
            if os.path.exists(DONE_FLAG):
                break
            time.sleep(1)
        if os.path.exists(RESULT_FILE):
            with open(RESULT_FILE, "rb") as f:
                raw = f.read()
            try:
                print(raw.decode("utf-8"))
            except UnicodeDecodeError:
                print(raw.decode("gbk", errors="replace"))
        else:
            print("等待管理员子进程超时或未能生成结果文件")

if __name__ == "__main__":
    main()
