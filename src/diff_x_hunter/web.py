import json
import sqlite3
from html import escape

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .github_mcp import query_release
from .repository import parse_repository
from .storage import (
    load_configs,
    load_releases,
    load_tool_calls,
    load_query_results,
    save_config,
)

app = FastAPI()


def query_message(outcome: str, reason: str | None = None) -> str:
    if outcome == "found":
        return "配置及查询结果已保存"
    if outcome == "empty":
        return "配置已保存，暂无已发布版本"
    reasons = {
        "timeout": "本次查询超时",
        "rate_limited": "本次查询受到 GitHub 限流，请稍后重试",
        "invalid_response": "返回资料格式无效，本次查询失败",
        "save_failed": "本次查询结果保存失败",
        "access_unavailable": "本次无法访问仓库，无法确定是仓库不存在还是权限或凭据问题",
    }
    detail = reasons.get(reason, "本次查询失败，具体原因未知")
    return f"配置已保存，但{detail}，无法确认当前版本"


def render_page(
    repository: str = "",
    focus: str = "",
    error: str = "",
) -> str:
    watch_configs = []
    try:
        watch_configs = load_configs()

        rows = "".join(
            f"<tr><td>{escape(repo)}</td><td>{escape(point)}</td></tr>"
            for repo, point in watch_configs
        )
        configs = (
            '<table><thead><tr><th>仓库</th><th>关注点</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
            if rows
            else "<p>还没有关注配置。</p>"
        )
    except sqlite3.Error:
        configs = "<p>暂时无法读取关注配置。</p>"
        error = error or "关注配置读取失败，请检查数据库后重试"

    release_content = "<p>暂时无法读取版本资料。</p>"
    call_content = "<p>暂时无法读取调用记录。</p>"
    try:
        releases_by_repo = {}
        for repo, _ in watch_configs:
            repo = repo.lower()
            if repo not in releases_by_repo:
                releases_by_repo[repo] = load_releases(repo)

        tool_calls = load_tool_calls()
        queries_by_repo = {repo: load_query_results(repo) for repo in releases_by_repo}
        queries_by_id = {
            record["id"]: record
            for records in queries_by_repo.values() for record in records
        }
        releases_by_id = {}
        for releases in releases_by_repo.values():
            for release in releases:
                releases_by_id[release[0]] = release

        call_records = []
        for call in tool_calls:
            (
                call_id, tool_name, arguments_json, called_at,
                status, result_summary, _call_error, release_id,
            ) = call
            arguments = json.loads(arguments_json)
            release = releases_by_id.get(release_id)
            call_records.append({
                "id": call_id,
                "repository": f"{arguments['owner']}/{arguments['repo']}",
                "tool_name": tool_name,
                "called_at": called_at,
                "status": status,
                "result_summary": result_summary,
                "release_id": release_id,
                "tag_name": release[2] if release is not None else None,
            })

        release_sections = []
        for repo, releases in releases_by_repo.items():
            calls = [record for record in call_records if record["repository"].lower() == repo]
            queries = queries_by_repo[repo]
            latest = queries[-1] if queries else None
            # A newer partial/legacy call cannot be presented as an older confirmed success.
            current = latest if latest and (not calls or latest["id"] == calls[-1]["id"]) else None
            if current:
                message = query_message(current["query_outcome"], current["reason_code"])
                role = "status" if current["query_outcome"] in ("found", "empty") else "alert"
                query_html = (
                    f'<h4>最近一次查询</h4><p role="{role}">{escape(message)}</p>'
                    f'<p>对应调用开始时间（UTC）：{escape(current["called_at"])}</p>'
                )
                if current["failure_stage"]:
                    stage = {"query": "调用 GitHub MCP", "parse": "解析版本资料", "save": "保存结果"}
                    query_html += f'<p>失败环节：{stage.get(current["failure_stage"], "未知")}</p>'
            else:
                query_html = "<p>暂无明确的查询结论；历史记录或未完成调用不能确认当前版本。</p>"
            if releases and (not current or current["query_outcome"] != "found"):
                query_html += "<p>以下为此前保存的资料，不代表本次查询成功。</p>"
            cards = []
            for release in releases:
                release_id, _github_id, tag, body, url, published_at, prerelease = release
                obtained = next((call["called_at"] for call in calls
                                 if call["status"] == "success" and call["release_id"] == release_id), None)
                source = (
                    f'<a href="{escape(url)}">{escape(url)}</a>'
                    if url.startswith(("https://", "http://"))
                    else escape(url)
                )
                cards.append(
                    f'<article id="release-{release_id}">'
                    f"<h4>{escape(tag)}</h4>"
                    f"<p>版本类型：{'预发布版' if prerelease else '正式版'}</p>"
                    f"<p>GitHub 发布时间：{escape(published_at)}</p>"
                    f"<p>资料取得时间（关联成功调用，UTC）：{escape(obtained or '未知')}</p>"
                    f"<p>原始来源：{source}</p>"
                    f"<h5>发布说明</h5><pre>{escape(body or '暂无发布说明。')}</pre>"
                    "</article>"
                )
            release_sections.append(
                f"<section><h3>{escape(repo)}</h3>"
                + query_html
                + ("".join(cards) or "<p>尚无保存的版本资料。</p>")
                + "</section>"
            )
        release_content = "".join(release_sections) or "<p>尚无保存的版本资料。</p>"

        call_rows = []
        for record in call_records:
            version = (
                f'<a href="#release-{record["release_id"]}">'
                f'{escape(record["tag_name"])}</a>'
                if record["tag_name"] is not None
                else "无关联版本"
            )
            status_text = "成功" if record["status"] == "success" else "失败"
            summary = (
                record["result_summary"] or "无结果摘要"
                if record["status"] == "success"
                else "查询或结果保存失败，请检查服务配置后重试"
            )
            query = queries_by_id.get(record["id"])
            if query and record["status"] != "success":
                summary = query_message(query["query_outcome"], query["reason_code"])
            call_rows.append(
                f'<tr id="call-{record["id"]}">'
                f'<td>{escape(record["repository"])}</td>'
                f'<td>{escape(record["tool_name"])}</td>'
                f'<td>{escape(record["called_at"])}</td>'
                f"<td>{status_text}</td><td>{version}</td>"
                f"<td>{escape(summary)}</td></tr>"
            )
        call_content = (
            '<table id="tool-calls"><thead><tr><th>仓库</th><th>工具</th>'
            '<th>查询时间（UTC）</th><th>状态</th><th>关联版本</th><th>调用摘要</th>'
            '</tr></thead><tbody>' + "".join(call_rows) + '</tbody></table>'
            if call_rows
            else "<p>尚无调用记录。</p>"
        )
    except sqlite3.Error:
        error = error or "版本资料或调用记录读取失败，请检查数据库后重试"

    error_message = f'<p role="alert">{escape(error)}</p>' if error else ""
    return f"""
    <!doctype html>
    <html lang="zh-CN">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Diff X Hunter</title>
            <style>
                body {{ max-width: 1100px; margin: 2rem auto; padding: 0 1rem;
                        font-family: sans-serif; line-height: 1.6; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ccc; padding: .5rem;
                          text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
                pre {{ white-space: pre-wrap; overflow-wrap: anywhere;
                       font-family: inherit; }}
                article {{ border-bottom: 1px solid #ddd; padding-bottom: 1rem; }}
                [role="alert"] {{ color: #a11; }}
                [role="status"] {{ color: #165b2d; }}
            </style>
        </head>
        <body>
            <h1>Diff X Hunter</h1>
            <h2>添加关注配置</h2>
            <p>仓库使用 owner/repo 格式，例如 astral-sh/uv。</p>
            {error_message}
            <form method="post" action="/watch-configs">
                <p>
                    <label for="repository">GitHub 仓库</label><br>
                    <input id="repository" name="repository" required
                           placeholder="astral-sh/uv" value="{escape(repository)}">
                </p>
                <p>
                    <label for="focus">关注点（可选）</label><br>
                    <textarea id="focus" name="focus" rows="3" cols="40">{escape(focus)}</textarea>
                </p>
                <button type="submit">保存并查询</button>
            </form>
            <h2>已保存的关注配置</h2>
            {configs}
            <h2>已保存的版本资料</h2>
            <p>以下为已保存的 Release 资料，尚未生成调查报告。</p>
            {release_content}
            <h2>查询记录</h2>
            {call_content}
        </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_page()


@app.post("/watch-configs", response_class=HTMLResponse)
async def add_watch_config(
    repository: str = Form(""), focus: str = Form("")
) -> Response:
    try:
        owner, repo = parse_repository(repository)
    except ValueError:
        return HTMLResponse(
            render_page(repository, focus, "仓库请填写 owner/repo，例如 astral-sh/uv。"),
            status_code=400,
        )
    try:
        save_config(f"{owner}/{repo}", focus)
    except sqlite3.Error:
        return HTMLResponse(
            render_page(repository, focus, "配置保存失败，请稍后重试"),
            status_code=500,
        )

    try:
        result = await query_release(owner, repo)
    except Exception as exc:
        message = "配置已保存，但查询或结果保存失败，请检查服务配置后重试"
        if "本次失败记录未能保存" in getattr(exc, "__notes__", ()):
            message += "；本次失败记录未能保存"
        return HTMLResponse(
            render_page(
                repository,
                focus,
                message,
            ),
            status_code=502,
        )

    if result.outcome not in ("found", "empty"):
        return HTMLResponse(
            render_page(
                repository,
                focus,
                query_message(result.outcome, result.reason_code),
            ),
            status_code=502,
        )
    return RedirectResponse("/", status_code=303)
