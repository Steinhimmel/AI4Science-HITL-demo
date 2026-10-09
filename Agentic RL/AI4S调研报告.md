# AI4S与safe agent接入方案

本项目选型结论：用MCP提供agent与实验工具的交互边界，用确定性控制层执行人工批准的计划，用Ophyd/Bluesky类接口对接设备。agent可以承担上位机中的计划与数据功能，独立硬件保护仍应承担失效时的安全功能。

“MHS”如果指Claude常用的工具连接协议，很可能是MCP；此处按MCP实现。MCP定义工具、资源和提示等交互能力，本身不是工业安全协议。[MCP官方服务端文档](https://modelcontextprotocol.io/docs/develop/build-server)

|方案|适合承担的职责|本项目使用方式与边界|
|---|---|---|
|OpenAI Agents SDK护栏与人工审批|模型工作流、工具校验和审批中断|GPT可调用实验工具；最终审批与设备限制仍在可信控制层强制执行|
|Safe Lab Agents|将agent放在Docker隔离环境，仅通过宿主机MCP工具操作实验|参考其隔离与宿主机工具设计；参数范围和深层数据结构仍要在工具端验证|
|NeMo Guardrails|输入/输出及工具执行阶段的可编程护栏|可增加注入检查和工具参数校验；不能替代设备门联锁或紧急停机|
|Safe-SDL研究框架|规划、编排安全核、物理执行分层与操作域边界|参考其独立安全核设计；论文方法不等于本原型已具备形式验证或安全认证|
|Ophyd/Bluesky Queue Server|硬件抽象、实验编排、计划权限与流程中断|提供设备接入路线；队列停止/abort不能直接视为硬件辐射安全确认|

OpenAI官方文档区分输入、输出、工具护栏与人工审查，并说明agent级输入/输出护栏不会覆盖所有工具调用。这里将不可豁免的检查放在产生设备副作用的控制端；MCP也无法提供自批工具。[OpenAI护栏与人工审批](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)

Safe Lab Agents仓库采用容器中的agent与宿主机MCP工具分离，并提醒类型检查较浅，工具作者仍须校验元素类型、形状和数值边界。该隔离路线与本项目目标匹配，但不能据此宣称真实XRD已获安全保证。[Safe Lab Agents源码与说明](https://github.com/MaxNaeg/safe_lab_agents)

NeMo Guardrails提供多阶段rails和工具输入/输出检查，适合模型交互与工具策略层。本项目的角度预算、联锁状态和人工审批采用确定性判断，避免将设备安全是否放行交给模型评分。[NeMo Guardrails官方概览](https://docs.nvidia.com/nemo/guardrails/about-nemo-guardrails-library/overview)

Safe-SDL提出规划层、编排与安全核层、物理执行层的分层，以及操作设计域等安全边界。可作为实验室风险设计参考；本实现未实现数字孪生、控制屏障函数或ROS2安全控制。[Safe-SDL论文](https://arxiv.org/abs/2602.15061)

Ophyd统一设备的trigger/read/set等接口；Queue Server提供计划和设备权限及实验运行控制。本项目先提供Ophyd仿真适配器，真实XRD接入须确定厂商API、EPICS PV或其他控制协议，不能猜测设备寄存器。hklpy是衍射仪几何计算路线，并非通用厂商驱动。[Ophyd架构](https://blueskyproject.io/ophyd/architecture.html)、[Queue Server使用说明](https://blueskyproject.io/bluesky-queueserver/using_queue_server.html)、[hklpy概览](https://blueskyproject.io/hklpy/overview.html)

真实X射线设备的安全要求应结合设备类别和适用规范评估。ISO 13850讨论机械急停设计；IEC 61010-2-091讨论柜式X射线系统的专门要求。这里只查阅官方目录与范围，未进行条款符合性评估，不将模拟急停按钮描述为符合标准的急停装置。[ISO 13850](https://www.iso.org/standard/59970.html)、[IEC 61010-2-091](https://webstore.iec.ch/en/publication/33872)

交付边界：AI4S核心、人工审批与数据demo可独立运行；MCP SDK接口和Ophyd可选接入代码已提供，当前未安装联调；没有连接实体仪器，没有生产级硬件安全验收。
