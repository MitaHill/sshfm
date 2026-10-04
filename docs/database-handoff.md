# 数据库基础实现审查与交接

日期：2026-10-04。审查基线：`9aa8bb61e2613508e329d4660168eef9c6a41172`。本轮修改保留在工作区，未提交或推送。

## 本轮范围与结论

用户要求移除现有数据库压缩，保持最轻量基础功能，调查真正的整库透明压缩。本轮只修改 SQLite 存储与对应回归测试、README 和文档；没有新增压缩实现、ORM、连接池或存储接口。TUI 与编辑器不改版。

审查范围为 `src/sshfm/store.py`、`src/sshfm/service.py`，并核对 server/TUI 调用及 tests 中的基础和多会话行为。原 C++ 仓库规范的具体构建/排版规则不适用于已获用户授权的 Python 迁移；路径隔离、稳定身份、UTF-8、会话锁、数据保护、聚焦修改和真实 SSH 测试要求继续适用。

| 严重度 | 发现 | 处理 |
| --- | --- | --- |
| P1 | 仅压缩 content，不满足整个数据库透明压缩 | 删除 store 中 zlib、codec/raw_size 和启动压缩升级；正文直接存 TEXT |
| P2 | 目录与路径查询 SELECT * 将所有正文带入 Python | 统一元数据投影，正文仅在读取文件时返回；大小由 SQLite 计算 |
| P1 | 直接移除解压会把旧压缩库当作普通正文，产生误读/写入风险 | 启动检查旧 codec 字段并拒绝打开，在改变 schema/WAL 前停止；无自动转换逻辑 |
| 基础约束 | SQLite TEXT affinity 本身不禁止 BLOB | 新表 CHECK 要求文件 content 的 typeof 为 text，非法保存回滚并保留原数据 |

## 保留的基础行为

- `Store` 使用标准库 sqlite3。每个操作拥有连接和完整事务；写操作 BEGIN IMMEDIATE，revision 检查与写入位于同一事务。
- WAL、外键、根目录保护、路径校验、移动环路检查及父目录/名称唯一约束保持。
- AUTOINCREMENT 的稳定 ID 防止被删除重建的同名文件接收旧保存/删除确认。失败写入不会增加 revision 或覆盖正文。
- 文件正文为 UTF-8 TEXT，目录正文为 NULL。返回的 size 是 UTF-8 字节数，包含嵌入 NUL；没有冗余 raw_size 字段。
- stat/ls/view 的目录条目不返回 content。`length(CAST(content AS BLOB))` 仍可能涉及 SQLite 内部正文页读取；不能宣传为大文件目录扫描完全无正文 I/O。
- `Service.gate` 保留：保护共享会话锁与数据库操作之间的时序，取消时等待后台工作结束。不能因为 SQLite 有事务就直接删掉这把锁。
- 同一文件首次修改取得独占编辑锁，查看不锁；保存失败保留锁；断开释放锁；移动保持 ID；递归删除保留锁定文件和祖先。
- `collation.bin.zlib` 及 collation.py 的压缩排序表不是数据库压缩，保留。

## 旧数据与本地运行状态

旧压缩库不能直接交给新版本。README 使用新卷 `sshfm-python-plain-data`，避免覆盖旧卷。当前已有 `sshfm-python-data` 与旧开发容器 `sshfm-python`（localhost:2223）保持原状，仍运行旧压缩实现；本轮新实现通过隔离测试容器运行，不把旧服务测试结果当作新实现验证。

旧镜像另有本地标签 `sshfm:compressed-9aa8bb6`。不要删除旧卷、私钥或旧镜像。若需要保留内容后切换，应先停止旧写入，备份旧卷，再离线解码到新库，验证正文和全部元数据、ID、revision、sqlite_sequence 高水位、foreign_key_check / integrity_check，以及主机密钥。确认后才切换容器。不要在新运行时代码中恢复自动压缩/解压迁移框架。此次没有实施旧库转换。

## 验证

在 Docker Linux ARM64 / Python 3.12 / SQLite 3.40.1 上构建并执行：

```sh
docker build -t sshfm:python-local .
docker run --rm sshfm:python-local python -m unittest discover -s tests -v
python3 tests/docker_ssh.py sshfm:python-local
git diff --check
```

单元测试共 26 项，覆盖普通正文持久化、旧库拒绝且不修改、元数据不返回正文、UTF-8/NUL 字节数、非法 BLOB 保存回滚，以及现有路径、移动、删除、稳定 ID、revision、事务和会话锁行为。真实 Docker/OpenSSH 检查包含命令接口、并发保存冲突、PTY TUI/编辑、锁与断连、重启持久化和密钥稳定。以上 26 项单元测试、全部 Docker/OpenSSH/PTY 检查和 git diff --check 均通过。最终测试镜像为 `sha256:7fcd4e33496121d6e050f7c6f52827f8176ee6eb882613bee196abc0b20dd6ab`，Python 3.12.15。这些结果不代表未来压缩方案通过测试。

本轮没有改变终端布局；复用现有布局回归及 PTY 检查，没有新增人工视觉验收。尚未验证 x86-64 本地运行、100 会话负载、磁盘写满、强制终止/断电恢复，也未验证任何压缩后端。

## 下一位接手者

先读 [透明压缩调查](sqlite-transparent-compression.md) 与 [README](../README.md)。用户询问 50–100 人在线、短文本、不启用 WAL 加透明压缩的性能；当前回答是有希望满足交互负载，但必须以真实保存/刷新频率压测确认。底部广播不落库，TUI 版本无变化不查询；保存等变更会触发各会话刷新，且 Service 串行协调数据库访问。不要擅自把本轮 WAL 改为 OFF，也不要声称人数已经压测通过。

后续建议使用 ponytail 保持最小实现，reuse-first 评估已有 VFS，runtime-verification 验证真实运行，git-workflow 保护旧数据与保持授权的 Git 结束状态。用户本轮只要求调查方案，待选择路径后再实现：有合适压缩文件系统时优先零应用改动；必须文件内压缩且接受关闭 WAL 时隔离验证 sqlite_zstd_vfs；需要 WAL 且接受商业授权时评估 ZIPVFS。
