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

风控在下单前把**持仓、待成交订单与风险规则**连接起来，在三个维度上计算
可解释的剩余额度，避免"每笔订单单独合规、合在一起超预算"：

- 维度：组合**总敞口**、**行业**敞口、**品种**敞口，以及单笔委托金额；
- 口径：预计敞口 = 持仓市值（随报价重算）+ 待成交买单在途预留 + 本次买单；
- 触发：报价变化可随时重算 `/api/risk/residual`；订单状态变化（挂单/部分
  成交/全成/撤销/拒绝）通过 on_order 回调自动迁移预留。

版本与事务边界：

- **规则换版**：版本 append-only 不可修改，新决策绑定最新版本，历史决策
  内嵌完整规则快照，可按当时口径逐条复核（`/api/risk/orders/{id}/trail`）；
- **临时豁免**：授予/撤销都产生不可变版本行，撤销不删除历史，到期自动失效；
- **检查并预留**：在"组合锁 + 数据库事务"内原子完成，并发下单不会合计
  超预算；同单重试幂等；部分成交按未成交量缩减预留，撤单/柜台拒绝/失败
  回滚释放全部预留；
- **审计**：放行与拒绝都落 `risk_decisions`，含估值价、生效豁免、每条规则
  的限额/已用/在途/本次/剩余与中文判定原因。

主要接口（前缀 `/api/risk`）：`GET /residual`、`POST /precheck`、
`GET|POST /rules`、`GET /rules/{version}`、`GET|POST /exemptions`、
`POST /exemptions/{id}/revoke`、`PUT /instruments`、`GET /orders`、
`GET /orders/{id}/trail`、`GET /decisions`、`GET /decisions/{id}`。
买入接口 `/api/trading/buy` 的返回中带 `risk_decision_id` 与 `risk_rule_version`，
预算不足时返回 409 及逐条拒绝原因。
