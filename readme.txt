================================================================================
                                PyFTP-Backup
================================================================================

A lightweight, multi-threaded incremental backup tool written in pure Python.
Designed for automated backups over FTP and FTPS (TLS), featuring local state
caching, pattern-based exclusions, automatic log rotation, and SMTP email status
reports.

Zero external dependencies - runs directly on the Python Standard Library.


--------------------------------------------------------------------------------
1. FEATURES
--------------------------------------------------------------------------------

- Zero External Dependencies: Built entirely using Python's standard modules
  (ftplib, threading, smtplib, fnmatch, etc.). No "pip install" required.
- Multi-threaded Downloads: Configurable worker threads per project for high-
  throughput transfers.
- Incremental Sync with Caching: Tracks remote file sizes and timestamps in
  .backup_cache.json to skip unchanged files.
- Smart Directory-Aware Exclusions: Exclude directories or files anywhere in
  the tree using patterns like *cache*, *updraft*, node_modules, or *.log.
- Automatic Fallback to FTPS: Automatically upgrades to TLS/SSL if the server
  rejects cleartext connections.
- Fast Directory Listing (MLSD / LIST): Uses modern machine-readable listings
  (MLSD) where supported, with transparent fallback to standard LIST.
- Pre-flight Connectivity Check: Verifies connections to all servers before
  triggering downloads.
- Project Isolation: Run all projects at once or target an individual project
  using -p / --project.
- Self-Cleaning Logs: Keeps disk usage under control by automatically removing
  log lines older than LOG_DAYS (default: 60 days).
- Email Notifications: Dispatches an execution report containing elapsed time,
  statistics, and error logs via SMTP.


--------------------------------------------------------------------------------
2. PROJECT STRUCTURE
--------------------------------------------------------------------------------

PyFTP-Backup/
|-- .gitignore
|-- LICENSE
|-- README.txt
|-- pyftp_backup.py
|-- config.example.json
`-- smtp.example.json


--------------------------------------------------------------------------------
3. GETTING STARTED
--------------------------------------------------------------------------------

1. Clone the repository:
   git clone https://github.com/your-username/PyFTP-Backup.git
   cd PyFTP-Backup

2. Prepare configuration files:
   cp config.example.json config.json
   cp smtp.example.json smtp.json


--------------------------------------------------------------------------------
4. CONFIGURATION
--------------------------------------------------------------------------------

[A] config.json (Required)
Defines one or more FTP/FTPS backup targets. Example:

[
  {
    "name": "Production_WP",
    "host": "ftp.example.com",
    "username": "ftp_user",
    "password": "your_secure_password_here",
    "tls": true,
    "threads": 4,
    "remote_path": "/public_html/wp-content",
    "local_path": "/var/backups/production_wp/wp-content",
    "exclude": [
      "*updraft*",
      "*ai1wm-backups*",
      "*cache*",
      "*languages*",
      "*upgrade*",
      "*wflogs*",
      "*.log",
      "*.tmp"
    ]
  },
  {
    "name": "Staging_App",
    "host": "192.168.1.50",
    "username": "backup_agent",
    "password": "another_secure_password",
    "tls": false,
    "threads": 2,
    "remote_path": "/var/www/html",
    "local_path": "/mnt/storage/backups/staging_app",
    "exclude": [
      "*cache*",
      "node_modules",
      ".git",
      "*.tar.gz"
    ]
  }
]

Settings Reference:
- name         (string)  : Unique project identifier used in logs and CLI filters.
- host         (string)  : FTP server hostname or IP address.
- username     (string)  : FTP account username.
- password     (string)  : FTP account password.
- tls          (boolean) : Set to true for explicit FTPS. If false, TLS fallback
                           is still attempted if the server demands encryption.
- threads      (integer) : Number of parallel worker threads (default: 4).
- remote_path  (string)  : Source directory on the FTP server.
- local_path   (string)  : Destination directory on your local machine / server.
- exclude      (array)   : Patterns to skip. Matches folder names (*cache*,
                           node_modules) or file patterns (*.log).


[B] smtp.json (Optional)
Enables email alerts upon backup completion. If omitted, backups continue
without sending notifications. Example:

{
  "host": "smtp.example.com",
  "port": 587,
  "user": "notifications@example.com",
  "password": "your_app_specific_password_here",
  "to": "admin@example.com"
}


--------------------------------------------------------------------------------
5. USAGE
--------------------------------------------------------------------------------

- Run all configured projects:
  python3 pyftp_backup.py

- Run a single project by name (case-insensitive):
  python3 pyftp_backup.py -p Production_WP


--------------------------------------------------------------------------------
6. AUTOMATION (CRON JOB)
--------------------------------------------------------------------------------

To schedule unattended backups (e.g., every night at 2:00 AM):

1. Open crontab editor:
   crontab -e

2. Add the following entry:
   0 2 * * * /usr/bin/python3 /path/to/PyFTP-Backup/pyftp_backup.py >> /path/to/PyFTP-Backup/cron.log 2>&1


--------------------------------------------------------------------------------
7. LOG MANAGEMENT
--------------------------------------------------------------------------------

- Log files are stored in "../logs/backup.log" relative to each project's
  "local_path".
- The clean_old_logs() function runs before each project backup and removes
  lines older than LOG_DAYS.
- To adjust the retention period, edit the constant at the top of pyftp_backup.py:
  LOG_DAYS = 30  # Keep logs for 30 days


--------------------------------------------------------------------------------
8. LICENSE
--------------------------------------------------------------------------------

This project is licensed under the MIT License.