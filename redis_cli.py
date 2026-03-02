#!/usr/bin/env python3
"""
A simple redis-cli replacement written in Python.

Usage:
  python redis_cli.py [OPTIONS] [CMD [ARGS...]]

Options:
  -h HOST       Server hostname (default: 127.0.0.1)
  -p PORT       Server port (default: 6379)
  -a PASSWORD   Password
  -n DB         Database number (default: 0)
  --help        Show this help message

Examples:
  python redis_cli.py                        # interactive mode
  python redis_cli.py -h 10.0.0.1 -p 6380   # connect to remote
  python redis_cli.py GET mykey              # single command
  python redis_cli.py SET foo bar            # single command
"""

import sys
import shlex
import argparse

try:
    import redis
except ImportError:
    print("Error: redis-py not installed. Run: pip install redis")
    sys.exit(1)

try:
    import readline  # enables arrow keys and history in interactive mode
except ImportError:
    pass


def format_response(resp, indent=0):
    """Format a Redis response for display, similar to redis-cli output."""
    prefix = "  " * indent
    if resp is None:
        return f"{prefix}(nil)"
    elif isinstance(resp, bool):
        return f"{prefix}{'1' if resp else '0'}"
    elif isinstance(resp, int):
        return f"{prefix}(integer) {resp}"
    elif isinstance(resp, float):
        return f"{prefix}(double) {resp}"
    elif isinstance(resp, bytes):
        return f"{prefix}\"{resp.decode(errors='replace')}\""
    elif isinstance(resp, str):
        return f"{prefix}\"{resp}\""
    elif isinstance(resp, (list, tuple)):
        if not resp:
            return f"{prefix}(empty array)"
        lines = []
        for i, item in enumerate(resp, 1):
            formatted = format_response(item, indent + 1).lstrip()
            lines.append(f"{prefix}{i}) {formatted}")
        return "\n".join(lines)
    elif isinstance(resp, dict):
        if not resp:
            return f"{prefix}(empty hash)"
        lines = []
        i = 1
        for k, v in resp.items():
            lines.append(f"{prefix}{i}) \"{k.decode(errors='replace') if isinstance(k, bytes) else k}\"")
            formatted = format_response(v, indent + 1).lstrip()
            lines.append(f"{prefix}{i + 1}) {formatted}")
            i += 2
        return "\n".join(lines)
    else:
        return f"{prefix}{resp}"


def run_command(client, args):
    """Execute a Redis command and print the result."""
    if not args:
        return
    command = args[0].upper()

    # Handle client-side commands
    if command in ("QUIT", "EXIT"):
        print("Bye!")
        sys.exit(0)
    if command == "CLEAR":
        print("\033[2J\033[H", end="")
        return
    if command == "HELP":
        print(__doc__)
        return

    try:
        resp = client.execute_command(*args)
        if resp == b"OK" or resp == "OK":
            print("OK")
        else:
            print(format_response(resp))
    except redis.exceptions.ResponseError as e:
        print(f"(error) {e}")
    except redis.exceptions.DataError as e:
        print(f"(error) {e}")


def interactive_mode(client, prompt):
    """Run an interactive REPL."""
    print(f"Connected to Redis. Type 'quit' or 'exit' to disconnect.\n")
    while True:
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break
        if not line:
            continue
        try:
            args = shlex.split(line)
        except ValueError as e:
            print(f"(error) Parse error: {e}")
            continue
        run_command(client, args)


def main():
    parser = argparse.ArgumentParser(
        description="Simple Python redis-cli replacement",
        add_help=False,
    )
    parser.add_argument("-h", dest="host", default="127.0.0.1", metavar="HOST")
    parser.add_argument("-p", dest="port", type=int, default=6379, metavar="PORT")
    parser.add_argument("-a", dest="password", default=None, metavar="PASSWORD")
    parser.add_argument("-n", dest="db", type=int, default=0, metavar="DB")
    parser.add_argument("--help", action="store_true")
    parser.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args()

    if args.help:
        print(__doc__)
        sys.exit(0)

    # Connect
    try:
        client = redis.Redis(
            host=args.host,
            port=args.port,
            password=args.password,
            db=args.db,
            socket_connect_timeout=5,
        )
        client.ping()
    except redis.exceptions.AuthenticationError:
        print("(error) NOAUTH Authentication required. Use -a <password>.")
        sys.exit(1)
    except redis.exceptions.ConnectionError as e:
        print(f"Could not connect to Redis at {args.host}:{args.port}: {e}")
        sys.exit(1)

    prompt = f"{args.host}:{args.port}[{args.db}]> "

    if args.cmd:
        # Single command mode
        run_command(client, args.cmd)
    else:
        # Interactive mode
        interactive_mode(client, prompt)


if __name__ == "__main__":
    main()
