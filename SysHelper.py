#!/usr/bin/env python3
import cmd
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from typing import ClassVar

# SysHelper only uses the Python standard library plus command-line tools
# that already ship on the system (systemctl, ping, and whichever package
# manager the distro uses). Nothing here needs to be pip installed, so
# people can run this file directly without setting up a virtual
# environment first.


def read_cpu_times():
    """Read total and idle CPU time from /proc/stat."""
    with open('/proc/stat', 'r') as f:
        fields = [float(column) for column in f.readline().strip().split()[1:]]
    idle = fields[3]
    total = sum(fields)
    return idle, total


def read_memory_percent():
    """Return the percentage of memory currently in use."""
    mem_info = {}
    with open('/proc/meminfo', 'r') as f:
        for line in f:
            if line.startswith(('MemTotal', 'MemAvailable')):
                parts = line.split()
                mem_info[parts[0].strip(':')] = int(parts[1])
    mem_total = mem_info.get('MemTotal', 1)
    mem_avail = mem_info.get('MemAvailable', 0)
    return 100.0 * (1.0 - (mem_avail / mem_total))


def list_pids():
    return [name for name in os.listdir('/proc') if name.isdigit()]


def read_process_name(pid):
    try:
        with open(f'/proc/{pid}/comm', 'r') as f:
            return f.read().strip()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def read_process_cpu_ticks(pid):
    """Return total (user + system) CPU ticks used by a process so far."""
    try:
        with open(f'/proc/{pid}/stat', 'r') as f:
            content = f.read()
        # The process name is in parentheses and may itself contain spaces
        # or parentheses, so split on the last ')' to safely skip past it.
        after_name = content.rsplit(')', 1)[1].split()
        utime = float(after_name[11])
        stime = float(after_name[12])
        return utime + stime
    except (FileNotFoundError, ProcessLookupError, PermissionError,
            IndexError, ValueError):
        return None


def read_process_memory_kb(pid):
    try:
        with open(f'/proc/{pid}/status', 'r') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    return None


def directory_size_bytes(path):
    total = 0
    for dirpath, dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, filename))
            except OSError:
                pass
    return total


def sample_resource_usage(pids, interval=0.5):
    """Sample CPU percent (relative to one core) and memory in MB for each pid."""
    ticks_before = {}
    for pid in pids:
        ticks = read_process_cpu_ticks(pid)
        if ticks is not None:
            ticks_before[pid] = ticks

    time.sleep(interval)

    try:
        clock_ticks_per_second = os.sysconf('SC_CLK_TCK')
    except (ValueError, AttributeError):
        clock_ticks_per_second = 100

    usage = {}
    for pid in pids:
        cpu_percent = 0.0
        if pid in ticks_before:
            ticks_after = read_process_cpu_ticks(pid)
            if ticks_after is not None:
                delta_ticks = ticks_after - ticks_before[pid]
                cpu_percent = 100.0 * (delta_ticks / clock_ticks_per_second) / interval

        rss_kb = read_process_memory_kb(pid)
        mem_mb = (rss_kb / 1024.0) if rss_kb else 0.0

        usage[pid] = (cpu_percent, mem_mb)
    return usage


def parse_unit_descriptions(systemctl_output):
    """Parse 'UNIT LOAD ACTIVE SUB DESCRIPTION' lines into {unit_name: description}.

    Unit names often embed a random per-instance ID (e.g.
    'app-floorp@ff6c460c...service'), which a screen reader reads out
    character by character. systemd's own description column ('Floorp -
    Web Browser') is what people should actually hear, so it's used for
    every user-facing label; the raw unit name is kept only for the
    systemctl calls that need it.
    """
    descriptions = {}
    for line in systemctl_output.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(None, 4)
        if not parts:
            continue
        unit_name = parts[0]
        description = parts[4].strip() if len(parts) >= 5 else unit_name
        descriptions[unit_name] = description
    return descriptions


def get_running_user_services():
    """Return (unit_name, pid, description) for each running service with a main pid."""
    result = subprocess.run(
        ['systemctl', '--user', 'list-units', '--type=service',
         '--state=running', '--no-legend', '--plain'],
        capture_output=True, text=True, check=False
    )
    unit_descriptions = parse_unit_descriptions(result.stdout)
    unit_names = list(unit_descriptions.keys())
    if not unit_names:
        return []

    show_result = subprocess.run(
        ['systemctl', '--user', 'show', *unit_names,
         '-p', 'Id', '-p', 'MainPID', '--value'],
        capture_output=True, text=True, check=False
    )

    services = []
    blocks = show_result.stdout.strip('\n').split('\n\n')
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) >= 2:
            unit_id, pid_str = lines[0].strip(), lines[1].strip()
            if pid_str.isdigit() and int(pid_str) != 0:
                description = unit_descriptions.get(unit_id, unit_id)
                services.append((unit_id, pid_str, description))
    return services


def kill_process(pid_str, name):
    """Close a process by pid, escalating from a polite request to a forced close."""
    try:
        pid = int(pid_str)
    except ValueError:
        print(f"I couldn't identify '{name}' anymore.")
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        print(f"'{name}' isn't running anymore.")
        return
    except PermissionError:
        print(f"I don't have permission to close '{name}'. "
              "It may need administrator rights.")
        return

    time.sleep(0.5)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        print(f"Closed '{name}'.")
        return
    except PermissionError:
        print(f"Closed '{name}'.")
        return

    try:
        os.kill(pid, signal.SIGKILL)
        print(f"'{name}' didn't close right away, so I closed it forcefully.")
    except (ProcessLookupError, PermissionError):
        print(f"Closed '{name}'.")


def stop_service(unit_name):
    result = subprocess.run(
        ['systemctl', '--user', 'stop', unit_name],
        capture_output=True, text=True, check=False
    )
    if result.returncode == 0:
        print(f"Stopped '{unit_name}'.")
    else:
        print(f"I couldn't stop '{unit_name}'. {result.stderr.strip()}")


def run_with_sudo(command, action_description):
    """Run a command that needs administrator rights, and report whether it worked.

    The command's own output is left connected to the terminal (not
    captured) so the password prompt sudo shows, and any yes/no prompt
    the command itself shows, both still reach the person running it.
    Once it finishes, the exit code is checked and the result is stated
    plainly, since silently assuming success would leave a screen reader
    user with no way to tell a failed password or a declined prompt from
    a real success.
    """
    if shutil.which('sudo') is None:
        print("I can't do that because the 'sudo' command isn't "
              "available on this computer.")
        return False

    full_command = ['sudo', *command]
    print(f"Running: {' '.join(full_command)}")
    result = subprocess.run(full_command, check=False)

    if result.returncode == 0:
        print(f"{action_description} finished successfully.")
        return True
    print(f"{action_description} did not finish successfully "
          f"(exit code {result.returncode}). Nothing may have changed.")
    return False


SPEEDTEST_URL = 'https://speed.cloudflare.com/__down?bytes=10000000'


def measure_download_speed_mbps(url=SPEEDTEST_URL):
    """Download a test file once and return the speed in megabits per second.

    Returns None if the download fails (no internet, host unreachable,
    and so on) so callers can report that plainly instead of crashing.
    """
    # Cloudflare rejects requests with urllib's default User-Agent (it
    # looks like a bot), so a normal browser-style one is sent instead.
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=15) as response:
            total_bytes = 0
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
        elapsed = time.monotonic() - start
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    if elapsed <= 0 or total_bytes == 0:
        return None
    return (total_bytes * 8) / elapsed / 1_000_000


SPEEDTEST_UPLOAD_URL = 'https://speed.cloudflare.com/__up'
SPEEDTEST_UPLOAD_BYTES = 1_000_000


def measure_upload_speed_mbps(
    url=SPEEDTEST_UPLOAD_URL, num_bytes=SPEEDTEST_UPLOAD_BYTES
):
    """Upload random test data once and return the speed in megabits per second.

    Returns None if the upload fails, the same way
    measure_download_speed_mbps() does.
    """
    payload = os.urandom(num_bytes)
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Content-Type': 'application/octet-stream',
    }
    request = urllib.request.Request(url, data=payload, method='POST', headers=headers)
    try:
        start = time.monotonic()
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        elapsed = time.monotonic() - start
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    if elapsed <= 0:
        return None
    return (num_bytes * 8) / elapsed / 1_000_000


def build_resource_items():
    """Return one entry per running pid, merging processes and services with no repeats.

    A pid that is the main process of a running user service is labeled
    and acted on as that service (a friendly description, stopped via
    systemctl); every other pid is labeled and acted on as a plain
    process (its /proc name, closed directly). Each entry carries
    'cpu_percent' and 'mem_mb' so callers can sort by either.
    """
    service_by_pid = {
        pid: (unit_name, description)
        for unit_name, pid, description in get_running_user_services()
    }

    names = {}
    for pid in list_pids():
        name = read_process_name(pid)
        if name is not None:
            names[pid] = name

    usage = sample_resource_usage(list(names.keys()))

    items = []
    for pid, name in names.items():
        cpu_percent, mem_mb = usage.get(pid, (0.0, 0.0))
        if pid in service_by_pid:
            unit_name, description = service_by_pid[pid]
            items.append({
                'label': description,
                'verb': 'stop',
                'act': lambda unit=unit_name: stop_service(unit),
                'cpu_percent': cpu_percent,
                'mem_mb': mem_mb,
            })
        else:
            items.append({
                'label': name,
                'verb': 'close',
                'act': lambda pid=pid, name=name: kill_process(pid, name),
                'cpu_percent': cpu_percent,
                'mem_mb': mem_mb,
            })
    return items


def check_pacman_updates():
    """Return (package_names, install_command), or None if updates can't be checked.

    install_command excludes 'sudo' itself; run_with_sudo() adds that.
    """
    if shutil.which('checkupdates') is None:
        print("I can't check for updates yet because the 'checkupdates' "
              "tool isn't installed.")
        print("It comes from the 'pacman-contrib' package and checks for "
              "updates safely, without needing your password.")
        print("You can install it by typing: sudo pacman -S pacman-contrib")
        return None

    result = subprocess.run(
        ['checkupdates'], capture_output=True, text=True, check=False
    )
    packages = [
        line.split()[0] for line in result.stdout.strip().split('\n') if line.strip()
    ]
    return packages, ['pacman', '-Syu']


def check_apt_updates():
    """Return (package_names, install_command) for Debian, Ubuntu, and similar."""
    result = subprocess.run(
        ['apt', 'list', '--upgradable'], capture_output=True, text=True, check=False
    )
    packages = [
        line.split('/')[0].strip()
        for line in result.stdout.strip().split('\n')
        if '/' in line
    ]
    return packages, ['apt', 'upgrade']


def check_dnf_updates():
    """Return (package_names, install_command) for Fedora, RHEL, and similar distros."""
    result = subprocess.run(
        ['dnf', 'check-update'], capture_output=True, text=True, check=False
    )
    packages = []
    for line in result.stdout.strip().split('\n'):
        parts = line.split()
        # Real package rows are exactly "name.arch  version  repo", e.g.
        # "bash.x86_64  5.1.16-1.fc36  updates". Notices like "Last
        # metadata expiration check..." have more than three columns and
        # no dot in the first one, so this skips them along with blanks.
        if len(parts) == 3 and '.' in parts[0]:
            packages.append(parts[0].rsplit('.', 1)[0])
    return packages, ['dnf', 'upgrade']


def check_zypper_updates():
    """Return (package_names, install_command) for openSUSE and similar distros."""
    result = subprocess.run(
        ['zypper', '--non-interactive', 'list-updates'],
        capture_output=True, text=True, check=False
    )
    packages = []
    for line in result.stdout.strip().split('\n'):
        columns = [column.strip() for column in line.split('|')]
        # zypper's table marks each upgradable package with 'v' in the
        # first column; that also skips the header and separator rows.
        if len(columns) >= 3 and columns[0] == 'v':
            packages.append(columns[2])
    return packages, ['zypper', 'update']


# One entry per supported package manager: the command that reveals it's
# installed, the function that checks it for updates, where it caches
# downloaded package files, and the command that clears that cache.
# Checked in this order so the first one actually installed on the
# computer is the one that's used.
# cleanup_command excludes 'sudo' itself; run_with_sudo() adds that.
PACKAGE_MANAGERS = [
    {
        'command': 'pacman',
        'check_updates': check_pacman_updates,
        'cache_dir': '/var/cache/pacman/pkg',
        'cleanup_command': ['pacman', '-Sc'],
    },
    {
        'command': 'apt',
        'check_updates': check_apt_updates,
        'cache_dir': '/var/cache/apt/archives',
        'cleanup_command': ['apt', 'clean'],
    },
    {
        'command': 'dnf',
        'check_updates': check_dnf_updates,
        'cache_dir': '/var/cache/dnf',
        'cleanup_command': ['dnf', 'clean', 'packages'],
    },
    {
        'command': 'zypper',
        'check_updates': check_zypper_updates,
        'cache_dir': '/var/cache/zypp/packages',
        'cleanup_command': ['zypper', 'clean', '--all'],
    },
]


def detect_package_manager():
    """Return the first PACKAGE_MANAGERS entry actually installed, or None."""
    for package_manager in PACKAGE_MANAGERS:
        if shutil.which(package_manager['command']) is not None:
            return package_manager
    return None


class SysHelper(cmd.Cmd):
    intro = (
        "Welcome to SysHelper.\n"
        "This tool helps you look after your computer without needing to learn\n"
        "Linux commands. Type 'menu' at any time to see your options, or type\n"
        "the number of an option directly.\n"
    )
    prompt = "(sys) > "

    # Each entry is (command name, plain-English description).
    # This drives both the numbered menu and number-based shortcuts.
    MENU: ClassVar[list[tuple[str, str]]] = [
        ("health", "Check your CPU, memory, and disk space"),
        ("cpu", "Show what's using the most processor time, and close or stop one"),
        ("ram", "Show what's using the most memory, and close or stop one"),
        ("logs", "Read the recent error logs for your background services"),
        ("updates", "Check for available software updates"),
        ("uptime", "Show how long your computer has been running"),
        ("battery", "Check battery charge and status, if you have a laptop"),
        ("network", "Check if you're connected to the internet"),
        ("speedtest", "Test your internet download and upload speed"),
        ("cleanup", "Clean up old package files to free up disk space"),
        ("quit", "Exit SysHelper"),
    ]

    def do_menu(self, arg):
        """Show the list of things SysHelper can do."""
        print("Here is what SysHelper can do. Type the name or the number.")
        for i, (name, description) in enumerate(self.MENU, 1):
            print(f"{i}. {name} - {description}")

    def do_help(self, arg):
        """Show this menu, or details about one option (help <name>)."""
        if arg:
            cmd.Cmd.do_help(self, arg)
        else:
            self.do_menu(arg)

    def prompt_for_action(self, actionable_items):
        """Let the user pick one numbered item to act on (close, stop, or restart)."""
        if not actionable_items:
            return
        print("Type the number of one to act on it, or press Enter to skip.")
        choice = input("> ").strip()
        if not choice:
            print("Okay, leaving things as they are.")
            return
        if not choice.isdigit():
            print("I didn't understand that, so I'm leaving things as they are.")
            return
        index = int(choice) - 1
        if not (0 <= index < len(actionable_items)):
            print("That number wasn't in the list, so I'm leaving things as they are.")
            return

        item = actionable_items[index]
        confirm = input(
            f"Are you sure you want to {item['verb']} '{item['label']}'? "
            "Type yes to confirm: "
        ).strip().lower()
        if confirm not in ('yes', 'y'):
            print("Okay, I won't do that.")
            return
        item['act']()

    def default(self, line):
        choice = line.strip()
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(self.MENU):
                command_name = self.MENU[index][0]
                return self.onecmd(command_name)
            print("That number isn't on the menu. "
                  "Type 'menu' to see the options again.")
            return
        print(f"I don't recognize '{line}'. Type 'menu' to see what SysHelper can do.")

    def do_health(self, arg):
        """Check system health: CPU usage, memory usage, and free disk space."""
        print("Checking system health...")

        idle1, total1 = read_cpu_times()
        time.sleep(0.5)
        idle2, total2 = read_cpu_times()
        idle_delta = idle2 - idle1
        total_delta = total2 - total1
        cpu_usage = 100.0 * (1.0 - idle_delta / total_delta) if total_delta else 0.0
        print(f"CPU usage is at {cpu_usage:.1f} percent.")

        mem_used_percent = read_memory_percent()
        print(f"Memory is {mem_used_percent:.1f} percent full.")

        disk = shutil.disk_usage('/')
        free_gb = disk.free / (1024 ** 3)
        print(f"Main storage has {free_gb:.1f} gigabytes remaining.")

    def do_cpu(self, arg):
        """Show what's using the most processor time, and close or stop one."""
        print("Checking what's using your processor, please wait...")
        items = [item for item in build_resource_items() if item['cpu_percent'] > 0]
        items.sort(key=lambda item: item['cpu_percent'], reverse=True)
        top_items = items[:5]

        if top_items:
            print("Using the most processor time:")
            for i, item in enumerate(top_items, 1):
                print(f"{i}. {item['label']} - {item['cpu_percent']:.1f} percent")
            print(f"'{top_items[0]['label']}' is using the most processor time.")
        else:
            print("Nothing is using noticeable processor time right now.")

        self.prompt_for_action(top_items)

    def do_updates(self, arg):
        """Check for available software updates."""
        package_manager = detect_package_manager()
        if package_manager is None:
            print("I couldn't find a supported package manager (pacman, apt, "
                  "dnf, or zypper) on this computer, so I can't check for updates.")
            return

        print("Checking for updates. This may take a moment...")
        result = package_manager['check_updates']()
        if result is None:
            return

        packages, install_command = result
        if not packages:
            print("Your system is up to date.")
            return

        print(f"Found {len(packages)} update(s) available:")
        for i, package_name in enumerate(packages, 1):
            print(f"{i}. {package_name}")

        answer = input(
            "Would you like to install these updates now? This may take a "
            "while and will ask for your password. Type yes or no: "
        ).strip().lower()
        if answer not in ('yes', 'y'):
            command_text = ' '.join(['sudo', *install_command])
            print(f"Okay, I won't install anything. To do it yourself "
                  f"later, type: {command_text}")
            return

        run_with_sudo(install_command, "The update")

    def do_ram(self, arg):
        """Show what's using the most memory, and close or stop one."""
        print("Checking what's using your memory, please wait...")
        items = build_resource_items()
        items.sort(key=lambda item: item['mem_mb'], reverse=True)
        top_items = items[:5]

        if top_items:
            print("Using the most memory:")
            for i, item in enumerate(top_items, 1):
                print(f"{i}. {item['label']} - {item['mem_mb']:.1f} megabytes")
            print(f"'{top_items[0]['label']}' is using the most memory.")
        else:
            print("Nothing appears to be using noticeable memory right now.")

        self.prompt_for_action(top_items)

    def do_logs(self, arg):
        """View the most recent log entries for your background services."""
        print("Fetching your background services...")

        # Grab running and failed user services
        result = subprocess.run(
            ['systemctl', '--user', 'list-units', '--type=service',
             '--state=running,failed', '--no-legend', '--plain'],
            capture_output=True, text=True, check=False
        )

        output = result.stdout.strip()
        if not output:
            print("You don't have any running or failed services right now.")
            return

        # We can reuse your existing parser!
        descriptions = parse_unit_descriptions(output)
        items = list(descriptions.items())

        print("Which service's logs would you like to read? Type a number:")
        for i, (unit_name, desc) in enumerate(items, 1):
            print(f"{i}. {desc}")

        choice = input("> ").strip()
        if not choice.isdigit() or not (1 <= int(choice) <= len(items)):
            print("Okay, returning to the main menu.")
            return

        unit_name = items[int(choice) - 1][0]
        friendly_name = items[int(choice) - 1][1]

        print(f"\n--- Last 15 lines of {friendly_name} ---")

        # Fetch the last 15 lines of logs for the specific user unit
        log_result = subprocess.run(
            ['journalctl', '--user', '-u', unit_name, '-n', '15', '--no-pager'],
            capture_output=True, text=True, check=False
        )

        logs = log_result.stdout.strip()
        if logs:
            print(logs)
        else:
            print("No recent logs found for this service.")
        print("-" * 40 + "\n")

    def do_uptime(self, arg):
        """Show how long the computer has been running since it last started."""
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])

        days, remainder = divmod(int(uptime_seconds), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)

        parts = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

        print("Your computer has been running for " + ", ".join(parts) + ".")

    def do_battery(self, arg):
        """Check battery charge and status, if this computer has a battery."""
        no_battery_message = (
            "This computer doesn't appear to have a battery. "
            "It might be a desktop computer."
        )

        power_supply_dir = '/sys/class/power_supply'
        if not os.path.isdir(power_supply_dir):
            print(no_battery_message)
            return

        battery_names = [
            name for name in os.listdir(power_supply_dir)
            if name.startswith('BAT')
        ]
        if not battery_names:
            print(no_battery_message)
            return

        battery_path = os.path.join(power_supply_dir, battery_names[0])
        try:
            with open(os.path.join(battery_path, 'capacity'), 'r') as f:
                capacity = f.read().strip()
            with open(os.path.join(battery_path, 'status'), 'r') as f:
                status = f.read().strip()
        except FileNotFoundError:
            print("I found a battery, but couldn't read its status.")
            return

        print(f"Your battery is at {capacity} percent and is currently "
              f"{status.lower()}.")

    def do_network(self, arg):
        """Check whether this computer is connected to the internet."""
        print("Checking your internet connection...")
        try:
            result = subprocess.run(
                ['ping', '-c', '1', '-W', '3', '1.1.1.1'],
                capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            print("Error: the ping command isn't available on this system.")
            return

        if result.returncode == 0:
            print("You're connected to the internet.")
        else:
            print("I couldn't reach the internet. You might be disconnected, "
                  "or a website might be down.")

    def do_speedtest(self, arg):
        """Test your internet download and upload speed, three times each."""
        print("Testing your internet speed. This runs three times and "
              "takes a moment...")

        download_results = []
        upload_results = []
        for i in range(1, 4):
            print(f"Run {i} of 3...")

            download_mbps = measure_download_speed_mbps()
            if download_mbps is None:
                print("I couldn't complete the download part of that run. "
                      "You might be disconnected, or the test server might "
                      "be unreachable.")
            else:
                print(f"Run {i} download: {download_mbps:.1f} megabits per second.")
                download_results.append(download_mbps)

            upload_mbps = measure_upload_speed_mbps()
            if upload_mbps is None:
                print("I couldn't complete the upload part of that run.")
            else:
                print(f"Run {i} upload: {upload_mbps:.1f} megabits per second.")
                upload_results.append(upload_mbps)

        if not download_results and not upload_results:
            print("I wasn't able to complete any test runs, so I can't "
                  "report a speed.")
            return

        if download_results:
            average_download = sum(download_results) / len(download_results)
            print(f"Average download speed over {len(download_results)} "
                  f"run(s): {average_download:.1f} megabits per second.")
        else:
            print("None of the download runs completed.")

        if upload_results:
            average_upload = sum(upload_results) / len(upload_results)
            print(f"Average upload speed over {len(upload_results)} "
                  f"run(s): {average_upload:.1f} megabits per second.")
        else:
            print("None of the upload runs completed.")

    def do_cleanup(self, arg):
        """Clean up old package files that take up disk space."""
        package_manager = detect_package_manager()
        if package_manager is None:
            print("I couldn't find a supported package manager (pacman, apt, "
                  "dnf, or zypper) on this computer, so I can't clean up a "
                  "package cache.")
            return

        cache_dir = package_manager['cache_dir']
        if not os.path.isdir(cache_dir):
            print("I couldn't find a package cache to clean up on this computer.")
            return

        size_gb = directory_size_bytes(cache_dir) / (1024 ** 3)
        print(f"Your package cache is using {size_gb:.1f} gigabytes of space.")

        answer = input(
            "Would you like to clean it up now? This removes old, unused package "
            "files and will ask for your password. Type yes or no: "
        ).strip().lower()
        if answer not in ('yes', 'y'):
            print("Okay, I won't clean anything up.")
            return

        run_with_sudo(package_manager['cleanup_command'], "Cleanup")

    def do_quit(self, arg):
        """Exit the application."""
        print("Goodbye.")
        return True

    # Map 'exit' and 'EOF' (Ctrl+D) to the quit command
    do_exit = do_quit
    do_EOF = do_quit


if __name__ == '__main__':
    SysHelper().cmdloop()
