# SQLite 整库透明压缩调查

调查日期：2026-10-04。本次仅调查，不引入压缩代码、依赖或新的存储接口。

## 需求与当前边界

透明压缩应位于数据库页或文件系统层，覆盖文件正文、名称、关系、索引和数据库结构；应用仍使用正常 SQL 读写。仅对 `entries.content` 做 zlib 压缩不满足要求。压缩容器的头部、页映射和不能有效压缩的块不一定被压缩，不能承诺每一个物理字节都变小。还应明确是否要求回滚日志、WAL 等辅助文件也受压缩策略覆盖。

当前基础版本使用 Python 标准库 sqlite3，正文为 TEXT，保留 WAL。本轮删除内容压缩，不启用下面任何方案。用户提出的“不启用 WAL、透明压缩、50–100 人在线”是下一阶段的候选组合，尚未实测或实施。

## 可行路径

| 路径 | 应用改动 | WAL / 日志 | 部署条件与限制 |
| --- | --- | --- | --- |
| Btrfs / OpenZFS 文件系统压缩 | 可以为零，SQL、schema 和连接方式不变 | 文件系统策略可覆盖数据库和同一存储上的辅助文件 | 必须配置 Docker daemon 所在主机/虚拟机的实际存储；普通 Docker 卷没有自动压缩保证 |
| SQLite 官方 ZIPVFS | Python 所链接的 SQLite 需原生集成，调整数据库创建/连接和日志设置 | 支持自己的底层 WAL，不能照搬普通 WAL 初始化 | 商业授权，需编译并验证平台与 Python 绑定 |
| sqlite_zstd_vfs / GenomicSQLite | 加载扩展或使用绑定，再以压缩 VFS 打开；SQL/schema 可保留 | 作者明确不支持 WAL；连接生命周期有独占写锁限制 | 开源；原生依赖与 ARM64 可用性、缓存配置必须验证 |

### 文件系统压缩：应用改动最小

[Btrfs 官方文档](https://btrfs.readthedocs.io/en/latest/Compression.html)描述透明的 extent 压缩。设置通常作用于新写入数据，旧块需重写才能压缩；不可压缩数据会回退。预分配、COW/checksum 配置也会影响压缩，不能套用“SQLite 禁用 COW”的建议后仍假定压缩有效。

[OpenZFS compression 属性](https://openzfs.github.io/openzfs-docs/man/master/7/zfsprops.7.html)支持 lz4、zstd 等，并作用于新写入块。它保留普通 SQLite 文件格式，适合希望应用代码最少变动的部署。

Docker 的挂载发生在 daemon 主机上；[Docker 挂载文档](https://docs.docker.com/engine/storage/bind-mounts/)说明虚拟机部署的边界。本地使用 Colima，必须核实 Linux VM 中的数据卷文件系统，不能根据 macOS 主机磁盘或“用了 Docker”推断已经透明压缩。本轮没有安装文件系统驱动、改造虚拟机或迁移现有卷。

### ZIPVFS：数据库文件自身压缩且需要 WAL 时的候选

[官方工作原理](https://www.sqlite.org/zipvfs/doc/trunk/www/howitworks.wiki)说明压缩位于 VFS 层，上层看到正常 SQLite 数据库页；物理层另有页映射与事务机制。

[官方编译与使用说明](https://www.sqlite.org/zipvfs/doc/trunk/www/readme.wiki)提供替换 SQLite amalgamation、注册 VFS 和压缩函数的方式；新数据库可通过 URI 选择算法。Python 必须使用相同的原生 SQLite 实现，并非给现有标准库连接加一个参数就能生效。启用底层 WAL 使用 `PRAGMA zipvfs_journal_mode=WAL`，不能原样保留当前 `PRAGMA journal_mode=WAL`。需要实测辅助文件的实际内容及磁盘占用。

[官方授权页面](https://www.sqlite.org/prosupport.html)本次查询标价为一次性 US$4,000，具体以采购时条款为准。本轮没有购买或取得源码。SQL 和业务模型可保留，但 Docker 原生构建、Python 链接与备份工具一致性会增加维护成本。

### sqlite_zstd_vfs：允许关闭 WAL 后值得验证的开源候选

[项目 README](https://github.com/mlin/sqlite_zstd_vfs)描述将逻辑数据库页压缩后存入外层 SQLite，并提供 Python 加载扩展、URI 指定 VFS 的示例。作者明确指出不支持 WAL，且连接在整个生命周期内持有独占写锁，偏向 Unix x86-64 环境。

[GenomicSQLite 连接文档](https://genomicsqlite.readthedocs.io/en/latest/guide_db/)提供返回 sqlite3.Connection 的绑定。但其依赖、默认缓存与大数据优化需要评估，不能为了少写连接代码就假定它是最轻量的运行时。普通 SQLite 工具看到的是外层结构，不能用它直接修改逻辑数据库。

当前 Store 每个操作创建并关闭连接，Service 又串行协调数据库操作，这与短时独占访问有一定适配性。但这只是源码层面的判断：必须验证反复开关连接的成本、退出释放锁、回滚、损坏恢复、Linux ARM64 编译和内存开销，才能称为可用方案。若未来换成长连接/多个进程，不可沿用这个推断。

### 不满足本需求的方案

[sqlite-zstd](https://github.com/phiresky/sqlite-zstd)是行/列内容压缩，不能替代整库页压缩。`VACUUM` 回收空闲页，不是透明压缩；gzip 数据库备份不能直接承载在线随机读写。[SQLite CEROD](https://www.sqlite.org/prosupport.html)提供压缩只读数据库，不能承担本项目的编辑保存。

## 50–100 人在线与关闭 WAL 的判断

截图主要为短文本及约几十个目录条目，这类交互负载有希望满足需求，但在线人数不能单独证明吞吐量。假设 100 人平均每 10 秒保存一次，则平均约 10 次写入/秒；每人每秒保存一次则约 100 次/秒。这只是负载模型，不是实测容量。

[SQLite 回滚日志锁机制](https://www.sqlite.org/lockingv3.html)允许多个读者，但提交需要独占锁，写入/提交可能等待读者；[WAL 文档](https://www.sqlite.org/wal.html)说明 WAL 改善读写并行。关闭 WAL 时应使用正常的 DELETE 等回滚日志模式，保留可靠同步和事务，不能通过 journal_mode=OFF 或同步关闭换取虚假的性能。

本项目的具体影响：

- `Service.gate` 已串行协调读写，因此单个服务进程未充分利用 WAL 的读写并行。保留它是为了编辑锁、删除和取消操作的一致性。
- TUI 每约 0.1 秒检查状态版本；版本未变化则跳过数据库查询，不是每人每秒十次 SQL 查询。
- 保存、编辑锁等版本变化会让会话重新读取目录/当前文件。100 个会话的一次全体刷新可以产生约 100 次快照读取；持续变化会合并部分刷新，不能把写入频率机械乘以人数当作实际查询率。
- 底部广播使用内存分发，不写数据库。需要历史消息持久化时，负载和容量模型会改变。
- SQLite 文件更新和压缩按页/块工作，通常不需要每保存一条消息就重压整个数据库。但连接、压缩、fsync、刷新和 SSH 渲染可能比短文本 SQL 本身更贵。

结论：普通 SSD 上的少量短文本、偶尔保存，50–100 个连接是合理的验证目标。大量持续保存、大目录或文件、慢虚拟磁盘可能排队。目前没有关闭 WAL 加透明压缩的 100 会话压测，不能承诺容量。

## 下一步的最小验证

若部署存储已有 Btrfs/ZFS，先验证文件系统压缩，应用改动最少。若必须让数据库文件自身压缩、又接受关闭 WAL，先在隔离目录验证 sqlite_zstd_vfs；不要提前引入新存储框架。若必须保留 WAL 且接受商业授权，再评估 ZIPVFS。

比较普通 SQLite DELETE 与候选压缩 VFS，使用同一份数据、同一硬件与持久化强度；至少覆盖 50/100 个真实 SSH 会话，空闲、偶尔保存、10/100 次保存每秒及并发刷新。记录端到端保存/刷新 p50/p95/p99、CPU、RSS、锁错误和队列等待。可将 p95 保存 <200ms、刷新 <500ms、零丢失/锁错误作为待确认的验收目标，不能作为现状结果。

同时验证：以名称/关系为主的数据也能压缩；文件逻辑大小与物理占用分开测量；UTF-8/NUL 正文保持；ID、revision 和 sqlite_sequence 保持；写入失败、断连、递归删除锁保护保持；SIGKILL 后恢复、备份恢复与完整性检查通过；压缩数据库只能用匹配的工具打开。记录主库与日志分别如何压缩，并测试 ARM64 与 x86-64。通过这些检查之后才修改基础实现。
