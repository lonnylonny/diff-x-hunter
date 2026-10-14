# Diff X Hunter

保存 GitHub 仓库与关注点，供后续版本更新调查使用。

## 使用

需要 Python 3.12 或更新版本，以及 uv。在项目根目录运行：

```bash
uv sync
uv run diff-x-hunter add astral-sh/uv "关注依赖解析变化"
uv run diff-x-hunter list
```

`add` 保存一条配置，`list` 显示全部配置。仓库使用 `owner/repo` 格式，一个仓库可以保存多条关注点；重复添加会保存为多条记录。

## 网页配置关注

在项目根目录启动：

```bash
uv run uvicorn diff_x_hunter.web:app --host 127.0.0.1 --port 8000
```

打开 http://127.0.0.1:8000 ，填写 `owner/repo` 格式的仓库和可选关注点，点击“保存关注配置”。提交后返回首页，在列表中查看已保存内容。关闭程序再启动，配置仍会显示；网页和 CLI 使用同一个 SQLite 数据库。

当前网页只保存、展示配置，尚未接入 GitHub 查询或启动版本调查。

## 验证

```bash
uv run python -m unittest discover -s tests
```

测试使用临时数据库，通过网页接口验证提交、格式错误、内容转义和跨进程保存，不修改已有配置。

## 数据库

项目附带已建好表的空 SQLite 数据库：

```text
src/diff_x_hunter/owner_repo/repository.db
```

数据库随源码和 Python 安装包交付，无需手动建表，也无需启动时初始化。程序直接使用包内的数据库文件；保存后退出程序，再运行 `list` 仍能读取配置。

`watch_configs` 表包含两个文本字段：`repository`（仓库）和 `focus`（关注点）。

使用后，数据库文件会包含自己的关注配置。更新或重新安装前，请备份此文件并保留已有数据；对外分发时使用不含个人配置的空数据库。

## 当前进度

- 已完成：仓库与关注点保存到 SQLite，退出后重新运行仍能读取，已由用户验收。
- 网页表单保存与列表展示已实现，5 项自动化测试通过，包括跨进程读取；浏览器人工试用待完成。
- Docker 镜像尚未提供。
