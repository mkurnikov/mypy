"""Client for mypy daemon mode.

Highly experimental!  Only supports UNIX-like systems.

This manages a daemon process which keeps useful state in memory
rather than having to read it back from disk on each run.
"""

import argparse
import base64
import json
import os
import pickle
import signal
import subprocess
import sys
import time

from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from mypy.dmypy_util import STATUS_FILE, receive
from mypy.ipc import IPCClient, IPCException
from mypy.dmypy_os import alive, kill

from mypy.version import __version__

# Argument parser.  Subparsers are tied to action functions by the
# @action(subparse) decorator.


class AugmentedHelpFormatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:
        super().__init__(prog=prog, max_help_position=30)


parser = argparse.ArgumentParser(description="Client for mypy daemon mode",
                                 fromfile_prefix_chars='@')
parser.set_defaults(action=None)
subparsers = parser.add_subparsers()

start_parser = p = subparsers.add_parser('start', help="Start daemon")
p.add_argument('--log-file', metavar='FILE', type=str,
               help="Direct daemon stdout/stderr to FILE")
p.add_argument('--timeout', metavar='TIMEOUT', type=int,
               help="Server shutdown timeout (in seconds)")
p.add_argument('flags', metavar='FLAG', nargs='*', type=str,
               help="Regular mypy flags (precede with --)")

restart_parser = p = subparsers.add_parser('restart',
    help="Restart daemon (stop or kill followed by start)")
p.add_argument('--log-file', metavar='FILE', type=str,
               help="Direct daemon stdout/stderr to FILE")
p.add_argument('--timeout', metavar='TIMEOUT', type=int,
               help="Server shutdown timeout (in seconds)")
p.add_argument('flags', metavar='FLAG', nargs='*', type=str,
               help="Regular mypy flags (precede with --)")

status_parser = p = subparsers.add_parser('status', help="Show daemon status")
p.add_argument('-v', '--verbose', action='store_true', help="Print detailed status")

stop_parser = p = subparsers.add_parser('stop', help="Stop daemon (asks it politely to go away)")

kill_parser = p = subparsers.add_parser('kill', help="Kill daemon (kills the process)")

check_parser = p = subparsers.add_parser('check', formatter_class=AugmentedHelpFormatter,
                                         help="Check some files (requires daemon)")
p.add_argument('-v', '--verbose', action='store_true', help="Print detailed status")
p.add_argument('-q', '--quiet', action='store_true', help=argparse.SUPPRESS)  # Deprecated
p.add_argument('--junit-xml', help="Write junit.xml to the given file")
p.add_argument('files', metavar='FILE', nargs='+', help="File (or directory) to check")

run_parser = p = subparsers.add_parser('run', formatter_class=AugmentedHelpFormatter,
                                       help="Check some files, [re]starting daemon if necessary")
p.add_argument('-v', '--verbose', action='store_true', help="Print detailed status")
p.add_argument('--junit-xml', help="Write junit.xml to the given file")
p.add_argument('--timeout', metavar='TIMEOUT', type=int,
               help="Server shutdown timeout (in seconds)")
p.add_argument('--log-file', metavar='FILE', type=str,
               help="Direct daemon stdout/stderr to FILE")
p.add_argument('flags', metavar='ARG', nargs='*', type=str,
               help="Regular mypy flags and files (precede with --)")

recheck_parser = p = subparsers.add_parser('recheck', formatter_class=AugmentedHelpFormatter,
    help="Re-check the previous list of files, with optional modifications (requires daemon).")
p.add_argument('-v', '--verbose', action='store_true', help="Print detailed status")
p.add_argument('-q', '--quiet', action='store_true', help=argparse.SUPPRESS)  # Deprecated
p.add_argument('--junit-xml', help="Write junit.xml to the given file")
p.add_argument('--update', metavar='FILE', nargs='*',
               help="Files in the run to add or check again (default: all from previous run)..")
p.add_argument('--remove', metavar='FILE', nargs='*',
               help="Files to remove from the run")

hang_parser = p = subparsers.add_parser('hang', help="Hang for 100 seconds")

daemon_parser = p = subparsers.add_parser('daemon', help="Run daemon in foreground")
p.add_argument('--timeout', metavar='TIMEOUT', type=int,
               help="Server shutdown timeout (in seconds)")
p.add_argument('flags', metavar='FLAG', nargs='*', type=str,
               help="Regular mypy flags (precede with --)")
p.add_argument('--options-data', help=argparse.SUPPRESS)
help_parser = p = subparsers.add_parser('help')

del p


class BadStatus(Exception):
    """Exception raised when there is something wrong with the status file.

    For example:
    - No status file found
    - Status file malformed
    - Process whose pid is in the status file does not exist
    """
    pass


def main() -> None:
    """The code is top-down."""
    args = parser.parse_args()
    if not args.action:
        parser.print_usage()
    else:
        try:
            args.action(args)
        except BadStatus as err:
            sys.exit(err.args[0])


ActionFunction = Callable[[argparse.Namespace], None]


def action(subparser: argparse.ArgumentParser) -> Callable[[ActionFunction], ActionFunction]:
    """Decorator to tie an action function to a subparser."""
    def register(func: ActionFunction) -> ActionFunction:
        subparser.set_defaults(action=func)
        return func
    return register


# Action functions (run in client from command line).

@action(start_parser)
def do_start(args: argparse.Namespace) -> None:
    """Start daemon (it must not already be running).

    This is where mypy flags are set from the command line.

    Setting flags is a bit awkward; you have to use e.g.:

      dmypy start -- --strict

    since we don't want to duplicate mypy's huge list of flags.
    """
    try:
        get_status()
    except BadStatus:
        # Bad or missing status file or dead process; good to start.
        pass
    else:
        sys.exit("Daemon is still alive")
    start_server(args)


@action(restart_parser)
def do_restart(args: argparse.Namespace) -> None:
    """Restart daemon (it may or may not be running; but not hanging).

    We first try to stop it politely if it's running.  This also sets
    mypy flags from the command line (see do_start()).
    """
    restart_server(args)


def restart_server(args: argparse.Namespace, allow_sources: bool = False) -> None:
    """Restart daemon (it may or may not be running; but not hanging)."""
    try:
        do_stop(args)
    except BadStatus:
        # Bad or missing status file or dead process; good to start.
        pass
    start_server(args, allow_sources)


def start_server(args: argparse.Namespace, allow_sources: bool = False) -> None:
    """Start the server from command arguments and wait for it."""
    # Lazy import so this import doesn't slow down other commands.
    from mypy.dmypy_server import daemonize, process_start_options
    start_options = process_start_options(args.flags, allow_sources)
    if daemonize(start_options, timeout=args.timeout, log_file=args.log_file):
        sys.exit(1)
    wait_for_server()


def wait_for_server(timeout: float = 5.0) -> None:
    """Wait until the server is up.

    Exit if it doesn't happen within the timeout.
    """
    endtime = time.time() + timeout
    while time.time() < endtime:
        try:
            data = read_status()
        except BadStatus:
            # If the file isn't there yet, retry later.
            time.sleep(0.1)
            continue
        # If the file's content is bogus or the process is dead, fail.
        check_status(data)
        print("Daemon started")
        return
    sys.exit("Timed out waiting for daemon to start")


@action(run_parser)
def do_run(args: argparse.Namespace) -> None:
    """Do a check, starting (or restarting) the daemon as necessary

    Restarts the daemon if the running daemon reports that it is
    required (due to a configuration change, for example).

    Setting flags is a bit awkward; you have to use e.g.:

      dmypy run -- --strict a.py b.py ...

    since we don't want to duplicate mypy's huge list of flags.
    (The -- is only necessary if flags are specified.)
    """
    if not is_running():
        # Bad or missing status file or dead process; good to start.
        start_server(args, allow_sources=True)
    t0 = time.time()
    response = request('run', version=__version__, args=args.flags)
    # If the daemon signals that a restart is necessary, do it
    if 'restart' in response:
        print('Restarting: {}'.format(response['restart']))
        restart_server(args, allow_sources=True)
        response = request('run', version=__version__, args=args.flags)

    t1 = time.time()
    response['roundtrip_time'] = t1 - t0
    check_output(response, args.verbose, args.junit_xml)


@action(status_parser)
def do_status(args: argparse.Namespace) -> None:
    """Print daemon status.

    This verifies that it is responsive to requests.
    """
    status = read_status()
    if args.verbose:
        show_stats(status)
    # Both check_status() and request() may raise BadStatus,
    # which will be handled by main().
    check_status(status)
    response = request('status', timeout=5)
    if args.verbose or 'error' in response:
        show_stats(response)
    if 'error' in response:
        sys.exit("Daemon is stuck; consider %s kill" % sys.argv[0])
    print("Daemon is up and running")


@action(stop_parser)
def do_stop(args: argparse.Namespace) -> None:
    """Stop daemon via a 'stop' request."""
    # May raise BadStatus, which will be handled by main().
    response = request('stop', timeout=5)
    if response:
        show_stats(response)
        sys.exit("Daemon is stuck; consider %s kill" % sys.argv[0])
    else:
        print("Daemon stopped")


@action(kill_parser)
def do_kill(args: argparse.Namespace) -> None:
    """Kill daemon process with SIGKILL."""
    pid, _ = get_status()
    try:
        kill(pid)
    except OSError as err:
        sys.exit(str(err))
    else:
        print("Daemon killed")


@action(check_parser)
def do_check(args: argparse.Namespace) -> None:
    """Ask the daemon to check a list of files."""
    t0 = time.time()
    response = request('check', files=args.files)
    t1 = time.time()
    response['roundtrip_time'] = t1 - t0
    check_output(response, args.verbose, args.junit_xml)


@action(recheck_parser)
def do_recheck(args: argparse.Namespace) -> None:
    """Ask the daemon to recheck the previous list of files, with optional modifications.

    If at least one of --remove or --update is given, the server will
    update the list of files to check accordingly and assume that any other files
    are unchanged.  If none of these flags are given, the server will call stat()
    on each file last checked to determine its status.

    Files given in --update ought to exist.  Files given in --remove need not exist;
    if they don't they will be ignored.
    The lists may be empty but oughtn't contain duplicates or overlap.

    NOTE: The list of files is lost when the daemon is restarted.
    """
    t0 = time.time()
    if args.remove is not None or args.update is not None:
        response = request('recheck', remove=args.remove, update=args.update)
    else:
        response = request('recheck')
    t1 = time.time()
    response['roundtrip_time'] = t1 - t0
    check_output(response, args.verbose, args.junit_xml)


def check_output(response: Dict[str, Any], verbose: bool, junit_xml: Optional[str]) -> None:
    """Print the output from a check or recheck command.

    Call sys.exit() unless the status code is zero.
    """
    if 'error' in response:
        sys.exit(response['error'])
    try:
        out, err, status_code = response['out'], response['err'], response['status']
    except KeyError:
        sys.exit("Response: %s" % str(response))
    sys.stdout.write(out)
    sys.stderr.write(err)
    if verbose:
        show_stats(response)
    if junit_xml:
        # Lazy import so this import doesn't slow things down when not writing junit
        from mypy.util import write_junit_xml
        messages = (out + err).splitlines()
        write_junit_xml(response['roundtrip_time'], bool(err), messages, junit_xml)
    if status_code:
        sys.exit(status_code)


def show_stats(response: Mapping[str, object]) -> None:
    for key, value in sorted(response.items()):
        if key not in ('out', 'err'):
            print("%-24s: %10s" % (key, "%.3f" % value if isinstance(value, float) else value))
        else:
            value = str(value).replace('\n', '\\n')
            if len(value) > 50:
                value = value[:40] + ' ...'
            print("%-24s: %s" % (key, value))


@action(hang_parser)
def do_hang(args: argparse.Namespace) -> None:
    """Hang for 100 seconds, as a debug hack."""
    print(request('hang', timeout=1))


@action(daemon_parser)
def do_daemon(args: argparse.Namespace) -> None:
    """Serve requests in the foreground."""
    # Lazy import so this import doesn't slow down other commands.
    from mypy.dmypy_server import Server, process_start_options
    if args.options_data:
        from mypy.options import Options
        options_dict, timeout, log_file = pickle.loads(base64.b64decode(args.options_data))
        options_obj = Options()
        options = options_obj.apply_changes(options_dict)
        if log_file:
            sys.stdout = sys.stderr = open(log_file, 'a', buffering=1)
            fd = sys.stdout.fileno()
            os.dup2(fd, 2)
            os.dup2(fd, 1)
    else:
        options = process_start_options(args.flags, allow_sources=False)
        timeout = args.timeout
    Server(options, timeout=timeout).serve()


@action(help_parser)
def do_help(args: argparse.Namespace) -> None:
    """Print full help (same as dmypy --help)."""
    parser.print_help()


# Client-side infrastructure.


def request(command: str, *, timeout: Optional[int] = None,
            **kwds: object) -> Dict[str, Any]:
    """Send a request to the daemon.

    Return the JSON dict with the response.

    Raise BadStatus if there is something wrong with the status file
    or if the process whose pid is in the status file has died.

    Return {'error': <message>} if an IPC operation or receive()
    raised OSError.  This covers cases such as connection refused or
    closed prematurely as well as invalid JSON received.
    """
    response = {}  # type: Dict[str, str]
    args = dict(kwds)
    args.update(command=command)
    bdata = json.dumps(args).encode('utf8')
    _, name = get_status()
    try:
        with IPCClient(name, timeout) as client:
                client.write(bdata)
                response = receive(client)
    except (OSError, IPCException) as err:
        return {'error': str(err)}
    # TODO: Other errors, e.g. ValueError, UnicodeError
    else:
        return response


def get_status() -> Tuple[int, str]:
    """Read status file and check if the process is alive.

    Return (pid, connection_name) on success.

    Raise BadStatus if something's wrong.
    """
    data = read_status()
    return check_status(data)


def check_status(data: Dict[str, Any]) -> Tuple[int, str]:
    """Check if the process is alive.

    Return (pid, connection_name) on success.

    Raise BadStatus if something's wrong.
    """
    if 'pid' not in data:
        raise BadStatus("Invalid status file (no pid field)")
    pid = data['pid']
    if not isinstance(pid, int):
        raise BadStatus("pid field is not an int")
    if not alive(pid):
        raise BadStatus("Daemon has died")
    if 'connection_name' not in data:
        raise BadStatus("Invalid status file (no connection_name field)")
    connection_name = data['connection_name']
    if not isinstance(connection_name, str):
        raise BadStatus("connection_name field is not a string")
    return pid, connection_name


def read_status() -> Dict[str, object]:
    """Read status file.

    Raise BadStatus if the status file doesn't exist or contains
    invalid JSON or the JSON is not a dict.
    """
    if not os.path.isfile(STATUS_FILE):
        raise BadStatus("No status file found")
    with open(STATUS_FILE) as f:
        try:
            data = json.load(f)
        except Exception:
            raise BadStatus("Malformed status file (not JSON)")
    if not isinstance(data, dict):
        raise BadStatus("Invalid status file (not a dict)")
    return data


def is_running() -> bool:
    """Check if the server is running cleanly"""
    try:
        get_status()
    except BadStatus:
        return False
    return True


# Run main().

if __name__ == '__main__':
    main()
