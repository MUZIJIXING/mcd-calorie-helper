# 麦当劳热量友好 MCP

为想控制热量、但不想费脑计算的普通人搭配麦当劳餐食。本地 stdio MCP 提供官方营养、门店菜单和价格查询，以及计算、推荐和替换餐品；客户端负责自然语言对话。

Python 3.10+，仅使用标准库，无需安装生产依赖或 Skill。每位用户配置自己的麦当劳官方 Token。

## 下载与配置

下载整个仓库 ZIP 并解压，保留目录结构，或：

```text
git clone https://github.com/MUZIJIXING/mcd-calorie-helper.git
```

按[麦当劳官方接入说明](https://github.com/M-China/mcd-mcp-server)申请 Token。在仓库根目录创建 `local/mcd-token.txt`，用 UTF-8 保存 Token，不加 `Bearer`。该文件是本机明文，不要分享或提交。也可设置环境变量 `MCD_MCP_TOKEN`，并重启客户端；环境变量优先于文件。

默认从脚本所在仓库读取凭据，与客户端工作目录无关。使用另一个凭据目录时，在脚本参数后添加 `--workspace <目录绝对路径>`，其中仍使用 `local/mcd-token.txt`。

## 接入客户端

客户端需要支持本地 stdio MCP。配置命令为 `python` 或 Python 可执行文件的绝对路径，参数为 `mcp_server.py` 的绝对路径。

Codex 配置示例（替换为自己的解压路径，加入现有配置，不覆盖其他设置）：

```toml
[mcp_servers.mcd_calorie_helper]
command = "python"
args = ["D:/tools/mcd-calorie-helper/mcp_server.py"]
env_vars = ["MCD_MCP_TOKEN"]
startup_timeout_sec = 10
tool_timeout_sec = 60
```

配置依据：[OpenAI MCP 文档](https://developers.openai.com/codex/mcp)。使用 Token 文件时无需环境变量转发。配置后重启客户端。

JSON 客户端参考 [mcp-local.example.json](mcp-local.example.json)；Codex 参考 [mcp-local.codex.example.toml](mcp-local.codex.example.toml)。不同客户端的配置位置和环境变量继承方式不同。macOS/Linux 使用自己的 `python3` 及脚本绝对路径。带空格的路径保持一个参数，不加内层引号。

## 使用

```text
使用麦当劳热量助手，先读取使用指南。麦香鸡加中薯条，一共多少热量？
```

```text
我这顿想控制在 700 千卡、30 元以内，保留中薯条，帮我搭配。先确认门店。
```

首次由客户端模型调用 `mcd_guide` 读取流程和契约。官方参数通过 `mcd_official_tools` 实时获取，普通用户不必填写 JSON。

12 项工具包含官方 schema/只读查询、直接查热量、调用指南，以及目录适配、覆盖检查、计算、候选、推荐、修改、价格参数和报价绑定。

```text
mcp_server.py                 stdio 入口
src/                         四个计算与官方接口模块
docs/mcp-workflow.md          客户端模型调用指南
docs/mcp-contract.md          运行数据契约
mcp-local.*                  客户端配置示例
```

`docs/` 中两个文件由 `mcd_guide` 读取，属于运行所需文件，解压后保留。直接在终端运行 `python mcp_server.py` 后等待输入是正常状态；它由客户端启动，标准输出只用于 MCP JSON 消息。

## 排错与边界

- 无法启动：核对 Python 和脚本的绝对路径，并保留 `src/` 与 `docs/`。
- `token_not_configured`：在正确目录保存自己的凭据，或检查客户端能否读取环境变量。
- `token_invalid_or_expired`：在本机更新 Token，不发到聊天。
- `network_or_tls_error` 或限流：检查到官方服务的网络与服务状态；失败不等于没有餐品，不自动重放超时请求。
- `official_business_error`：保留官方整数错误码，按调用指南有限刷新，不能把失败当成空菜单或零价格。
- 参数或响应异常：模型重新读取指南、官方 schema 和菜单，不猜编码、规格、营养或价格。

仅支持只读查询和价格试算，不下单、领券或支付。真实菜单适配限到店取餐、标准餐品和固定数量的套餐槽位。无可靠营养时报告缺项，未知不算零；不为去酱、去面包推算精确减卡量。未指定热量目标时不添加默认上限。单轮最多检查 256 个组合、请求 8 个候选报价；结论限定在已查范围。报价绑定餐品和门店，五分钟后须重新核验。

采用 [MCP stdio](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)，支持 2025-03-26、2025-06-18、2025-11-25；新客户端需接受协商至受支持版本。未提供远程 HTTP 服务。查询只访问官方 `https://mcp.mcd.cn`，本 MCP 不落盘原始响应或向开发者发送遥测；客户端可能按自身设置保存聊天与工具结果。

这是独立开发作品，不是麦当劳官方产品。营养与搭配用于日常点餐参考，不承诺减重效果。实际使用需要自己的有效 Token 和可用网络，不保证所有客户端及系统环境兼容。
