# 组合级风险预算业务服务

这是一个使用 Python、FastAPI 与 SQLite 实现的纯后端业务服务，包含领域模型、数据访问、业务编排、接口和异常路径测试。项目可在单个 Linux 应用容器内离线运行，使用本地 SQLite 或内存替身，不依赖外部运行服务。

## 安装

```bash
python3 -m pip install -r requirements.txt
```

## 测试

```bash
python3 -m pytest -q
```

## 构建检查

```bash
python3 -m compileall -q app
```

## API 导入冒烟

```bash
python3 -c "from app.main import app; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 组合级风险预算

`app/risk/` 在单笔订单检查之上提供组合维度（总敞口、行业、品种、单笔、
资金、可交割持仓）的预算管理：

- **可解释剩余额度**：持仓（按最新行情重估）＋待成交订单（按批准时
  冻结价）＋候选订单，逐桶给出 `base_limit/effective_limit/current/
  pending/incoming/projected/remaining`，放行与拒绝都附完整试算。
- **版本化规则**：`POST /api/trading/risk/rules` 发布不可变新版本，
  版本号单调递增；订单批准时冻结版本，成交、撤销、审计均按当时口径
  回放，历史版本永不修改。
- **临时豁免**：`POST /api/trading/risk/exemptions` 可按维度桶或按
  单笔订单追加额度，支持有效期、审批人与事由，单订单豁免批准即消费。
- **事务与并发**：每组合一把锁，“评估＋预留＋审计”在同一存储事务内
  原子完成；并发下单不会重复看到同一份额度。柜台拒单或适配器异常时
  预留全额回滚，撤单释放剩余占用，部分成交只迁移成交数量。
- **审计**：`GET /api/trading/risk/audit` 返回 APPROVE/REJECT/FILL/
  RELEASE/EXPIRE/REJECT_DOWNSTREAM 全链路，含规则版本与试算快照。

主要接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/trading/risk/exposure` | 各维度剩余额度全景（可带 `stock_code` 先刷新行情） |
| GET | `/api/trading/risk/open-orders` | 仍占用预算的待成交订单 |
| GET | `/api/trading/risk/audit` | 决策审计链（可按 `order_id` 过滤） |
| GET | `/api/trading/risk/rules` | 当前规则版本 |
| POST | `/api/trading/risk/rules` | 发布新版规则 |
| POST | `/api/trading/risk/instruments` | 登记证券行业/品种主数据 |
| POST | `/api/trading/risk/exemptions` | 授予临时豁免 |

买入/卖出（`/api/trading/buy`、`/api/trading/sell`）已接入该预算链路：
预算不足时返回 `403 RISK_REJECTED`，响应体携带完整试算报告。

