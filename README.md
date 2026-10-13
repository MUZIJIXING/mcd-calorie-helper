# 麦麦轻松吃

<p align="center">
  <img src="assets/maiqingqing-mascot.png" alt="麦轻轻：系着薄荷绿围巾、抱着汉堡的小麦粒精灵" width="320">
</p>

**热量交给我，你负责好好吃饭。**

为想控制热量、但不想费脑计算的普通人搭配麦当劳餐食。本地 stdio MCP 提供官方营养、门店菜单和价格查询，以及计算、推荐和替换餐品；客户端负责自然语言对话。

Python 3.10+，仅使用标准库，无需安装生产依赖或 Skill。每位用户配置自己的麦当劳官方 Token。

## 认识麦轻轻

项目名字叫 **「麦麦轻松吃」**，吉祥物昵称 **「麦轻轻」**。

麦轻轻是一只圆滚滚的小麦粒精灵，系着薄荷绿围巾，开心地抱着汉堡。它的口头禅是：**“热量交给我，你负责好好吃饭。”**

## 下载与配置

下载整个仓库 ZIP 并解压，保留目录结构，或：

```text
git clone https://github.com/MUZIJIXING/mcd-calorie-helper.git
```

仓库包含完整运行代码和全部 12 项工具。配置好 Python、自己的麦当劳 Token 和客户端后，即可使用；精简发布保留了完整功能。

按[麦当劳官方接入说明](https://github.com/M-China/mcd-mcp-server)申请 Token。在仓库根目录创建 `local/mcd-token.txt`，用 UTF-8 保存 Token，不加 `Bearer`。该文件是本机明文，不要分享或提交。也可设置环境变量 `MCD_MCP_TOKEN`，并重启客户端；环境变量优先于文件。

默认从脚本所在仓库读取凭据，与客户端工作目录无关。使用另一个凭据目录时，在脚本参数后添加 `--workspace <目录绝对路径>`，其中仍使用 `local/mcd-token.txt`。

### 让大模型帮你安装配置

把下面的 prompt 发给能操作本机文件和终端的大模型助手，例如 Codex。配置过程中，在本机保存自己的 Token。

```text
请帮我在这台电脑安装并配置“麦麦轻松吃”MCP。
仓库：https://github.com/MUZIJIXING/mcd-calorie-helper

请按以下步骤执行：
1. 确认我要接入的 MCP 客户端和安装目录；当前目录已有该仓库时直接复用。
2. 检查 Python 3.10 或以上是否可用，找到其可执行文件的绝对路径；缺少 Python 时指导我安装。
3. 下载并解压完整仓库，确认 mcp_server.py、src/ 和 docs/ 齐全。本项目只用标准库，无需安装 Python 依赖。
4. 指导我按麦当劳官方说明申请 Token，并由我在本机保存到仓库的 local/mcd-token.txt；只保存 Token，不加 Bearer。不要让我把 Token 发到聊天，也不要输出文件内容。
5. 按我使用的客户端配置本地 stdio MCP：command 使用实际 Python 路径，args 使用 mcp_server.py 的绝对路径。合并到已有配置，保留其他服务和设置；参考仓库的 mcp-local 配置示例。
6. 告诉我如何重启或重新连接客户端，确认能发现 12 项工具，并调用 mcd_guide 读取使用流程。
7. 用“麦香鸡 1 份多少热量”调用 mcd_nutrition 完成真实查询验证。有连接或凭据问题时给出对应处理步骤，成功后再报告配置完成。
8. 最后给出安装目录、接入客户端、连接结果和一条可直接使用的聊天示例。

请直接完成能执行的步骤，只在缺少必要信息或需要我在本机操作时询问。
```

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

## 常见问题

- 无法启动：核对 Python 和脚本的绝对路径，确认 `src/` 与 `docs/` 齐全。
- 客户端未显示工具：确认配置为本地 stdio MCP，并重启或重新连接客户端。
- Token 未配置或失效：检查 `local/mcd-token.txt` 或 `MCD_MCP_TOKEN`，在本机更新凭据。
- 连接失败或限流：检查网络，稍后重新尝试官方查询。

热量按官方标准餐品数据计算；餐品可售状态与价格以当前门店查询为准。修改餐品后，助手会重新计算并核验价格。

本项目为独立开发的日常点餐助手，营养与搭配信息用于点餐参考。

## 活动与接入说明

本项目按[麦当劳程序员创意开发大赛](https://github.com/M-China/mcd-developer-innovation-challenge)的项目要求准备。官方 MCP 工具、实际调用流程和业务价值见 [MCP_INTEGRATION.md](MCP_INTEGRATION.md)；官方参赛声明见 [CONTEST_DECLARATION.md](CONTEST_DECLARATION.md)。

报名入口：[麦麦轻松吃 · Issue #636](https://github.com/M-China/mcd-developer-innovation-challenge/issues/636)。
