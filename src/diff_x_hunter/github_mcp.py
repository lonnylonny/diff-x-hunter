# 通过 GitHub MCP 分页查询，保存最新已发布版本及调用记录。
import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult
from mcp.shared.exceptions import MCPError

from .storage import save_release, save_tool_call

PAGE_SIZE = 100
QUERY_TIMEOUT_SECONDS = 60

@dataclass(frozen=True)
class QueryResult:
    # 整次查询结论：找到、为空、不可访问或失败。
    outcome: Literal["found", "empty", "inaccessible", "failed"]
    release: dict | None = None
    reason_code: str | None = None


class ToolQueryError(RuntimeError):
    """The MCP tool explicitly reported an error, not malformed Release data."""


def classify_failure(exc: Exception, stage: str, owner: str, repo: str) -> QueryResult:
    # 结合失败环节和异常，生成可展示的原因码。
    if stage == "save":
        return QueryResult("failed", reason_code="save_failed")
    if isinstance(exc, TimeoutError):
        return QueryResult("failed", reason_code="timeout")
    if isinstance(exc, (ToolQueryError, MCPError)):
        # 精确匹配当前接口的错误，避免误判无关的 404。
        endpoint = re.escape(f"https://api.github.com/repos/{owner}/{repo}/releases")
        match = re.fullmatch(
            rf"failed to list releases: GET {endpoint}(?:\?[^\s:]*)?: "
            r"(401|403|404|429) (.+)", str(exc), re.DOTALL,
        )
        if match:
            status, detail = match.groups()
            detail = detail.lower()
            if status == "429" or (status == "403" and "rate limit" in detail):
                return QueryResult("failed", reason_code="rate_limited")
            if status in ("401", "404") or (
                status == "403" and "resource not accessible" in detail
            ):
                return QueryResult("inaccessible", reason_code="access_unavailable")
    if stage == "parse" and isinstance(exc, (ValueError, TypeError, KeyError, StopIteration)):
        return QueryResult("failed", reason_code="invalid_response")
    return QueryResult("failed", reason_code="unknown")


def safe_error(exc: Exception) -> str:
    # 写日志前隐藏令牌，避免凭据入库。
    message = str(exc)
    token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
    if token:
        message = message.replace(token, "[REDACTED]")
    message = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)",
                     "[REDACTED]", message)
    message = re.sub(r"(?i)\b(Bearer|token)\s+[^\s,;]+", r"\1 [REDACTED]", message)
    # 工具错误沿用原有的诊断类型名。
    name = "RuntimeError" if isinstance(exc, ToolQueryError) else type(exc).__name__
    return f"{name}: {message}"


async def list_releases(owner: str, repo: str, page: int) -> CallToolResult:
    # 将凭据及工具配置传给 Docker 中的 MCP 服务。
    env = {
        name: os.environ[name]
        for name in (
            "GITHUB_PERSONAL_ACCESS_TOKEN",
            "GITHUB_READ_ONLY",
            "GITHUB_TOOLS",
        )
    }

    server = StdioServerParameters(
        command="docker",
        args=[
            "run", "-i", "--rm",
            "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
            "-e", "GITHUB_READ_ONLY",
            "-e", "GITHUB_TOOLS",
            "ghcr.io/github/github-mcp-server:latest",
        ],
        env=env,
    )

    # 建立标准输入输出连接，读取指定页。
    async with Client(server, raise_exceptions=True) as client:
        return await client.call_tool(
            "list_releases",
            {"owner": owner, "repo": repo, "page": page, "perPage": PAGE_SIZE},
        )


def parse_releases(result: CallToolResult) -> list[dict]:
    # 工具报错直接抛出，不按版本数据处理。
    if result.is_error:
        message = "\n".join(
            block.text for block in result.content if block.type == "text"
        )
        raise ToolQueryError(message or "GitHub MCP 查询失败")
    # 优先取结构化数据，否则解析文本中的 JSON。
    data = result.structured_content
    if data is None:
        data = json.loads(next(
            block.text for block in result.content if block.type == "text"
        ))
    # 兼容数组包在 result 字段中的响应。
    if isinstance(data, dict) and "result" in data:
        data = data["result"]
    if not isinstance(data, list):
        raise ValueError("GitHub MCP 未返回 Release 列表")
    return data


async def query_release(owner: str, repo: str) -> QueryResult:
    """Save the latest published Release across all pages and return its outcome."""
    latest = None
    latest_key = None
    page = 1
    loop = asyncio.get_running_loop()
    # 所有分页共用截止时间，限制总等待时长。
    deadline = loop.time() + QUERY_TIMEOUT_SECONDS
    while True:
        arguments = {"owner": owner, "repo": repo, "page": page, "perPage": PAGE_SIZE}
        called_at = datetime.now(UTC).isoformat()
        stage = "query"
        try:
            if loop.time() >= deadline:
                raise TimeoutError("查询等待期限已到")
            async with asyncio.timeout_at(deadline):
                response = await list_releases(owner, repo, page)
            # 工具报错归入查询环节。
            if response.is_error:
                parse_releases(response)
            stage = "parse"
            releases = parse_releases(response)
            # 跳过草稿，校验后续排序和存储所需字段。
            for release in releases:
                if not isinstance(release["draft"], bool):
                    raise ValueError("Release 草稿标记无效")
                if release["draft"]:
                    continue
                if not isinstance(release["id"], int) or isinstance(release["id"], bool):
                    raise ValueError("Release ID 无效")
                for field in ("tag_name", "html_url", "published_at"):
                    if not isinstance(release[field], str):
                        raise ValueError(f"Release {field} 无效")
                if release.get("body") is not None and not isinstance(release["body"], str):
                    raise ValueError("Release 发布说明无效")
                published_at = datetime.fromisoformat(release["published_at"])
                if published_at.utcoffset() is None:
                    raise ValueError("Release 发布时间缺少时区")
                if not isinstance(release["prerelease"], bool):
                    raise ValueError("Release 类型无效")
                # 按发布时间选最新版本，同一时间用 ID 比较。
                key = (published_at, release["id"])
                if latest_key is None or key > latest_key:
                    latest, latest_key = release, key

            stage = "save"
            # 不满一页表示遍历完成，此时才确定整次结论。
            finished = len(releases) < PAGE_SIZE
            outcome = ("empty" if latest is None else "found") if finished else None
            summary = f"第 {page} 页：取得 {len(releases)} 条 Release"
            release_id = None
            if finished:
                if latest is None:
                    summary += "；查询完成，暂无已发布版本"
                else:
                    # 全部分页完成后，仅保存选中的版本。
                    body = latest.get("body") or ""
                    release_id = save_release(
                        repository=f"{owner}/{repo}",
                        github_release_id=latest["id"],
                        tag_name=latest["tag_name"],
                        body=body,
                        html_url=latest["html_url"],
                        published_at=latest["published_at"],
                        prerelease=latest["prerelease"],
                    )
                    kind = "预发布版" if latest["prerelease"] else "正式版"
                    summary += (
                        f"；查询完成，选定 {latest['tag_name']}（{kind}），"
                        f"发布说明 {len(body)} 字符"
                    )
            # 每页留痕，最后一页关联版本和整次结论。
            save_tool_call(
                tool_name="list_releases", arguments=arguments,
                called_at=called_at, status="success",
                result_summary=summary, release_id=release_id,
                query_outcome=outcome,
            )
        except Exception as exc:
            # 记录失败原因和发生环节，错误信息先脱敏。
            result = classify_failure(exc, stage, owner, repo)
            try:
                save_tool_call(
                    tool_name="list_releases", arguments=arguments,
                    called_at=called_at, status="error", error=safe_error(exc),
                    query_outcome=result.outcome, reason_code=result.reason_code,
                    failure_stage=stage,
                )
            except Exception:
                # 连失败记录也未保存时，通知上层页面。
                exc.add_note("本次失败记录未能保存")
                raise exc
            if stage == "save":
                # 保存失败交给调用方处理，不能当作正常结果。
                raise
            return result

        if finished:
            if latest is None:
                return QueryResult(outcome="empty")
            return QueryResult(outcome="found", release=latest)
        page += 1
