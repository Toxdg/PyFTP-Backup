#!/usr/bin/env python3
"""
PyFTP-Backup
============
A lightweight, multi-threaded incremental backup utility written in pure Python.
Supports FTP/FTPS (TLS), directory pattern exclusions, local state caching,
automated log rotation, and SMTP email notifications.
"""

import argparse
from datetime import datetime, timedelta
from email.mime.text import MIMEText
import fnmatch
from ftplib import FTP, FTP_TLS
import json
import os
from queue import Queue
import smtplib
import socket
import sys
import threading
import time

# Determine base application directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Default paths for configuration and credentials
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SMTP_FILE = os.path.join(BASE_DIR, "smtp.json")

# Default global socket timeout in seconds
socket.setdefaulttimeout(300)

# Global execution state and thread synchronization locks
REPORT = []
TOTAL_ERRORS = 0
LOCK = threading.Lock()
LOG_LOCK = threading.Lock()

# Retention period for log lines in days
LOG_DAYS = 60

# Persistent log file handles
LOG_HANDLES = {}


# ---------------- UTILITY HELPERS ----------------

def safe_close_ftp(ftp):
    """Safely terminate and close an FTP/FTPS connection."""
    if ftp:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


# ---------------- LOGGING SUBSYSTEM ----------------

def log(msg, logfile, level="INFO", project=None):
    """
    Format and output log messages to stdout and optionally to a project log file.

    File writes are limited to errors, warnings, and lifecycle milestones (START/END).
    Thread-safe access to persistent file handles is maintained via LOG_LOCK.
    """
    prefix = f"[{project}] " if project else ""
    line = f"{datetime.now()} {prefix}[{level}] {msg}"

    # Print to console always
    print(line, flush=True)

    # Persist critical entries and milestones to the log file
    if level in ["ERROR", "WARNING"] or "START" in msg or "END" in msg:
        try:
            with LOG_LOCK:
                if logfile not in LOG_HANDLES:
                    log_dir = os.path.dirname(os.path.abspath(logfile))
                    os.makedirs(log_dir, exist_ok=True)
                    LOG_HANDLES[logfile] = open(logfile, "a", encoding="utf-8")

                f = LOG_HANDLES[logfile]
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"Logging error: {e}")


def clean_old_logs(logfile, max_days=LOG_DAYS):
    """
    Purge log entries older than `max_days` from the specified log file.

    Closes active file handles, reads and parses line timestamps,
    retains recent entries or unparseable lines (e.g., multiline tracebacks),
    and rewrites the file safely.
    """
    if not os.path.exists(logfile):
        return

    cutoff_date = datetime.now() - timedelta(days=max_days)
    new_lines = []

    with LOG_LOCK:
        # Close any open file handle before rewriting the file
        if logfile in LOG_HANDLES:
            try:
                LOG_HANDLES[logfile].close()
            except Exception:
                pass
            del LOG_HANDLES[logfile]

        try:
            with open(logfile, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    parts = line.strip().split(" ", 2)
                    if len(parts) >= 2:
                        timestamp_str = f"{parts[0]} {parts[1]}"
                        try:
                            if "." in timestamp_str:
                                entry_date = datetime.strptime(
                                    timestamp_str, "%Y-%m-%d %H:%M:%S.%f"
                                )
                            else:
                                entry_date = datetime.strptime(
                                    timestamp_str, "%Y-%m-%d %H:%M:%S"
                                )

                            # Keep lines within retention boundary
                            if entry_date >= cutoff_date:
                                new_lines.append(line)
                        except ValueError:
                            # Retain multi-line tracebacks and non-standard lines
                            new_lines.append(line)
                    else:
                        new_lines.append(line)

            with open(logfile, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        except Exception as e:
            print(f"Log cleanup error for {logfile}: {e}")


# ---------------- NOTIFICATIONS (SMTP) ----------------

def send_email(subject, body):
    """
    Send an email summary report using credentials defined in smtp.json.

    Supports both single JSON objects and list configurations.
    If smtp.json is absent, dispatch is gracefully skipped without error.
    """
    if not os.path.exists(SMTP_FILE):
        return

    with open(SMTP_FILE, encoding="utf-8") as f:
        data = json.load(f)
        smtp_cfg = data[0] if isinstance(data, list) else data

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_cfg["user"]
    msg["To"] = smtp_cfg["to"]

    try:
        with smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"]) as server:
            server.starttls()
            server.login(smtp_cfg["user"], smtp_cfg["password"])
            server.send_message(msg)
    except Exception as e:
        print("SMTP ERROR:", e)


# ---------------- STATE CACHE ----------------

def load_cache(path):
    """
    Load the local synchronization cache (.backup_cache.json) from the target directory.

    Returns a mapping of remote paths to file metadata (size, mtime).
    """
    file = os.path.join(path, ".backup_cache.json")
    if os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    """
    Persist the synchronization cache (.backup_cache.json) to the target directory.
    """
    file = os.path.join(path, ".backup_cache.json")
    try:
        os.makedirs(path, exist_ok=True)
        with open(file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Cache save error: {e}")


# ---------------- EXCLUSION ENGINE ----------------

def should_exclude(path, exclude):
    """
    Determine whether a remote path matches configured exclusion rules.

    Supports:
    1. Direct file or directory basename matching (e.g., 'updraft', '*cache*').
    2. Path segment matching for encapsulated directories (e.g., '/cache/').
    3. Full path glob patterns (e.g., '*.log', '/public_html/tmp/*').
    """
    if not exclude:
        return False

    path_clean = path.lower().replace("\\", "/").rstrip("/")
    folder_or_file_name = path_clean.split("/")[-1]

    for pattern in exclude:
        pat = pattern.lower()
        clean_pat = pat.strip("*")

        # 1. Match against the file/directory name directly
        if fnmatch.fnmatch(folder_or_file_name, pat):
            return True

        # 2. Match directory segment boundary (/name/)
        if f"/{clean_pat}/" in f"/{path_clean}/":
            return True

        # 3. Match against the entire clean path
        if fnmatch.fnmatch(path_clean, pat):
            return True

    return False


# ---------------- FTP CONNECTION HANDLERS ----------------

def connect(cfg):
    """
    Establish an FTP or FTPS connection based on target configuration.

    Attempts standard cleartext FTP or explicit TLS (FTP_TLS). Automatically
    falls back to explicit TLS if cleartext connection is rejected by the server.
    Configures passive mode before returning the client object.
    """
    use_tls = cfg.get("tls", False)

    try:
        if use_tls:
            ftp = FTP_TLS(cfg["host"], timeout=300)
            ftp.login(cfg["username"], cfg["password"])
            ftp.prot_p()
        else:
            ftp = FTP(cfg["host"], timeout=300)
            ftp.login(cfg["username"], cfg["password"])
    except Exception as e:
        err_str = str(e).lower()
        # Fallback to explicit TLS if server demands encryption
        if not use_tls and any(term in err_str for term in ("tls", "421", "cleartext")):
            print(f"[{cfg.get('name')}] Server requires TLS, switching to FTP_TLS...")
            ftp = FTP_TLS(cfg["host"], timeout=300)
            ftp.login(cfg["username"], cfg["password"])
            ftp.prot_p()
        else:
            raise e

    ftp.set_pasv(True)
    return ftp


def test_connection(cfg):
    """
    Perform a rapid pre-flight connection test prior to scheduling tasks.

    Returns a tuple of (bool, error_message).
    """
    ftp = None
    try:
        ftp = connect(cfg)
        ftp.nlst(cfg.get("remote_path", "/"))
        safe_close_ftp(ftp)
        return True, "OK"
    except Exception as e:
        safe_close_ftp(ftp)
        return False, str(e)


# ---------------- CONCURRENT DOWNLOAD WORKER ----------------

def worker(cfg, logfile, stats, errors, queue, cache):
    """
    Background worker thread processing file download jobs from the queue.

    Manages dedicated FTP connections with retry logic (up to 3 attempts per file).
    Updates shared download counters and metadata cache upon successful retrieval.
    """
    ftp = None

    while True:
        item = queue.get()
        if item is None:
            # Sentinel received: disconnect and terminate thread
            safe_close_ftp(ftp)
            queue.task_done()
            return

        remote, local = item
        success = False
        error_msg = ""

        try:
            os.makedirs(os.path.dirname(local), exist_ok=True)

            for attempt in range(3):
                try:
                    if ftp is None:
                        ftp = connect(cfg)

                    with open(local, "wb") as f:
                        ftp.retrbinary(f"RETR {remote}", f.write)

                    local_size = os.path.getsize(local)
                    local_mtime = os.path.getmtime(local)

                    with LOCK:
                        cache[remote] = {"size": local_size, "mtime": local_mtime}
                        stats["downloaded"] += 1
                        current = stats["downloaded"] + stats["skipped"]
                        total = stats["total"]

                    log_msg = f"OK {remote} ({current}/{total})"
                    log(log_msg, logfile, "INFO", cfg["name"])
                    success = True
                    break

                except Exception as e:
                    error_msg = str(e)
                    log(
                        f"RETRY {remote} ({attempt + 1}/3) - {error_msg}",
                        logfile,
                        "ERROR",
                        cfg["name"],
                    )
                    safe_close_ftp(ftp)
                    ftp = None
                    time.sleep(2)

            if not success:
                with LOCK:
                    errors.append(remote)
                log(f"FAILED {remote} - {error_msg}", logfile, "ERROR", cfg["name"])

        except Exception as e:
            error_msg = str(e)
            with LOCK:
                errors.append(remote)
            log(
                f"FATAL WORKER ERROR {remote} - {error_msg}",
                logfile,
                "ERROR",
                cfg["name"],
            )
        finally:
            queue.task_done()


# ---------------- RECURSIVE SCAN & DISCOVERY ----------------

def sync_ftp(
    ftp,
    remote_dir,
    local_dir,
    exclude,
    logfile,
    stats,
    queue,
    cache,
    project,
    use_mlsd=None,
):
    """
    Recursively scan remote FTP directory tree and enqueue files for synchronization.

    Feature negotiation:
    - Queries server FEAT to detect machine listings (MLSD).
    - Falls back to RFC 959 standard LIST parsing if MLSD is unsupported or fails.
    - Evaluates exclusion patterns before descending into subdirectories.
    - Compares remote file sizes and timestamps against local state cache
      to avoid redundant downloads.
    """
    log(f"Scanning {remote_dir}", logfile, "DEBUG", project)
    os.makedirs(local_dir, exist_ok=True)

    # Detect MLSD capability during the initial call
    if use_mlsd is None:
        try:
            feat_resp = ftp.voidcmd("FEAT")
            use_mlsd = "MLSD" in feat_resp
        except Exception:
            use_mlsd = False

    items = []
    success = False

    # Attempt MLSD listing
    if use_mlsd:
        try:
            items = list(ftp.mlsd(remote_dir))
            success = True
        except Exception as e:
            try:
                ftp.voidcmd("NOOP")
            except Exception:
                raise e

    # Fallback to LIST command
    if not success:
        try:
            raw_items = []
            ftp.retrlines(f"LIST {remote_dir}", raw_items.append)
            success = True
        except Exception as e:
            log(f"LIST ERROR {remote_dir}: {e}", logfile, "ERROR", project)
            try:
                ftp.voidcmd("NOOP")
            except Exception:
                raise e
            return

    # Process items parsed via MLSD
    if use_mlsd and success and items:
        for name, facts in items:
            if name in [".", ".."]:
                continue

            if name.startswith("/"):
                remote_path = name
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
            else:
                remote_path = f"{remote_dir.rstrip('/')}/{name}"

            clean_name = os.path.basename(remote_path.rstrip("/"))
            local_path = os.path.join(local_dir, clean_name)

            if should_exclude(remote_path, exclude):
                continue

            stats["scanned"] += 1

            ftype = facts.get("type")
            if ftype in ["cdir", "pdir"]:
                continue

            if ftype == "dir":
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
                if name.startswith("/") and not name.startswith(
                    remote_dir.rstrip("/") + "/"
                ):
                    continue

                sync_ftp(
                    ftp,
                    remote_path,
                    local_path,
                    exclude,
                    logfile,
                    stats,
                    queue,
                    cache,
                    project,
                    use_mlsd,
                )
            else:
                remote_size = int(facts.get("size", 0))
                cached = cache.get(remote_path)
                local_size = (
                    os.path.getsize(local_path) if os.path.exists(local_path) else None
                )
                cached_size = (
                    cached.get("size") if isinstance(cached, dict) else cached
                )

                needs_download = False
                if (
                    local_size is None
                    or cached_size != local_size
                    or local_size != remote_size
                ):
                    needs_download = True

                if needs_download:
                    queue.put((remote_path, local_path))
                    with LOCK:
                        stats["total"] += 1
                else:
                    with LOCK:
                        stats["skipped"] += 1

    # Process items parsed via LIST fallback
    else:
        for item in raw_items:
            parts = item.split(maxsplit=8)
            if len(parts) < 9:
                continue

            perm, name = parts[0], parts[8]
            if name in [".", ".."]:
                continue

            if name.startswith("/"):
                remote_path = name
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
            else:
                remote_path = f"{remote_dir.rstrip('/')}/{name}"

            clean_name = os.path.basename(remote_path.rstrip("/"))
            local_path = os.path.join(local_dir, clean_name)

            if should_exclude(remote_path, exclude):
                continue

            stats["scanned"] += 1

            if perm.startswith("d"):
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
                if name.startswith("/") and not name.startswith(
                    remote_dir.rstrip("/") + "/"
                ):
                    continue
                sync_ftp(
                    ftp,
                    remote_path,
                    local_path,
                    exclude,
                    logfile,
                    stats,
                    queue,
                    cache,
                    project,
                    use_mlsd,
                )
            else:
                local_size = (
                    os.path.getsize(local_path) if os.path.exists(local_path) else None
                )
                cached = cache.get(remote_path)
                cached_size = (
                    cached.get("size") if isinstance(cached, dict) else cached
                )

                if not os.path.exists(local_path) or cached_size != local_size:
                    queue.put((remote_path, local_path))
                    with LOCK:
                        stats["total"] += 1
                else:
                    with LOCK:
                        stats["skipped"] += 1


# ---------------- PROJECT ORCHESTRATION ----------------

def run(cfg):
    """
    Orchestrate full backup workflow for a single project configuration.

    Executes:
    1. Log file rotation based on LOG_DAYS.
    2. Initialization of worker threads.
    3. Recursive scanning of remote FTP path.
    4. Task queue drainage and thread teardown.
    5. Cache state serialization.
    6. Performance metrics aggregation.
    """
    global TOTAL_ERRORS
    start_time = time.time()

    logfile = os.path.abspath(f"{cfg['local_path']}/../logs/backup.log")

    # Rotate and purge old log entries before starting
    clean_old_logs(logfile, max_days=LOG_DAYS)

    stats = {"scanned": 0, "downloaded": 0, "skipped": 0, "total": 0}
    errors = []
    queue = Queue()
    cache = load_cache(cfg["local_path"])

    log("START", logfile, "INFO", cfg["name"])
    log("Starting threads...", logfile, "DEBUG", cfg["name"])

    workers = []
    for _ in range(cfg.get("threads", 4)):
        t = threading.Thread(
            target=worker,
            args=(cfg, logfile, stats, errors, queue, cache),
            daemon=True,
        )
        t.start()
        workers.append(t)

    log(f"Connecting to FTP {cfg['host']}...", logfile, "DEBUG", cfg["name"])

    ftp = None
    try:
        ftp = connect(cfg)
        log("Connected. Starting scan...", logfile, "DEBUG", cfg["name"])
        sync_ftp(
            ftp,
            cfg["remote_path"],
            cfg["local_path"],
            cfg.get("exclude", []),
            logfile,
            stats,
            queue,
            cache,
            cfg["name"],
        )
    except Exception as e:
        log(f"CRITICAL ERROR during sync: {e}", logfile, "ERROR", cfg["name"])
        errors.append(f"Sync error: {e}")
    finally:
        safe_close_ftp(ftp)

        # Stop worker threads with sentinels
        for _ in workers:
            queue.put(None)

        try:
            queue.join()
        except Exception:
            pass

        for t in workers:
            try:
                t.join(timeout=10)
            except Exception:
                pass

    save_cache(cfg["local_path"], cache)

    TOTAL_ERRORS += len(errors)

    duration = time.time() - start_time
    m, s = divmod(int(duration), 60)
    duration_str = f"{m}m {s}s"

    summary = (
        f"Project: {cfg['name']}\n"
        f"Scanned: {stats['scanned']}\n"
        f"Downloaded: {stats['downloaded']}\n"
        f"Skipped: {stats['skipped']}\n"
        f"Errors: {len(errors)}\n"
        f"Duration: {duration_str}\n"
    )

    if errors:
        summary += "Error list (first 50 entries):\n"
        summary += "\n".join([f" - {e}" for e in errors[:50]])
        if len(errors) > 50:
            summary += f"\n ... and {len(errors) - 50} more."
        summary += "\n"

    REPORT.append(summary)

    if errors:
        log(f"END WITH ERRORS ({len(errors)})", logfile, "ERROR", cfg["name"])
    else:
        log(f"END OK ({stats['downloaded']} new files)", logfile, "INFO", cfg["name"])

    # Close open log file handle after project execution completes
    with LOG_LOCK:
        if logfile in LOG_HANDLES:
            try:
                LOG_HANDLES[logfile].close()
            except Exception:
                pass
            del LOG_HANDLES[logfile]


# ---------------- ENTRY POINT ----------------

def main():
    """
    Parse CLI options, validate configuration, run pre-flight tests,
    and execute backup tasks across all or targeted projects.
    """
    parser = argparse.ArgumentParser(
        description="PyFTP-Backup: Multi-threaded Incremental FTP Backup Tool"
    )
    parser.version = "1.1"
    parser.add_argument(
        "-p",
        "--project",
        help="Filter execution to a single project name defined in config.json",
    )
    args = parser.parse_args()

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Configuration file '{CONFIG_FILE}' not found.")
        print(
            "Please copy 'config.example.json' to 'config.json' "
            "and fill in your FTP details."
        )
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        configs = json.load(f)

    if args.project:
        configs = [c for c in configs if c["name"].lower() == args.project.lower()]
        if not configs:
            print(f"Error: Project '{args.project}' not found in config.json")
            sys.exit(1)

    print(f"Starting pre-flight connection check for {len(configs)} project(s)...")
    valid_configs = []
    for cfg in configs:
        ok, err = test_connection(cfg)
        if ok:
            valid_configs.append(cfg)
        else:
            global TOTAL_ERRORS
            TOTAL_ERRORS += 1
            REPORT.append(
                f"Project: {cfg['name']}\nSTATUS: CONNECTION FAILED\nDetails: {err}\n"
            )
            print(f"  [!] {cfg['name']}: Connection failed - skipping.")

    start_total = time.time()
    for cfg in valid_configs:
        run(cfg)

    total_duration = time.time() - start_total
    tm, ts = divmod(int(total_duration), 60)

    if not configs:
        print("No projects to backup.")
        return

    body = f"Total duration: {tm}m {ts}s\n"
    body += "\n\n" + "=" * 30 + "\n\n"
    body += "\n\n".join(REPORT)
    status = "OK" if TOTAL_ERRORS == 0 else "ERROR"
    subject = f"[BACKUP {status}] {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    send_email(subject, body)


if __name__ == "__main__":
    main()
