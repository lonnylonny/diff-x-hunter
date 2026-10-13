def parse_repository(value: str) -> tuple[str, str]:
    value = value.strip()

    if value.count("/") != 1:
        raise ValueError(f"Invalid repository format: {value}")

    owner, repo = value.split("/")
    if not owner or not repo:
        raise ValueError(f"Invalid repository format: {value}")

    return owner, repo
