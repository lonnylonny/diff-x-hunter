# 校验仓库标识，拆分为所有者和仓库名。
def parse_repository(value: str) -> tuple[str, str]:
    value = value.strip()

    # 只接受两段非空的 owner/repo。
    if value.count("/") != 1:
        raise ValueError(f"Invalid repository format: {value}")

    owner, repo = value.split("/")
    if not owner or not repo:
        raise ValueError(f"Invalid repository format: {value}")

    return owner, repo
