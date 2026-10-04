# sshfm

通过 SSH 使用的文件管理器与文本编辑器，基于 Python、SQLite 和 Docker。
支持多会话查看、独占编辑锁、鼠标操作、中文和 emoji。

## 启动

```bash
docker build -t sshfm:python-local .
docker volume create sshfm-python-plain-data
docker run -d --name sshfm-python --init \
  -p 127.0.0.1:2223:2222 \
  -v sshfm-python-plain-data:/data sshfm:python-local
ssh -p 2223 anyone@127.0.0.1
```

接受任意用户名，无需密码；示例仅监听本机。操作限定在数据库内的虚拟文件系统。
更新容器时复用数据卷，保留数据库、配置和 SSH 主机密钥。

## 配置

推荐将 [config.yaml](config.yaml) 与数据库放在一起，默认 `/data/config.yaml`，首次启动自动创建。
可复制项目模板，或用 `--config PATH` 指定其他位置：

```bash
docker cp config.yaml sshfm-python:/data/config.yaml
```

以下规则每秒自动重载；配置错误时保留上一次有效规则：

```yaml
blacklist: []
send_rate_per_ip: 2KB
max_connections_per_ip: 3
```

- `blacklist`：IP 或 CIDR 网段，支持 IPv4/IPv6；只拒绝新连接。
- `send_rate_per_ip`：同一 IP 的所有会话共享发送速率。`1KB` = 1024 字节/秒，也可填整数；`0` 不限速。已有会话同步生效，低速下重绘会变慢。
- `max_connections_per_ip`：同时在线的连接数和会话数上限，默认允许 3 个，第 4 个拒绝；断开后恢复额度，`0` 不限制。已有连接保留。

发送额度按 UTF-8 输出计量，包含终端控制序列，不包含 SSH 协议开销。共用 NAT 出口的用户共享额度。
数据库、密钥路径相对于配置文件；命令行参数优先。监听地址、端口、路径和时区修改后需要重启；`time: 8` 表示 UTC+8。

## 操作

| 场景 | 按键 |
| --- | --- |
| 文件列表 | 方向键选择，Enter 打开，Esc 返回，`q` 退出 |
| 文件管理 | `n` 新建文件，`N` 新建目录，`m` 移动，`d` 删除，`g` 定位 |
| 排序与刷新 | `t` 按编辑时间，`s` 按名称，`r` 刷新 |
| 消息 | `B` 广播，`b` 当前目录响铃，Ctrl+B 全部响铃 |
| 编辑器 | Ctrl+S 保存，Ctrl+G 跳行，Esc / Ctrl+C 返回 |
| 鼠标 | 单击选择或定位光标，双击打开，滚轮滚动 |

`g` 输入 `目录/` 进入目录；移动目标为完整新路径。删除需输入 `del`，放弃草稿需输入 `esc`。
首次修改取得文件锁，保存、放弃或断开后释放；保存失败保留草稿和锁，可重试。
同一数据卷只运行一个服务进程。

## 命令接口

SSH 命令返回 JSON，例如：

```bash
ssh -p 2223 anyone@127.0.0.1 'read /notes/hello.txt'
```

支持 `ls [PATH]`、`stat PATH`、`read PATH`、`write PATH TEXT`、`mkdir PATH`、
`save ID REVISION TEXT`、`mv SOURCE DESTINATION`、`rm [-r] PATH`、`help`。
`write` 仅创建文件；`save` 使用 `read` 返回的 ID 和版本，拒绝过期版本。

## 存储与开发

正文保存为 SQLite UTF-8 TEXT，启用 WAL，不压缩。旧压缩数据库不能直接使用，需新数据卷或离线转换副本。
支持文本，不支持二进制上传和 SFTP。更多说明见 [数据库交接](docs/database-handoff.md) 与 [压缩调研](docs/sqlite-transparent-compression.md)。

```bash
docker run --rm sshfm:python-local python -m unittest discover -s tests -v
python3 tests/docker_ssh.py sshfm:python-local
git diff --check
```

本机开发需 Python 3.10+：`python -m pip install -e .`，再运行
`sshfm --database ./dev.sqlite3 --host-key ./sshfm_hostkey --host 127.0.0.1 --port 2223`。
可选终端预览：`python3 scripts/preview_tui.py`，访问 `http://127.0.0.1:8893`。
