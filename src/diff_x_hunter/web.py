from html import escape

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .repository import parse_repository
from .storage import load_configs, save_config

app = FastAPI()

def render_page(repository: str = "", focus: str = "", error: str = "") -> str:
    rows = "".join(
        f"<tr><td>{escape(repo)}</td><td>{escape(point)}</td></tr>"
        for repo, point in load_configs()
    )
    configs = (
        '<table><thead><tr><th>仓库</th><th>关注点</th></tr></thead>'
        f"<tbody>{rows}</tbody></table>"
        if rows
        else "<p>还没有关注配置。</p>"
    )
    error_message = f'<p role="alert">{escape(error)}</p>' if error else ""
    return f"""
    <!doctype html>
    <html lang="zh-CN">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>Diff X Hunter</title>
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
                <button type="submit">保存关注配置</button>
            </form>
            <h2>已保存的关注配置</h2>
            {configs}
        </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_page()


@app.post("/watch-configs", response_class=HTMLResponse)
def add_watch_config(
    repository: str = Form(""), focus: str = Form("")
) -> Response:
    try:
        owner, repo = parse_repository(repository)
    except ValueError:
        return HTMLResponse(
            render_page(repository, focus, "仓库请填写 owner/repo，例如 astral-sh/uv。"),
            status_code=400,
        )
    save_config(f"{owner}/{repo}", focus)
    return RedirectResponse(url="/", status_code=303)
