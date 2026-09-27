<div align="center">

# BTC 全市场智能研究平台

**把散落在几十个数据源里的 BTC 信息，收敛成一个能长期运行、每个数字都能追到来源的研究底座。**

多数据源高可用 · 本地历史优先 · 严格防未来数据泄漏 · 预测只给概率区间

[![Python](https://img.shields.io/badge/Python-3.13-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.141-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/SQLite-aiosqlite-003B57?logo=sqlite&logoColor=white)](https://sqlite.org/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-275%20passed-brightgreen.svg)](#测试)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey.svg)](#快速开始)

</div>

---

## 这是什么

一个**自托管的 BTC 全市场研究平台**。它把行情、链上、资金流、衍生品、宏观、情绪六类数据统一接入，
在其上做周期识别、估值、风险监测、概率预测、策略回测与条件预警，并保证**每一个展示出来的数字都能点进去看到它的来源报文**。

它不是一个"抓个价格显示出来"的看板，而是围绕三件事认真做的工程：

| 目标 | 具体做法 |
|---|---|
| **数据要可信** | 多源交叉验证，偏差超阈值就标 `DISPUTED`；宁可显示"数据不可用"，**绝不用假数据填充** |
| **结论要可追** | Raw + Normalized 双存储，页面上任何数字都能回溯到原始 JSON 与请求时间 |
| **长期运行不烂** | 健康评分 + 主动熔断 + 自动切换到备用源；断点续传补洞；45 张表的迁移机制 |

---

## 快速开始

### 环境要求

| 项 | 要求 |
|---|---|
| Python | **3.11+**（项目在 3.13 上开发与验证） |
| 操作系统 | Windows 10/11、Linux |
| 数据库 | SQLite 开箱即用；生产可选 PostgreSQL + TimescaleDB |
| Node | **不需要**。前端零构建，原生 JS + ECharts |

### 三步跑起来

```bash
# 1. 安装依赖
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
# 国内加速：pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple

# 2. 初始化数据库（建表 + 写入种子数据）
python scripts/btcctl.py init-db

# 3. 启动
python scripts/btcctl.py serve --host 127.0.0.1 --port 8000
```

打开 **http://127.0.0.1:8000** 即可。接口文档在 `/docs`。

### 一键脚本（推荐用于长期部署）

```bash
scripts/windows/install.bat      # 或 scripts/linux/install.sh
scripts/windows/menu.bat         # 交互式运维菜单
```

提供 `start / stop / restart / status / doctor / backup / restore / update / uninstall` 全套，
Linux 端另附 systemd 单元与 nginx 反代样例。

### 回填历史数据

```bash
python scripts/btcctl.py backfill --days 730     # 断点续传，中断了再跑会接着补
python scripts/btcctl.py gap-repair              # 扫描并补齐缺口
python scripts/btcctl.py collect-all             # 链上/衍生品/情绪/宏观/ETF 全量采集
```

---

## 界面截图

### 首页总览

实时价格、多源交叉验证状态与偏差一目了然。顶部显示的 `CROSS_VERIFIED` 与偏差值来自真实的多源比对，不是装饰。

![首页总览](docs/assets/screenshots/01-首页总览.png)

### 数据源中心

27 个数据源的实时健康情况、评分与自动切换记录。**失败的源会如实显示失败原因**（如 Yahoo 的 `HTTP 403`、Stooq 的解析为空），而不是假装正常。

![数据源中心](docs/assets/screenshots/02-数据源中心.png)

### 系统设置

全部 56 项配置都能在后台改，保存后立即生效，**无需编辑 `.env`、无需重启**。密钥类配置只显示掩码。

![系统设置](docs/assets/screenshots/03-系统设置.png)

### 市场周期

![市场周期](docs/assets/screenshots/04-市场周期.png)

### 风险监测

![风险监测](docs/assets/screenshots/05-风险监测.png)

### 综合市场状态（Regime）

![综合市场状态](docs/assets/screenshots/06-综合市场状态.png)

### 概率预测

只输出概率区间与不确定性说明，不给"明天涨到 X"这种无法证伪的结论。

![概率预测](docs/assets/screenshots/07-概率预测.png)

### 链上数据 / 资金流 / ETF / 衍生品

| | |
|---|---|
| ![链上数据](docs/assets/screenshots/08-链上数据.png) | ![资金流](docs/assets/screenshots/09-资金流.png) |
| ![ETF资金流](docs/assets/screenshots/10-ETF资金流.png) | ![衍生品](docs/assets/screenshots/11-衍生品.png) |

### BTC 行情 / 估值 / 宏观 / 情绪

| | |
|---|---|
| ![BTC行情](docs/assets/screenshots/12-BTC行情.png) | ![估值分析](docs/assets/screenshots/13-估值分析.png) |
| ![宏观环境](docs/assets/screenshots/14-宏观环境.png) | ![市场情绪](docs/assets/screenshots/15-市场情绪.png) |

### 策略回测 / 定投模拟 / 智能预警 / 数据质量

| | |
|---|---|
| ![策略回测](docs/assets/screenshots/16-策略回测.png) | ![定投模拟](docs/assets/screenshots/17-定投模拟.png) |
| ![智能预警中心](docs/assets/screenshots/18-智能预警中心.png) | ![数据质量](docs/assets/screenshots/19-数据质量.png) |

---

## 架构

![架构分层](docs/assets/screenshots/00-架构分层.png)

```
数据源（27 个）
      ↓
接入层    ProviderTransport ── 代理 / DoH / 重试 / 限流 / 超时 统一落这里
          ResilientRouter  ── 健康评分（0-100）+ 主动熔断 + 主备自动切换
      ↓
存储层    Raw 原始 JSON（可回溯）+ Normalized 规范化（45 张表，幂等写入）
      ↓
引擎层    指标 / 周期 / 估值 / 风险 / Regime / 预测 / 回测 / 回放 / 预警
      ↓
交付层    FastAPI（79 个接口）+ SPA 前端（35 个页面）+ CLI（19 个子命令）
```

### 项目结构

```
btc数据监测网站/
├── backend/
│   ├── app/
│   │   ├── main.py            # 应用入口、lifespan、路由挂载、静态前端
│   │   ├── core/              # 配置、日志、DNS 解析、代理、缓存、错误
│   │   ├── providers/         # 数据源抽象层
│   │   │   ├── base.py        #   DataProvider 基类
│   │   │   ├── registry.py    #   注册表与自动发现
│   │   │   ├── router.py      #   ResilientRouter 主备切换
│   │   │   ├── health.py      #   健康评分与熔断
│   │   │   ├── validation.py  #   多源交叉验证
│   │   │   ├── transport.py   #   统一 HTTP 出口（代理/DoH/重试）
│   │   │   └── impl/          #   12 个 Provider 实现（覆盖 27 个源）
│   │   ├── collectors/        # 历史回填、增量同步、缺口补洞
│   │   ├── db/                # 模型、迁移、仓储、种子数据
│   │   ├── engines/           # 指标/周期/估值/风险/Regime/预测
│   │   ├── services/          # 行情/回测/回放/策略/资金计划/健康
│   │   └── alerts/            # 智能监测、规则求值、通知渠道
│   └── tests/                 # 275 个用例，16 个测试文件
├── frontend/                  # SPA 前端（原生 JS，零构建）
│   └── static/{css,js,vendor}
├── scripts/
│   ├── btcctl.py              # 主运维 CLI（19 个子命令）
│   ├── windows/  linux/       # 双平台一键脚本
│   └── *_drill.py             # 演练脚本（数据源/代理/备份/预警）
├── docs/                      # 36 份设计文档
└── requirements.txt
```

---

## 核心特性

### 1. 多数据源高可用

每个数据源都有独立的健康评分（0-100）。评分持续走低会自动降级到备用源，**失败原因如实记录并展示**，不做静默兜底。

- **主备切换**：`ResilientRouter` 按优先级 + 健康分选路，主源失败自动切备用
- **主动熔断**：连续失败进入冷却，避免每次请求都卡在坏源上
- **交叉验证**：同一指标多源取数，偏差超阈值标 `DISPUTED`，一致则标 `CROSS_VERIFIED`

### 2. 网络策略：代理与 DNS 污染

国内环境访问海外数据源，**最常见的失败不是网络不通，而是 DNS 被污染** —— 同一域名每次解析出不同 IP，表现成"一会儿通一会儿超时"。

平台内置两套解法：

| 能力 | 说明 |
|---|---|
| **SOCKS5 / HTTP 代理** | 全局代理 + 单源独立代理，改完立即生效（连接池自动重建） |
| **DoH 解析** | 绕开本地 DNS 污染；支持 JSON 与二进制报文两种协议方言，自动降级 |

> **实测数据**：阿里公共 DNS 只认 RFC 8484 二进制报文（`?dns=<base64url>`），问 JSON 形式会返回
> `400 no 'dns' query parameter found`；而它又是国内少数稳定可达的 DoH 端点。
> 平台因此实现了"先问 JSON，失败自动改问报文"的降级链 —— 详见 [`docs/36`](docs/36-网络策略代理与DNS解析.md)。

内置污染诊断接口，直接告诉你本地 DNS 返回了什么、真实答案是什么：

```bash
curl "http://127.0.0.1:8000/api/v1/system/dns/diagnose?host=api.binance.com"
```

```json
{
  "system_ips": ["108.160.162.98", "2001::6ca0:a5d4"],
  "doh_ips": ["75.126.124.162"],
  "poisoned_ips": ["2001::6ca0:a5d4"],
  "verdict": "poisoned",
  "advice": "本地 DNS 返回了保留地址段（典型的污染特征）。建议把「DNS 解析方式」改成 DoH。"
}
```

`2001::/32` 是 Teredo 保留段 —— 正常 A 记录里不该出现，命中即可判定污染。

### 3. 本地历史优先

- **断点续传**：回填中断后重跑会从断点继续，不重复拉取
- **缺口补洞**：主动扫描本地历史的空洞并补齐（`btcctl gap-repair`）
- **双存储**：Raw 层保存原始 JSON（可回溯），Normalized 层规范化后供引擎消费
- **幂等写入**：重复采集不产生重复数据

### 4. 严格防未来数据泄漏

回测与回放**只使用当时可得的数据**，不允许出现"用未来价格算今日指标"这类错误。
测试套件中有专门的用例守着这条线：

```bash
pytest backend/tests/test_backtest_no_future_leak.py -v
pytest backend/tests/test_checkpoint_resume.py -v
```

### 5. 配置全部可在后台修改

56 项配置（含数据源密钥、代理、DNS、预警阈值、邮件通道）都能在「系统设置」页改，
**保存即生效，不需要编辑 `.env`、不需要重启**。密钥类字段只显示掩码，不会回显明文。

### 6. 智能监测与条件预警

- 规则树支持嵌套条件（AND/OR），可视化编辑
- 触发前有冷却期，避免同一条件反复轰炸
- 通知失败自动重试，渠道连续失败触发熔断
- **规则回测同样防未来数据泄漏**

---

## 测试

```bash
pytest -q                        # 全量：275 passed
pytest backend/tests/test_dns.py -v      # 61 个用例：DoH 编解码 / 污染判定 / Host+SNI 保留
pytest backend/tests/test_proxy.py -v    # 31 个用例：代理归一化 / 密码掩码 / 真实 SOCKS5 穿透
pytest backend/tests/test_api_contract.py -v
```

```
275 passed, 0 failed, 0 error
```

测试设计上刻意规避了两类不可靠因素：

- **不依赖外网**：DNS 与代理测试用本地 HTTP 服务、假解析器、`httpx.MockTransport`，
  结果可重复，不会因为某个海外站点抽风而红
- **不污染真实数据**：每个用例一个独立临时库，绝不碰 `data/btc.db`

关键的编解码用例还做了**变异检验** —— 逐个改坏实现（去掉主机名规范化、把压缩指针偏移算错、把降级开关反向），
确认每条都能被对应用例抓出来，避免"假绿灯"。

---

## 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| Web | FastAPI + uvicorn | 原生 async，自动生成 OpenAPI 文档 |
| ORM | SQLAlchemy 2.0 async + aiosqlite | 数据库可平滑切到 PostgreSQL |
| HTTP | httpx + httpx-socks | 原生支持 SOCKS5 代理，async 友好 |
| 调度 | APScheduler | 进程内 15 个定时任务 |
| 计算 | numpy / pandas | 指标与回测 |
| 前端 | 原生 JS + ECharts | **零构建**，改完刷新即可，没有 node_modules |

---

## 文档

`docs/` 下有 **36 份**设计文档，覆盖从架构选型到故障排查的完整链路：

| 类别 | 文档 |
|---|---|
| 架构 | [01 总体架构](docs/01-总体架构与技术选型.md) · [02 目录结构](docs/02-运行环境与目录结构.md) |
| 数据源 | [03 数据源注册表](docs/03-数据源注册表与数据源清单.md) · [04 主备切换](docs/04-ResilientRouter主备切换与健康评分.md) · [05 交叉验证](docs/05-交叉验证与数据置信度.md) |
| 存储 | [07 断点续传](docs/07-断点续传与缺口补洞.md) · [08 Raw与Normalized](docs/08-存储层Raw与Normalized及幂等写入.md) |
| 引擎 | [13 指标口径](docs/13-指标计算口径.md) · [14 周期与估值](docs/14-周期识别与估值模型.md) · [21 预测口径](docs/21-预测口径与不确定性表达.md) |
| 准确性 | [17 回测验证](docs/17-策略回测与准确性验证.md) · [18 **防未来数据泄漏**](docs/18-防未来数据泄漏规范.md) |
| 预警 | [31 智能监测](docs/31-智能监测与条件预警.md) · [33 规则回测](docs/33-预警规则回测与防未来泄漏.md) |
| 运维 | [24 部署手册](docs/24-安装部署手册.md) · [26 故障排查](docs/26-故障排查手册.md) · [30 运维命令](docs/30-运维命令参考.md) · [36 代理与DNS](docs/36-网络策略代理与DNS解析.md) |
| 安全 | [28 安全与密钥管理](docs/28-安全与密钥管理.md) |

---

## 常见问题

**Q：为什么有的数据源显示"未配置"？**
A：Glassnode、CryptoQuant、Coinglass 等需要 API Key，未填时状态为 `NOT_CONFIGURED`，**平台不会用假数据假装正常**。填上 Key 后自动启用。

**Q：国内环境很多源连不上怎么办？**
A：两条路。一是配置代理（全局或单源）；二是把「DNS 解析方式」改成 DoH。先用诊断接口确认是不是 DNS 污染：

```bash
curl "http://127.0.0.1:8000/api/v1/system/dns/diagnose?host=api.binance.com"
```

**Q：能改用 PostgreSQL 吗？**
A：可以。改 `DATABASE_URL` 为 `postgresql+psycopg://...` 即可，生产环境建议叠加 TimescaleDB。

**Q：数据会丢吗？**
A：`btcctl backup` 支持数据库与配置备份，`restore` 恢复。历史数据落地在本地 SQLite，不依赖任何第三方服务存活。

---

## 免责声明

本项目是**数据研究与工程实践项目**，所有指标、估值、周期判断与概率预测**均不构成投资建议**。
加密资产波动极大，请独立判断并自行承担风险。

平台的设计原则是"如实呈现数据与不确定性"，而不是给出确定性的涨跌结论。

---

## License

[MIT](LICENSE)
