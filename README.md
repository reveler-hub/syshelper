# SysHelper

A screen-reader-friendly, menu-driven system maintenance tool for Linux — checks CPU, memory, and disk health, finds and closes the process or service using the most resources, checks for software updates across Arch, Debian/Ubuntu, Fedora, and openSUSE, and cleans your package cache, all through plain-English prompts instead of Linux commands. No installation, no dependencies, no virtual environment required.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)

## Why this exists

Most system tools assume you're comfortable with a terminal: remembering flags, parsing columnar output, knowing that `systemctl --user --failed` is even a thing. SysHelper is built for two kinds of people instead:

- People using a screen reader, where dense tables, ANSI colour, and cryptic identifiers (like `app-floorp@ff6c460c32fc41f091ca1bc02c33437b.service`) are actively unhelpful.
- People who just want their computer looked after without learning a command-line tool's entire vocabulary first.

Everything is read out as full sentences, every action is menu-driven or a plain number, and nothing destructive happens without an explicit yes/no confirmation.

## Features

- **Numbered menu with number shortcuts** — type `menu` to see your options, then type either the command name or its number. Nothing to memorize.
- **Zero dependencies** — reads CPU and memory straight from `/proc/stat` and `/proc/meminfo`; no third-party libraries, no `pip install`, no virtual environment.
- **Finds what's slowing your computer down** — merges running processes and systemd services into a single deduplicated list (a service and its underlying process are never listed twice), sorted by processor time or memory use, and lets you close or stop the worst offender.
- **Speaks in real names, not unit IDs** — uses systemd's own service descriptions (e.g. "Floorp - Web Browser") instead of raw unit names with random per-instance hashes that a screen reader would otherwise spell out letter by letter.
- **Cross-distro update checking** — auto-detects Arch (`pacman`/`checkupdates`), Debian/Ubuntu (`apt`), Fedora/RHEL (`dnf`), or openSUSE (`zypper`) and checks the right one.
- **Safe privileged actions** — installing updates or cleaning your package cache both ask for confirmation first, show you the exact `sudo` command before running it, and tell you plainly whether it actually succeeded or failed instead of assuming.
- **Battery, uptime, and connectivity checks** — all reported as plain sentences ("Your battery is at 82 percent and is currently charging.").
- **Recent service logs on demand** — pulls the last lines from `journalctl` for a service you pick by number.

## Installation

### Step 1 — Install Python

SysHelper needs Python 3.9 or newer. Most Linux distributions already have this installed — check with `python3 --version`. If you need to install it:

- **Ubuntu/Debian**: `sudo apt install python3`
- **Arch based**: `sudo pacman -Syu python`
- **Fedora**: `sudo dnf install python3`
- **openSUSE**: `sudo zypper install python3`

### Step 2 — System dependencies

None. SysHelper only calls tools your Linux system already ships with: `systemctl`, `ping`, and your distro's own package manager (`pacman`, `apt`, `dnf`, or `zypper`). There is nothing extra to install.

### Step 3 — Download and run

```bash
git clone https://github.com/reveler-hub/syshelper.git
cd syshelper
chmod +x SysHelper.py
./SysHelper.py
```

That's it — no virtual environment needed. `chmod +x` only has to be done once; after that, running it is just `./SysHelper.py`. `python3 SysHelper.py` still works too, if you'd rather not mark it executable. SysHelper is a single file with no third-party dependencies, so it's also safe to copy `SysHelper.py` anywhere and run it the same way.

## Usage

Run the script and you'll land in an interactive prompt:

```
$ ./SysHelper.py
Welcome to SysHelper.
This tool helps you look after your computer without needing to learn
Linux commands. Type 'menu' at any time to see your options, or type
the number of an option directly.

(sys) > menu
Here is what SysHelper can do. Type the name or the number.
1. health - Check your CPU, memory, and disk space
2. cpu - Show what's using the most processor time, and close or stop one
3. ram - Show what's using the most memory, and close or stop one
4. logs - Read the recent error logs for your background services
5. updates - Check for available software updates
6. uptime - Show how long your computer has been running
7. battery - Check battery charge and status, if you have a laptop
8. network - Check if you're connected to the internet
9. cleanup - Clean up old package files to free up disk space
10. quit - Exit SysHelper
(sys) >
```

Type a command name or its number, and press Enter with nothing typed to skip an optional prompt. A few examples:

- `health` — reports CPU usage, memory usage, and free disk space in three sentences.
- `cpu` — lists the top five processes and services by processor time, tells you which one is worst, and lets you type its number to close or stop it (with a yes/no confirmation first).
- `updates` — detects your package manager, lists what's available to update, and asks if you'd like to install them now (or just tells you the command to run yourself later).
- `cleanup` — shows how much space your package cache is using, and only clears it if you confirm.

## Troubleshooting

- **"I don't recognize '...'"** — you typed something that isn't in the menu. Type `menu` to see the exact list of valid commands and numbers.
- **"the 'checkupdates' tool isn't installed" (Arch only)** — `checkupdates` comes from a separate package so update checks don't need your password. Install it with `sudo pacman -S pacman-contrib`.
- **"I couldn't find a supported package manager"** — `updates` and `cleanup` currently support `pacman`, `apt`, `dnf`, and `zypper`. Other package managers aren't recognized yet.
- **Updates or cleanup says it "did not finish successfully"** — this usually means the `sudo` password was wrong, cancelled, or your user account doesn't have `sudo` rights. Nothing is installed or deleted when this happens.
- **"I don't have permission to close" a process** — SysHelper only closes processes and stops services your own user account owns. Anything owned by another user or `root` needs to be handled with elevated tools directly.
- **No battery found on a laptop** — SysHelper looks for a `BAT*` entry under `/sys/class/power_supply`. If your battery uses a non-standard name, it may not be detected.

## License

[MIT](LICENSE)
