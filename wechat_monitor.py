#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信实时监控 watchdog（2026-10-12 固化，多开版）
- 扫描多个微信数据根目录，自动发现已登录账号
- 每账号: wcdb-key-tool 提密钥 → 独立端口起 monitor_web.py → 固定端口
- 微信退出自动清理对应 monitor；重登自动重新接入
- 支持开机自启: python wechat_monitor.py install（任务计划程序，用户登录触发）

用法:
  wechat_monitor.py daemon      常驻守护（自启任务调这个）
  wechat_monitor.py install     注册开机自启
  wechat_monitor.py uninstall   移除开机自启
  wechat_monitor.py status      查看各账号监控状态
  wechat_monitor.py watch [wxid|chatroom...]  监听指定会话新消息（默认商务三会话）
  wechat_monitor.py logs        查看 watchdog 日志

权威源: D:/Claude/配置/.claude/memory 的 wechat-4.1.x-realtime-monitor + MEMORY-dev-recipes
"""
import os, sys, json, time, glob, socket, signal, subprocess, urllib.request, io

BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE, 'wechat-monitor-runtime')
os.makedirs(LOG_DIR, exist_ok=True)

def _log(msg):
    line = f'[{time.strftime("%m-%d %H:%M:%S")}] {msg}'
    print(line, flush=True)
    try:
        with open(os.path.join(LOG_DIR, 'watchdog.log'), 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass

# ---- 本机配置 ----
WCDB_TOOL = r"D:\Claude\配置\.claude\tools\wcdb-key-tool\wcdb_key_tool_windows.py"
KEYS_DIR = os.path.join(LOG_DIR, 'keys')
os.makedirs(KEYS_DIR, exist_ok=True)
MONITOR_DIR = BASE  # 本文件已迁入 wechat-decrypt-clean 目录
DATA_ROOTS = [
    r""  # 见 config.example.json，按你的实际路径填,
    os.path.expanduser(r"~\Documents\xwechat_files"),
]
BASE_PORT = 5678
LOOP_SEC = 30
_NOWIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)  # pythonw 下 spawn 控制台程序必加，否则每调用闪一个黑框
LOGIN_STATE_FILE = os.path.join(LOG_DIR, 'login_state.json')

DEFAULT_TARGETS = {
    'wxid_example': '张浩峰',
    'wxid_example': '小王(益然恩)',
    '45660251895@chatroom': '益然恩三人群',
}


def _port_open(p, host='127.0.0.1'):
    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect((host, p))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _api(port, path):
    with urllib.request.urlopen(f'http://localhost:{port}{path}', timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def scan_accounts():
    """发现所有微信账号数据目录（不管是否登录）"""
    accounts = {}
    for root in DATA_ROOTS:
        for db_dir in sorted(glob.glob(os.path.join(root, '*', 'db_storage'))):
            wxid_dir = os.path.dirname(db_dir)
            wxid = os.path.basename(wxid_dir)
            if wxid.startswith('wxid_') or wxid.endswith('_ca93'):
                accounts[wxid] = {'wxid': wxid, 'base_dir': wxid_dir, 'db_dir': db_dir}
    return accounts


def _port_map():
    pf = os.path.join(LOG_DIR, 'port_map.json')
    try:
        with open(pf, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_port_map(m):
    with open(os.path.join(LOG_DIR, 'port_map.json'), 'w', encoding='utf-8') as f:
        json.dump(m, f, indent=1)


def account_port(wxid):
    """端口持久化映射: 已知账号永不变, 新账号取 max+1（跳过被占端口）"""
    m = _port_map()
    if wxid in m:
        return m[wxid]
    used = set(m.values())
    port = BASE_PORT
    while port in used or _port_open(port):
        port += 1
    m[wxid] = port
    _save_port_map(m)
    return port


def runtime_dir(wxid):
    d = os.path.join(LOG_DIR, wxid)
    os.makedirs(d, exist_ok=True)
    return d


def extract_keys(acct):
    """wcdb-key-tool extract；成功=微信在跑。返回 keys 文件路径或 None。
    已登录账号成功提取后，120 秒内用 keys 文件存在性判断微信是否还在跑（避免每 30 秒真 extract 一次）。
    注意：这里不能只看 mtime——成功提取后文件不再变动，mtime 会一直停在首次提取时刻。"""
    out = os.path.join(KEYS_DIR, f'keys_{acct["wxid"]}.json')
    bak = out + '.bak'
    # 最近 120 秒成功提取过 → 视为微信仍在跑（文件存在即登录，靠 .bak 兜底路径区分多开）
    try:
        if os.path.exists(out) and os.path.getsize(out) > 500 and (time.time() - os.path.getmtime(out)) < 120:
            return out
    except OSError:
        pass
    if os.path.exists(out):
        try:
            os.replace(out, bak)
        except OSError:
            pass
    try:
        r = subprocess.run(
            [sys.executable, WCDB_TOOL, 'extract',
             '--db-dir', acct['db_dir'], '--output', out],
            capture_output=True, timeout=180,
            encoding='utf-8', errors='ignore', creationflags=_NOWIN)
        if os.path.exists(out) and os.path.getsize(out) > 500:
            # touch mtime，让上面的 120 秒短窗口知道"刚提取过"
            try:
                os.utime(out, None)
            except OSError:
                pass
            return out
        _log(f'[{acct["wxid"]}] extract失败: {(r.stdout or r.stderr)[-200:]}')
        return None
    except Exception as e:
        _log(f'[{acct["wxid"]}] extract异常: {e}')
        return None


def write_instance_config(acct, keys_path):
    rd = runtime_dir(acct['wxid'])
    cfg = {
        'db_dir': acct['db_dir'],
        'keys_file': keys_path,
        'decrypted_dir': os.path.join(rd, 'decrypted'),
        'decoded_image_dir': os.path.join(rd, 'decoded_images'),
        'wechat_process': 'Weixin.exe',
        'transcription_backend': 'local',
        'output_root': os.path.join(rd, 'export'),
    }
    with open(os.path.join(rd, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def read_pid(wxid):
    pf = os.path.join(runtime_dir(wxid), 'monitor.pid')
    try:
        with open(pf) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_pid(wxid, pid):
    with open(os.path.join(runtime_dir(wxid), 'monitor.pid'), 'w') as f:
        f.write(str(pid))


def kill_pid(pid):
    if not pid:
        return
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=15, creationflags=_NOWIN)
    except Exception as e:
        _log(f'taskkill {pid} 异常: {e}')


def _pid_is_our_monitor(pid):
    """该 pid 的命令行必须是我们的 monitor_web.py（防误杀外来进程）"""
    if not pid:
        return False
    try:
        r = subprocess.run(
            ['wmic', 'process', 'where', f'ProcessId={pid}', 'get', 'CommandLine', '/value'],
            capture_output=True, text=True, encoding='gbk', errors='ignore', timeout=15,
            creationflags=_NOWIN)
        return 'monitor_web.py' in (r.stdout or '')
    except Exception:
        return False


def monitor_alive(wxid, port):
    """返回 'ok' | 'dead' | 'degraded' | 'foreign'"""
    pid = read_pid(wxid)
    if pid and _pid_is_our_monitor(pid):
        if _port_open(port):
            try:
                _api(port, '/api/sessions?source=all')
                return 'ok'
            except Exception:
                return 'degraded'
        return 'dead'
    if pid and not _pid_is_our_monitor(pid):
        try:
            os.remove(os.path.join(runtime_dir(wxid), 'monitor.pid'))
        except OSError:
            pass
        pid = None
    if _port_open(port):
        return 'foreign'  # 端口被外来进程占用,不碰
    return 'dead'


def start_monitor(acct, keys_path):
    port = account_port(acct['wxid'])
    write_instance_config(acct, keys_path)
    rd = runtime_dir(acct['wxid'])
    logf = open(os.path.join(rd, 'monitor.log'), 'a', encoding='utf-8', errors='ignore')
    env = dict(os.environ)
    env['WECHAT_DECRYPT_APP_DIR'] = rd
    env['WECHAT_DECRYPT_NONINTERACTIVE'] = '1'
    env['WCM_PORT'] = str(port)
    env['WCM_HEADLESS'] = '1'  # 只供 API 不显示页面（用户拍板：有中台，看页面用 WCM_HEADLESS=0 手动起）
    p = subprocess.Popen(
        [sys.executable, os.path.join(MONITOR_DIR, 'monitor_web.py')],
        cwd=MONITOR_DIR, env=env,
        stdout=logf, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.executable.lower().endswith('pythonw.exe') else 0)
    write_pid(acct['wxid'], p.pid)
    _log(f'[{acct["wxid"]}] 启动 monitor pid={p.pid} port={port}')
    return p.pid


def stop_monitor(wxid, reason=''):
    pid = read_pid(wxid)
    kill_pid(pid)
    pf = os.path.join(runtime_dir(wxid), 'monitor.pid')
    try:
        os.remove(pf)
    except OSError:
        pass
    _log(f'[{wxid}] 停止 monitor {reason}')


def tick():
    """一轮扫描: 发现账号 → 该起的起，该停的停"""
    accounts = scan_accounts()
    for wxid, acct in accounts.items():
        port = account_port(wxid)
        state = monitor_alive(wxid, port)
        if state == 'ok':
            # monitor 在跑；但要顺带探测微信是否仍登录，刷新 login_state 供中台三态。
            # 用短窗口判断：刚提取过(<120s)直接复用，否则真 extract 一次探活。
            keys = extract_keys(acct)
            write_login_state(wxid, bool(keys))
            continue  # 健康,不动
        if state == 'degraded':
            _log(f'[{wxid}] monitor 异常,重启')
            stop_monitor(wxid, '(degraded)')
        elif state == 'foreign':
            _log(f'[{wxid}] 端口{port}被外来进程占用,跳过（请人工处理）')
            continue
        # dead: 要么从没起,要么已死 → 尝试密钥提取(=检测微信是否登录该账号)
        keys = extract_keys(acct)
        if keys:
            BAK_TRIED.discard(wxid)  # 拿到新密钥，旧的兜底失败标记作废
            write_login_state(wxid, True)   # extract 成功 = 微信在跑且登录了该账号
        else:
            write_login_state(wxid, False)  # extract 失败 = 微信未登录（或多开副号扫不到，保守判未登录）
        if not keys and wxid not in BAK_TRIED:
            # 多开/副号实例可能扫不到 Config.Cipher（extract 必失败）；
            # .bak 里是上次成功提取的账号级密钥（长期有效），兜底拉起
            kf = os.path.join(KEYS_DIR, f'keys_{wxid}.json')
            bak = kf + '.bak'
            if os.path.exists(bak) and os.path.getsize(bak) > 500:
                import shutil
                shutil.copy2(bak, kf)
                start_monitor(acct, kf)
                time.sleep(5)
                if monitor_alive(wxid, port) == 'ok':
                    _log(f'[{wxid}] extract 失败，.bak 密钥兜底拉起成功')
                else:
                    BAK_TRIED.add(wxid)
                    stop_monitor(wxid, '(bak兜底失败)')
                    _log(f'[{wxid}] .bak 兜底拉起失败（密钥可能已失效：微信重装/换账号？），本进程不再重试')
                continue
        if not keys:
            if state != 'dead' or read_pid(wxid):
                pass  # 微信未登录,保持停止
            continue
        start_monitor(acct, keys)
        time.sleep(3)

BAK_TRIED = set()  # bak 兜底试过且失败的账号（进程内，防死循环）

def write_login_state(wxid, logged_in):
    """把「该账号微信是否登录」写到 login_state.json，供中台顶栏三态（绿/黄/红）读取。
    只在状态变化时写文件，避免每 30 秒无谓 IO。"""
    try:
        try:
            st = json.load(open(LOGIN_STATE_FILE, encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            st = {}
        if st.get(wxid) != bool(logged_in):
            st[wxid] = bool(logged_in)
            with open(LOGIN_STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(st, f, ensure_ascii=False)
    except OSError:
        pass


def daemon():
    _log('watchdog 启动')
    while True:
        try:
            tick()
        except Exception as e:
            _log(f'tick异常: {e}')
        time.sleep(LOOP_SEC)


def status():
    accounts = scan_accounts()
    print(f'发现账号目录 {len(accounts)} 个:')
    for wxid, acct in accounts.items():
        port = account_port(wxid)
        st = monitor_alive(wxid, port)
        st_cn = {'ok': '✅监控中', 'degraded': '⚠️异常', 'dead': '⛔未运行', 'foreign': '🔒端口被外来进程占用'}
        kf = os.path.join(KEYS_DIR, f'keys_{wxid}.json')
        ktime = time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(kf))) if os.path.exists(kf) else '-'
        print(f'  {wxid}  端口{port}  {st_cn.get(st, st)}  密钥:{ktime}')
        if st == 'ok':
            try:
                n = len(_api(port, '/api/sessions'))
                print(f'    → http://localhost:{port}  ({n} 会话)')
            except Exception:
                pass
    wechat_pids = [p for p in _weixin_pids()]
    print(f'Weixin.exe 进程数: {len(wechat_pids)}')


def _weixin_pids():
    try:
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Weixin.exe', '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True, encoding='gbk', errors='ignore', timeout=15,
                           creationflags=_NOWIN)
        for line in r.stdout.splitlines():
            if 'Weixin.exe' in line:
                parts = line.split('","')
                if len(parts) > 1:
                    yield parts[1].strip().strip('"')
    except Exception:
        return


def watch(targets=None, max_sec=3600, ports=None):
    """监听目标会话新消息（跨账号轮询）。命中输出后退出。"""
    tg = targets or DEFAULT_TARGETS
    accounts = scan_accounts()
    since = {w: int(time.time()) for w in accounts}
    deadline = time.time() + max_sec
    _log(f'[watch] 目标: {list(tg.values())}')
    while time.time() < deadline:
        for wxid, acct in accounts.items():
            port = account_port(wxid)
            if monitor_alive(wxid, port) != 'ok':
                continue
            try:
                data = _api(port, f'/api/history?since={since[wxid]}&limit=500')
            except Exception:
                continue
            hits = [m for m in data if m.get('username') in tg]
            if data:
                since[wxid] = max(m['timestamp'] for m in data)
            if hits:
                print(f'\n===== {time.strftime("%H:%M:%S")} {wxid} 命中 {len(hits)} 条 =====')
                for m in hits:
                    who = '我' if m.get('is_self') else m.get('sender', '?')
                    print(f"[{m['time']}] 【{tg[m['username']]}】{who}: {m.get('content','')}")
                return
        time.sleep(15)
    print('[watch] 超时无新消息')


def _startup_lnk():
    return os.path.join(os.path.expanduser('~'), 'AppData', 'Roaming', 'Microsoft',
                        'Windows', 'Start Menu', 'Programs', 'Startup',
                        'WeChatMonitorWatchdog.lnk')


def install():
    """注册开机自启（启动文件夹 .lnk 直指 pythonw.exe，无需管理员；2026-10-12 实测 schtasks 被组策略拒绝）。
    2026-10-12 修复：废弃 vbs 中转层——其引号转义在 VBScript 里直接 800A0401（"a" "b" 隐式拼接语法错误），
    重启后弹 Windows Script Host 报错且监控不自启。lnk 为 Unicode 存储天然免疫转义问题。"""
    exe = sys.executable
    if exe.lower().endswith('python.exe'):
        exe = exe.replace('python.exe', 'pythonw.exe')
    lnk = _startup_lnk()
    try:
        wsh = __import__('win32com.client', fromlist=['Dispatch']).Dispatch('WScript.Shell')
        s = wsh.CreateShortcut(lnk)
        s.TargetPath = exe
        s.Arguments = f'"{os.path.abspath(__file__)}" daemon'
        s.WorkingDirectory = os.path.dirname(os.path.abspath(__file__))
        s.WindowStyle = 7
        s.Save()
        # 清理旧 vbs 中转层（若存在）
        old_vbs = os.path.join(LOG_DIR, 'start_daemon.vbs')
        try:
            os.remove(old_vbs)
        except OSError:
            pass
        print(f'已注册开机自启: {lnk}')
    except Exception:
        # 无 pywin32 时退回 .cmd（会有闪窗，但可用）
        cmd = os.path.join(os.path.dirname(lnk), 'WeChatMonitorWatchdog.cmd')
        with open(cmd, 'w', encoding='ascii') as f:
            f.write(f'@start /min "" "{exe}" "{os.path.abspath(__file__)}" daemon')
        os.replace(cmd, lnk.replace('.lnk', '.cmd')) if False else None
        print(f'已注册开机自启(cmd版): {cmd}')


def uninstall():
    for p in (_startup_lnk(),
              os.path.join(os.path.dirname(_startup_lnk()), 'WeChatMonitorWatchdog.cmd')):
        try:
            os.remove(p)
            print(f'已移除: {p}')
        except OSError:
            pass


if __name__ == '__main__':
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'
    if cmd == 'daemon':
        daemon()
    elif cmd == 'install':
        install()
    elif cmd == 'uninstall':
        uninstall()
    elif cmd == 'status':
        status()
    elif cmd == 'watch':
        watch()
    elif cmd == 'logs':
        lf = os.path.join(LOG_DIR, 'watchdog.log')
        print(open(lf, encoding='utf-8').read()[-3000:] if os.path.exists(lf) else '(无日志)')
    else:
        sys.exit(__doc__)
