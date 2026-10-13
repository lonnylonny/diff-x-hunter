import argparse

from .repository import parse_repository
from .storage import load_configs, save_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    add_parser = commands.add_parser("add")
    add_parser.add_argument("repository")
    add_parser.add_argument("focus")

    commands.add_parser("list")

    args = parser.parse_args()

    if args.command == "add":
        owner, repo = parse_repository(args.repository)
        args.repository = f"{owner}/{repo}"

    return args


def main() -> None:
    args = parse_args()

    if args.command == "add":
        save_config(args.repository, args.focus)
        print("已保存")

    elif args.command == "list":
        for repository, focus in load_configs():
            print(f"{repository}: {focus}")
