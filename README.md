# 科研影像 DICOM 统一标识入库平台

合并多次 DICOM 导出时，为同一研究项目维护**持久、跨批次稳定**的标识映射（PatientID、
Study/Series/SOP Instance UID），对入库文件做统一重编码并以整批 ZIP 下载。
基于 **FastAPI + pydicom + SQLite**，提供可浏览器操作的入库网页与 Docker Compose 一键环境。

并提供**项目引用核查**：用 SQLite 持久化「实例目录」与「引用目录」，按重编码后的 UID
关联已发布实例与任意嵌套序列中的 `ReferencedSOPInstanceUID`，查出“已分配 UID 但影像尚未
到库”的缺项，列出缺失目标及其来源批次、文件与标签路径，供向导出方补要文件。

---

## 1. 能力与约束（对照需求）

| 需求 | 实现 |
| --- | --- |
| 选择研究项目 | 内置两个种子项目，网页可新建；映射按 `(project_id, id_kind, source_value)` 维度**项目间完全隔离** |
| 上传最多 20 份、合计 ≤16 MiB | 路由层流式累计大小 + 流水线二次校验，超限整批拒绝（HTTP 413），可用环境变量调整 |
| 仅未压缩 DICOM | 只接受 Implicit/Explicit VR Little Endian（`1.2.840.10008.1.2[.1]`），Deflated/JPEG/RLE 等一律拒绝 |
| 整批 ZIP 下载 | 重编码后打包为 `<batch_id>.zip`，原子 `rename` 发布后才可下载 |
| 四类标识持久映射 | PatientID 与 Study/Series/SOP Instance UID；同项目同源标识跨批次沿用同一目标 |
| 嵌套序列引用替换 | 递归进入**任意** VR=SQ（Referenced SOP/Study/Series/Patient、OriginalAttributes 等），不限嵌套深度 |
| 目标尚未入库先分配 | 保留阶段对嵌套中出现的前向引用（如未来 SOP）同样预分配映射并随批发布 |
| SOP Class UID 与像素不变 | 白名单保护 `SOPClassUID`/`MediaStorageSOPClassUID`/`ReferencedSOPClassUID`；像素字节改写前后逐字节比对，传输语法不变 |
| 转换期不持数据库写锁 | 写锁仅存在于毫秒级的“映射保留”和“发布”两个短事务，转换为纯文件/CPU 操作，无事务 |
| 并发批次同源不产生不同结果 | `BEGIN IMMEDIATE` + 唯一约束 + 不持锁轮询等待，他批次 PENDING 时等待其发布/回滚，保证同一源标识全局唯一目标 |
| 整批文件与新增映射一起发布 | 发布单事务：批次置 PUBLISHED + 本批 PENDING 映射置 PUBLISHED；zip 先原子落盘 |
| 坏文件/失败/中断不留部分批次 | 解析失败零写入；转换/发布失败回收本批 PENDING 映射并删暂存；启动恢复回滚所有中断 PENDING，已发布结果永不改动 |
| 重启保留已发布结果 | SQLite 与 published ZIP 位于持久卷 `./data`（compose 命名卷），重启后映射与下载均保留 |
| 实例/引用目录持久化 | 发布事务同时写 `instances`（按重编码后 target SOP UID）与 `instance_references`（每个引用标签出现点，含来源批次/文件/标签路径）；查询纯走 SQLite，**不重读 ZIP** |
| 项目引用核查缺项 | 仅当引用的 target UID 在**同一项目**实例目录存在才算到齐；仅映射存在、其他项目有实例、来源批次未发布均不算。目标实例到库后缺项自动解除，目录行不删改 |
| 历史目录后台补建 | 网页一键启动后台线程补建历史批次：读 ZIP/解析**不持数据库写锁**，期间照常上传；可续建、幂等（不重复记引用）；缺包/解析失败标 FAILED 可重试；覆盖全部已发布批次前不显示结论 |
| 目录随批原子发布 | 实例/引用目录与批次、映射在同一发布事务提交；发布失败整体回滚，不留可见引用。每次核查在单一只读事务（同一已提交快照）内完成 |

---

## 2. 目录结构

```
.
├── app/
│   ├── config.py        # 环境变量配置（限额、数据目录、锁等待）
│   ├── ids.py           # 四类标识、标签规则、UID 生成
│   ├── db.py            # SQLite 表结构、保留/发布/回滚/恢复协议
│   ├── dicom.py         # pydicom 解析、校验、递归重写、像素/保护标签断言
│   ├── catalog.py       # 实例/引用目录提取、随批发布、历史批次后台补建（续建/幂等/重试）
│   ├── pipeline.py      # 批次流水线（校验→保留→转换→原子发布）与启动恢复
│   ├── main.py          # FastAPI 路由（项目/批次/映射/下载/引用核查/静态页）
│   └── web/index.html   # 浏览器入库与引用核查界面（原生 JS，无构建依赖）
├── scripts/
│   ├── generate_samples.py  # 生成含嵌套引用、前向引用、压缩/坏文件的合成样例
│   ├── demo.py              # 端到端验收（真实 HTTP + 多线程同源并发 + 引用核查/补建）
│   └── verify.sh            # 容器内一键验收
├── tests/               # pytest：29 个单元/集成/引用核查测试
├── Dockerfile
├── docker/entrypoint.sh
├── docker-compose.yml
└── requirements.txt
```

---

## 3. 快速启动（Docker Compose，推荐）

```bash
docker compose up --build
# 浏览器打开 http://localhost:8000
```

- 首次启动自动建库、写入两个种子项目、生成合成样例到卷内 `/data/samples`。
- 数据持久化在命名卷 `dicom-data`（容器内 `/data`）：
  - `/data/registry.db`（SQLite，WAL 模式）
  - `/data/staging/`（批次暂存，成功/失败后均清空）
  - `/data/published/<batch_id>.zip`（已发布整批 ZIP）
- 健康检查：`GET http://localhost:8000/api/health`。

容器内一键验收（另开终端）：

```bash
docker compose exec dicom-remap scripts/verify.sh
```

停止与清理：

```bash
docker compose down            # 保留数据卷
docker compose down -v         # 同时删除数据卷（清空所有批次与映射）
```

### 限额环境变量（docker-compose.yml）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DICOM_REMAP_MAX_FILES` | 20 | 单批文件数上限 |
| `DICOM_REMAP_MAX_TOTAL_BYTES` | 16777216 | 单批合计字节上限（16 MiB） |
| `DICOM_REMAP_LOCK_WAIT` | 60 | 并发冲突时等待他批次发布/回滚的最长秒数 |
| `GENERATE_SAMPLES` | true | 首次启动是否生成合成样例 |

---

## 4. 本地非 Docker 运行

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

export DICOM_REMAP_DATA=./data
python scripts/generate_samples.py ./data/samples     # 可选：生成样例
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 浏览器打开 http://localhost:8000
```

运行测试与端到端演示：

```bash
pytest tests/ -q
python scripts/demo.py --samples ./data/samples
```

---

## 5. 浏览器操作流程

1. 顶部下拉选择研究项目（或输入名称新建）。
2. 点击/拖拽选择最多 20 份未压缩 DICOM，界面实时显示份数与合计大小。
3. 点击「上传并入库整批」：成功后批次出现在列表，状态 `PUBLISHED`；失败（坏文件、压缩、超限）
   整批拒绝并提示原因，**不会产生任何部分批次或映射污染**。
4. 在「已发布映射」页签查看四类源→目标标识；点击批次行查看详情并**下载重编码整批 ZIP**。
5. 在「④ 项目引用核查」卡片查看缺项：
   - 新批次目录随发布自动建立；历史批次点「启动历史目录补建」在后台重建（补建期间可继续上传）。
   - 覆盖全部已发布批次后显示结论：列出每个“已分配 target UID 但本项目尚无实例到库”的缺失目标，
     展开可见来源批次、来源文件与嵌套标签路径，据此向导出方补要文件。
   - 缺包/解析失败的批次显示 `FAILED` 与原因，点「重试失败批次」即可重跑；目标实例后续到库后，
     对应缺项自动从清单消失（无需手工维护）。

---

## 6. HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET/POST | `/api/projects` | 项目列表 / 新建（表单 `name`） |
| POST | `/api/batches` | 上传批次（multipart：`project_id` + 多个 `files`），返回批次记录 |
| GET | `/api/batches?project_id=` | 批次列表（可按项目过滤） |
| GET | `/api/batches/{id}` | 批次详情含文件清单 |
| GET | `/api/batches/{id}/download` | 下载已发布整批 ZIP |
| GET | `/api/mappings?project_id=` | 已发布映射（可按项目过滤） |
| GET | `/api/reference-check?project_id=` | 项目引用核查：覆盖度 + 缺项清单（含来源批次/文件/标签路径），同一只读事务快照 |
| GET | `/api/catalog/backfill` | 后台补建运行态、全量目录覆盖度、各批次补建进度 |
| POST | `/api/catalog/backfill?retry_failed=` | 启动历史目录后台补建（`retry_failed=true` 同时重试失败批次），返回 202 |
| GET | `/api/meta` | 当前限额与标识类型 |

示例：

```bash
curl -X POST http://localhost:8000/api/batches \
  -F project_id=<项目ID> \
  -F files=@a1.dcm -F files=@a2.dcm
```

---

## 7. 并发一致性与原子性设计

### 7.1 批次状态机

```
PENDING ──发布事务成功──▶ PUBLISHED（终态，永不回滚）
   │
   └──解析失败 / 转换异常 / 发布异常 / 进程中断──▶ FAILED（PENDING 映射同步回收）
```

### 7.2 映射保留协议（保证同源同果）

1. **保留（短写锁）**：单条 `BEGIN IMMEDIATE` 事务内登记 PENDING 批次，并对每个源标识
   `INSERT OR IGNORE` 一条映射。唯一约束 `UNIQUE(project_id, id_kind, source_value)` 兜底；
   若标识已被他批次 PENDING 持有，则本批次**释放锁后轮询等待**，直到对方发布（复用其目标）
   或回滚（删除 PENDING 行后自行保留）。
2. **转换（不持锁）**：仅做 pydicom 重写、像素/保护标签断言、ZIP 打包，无数据库事务。
3. **发布（短写锁）**：ZIP 原子 `os.replace` 到 published 后，单事务把批次与本批新建映射
   一并置 PUBLISHED。

由此，并发批次不可能给同一源标识分配不同目标；等待超时则明确返回 409，绝不静默分叉。

### 7.3 失败与中断

- 解析/限额阶段失败：数据库零写入，仅清理暂存目录。
- 转换/发布阶段失败：`fail_batch` 删除本批全部 PENDING 映射、批次置 FAILED、清理暂存与半成品 ZIP。
- 进程中断：下次启动 `recover_on_startup` 回滚所有 PENDING 批次与映射、清理暂存孤儿和
  无主 ZIP；已 PUBLISHED 的数据与文件一律保留。

---

## 8. 样例与验收场景

`scripts/generate_samples.py` 生成固定 UID 的合成数据：

- `batch_a/`：两个实例，实例 2 嵌套引用实例 1 的 SOP、本研究 Study、患者，
  并含一个**前向引用**（目标 SOP 尚未入库）。
- `batch_b/`：新实例跨批次引用 `batch_a` 的 Series 与 SOP。
- `compressed/`：Deflated 压缩 DICOM，验证压缩拒绝。
- `bad/`：损坏文件，验证坏文件整批拒绝。

`scripts/demo.py` 覆盖七类断言（共 40+ 检查）：嵌套/前向引用替换、SOP Class 与像素不变、
跨批次映射复用、两线程同源并发结果唯一、项目隔离、坏文件/压缩/混合批次拒绝、20 份限额，
以及项目引用核查（缺项/标签路径/查询不重读 ZIP/后台补建/幂等）。
`tests/` 另含 29 个 pytest 用例，含 4 线程同源并发、中断恢复与引用核查补建测试。

---

## 9. 项目引用核查与历史目录补建设计

### 9.1 两张持久目录（SQLite）

- **实例目录 `instances`**：已发布 ZIP 内每个 DICOM 文件一行，记录重编码后的
  `target_sop_uid`（关联键）、源 SOP、所属项目/批次/文件，`UNIQUE(file_id)` 保证幂等。
- **引用目录 `instance_references`**：递归进入**任意** VR=SQ（不限嵌套深度），每个
  `ReferencedSOPInstanceUID` 标签出现点一行，记录来源批次、来源文件、标签路径
  （如 `ReferencedStudySequence[0]/ReferencedSOPSequence[0]/ReferencedSOPInstanceUID`）
  及重编码前后 UID；`UNIQUE(source_file_id, tag_path)` 兜底防重复。

### 9.2 “到齐”判定（缺项 = 已分配 UID 但影像未到库）

引用按**重编码后 target UID** 关联同项目实例：

```
缺项 ⇔ 存在引用 r，且同一 project 内没有任何 instances.target_sop_uid = r.target_referenced_uid
```

- 仅映射表存在（前向引用已预分配）但无实例 → 仍算缺项；
- 其他项目有相同源标识的实例（各项目独立重编码，target 不同）→ 不算到齐；
- 来源批次未发布 → 引用行随发布事务才可见，不会产生虚假结论；
- 目标实例后续到库 → 同项目实例目录出现匹配行，缺项自动解除，引用行保留不删。

### 9.3 随批原子发布

实例/引用目录在 `publish_batch` 的**同一写事务**内与批次状态、映射一并提交；
发布失败整体回滚，绝无“批次可见但引用目录半成品”的状态。因此新批次发布即建目录，
不需要事后补建。

### 9.4 历史批次后台补建

补建线程为进程内单例（`app/catalog.py`），网页 `POST /api/catalog/backfill` 启动：

1. 启动时快照待补批次（无目录记录 / PENDING；重试时另含 FAILED），每批本轮恰好处理一次；
2. **离线阶段**：读 ZIP + pydicom 解析全程不持有任何数据库事务/写锁，上传流水线的
   毫秒级保留/发布事务照常进行（WAL 允许读写并发）；
3. **写入阶段**：每个文件一条短事务，`INSERT OR IGNORE` + 唯一约束，中断重启后续建
   同一批次不会产生重复实例或重复引用；进度逐文件写入 `catalog_builds`；
4. 缺 ZIP / ZIP 损坏 / 解析失败 → 该批置 `FAILED` 并跳过（不忙等、不影响其他批次），
   页面显示未完成并可「重试失败批次」；
5. 进程重启时 `auto_resume_on_startup` 仅自动续建 NULL/PENDING 批次，FAILED 需人工重试。

### 9.5 结论门控与快照一致性

`GET /api/reference-check` 在单条 `BEGIN DEFERRED` 只读事务内完成全部读：

- 仅当项目内**全部已发布批次**目录均为 DONE（`coverage.complete`）才返回缺项清单；
  否则 `complete=false` 且 `missing=[]`，前端只显示“未完成”，避免把“尚未补建”误报成结论；
- 同一请求内覆盖度查询与缺项查询基于同一已提交快照，读不到补建/发布进行到一半的数据；
- 核查只读 SQLite 目录表，**不会重新打开或解析任何 ZIP**。
