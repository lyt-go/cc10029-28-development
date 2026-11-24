# 跨系统数据交换与批量传输平台

面向异构系统间批量数据交换的字段契约、增量窗口抽取、暂存落地、校验和对账、断点续传与到达核对平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/views/ReplayConsole.vue      历史重放凭据/转交/回执控制台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/data/types.ts                工作台与重放接口类型
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/routers/exchange_replay.py  历史重放凭据/转交/回执/审批接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 源端登记 | `source_register` | 源端系统 | 源端编号、系统名称、数据域 |
| 目标端登记 | `target_register` | 目标端点 | 目标编号、系统名称、接收格式 |
| 字段契约 | `field_contract` | 字段映射 | 契约编号、源字段、目标字段 |
| 增量窗口 | `extract_window` | 抽取窗口 | 窗口编号、源端编号、增量字段 |
| 传输批次 | `transfer_batch` | 传输批次 | 批次编号、窗口编号、目标编号 |
| 暂存落地 | `staging_area` | 暂存分区 | 暂存编号、批次编号、落地路径 |
| 校验核对 | `checksum_verify` | 校验任务 | 校验编号、批次编号、源端校验值 |
| 到达对账 | `arrival_reconcile` | 对账单 | 对账编号、批次编号、应到条数 |
| 字段冲突 | `conflict_resolve` | 冲突记录 | 冲突编号、记录主键、冲突字段 |
| 断点续传 | `resume_point` | 续传断点 | 断点编号、批次编号、已完成分片 |
| 速率整形 | `speed_control` | 限速策略 | 策略编号、作用范围、每秒记录数 |
| 拒收回执 | `reject_record` | 拒收记录 | 拒收编号、批次编号、记录主键 |
| 格式适配 | `format_adapt` | 适配版本 | 适配编号、记录格式、旧版结构 |
| 传输血缘 | `transfer_lineage` | 血缘链路 | 链路编号、源端编号、抽取窗口 |
| 批次归档 | `history_archive` | 归档卷 | 归档编号、批次范围、归档路径 |
| 历史重放 | `exchange_replay` | 重放任务 | 重放编号、归档编号、重放范围 |
| 时效监控 | `sla_monitor` | 时效指标 | 指标编号、源端编号、约定到达时限 |
| 通道配置 | `channel_config` | 传输通道 | 通道编号、传输协议、并发连接数 |

## 历史重放：按批次归属授权的可回取范围

归档卷可以混有不同源端的批次。`history_archive` 在创建/执行归档时逐成员登记
血缘明细（源端、目标端、链路），历史重放不再按整卷一刀切，而是按成员批次授权：

1. **签发重放可回取范围凭据**（`POST /api/exchange_replay/credentials`）
   按成员批次登记源端血缘、当时归属、一次性回取资格；签发对象必须是成员当前归属方。
2. **批次转交**（`POST /api/exchange_replay/handovers`）
   只变更归属，默认**不撤销**旧任务凭据；只有显式 `revoke_old_credential=true` 才撤销。
3. **创建并执行重放**（`POST /api/exchange_replay/tasks`、`POST /{id}/execute`）
   逐成员校验当前归属：归属一致的成员才写入接收方目标产物（`data/replay/<重放编号>/<接收方>.jsonl`）；
   受转交影响或权限冲突的成员不写入，登记待审批事项并写明未取原因。
   归档卷已进入凭据管理后，没有凭据的任务不会再按整卷兜底回放。
4. **交付回执**（`GET /api/exchange_replay/receipts/{回执编号}`）
   冻结生成当时的凭据状态与可回取范围；交接前签出的进行中任务会在回执里说明
   原凭据仍可用于哪些成员、哪些成员自交接时点起需由现接收方重新许可。
5. **目标数据与审批核对**（`GET /api/exchange_replay/{id}/delivery`、`/{id}/target-data`）
   对照实际写入条数、待补审批笔数与回执当时资格。

内部账本（`_replay_credentials`、`_replay_handovers`、`_replay_receipts`、
`_replay_approvals`）以下划线开头，不进入 18 个业务模块清单与运营概览。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数仓与数据湖
