# AI4S 安全实验框架

核心是 `ai4s/core.py` 中的 `Lab`：agent 负责提案，人工负责审批，确定性安全控制层负责执行与监测。默认设备为模拟 XRD，核心与浏览器 demo 只依赖 Python 3.8+ 标准库。

```powershell
cd 'E:\Agentic RL'
python -m ai4s.server
```

打开 http://127.0.0.1:8080 ，将 `.runtime/operator.token` 的内容粘贴到页面，再点击连接。每次服务重启都会更新凭据。不要将操作者凭据提供给模型。

演示顺序：人工复位 → 提交扫描计划 → 查看参数、计划ID、SHA256与有效期 → 批准或拒绝 → 启动已批准计划。审批与启动是两次独立操作。可以撤销尚未执行的批准；实验运行中用停止或软件急停中断。

取消门关闭或冷却选项可模拟联锁故障。恢复联锁后不会自动复位或启动。导入导出支持 CSV 和 JSON，谱图数据在浏览器显示；CSV 表头固定为 `two_theta,intensity`。JSON 导出保留运行ID、单位、来源、计划参数和结果状态。数据与事件保存在 `.runtime/lab.sqlite` 中。

### 网页界面模块说明

网页按操作者实际流程分区；页面下方也有逐项说明：

|界面模块|功能|
|---|---|
|操作者连接|使用本地 operator 凭据建立操作者会话；凭据不应提供给 Agent。|
|设备状态与安全控制|查看模拟状态和联锁反馈；人工复位、停止、软件急停分别处理恢复、停止与锁存急停。|
|实验计划与人工审批|生成扫描提案并查看参数、计划 ID 与摘要；批准、拒绝或撤销计划。只有批准计划才可启动。|
|模拟联锁|通过舱门、冷却状态开关演示保护条件，不是真实硬件传感器。|
|数据图表与文件|观察模拟 XRD 谱，导入或导出 CSV/JSON；导入数据不产生设备命令。|
|操作与审批记录|查看最近提案、人工决定、执行和安全事件，帮助复盘 HITL 流程。|

整页以浅暖黄背景、蓝色操作重点和黑色正文构成克制的实验记录台风格。图表、设备读数和联锁均属软件模拟；页面记录不是防篡改的生产审计系统。

无需网页的交互 demo：

```powershell
python demo_hitl.py
```

脚本实际等待人输入 RESET、APPROVE，不会自动批准。拒绝时不会启动设备。

## 核心 API

```python
from ai4s.core import Lab

lab = Lab('experiment.sqlite')
lab.reset('operator')  # 仅由可信操作者入口调用；不启动设备
plan = lab.propose('agent', {'low':20, 'high':70, 'step':0.1, 'dwell':0.02})
# 此处暂停，向人展示 plan。未收到批准时不能 execute。
# 人工审批入口须提交其查看过的摘要；不要在 agent 程序中自动调用 approve。
lab.approve('operator', plan['id'], plan['hash'])
lab.execute('agent', plan['id'])  # 参数来自批准的计划，不能在启动请求中替换
```

上述代码是可信应用的接口示意。`actor` 字符串不是身份认证；不可信 agent 应通过独立 HTTP/MCP 进程调用，不得导入核心、写工作区代码或读取操作者凭据。HTTP 网关从 bearer token 推导角色，并拒绝 JSON 中的 actor/role 字段。

主要接口：`propose`、`review(APPROVE/REJECT/REVOKE)`、`execute`、`stop`、`reset`、`status`、`import_data`、`export_csv`。停止与急停不用等待审批。审批时即记录决策；批准最多保留60秒，且单次使用。停止、急停、复位和重启均使现有审批失效。

## 接入 MCP 与开源设备 API

`mcp_server.py` 是可选的官方 MCP SDK 2.x stdio 接口，仅暴露状态查询、XRD提案、执行已批准计划、停止和软件急停。没有审批、复位、故障注入工具。

在 Python 3.10+ 的独立环境安装 `requirements-mcp.txt`，将 `AI4S_AGENT_TOKEN` 环境变量设置为 `.runtime/agent.token` 的内容，再运行 MCP server。示例宿主机配置（替换路径及 agent 凭据）：

```json
{"mcpServers":{"ai4s":{"command":"C:/path/to/python.exe","args":["E:/Agentic RL/mcp_server.py"],"env":{"AI4S_AGENT_TOKEN":"仅填agent凭据"}}}}
```

GPT、Claude或其他MCP客户端可复用此工具边界。默认HTTP服务必须运行在端口8080，才能使用当前MCP client。浏览器使用操作者身份批准 agent 提案后，agent 才能 execute。该可选接口未安装SDK或完成客户端联调；已测试的是核心、HTTP权限边界和人工审批闭环。

`ai4s/devices.py` 提供 Device Protocol、SimXRD 和 OphydSimXRD。后者调用开源 Ophyd 的 `set/trigger/read/stop` 仿真接口，在安装兼容 `ophyd` 的环境中运行：

```powershell
python -m ai4s.server --ophyd-sim
```

Ophyd适配器仍是仿真，当前未安装验证。真实设备控制被默认禁止，不能把 mode 字符串改掉就视为通过安全验收。设备型号和协议确定后，还需要设备反馈闭环、独立停止通道、硬件联锁和现场验证。

## 验证与文件

```powershell
python -m unittest -v test_hitl
```

测试覆盖未审批无设备副作用、agent不能自批、摘要不匹配、拒绝/撤销/过期、重复执行、停止后审批失效、急停锁存、联锁恢复不重启、停止反馈失败、采样超时、复位并发检查、审计故障、重启恢复与HTTP身份伪造。

|文件|用途|
|---|---|
|ai4s/core.py|人工审批、确定性安全门、采样流程与SQLite记录|
|ai4s/devices.py|模拟XRD与可选Ophyd适配器|
|ai4s/server.py、dashboard.html|操作者审批与数据可视化demo|
|ai4s/client.py、mcp_server.py|agent工具入口|
|demo_hitl.py|实际等待人工输入的最小示例|
|test_hitl.py|安全边界测试|
|HITL安全设计.md|权限、状态转换、威胁边界与上线条件|
|AI4S调研报告.md|safe agent与实验控制方案比较|

这是研究原型；软件ESTOP不证明真实设备安全，不声明SIL/PL或生产认证。完整边界见 HITL安全设计.md。当前环境的Python为3.8，可运行核心与HTTP demo；MCP需要另行配置较新Python环境。
