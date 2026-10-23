#!/usr/bin/env python3
import os
import json
import threading
import socket
import smtplib
import time
import sys
import argparse
import fnmatch
from email.mime.text import MIMEText
from queue import Queue
from ftplib import FTP, FTP_TLS
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SMTP_FILE = os.path.join(BASE_DIR, "smtp.json")

socket.setdefaulttimeout(300)

REPORT = []
TOTAL_ERRORS = 0
LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
LOG_DAYS = 60


# ---------------- LOG ----------------

LOG_HANDLES = {}

def log(msg, logfile, level="INFO", project=None):
    prefix = f"[{project}] " if project else ""
    line = f"{datetime.now()} {prefix}[{level}] {msg}"

    # Print to console always
    print(line, flush=True)

    # Save to file only if it's an error or important status
    if level in ["ERROR", "WARNING"] or "START" in msg or "END" in msg:
        try:
            with LOG_LOCK:
                if logfile not in LOG_HANDLES:
                    os.makedirs(os.path.dirname(os.path.abspath(logfile)), exist_ok=True)
                    LOG_HANDLES[logfile] = open(logfile, "a", encoding="utf-8")
                
                f = LOG_HANDLES[logfile]
                f.write(line + "\n")
                f.flush()
        except Exception as e:
            print(f"Logging error: {e}")


def clean_old_logs(logfile, max_days=LOG_DAYS):
    """Usuwa wpisy starsze niż max_days dni z pliku loga."""
    if not os.path.exists(logfile):
        return

    cutoff_date = datetime.now() - timedelta(days=max_days)
    new_lines = []

    with LOG_LOCK:
        # Upewniamy się, że żaden aktywny uchwyt nie blokuje pliku przed modyfikacją
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
                                entry_date = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S.%f")
                            else:
                                entry_date = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")

                            if entry_date >= cutoff_date:
                                new_lines.append(line)
                        except ValueError:
                            # Zachowaj wieloliniowe trace'y i linie bez standardowej daty
                            new_lines.append(line)
                    else:
                        new_lines.append(line)

            with open(logfile, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

        except Exception as e:
            print(f"Log cleanup error for {logfile}: {e}")


# ---------------- SMTP ----------------

def send_email(subject, body):
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


# ---------------- CACHE ----------------

def load_cache(path):
    file = os.path.join(path, ".backup_cache.json")
    if os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(path, cache):
    file = os.path.join(path, ".backup_cache.json")
    try:
        os.makedirs(path, exist_ok=True)
        with open(file, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Cache save error: {e}")


# ---------------- EXCLUDE ----------------

def should_exclude(path, exclude):
    if not exclude:
        return False

    path_clean = path.lower().replace("\\", "/").rstrip("/")
    folder_or_file_name = path_clean.split("/")[-1]

    for pattern in exclude:
        pat = pattern.lower()
        clean_pat = pat.strip("*")

        # 1. Dokładna nazwa folderu lub pliku (np. "updraft" lub "*updraft*")
        if fnmatch.fnmatch(folder_or_file_name, pat):
            return True

        # 2. Jeśli podano jako *nazwa* - sprawdź czy "nazwa" jest osobnym segmentem ścieżki (/nazwa/)
        if f"/{clean_pat}/" in f"/{path_clean}/":
            return True

        # 3. Dopasowanie do całej ścieżki (np. dla rozszerzeń *.log, *.tmp)
        if fnmatch.fnmatch(path_clean, pat):
            return True

    return False


# ---------------- FTP ----------------

def connect(cfg):
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
        if not use_tls and ("tls" in err_str or "421" in err_str or "cleartext" in err_str):
            print(f"[{cfg.get('name')}] Server requires TLS, switching to FTP_TLS...")
            ftp = FTP_TLS(cfg["host"], timeout=300)
            ftp.login(cfg["username"], cfg["password"])
            ftp.prot_p()
        else:
            raise e

    ftp.set_pasv(True)
    return ftp


def test_connection(cfg):
    """Fast connection test before starting full backup"""
    ftp = None
    try:
        ftp = connect(cfg)
        ftp.nlst(cfg.get("remote_path", "/"))
        try:
            ftp.quit()
        except Exception:
            try: ftp.close()
            except Exception: pass
        return True, "OK"
    except Exception as e:
        if ftp:
            try: ftp.quit()
            except Exception:
                try: ftp.close()
                except Exception: pass
        return False, str(e)


# ---------------- WORKER ----------------

def worker(cfg, logfile, stats, errors, queue, cache):
    ftp = None
    
    while True:
        item = queue.get()
        if item is None:
            if ftp:
                try: ftp.quit()
                except Exception:
                    try: ftp.close()
                    except Exception: pass
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

                    log(f"OK {remote} ({current}/{total})", logfile, "INFO", cfg["name"])
                    success = True
                    break

                except Exception as e:
                    error_msg = str(e)
                    log(f"RETRY {remote} ({attempt+1}/3) - {error_msg}", logfile, "ERROR", cfg["name"])
                    if ftp:
                        try: ftp.quit()
                        except Exception:
                            try: ftp.close()
                            except Exception: pass
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
            log(f"FATAL WORKER ERROR {remote} - {error_msg}", logfile, "ERROR", cfg["name"])
        finally:
            queue.task_done()


# ---------------- SCAN ----------------

def sync_ftp(ftp, remote_dir, local_dir, exclude, logfile, stats, queue, cache, project, use_mlsd=None):
    log(f"Scanning {remote_dir}", logfile, "DEBUG", project)
    os.makedirs(local_dir, exist_ok=True)

    if use_mlsd is None:
        try:
            feat_resp = ftp.voidcmd("FEAT")
            use_mlsd = "MLSD" in feat_resp
        except Exception:
            use_mlsd = False

    items = []
    success = False
    if use_mlsd:
        try:
            items = list(ftp.mlsd(remote_dir))
            success = True
        except Exception as e:
            try:
                ftp.voidcmd("NOOP")
            except Exception:
                raise e

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

    if use_mlsd and success and items:
        for name, facts in items:
            if name in [".", ".."]: continue
            
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
            if ftype in ["cdir", "pdir"]: continue

            if ftype == "dir":
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
                if name.startswith("/") and not name.startswith(remote_dir.rstrip("/") + "/"):
                    continue

                sync_ftp(ftp, remote_path, local_path, exclude, logfile, stats, queue, cache, project, use_mlsd)
            else:
                remote_size = int(facts.get("size", 0))
                cached = cache.get(remote_path)
                local_size = os.path.getsize(local_path) if os.path.exists(local_path) else None
                cached_size = cached.get("size") if isinstance(cached, dict) else cached
                
                needs_download = False
                if local_size is None or cached_size != local_size or local_size != remote_size:
                    needs_download = True

                if needs_download:
                    queue.put((remote_path, local_path))
                    with LOCK:
                        stats["total"] += 1
                else:
                    with LOCK:
                        stats["skipped"] += 1
    else:
        for item in raw_items:
            parts = item.split(maxsplit=8)
            if len(parts) < 9: continue
            
            perm, name = parts[0], parts[8]
            if name in [".", ".."]: continue

            if name.startswith("/"):
                remote_path = name
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
            else:
                remote_path = f"{remote_dir.rstrip('/')}/{name}"

            clean_name = os.path.basename(remote_path.rstrip("/"))
            local_path = os.path.join(local_dir, clean_name)

            if should_exclude(remote_path, exclude): continue

            stats["scanned"] += 1

            if perm.startswith("d"):
                if remote_path.rstrip("/") == remote_dir.rstrip("/"):
                    continue
                if name.startswith("/") and not name.startswith(remote_dir.rstrip("/") + "/"):
                    continue
                sync_ftp(ftp, remote_path, local_path, exclude, logfile, stats, queue, cache, project, use_mlsd)
            else:
                local_size = os.path.getsize(local_path) if os.path.exists(local_path) else None
                cached = cache.get(remote_path)
                cached_size = cached.get("size") if isinstance(cached, dict) else cached

                if not os.path.exists(local_path) or cached_size != local_size:
                    queue.put((remote_path, local_path))
                    with LOCK:
                        stats["total"] += 1
                else:
                    with LOCK:
                        stats["skipped"] += 1


# ---------------- RUN ----------------

def run(cfg):
    global TOTAL_ERRORS
    start_time = time.time()

    logfile = os.path.abspath(f"{cfg['local_path']}/../logs/backup.log")
    
    # Czyszczenie loga przed rozpoczęciem projektu
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
            daemon=True
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
            cfg["name"]
        )
    except Exception as e:
        log(f"CRITICAL ERROR during sync: {e}", logfile, "ERROR", cfg["name"])
        errors.append(f"Sync error: {e}")
    finally:
        if ftp:
            try: ftp.quit()
            except Exception:
                try: ftp.close()
                except Exception: pass
        
        # Zatrzymanie workerów
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
        f"Projekt: {cfg['name']}\n"
        f"Scanned: {stats['scanned']}\n"
        f"Downloaded: {stats['downloaded']}\n"
        f"Skipped: {stats['skipped']}\n"
        f"Errors: {len(errors)}\n"
        f"Duration: {duration_str}\n"
    )

    if errors:
        summary += "Lista błędów (pierwsze 50):\n"
        summary += "\n".join([f" - {e}" for e in errors[:50]])
        if len(errors) > 50:
            summary += f"\n ... i {len(errors)-50} więcej."
        summary += "\n"

    REPORT.append(summary)

    if errors:
        log(f"END WITH ERRORS ({len(errors)})", logfile, "ERROR", cfg["name"])
    else:
        log(f"END OK ({stats['downloaded']} new files)", logfile, "INFO", cfg["name"])
        
    # Zamknięcie uchwytu po zakończeniu projektu
    with LOG_LOCK:
        if logfile in LOG_HANDLES:
            try:
                LOG_HANDLES[logfile].close()
            except Exception:
                pass
            del LOG_HANDLES[logfile]


# ---------------- MAIN ----------------

def main():
    parser = argparse.ArgumentParser(description="FTP Backup Script")
    parser.version = "1.1"
    parser.add_argument("-p", "--project", help="Name of the project to backup (from config.json)")
    args = parser.parse_args()

    if not os.path.exists(CONFIG_FILE):
        print(f"Error: Configuration file '{CONFIG_FILE}' not found.")
        print("Please copy 'config.example.json' to 'config.json' and fill in your FTP details.")
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        configs = json.load(f)

    if args.project:
        configs = [c for c in configs if c["name"].lower() == args.project.lower()]
        if not configs:
            print(f"Error: Project '{args.project}' not found in config.json")
            sys.exit(1)

    print(f"Starting pre-flight connection check for {len(configs)} projects...")
    valid_configs = []
    for cfg in configs:
        ok, err = test_connection(cfg)
        if ok:
            valid_configs.append(cfg)
        else:
            global TOTAL_ERRORS
            TOTAL_ERRORS += 1
            REPORT.append(f"Project: {cfg['name']}\nSTATUS: CONNECTION FAILED\nDetails: {err}\n")
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
    body += "\n\n" + "="*30 + "\n\n"
    body += "\n\n".join(REPORT)
    status = "OK" if TOTAL_ERRORS == 0 else "ERROR"
    subject = f"[BACKUP {status}] {datetime.now().strftime('%Y-%m-%d %H:%M')}"

    send_email(subject, body)


if __name__ == "__main__":
    main()