# 命令行入口：添加、列出仓库关注配置。
import argparse

from .repository import parse_repository
from .storage import load_configs, save_config


def parse_args() -> argparse.Namespace:
    # 定义子命令及所需参数。
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    add_parser = commands.add_parser("add")
    add_parser.add_argument("repository")
    add_parser.add_argument("focus")

    commands.add_parser("list")

    args = parser.parse_args()

    if args.command == "add":
        # 校验仓库格式，统一为 owner/repo。
        owner, repo = parse_repository(args.repository)
        args.repository = f"{owner}/{repo}"

    return args


def main() -> None:
    # 按子命令读写配置，并输出结果。
    args = parse_args()

    if args.command == "add":
        save_config(args.repository, args.focus)
        print("已保存")

    elif args.command == "list":
        for repository, focus in load_configs():
            print(f"{repository}: {focus}")
