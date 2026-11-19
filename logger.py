"""
统一日志模块 — 把所有 print 输出与未捕获异常持久化到日志文件，便于事后排查。

设计要点(为什么这样做):
  现有代码用了几百处 print("[+]/[!]/[*]/[ERROR] ...")，全部改写成 logging 成本太高。
  这里改为"二重化(tee)":在不动任何现有 print 的前提下，把 stdout/stderr 镜像写入
  日志文件。控制台显示保持原样(干净)，文件里则给每行加时间戳，方便定位"什么时间发生了什么"。

用法:
  在每个入口(main.py / monitor_web.py / export_all_chats.py 等)最开头调用一次:
      from logger import setup_logging
      setup_logging()                 # 默认写到 <脚本目录>/logs/run_<YYYYMMDD>.log(每天一个,保留7天)
  之后所有 print 自动进日志;崩溃时的 traceback 也会被记录。

  需要结构化记录(尤其是替换 `except Exception: pass`)时:
      from logger import get_logger
      log = get_logger(__name__)
      try:
          ...
      except Exception:
          log.exception("解析 xxx 失败")   # 自动带 traceback,但不打断流程
"""
import io
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# ---- 模块级状态(只初始化一次) -----------------------------------------------
_initialized = False
_log_path = None
_lock = threading.Lock()


def _app_dir():
    # 打包后 __file__ 指向临时目录,优先用环境变量指定的 exe 目录(与 config.py 一致)。
    d = os.environ.get("WECHAT_DECRYPT_APP_DIR")
    if d and os.path.isdir(d):
        return d
    return os.path.dirname(os.path.abspath(__file__))


def _prune_old_logs(log_dir, days):
    """删除超过 days 天的 run_YYYYMMDD.log,只保留最近 days 天(含今天)。

    按文件名里的日期判断(而非 mtime),稳定可预测。days=7 时保留今天及之前 6 天。
    """
    if days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=days - 1)).strftime("%Y%m%d")
    try:
        for f in os.listdir(log_dir):
            m = re.fullmatch(r"run_(\d{8})\.log", f)
            if m and m.group(1) < cutoff:
                try:
                    os.remove(os.path.join(log_dir, f))
                except OSError:
                    pass
    except OSError:
        pass


class _TimestampTee(io.TextIOBase):
    """包装原始 stdout/stderr:写入控制台的同时,把每一行(带时间戳)写进日志。

    控制台保持原样输出(不加时间戳),只有文件里加前缀。按换行切分,
    保证多次 write() 拼出的同一行只加一个时间戳。
    """

    def __init__(self, original, log_handle, stream_label):
        self._original = original
        self._log = log_handle
        self._label = stream_label  # "OUT" / "ERR"
        self._buf = ""

    def write(self, s):
        # 始终原样写控制台
        try:
            self._original.write(s)
        except Exception:
            pass
        # 镜像到日志:按行加时间戳
        try:
            with _lock:
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._log.write(f"{ts} [{self._label}] {line}\n")
                self._log.flush()
        except Exception:
            pass
        return len(s)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass
        try:
            self._log.flush()
        except Exception:
            pass

    # 让依赖 isatty / fileno 的库(如 tqdm)正常工作
    def isatty(self):
        try:
            return self._original.isatty()
        except Exception:
            return False

    def fileno(self):
        return self._original.fileno()


def setup_logging(log_dir=None, level=logging.INFO, days=7, capture_stdio=True):
    """初始化日志。重复调用安全(只生效一次)。

    log_dir       : 日志目录,默认 <脚本目录>/logs
    level         : get_logger() 返回的 logger 的级别
    days          : 日志按天存放,保留最近 days 天(含今天),更早的自动删除
    capture_stdio : 是否把 stdout/stderr 镜像进日志(把现有 print 一并捕获)
    返回日志文件路径。
    """
    global _initialized, _log_path
    if _initialized:
        return _log_path

    if log_dir is None:
        log_dir = os.path.join(_app_dir(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    _prune_old_logs(log_dir, days)

    # 按天一个文件:同一天的多次运行追加到同一个 run_YYYYMMDD.log
    stamp = datetime.now().strftime("%Y%m%d")
    _log_path = os.path.join(log_dir, f"run_{stamp}.log")

    # 单一文件句柄,tee 和 logging 共用,避免多 handler 各自 open 造成交错。
    log_handle = open(_log_path, "a", encoding="utf-8", buffering=1)

    # root logger -> 同一个文件(也保留一份给结构化日志)
    root = logging.getLogger()
    root.setLevel(level)
    fh = logging.StreamHandler(log_handle)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root.addHandler(fh)

    if capture_stdio:
        sys.stdout = _TimestampTee(sys.stdout, log_handle, "OUT")
        sys.stderr = _TimestampTee(sys.stderr, log_handle, "ERR")

    # 记录未捕获异常的完整 traceback(否则崩溃后什么都不剩)
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.getLogger("uncaught").error(
            "未捕获异常,程序终止", exc_info=(exc_type, exc_value, exc_tb)
        )
        # 仍按默认行为打印到(已被 tee 的)stderr
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    # 线程里的未捕获异常(Python 3.8+)
    def _thread_excepthook(args):
        logging.getLogger("uncaught").error(
            "线程未捕获异常: %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass

    _initialized = True
    logging.getLogger("logger").info("日志已启动: %s", _log_path)
    return _log_path


def get_logger(name=None):
    """获取一个 logger。若尚未 setup_logging,会自动用默认配置初始化。"""
    if not _initialized:
        setup_logging()
    return logging.getLogger(name if name else "app")


def log_path():
    """返回当前日志文件路径(未初始化则 None)。"""
    return _log_path
