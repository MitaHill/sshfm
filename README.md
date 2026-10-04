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
docker volume create sshfm-python-plain-data
docker run -d --name sshfm-python --init \
  -p 127.0.0.1:2223:2222 \
  -v sshfm-python-plain-data:/data sshfm:python-local
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
  -v sshfm-python-plain-data:/data sshfm:python-local
```

## 主配置文件与热重载

项目提供 [config.yaml](config.yaml)。推荐将它与数据库放在同一目录，Docker 中为
`/data/config.yaml`，与数据库和 SSH 主机密钥一起保存在数据卷中。
默认首次启动会创建此文件；也可将项目模板复制到正在运行的容器：

```bash
docker cp config.yaml sshfm-python:/data/config.yaml
```

指定其他位置使用 `sshfm --config /path/to/config.yaml`。配置中的数据库和密钥路径
相对于配置文件所在目录；命令行参数优先于配置文件。
监听地址、端口、数据库、密钥和时区是启动参数，修改后需要重启。

以下三项每秒检查并自动重载，无需重启：

```yaml
blacklist:
  - 192.0.2.10
  - 198.51.100.0/24
  - "2001:db8::/32"
send_rate_per_ip: 2KB
max_connections_per_ip: 3
```

- `blacklist` 支持 IPv4、IPv6 和 CIDR 网段；空列表 `[]` 表示不封禁。
  只拒绝新 SSH 连接，加入黑名单不会踢掉已有连接。
- `send_rate_per_ip` 是每个 IP 共享的发送速率，同一 IP 的所有终端、命令输出和
  错误输出合计受限。`1KB`、`2KB` 或 `2KiB` 分别表示每秒 1024、2048 字节；
  也可填写整数（字节/秒），`0` 表示不限速。按 UTF-8 字节计量，包含 TUI 控制序列，
  不包含 SSH 握手、加密封装和保活流量。修改后已有会话也使用新速率。
- `max_connections_per_ip` 默认 `3`：同一 IP 的第 4 个同时连接被拒绝。
  还以同样上限限制该 IP 的会话总数，防止多个窗口复用一条 SSH 连接绕过规则。
  断开后自动回收额度，属于同时在线数量限制，不会永久加入黑名单。
  `0` 表示不限数量；降低上限不会关闭已有连接或会话。

限速期间各会话轮流发送小块数据。宽大终端不会获得额外额度；上一帧未发送完时，
TUI 继续处理输入并暂缓重绘，下一帧呈现最新状态，避免堆积过时画面。
启用限速时跳过启动闪屏；较低速率下完整画面的显示仍可能需要数秒。

热重载时格式错误、非法字段或文件暂时缺失，会记录错误并保留上一次有效规则。
建议编辑临时文件后替换 `config.yaml`；启动时显式指定的文件缺失或内容错误则拒绝启动。
规则按服务实际看到的来源 IP 生效，共用 NAT 出口的用户也会共享额度和连接上限。

## TUI 操作

文件列表：方向键、Home/End 和 PageUp/PageDown 选择；Enter 打开；Esc 返回父目录。
`n` 新建文件，`N` 新建目录，`m` 移动，`d` 删除，`g` 定位，
`t` 按编辑时间排序，`s` 按名称排序，`r` 刷新，`q` 退出。
`b` 向当前目录会话响铃，`Ctrl+B` 向全部会话响铃，`B` 广播消息。

支持鼠标终端协议（xterm 1000/1006，兼容传统 X10 报文）：单击选择文件，
双击打开，滚轮滚动列表或正文；编辑器和确认输入框可单击定位光标。
定位按显示宽度处理中文、组合字符和 emoji。退出时关闭鼠标模式。
需要 SSH 客户端的终端支持鼠标报文；通常按住 Shift 可使用终端自身的文字选择。

文件大小显示为 `3 B`、`1.5 KiB` 等单位格式，日期显示为 `2026-10-04 10:28`，
时区沿用服务的 `--time` 配置（默认 UTC）。

`g` 输入 `目录/` 进入目录，输入 `路径/文件` 则打开父目录并选中该文件。
移动目标是完整的新路径。删除确认必须输入 `del`。

编辑器：方向键按字素或视觉行移动，Home/End 定位逻辑行首尾，PageUp/PageDown 翻页。
Enter 换行，Backspace/Delete 删除完整字素；支持粘贴多行、中文、组合字符和 emoji。
`Ctrl+S` 保存，`Ctrl+G` 跳到逻辑行，Esc 或 Ctrl+C 返回。
未保存修改需要输入 `esc` 确认放弃。

允许多个会话同时查看；首次修改取得独占编辑锁，保存、放弃或断开后释放。
持锁期间，编辑器顶栏显示 `locked by <持锁会话的 IP>`，所有查看者实时更新。
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

## 最小存储实现

当前使用标准库 `sqlite3`，文件内容直接存为 UTF-8 TEXT，不启用任何数据库压缩。
已删除内容压缩、`codec`/`raw_size` 字段及启动时的压缩升级逻辑。
文件列表只返回元数据；字节数通过 SQLite 计算，打开文件时才取回正文。

旧版本的压缩数据库会被明确拒绝；使用新卷或停服后离线转换副本，不覆盖旧卷。
本地普通数据库卷使用 `sshfm-python-plain-data`，旧卷 `sshfm-python-data` 保留。
后续若启用压缩，必须是覆盖整个数据库的透明压缩，不能只压缩文件正文。
审查与交接见 [数据库交接](docs/database-handoff.md)，候选方案见
[SQLite 整库透明压缩调研](docs/sqlite-transparent-compression.md)。本次不接入任何候选。

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
多会话锁、保存失败、移动、递归删除、窗口变化、断开、配置热重载、IP 黑名单、
连接数量限制、UTF-8 输出限速、重建容器持久化和 SQLite 完整性。
布局测试使用从上游 C++ 源码生成的固定结果，检查不同终端尺寸。

可选可视预览：`python3 scripts/preview_tui.py`，浏览器打开 `http://127.0.0.1:8893`。
预览工具从 CDN 读取固定版本的 xterm.js，只用于开发，不是容器的部署依赖。

非容器开发：Python 3.10+ 执行 `python -m pip install -e .`，运行
`sshfm --database ./dev.sqlite3 --host-key ./sshfm_hostkey --host 127.0.0.1 --port 2223`。
