# sshfm

Python + Docker + SQLite 的 SSH 文件管理器与文本编辑器。
通过普通 SSH 打开全屏 TUI；目录关系、文件内容和版本保存在 SQLite 中。

TUI 依据 [MitaHill/sshfm](https://github.com/MitaHill/sshfm/tree/fd263ad0a85d36cce78921973f1612b353e12831)
的源码移植：反色标题、六列文件列表、自动折行、滚动条、底部快捷键、确认输入和编辑器布局。
沿用原版生成的中文名称排序表，不额外设计菜单或快捷键。

## 本地启动

只需要 Docker 和 SSH 客户端，不需要本机安装 Python 依赖或 Compose。

```bash
docker build -t sshfm:python-local .
docker volume create sshfm-python-data
docker run -d --name sshfm-python --init \
  -p 127.0.0.1:2223:2222 \
  -v sshfm-python-data:/data sshfm:python-local
ssh -p 2223 anyone@127.0.0.1
```

本地开发服务接受任意用户名且无需密码，端口只发布到回环地址。
所有操作限定在数据库内的虚拟文件系统，不会执行系统命令。
显示时间默认 UTC；如需 UTC+8，在 `docker run` 的镜像名后追加
`python -m sshfm --time 8`。

更新容器时保留数据卷：

```bash
docker build -t sshfm:python-local .
docker stop sshfm-python
docker rm sshfm-python
docker run -d --name sshfm-python --init \
  -p 127.0.0.1:2223:2222 \
  -v sshfm-python-data:/data sshfm:python-local
```

## TUI 操作

文件列表：方向键、Home/End 和 PageUp/PageDown 选择；Enter 打开；Esc 返回父目录。
`n` 新建文件，`N` 新建目录，`m` 移动，`d` 删除，`g` 定位，
`t` 按编辑时间排序，`s` 按名称排序，`r` 刷新，`q` 退出。
`b` 向当前目录会话响铃，`Ctrl+B` 向全部会话响铃，`B` 广播消息。

`g` 输入 `目录/` 进入目录，输入 `路径/文件` 则打开父目录并选中该文件。
移动目标是完整的新路径。删除确认必须输入 `del`。

编辑器：方向键按字素或视觉行移动，Home/End 定位逻辑行首尾，PageUp/PageDown 翻页。
Enter 换行，Backspace/Delete 删除完整字素；支持粘贴多行、中文、组合字符和 emoji。
`Ctrl+S` 保存，`Ctrl+G` 跳到逻辑行，Esc 或 Ctrl+C 返回。
未保存修改需要输入 `esc` 确认放弃。

允许多个会话同时查看；首次修改取得独占编辑锁，保存、放弃或断开后释放。
保存失败保留草稿和锁，可再次 Ctrl+S；其他查看者会刷新已保存内容。
移动保持文件 ID；递归删除保留正在编辑的文件及其父目录。
锁协调由单个服务进程完成，同一数据卷只运行一个 sshfm 服务。

## SSH 命令接口

非 TUI 会话及 SSH exec 保留 JSON 命令接口，方便测试和脚本调用：

```bash
ssh -p 2223 anyone@127.0.0.1 'mkdir /notes'
ssh -p 2223 anyone@127.0.0.1 'write /notes/hello.txt "你好，SQLite 👋"'
ssh -p 2223 anyone@127.0.0.1 'read /notes/hello.txt'
```

`ls [PATH]`、`stat PATH`、`read PATH`、`write PATH TEXT`、`mkdir PATH`、
`save ID REVISION TEXT`、`mv SOURCE DESTINATION`、`rm [-r] PATH`、`help`。
`write` 只创建新文件；`save` 的 ID/REVISION 来自 `read`，版本不匹配拒绝保存。

## 存储与压缩

文件内容**默认使用 zlib 压缩**，作为 BLOB 写入 SQLite；读取时自动解压为 UTF-8。
列表显示原始字节数，列出目录时不解压内容。目录关系、时间与 IP 元数据保持可查询。
这是内容压缩，不是 SQLite 整库页面压缩；短文本压缩后可能略增大。
旧最小原型的 TEXT 内容在启动时事务化升级，保留 ID、版本和时间。

`entries.id` 是稳定身份，`parent_id` 表示父目录，同目录名称唯一。
SQLite 启用 WAL、外键和级联删除；写操作在 `BEGIN IMMEDIATE` 事务中完成。
`/data` 保存数据库、WAL 文件和 SSH 主机密钥，重建容器复用该卷。

当前支持 UTF-8 文本，不支持二进制上传、SFTP、旧版 `1`/`0` 存储导入及可选登录验证码。
尚未验证大文件、大量连接、崩溃恢复及其他 CPU 架构。

## 开发与测试

```text
src/sshfm/       SSH 服务、SQLite 存储、会话协调、TUI、编辑器与原版排序表
tests/          标准库单元测试、Docker + OpenSSH PTY 集成测试
scripts/        本地 xterm.js 真实 SSH 终端预览
pyproject.toml  项目元数据、依赖与命令入口
Dockerfile      以非 root 用户运行的容器
```

```bash
docker run --rm sshfm:python-local python -m unittest discover -s tests -v
python3 tests/docker_ssh.py sshfm:python-local
git diff --check
```

集成测试自动创建独立容器和临时卷，覆盖真实 SSH 命令、全屏 TUI 创建与编辑、
多会话锁、保存失败、移动、递归删除、窗口变化、断开、重建容器持久化和 SQLite 完整性。
布局测试使用从上游 C++ 源码生成的固定结果，检查不同终端尺寸。

可选可视预览：`python3 scripts/preview_tui.py`，浏览器打开 `http://127.0.0.1:8893`。
预览工具从 CDN 读取固定版本的 xterm.js，只用于开发，不是容器的部署依赖。

非容器开发：Python 3.10+ 执行 `python -m pip install -e .`，运行
`sshfm --database ./dev.sqlite3 --host-key ./sshfm_hostkey --host 127.0.0.1 --port 2223`。
