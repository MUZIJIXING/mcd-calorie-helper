# 计算脚本数据契约

这是本项目的内部格式，不是官方 MCP schema。字段必须根据实际工具响应填写，不能由模型补造数值。餐品编码、规格、营养和价格均须来自真实工具响应；不得把模拟数据作为真实点餐依据。

## 请求入口

| 命令 | 必填输入 | 可选输入 |
| --- | --- | --- |
| `normalize` | `nutrition`, `menu`, `details`, `context` | 无 |
| `nutrition-meal` | `nutrition`, `items`（完整 `name`、整数 `quantity`） | `requirements` 仅限热量上、下限 |
| `calculate` | `catalog`, `meal` | `requirements`, `context`, `quote` |
| `candidates` | `catalog`, `selection` | `requirements`, `context`, `max_candidates` |
| `recommend` | `catalog`，及 `groups` / `candidates` / `candidate_batch` 三选一 | `requirements`, `context`, `quotes`, `current_meal`, `max_candidates`, `max_quotes` |
| `coverage` | `nutrition`, `menu`, `details`, `context` | 无 |
| `revise` | `catalog`, `meal`, `edits` | `requirements`, `context`, `quote` |
| `price-arguments` | `catalog`, `meal`, `context` | 无 |
| `normalize-price` | `price_response`, `catalog`, `meal`, `context` | 无 |

官方响应包装为 `{"observed_at":"带时区的 ISO 时间", "result": MCP工具返回对象}`。适配器要求 `result.structuredContent.success=true`、`code=200`。它不解析 text 中的自然语言说明。`details` 是从同一次门店菜单取得的目标根商品详情响应数组。

价格响应包装还必须包含实际发送的 `request_arguments`，与 `price-arguments` 生成的参数一致。不得将一个旧报价重新绑定到未发送过的餐品、门店或优惠条件。

## Catalog

仅问热量时使用 `nutrition-meal`，输入官方营养响应和 1–20 个明确名称的标准餐品，例如 `items=[{"name":"麦香鸡","quantity":1}]`。脚本按完整名称匹配，支持数量合并；未匹配的名称仍报告缺项。返回计算结果，`checks.available=unknown`、价格未知，不据此宣称可售或生成购买编码。`satisfied` 同时包含可售检查，因此纯营养模式应分别读取 `nutrition_complete`、`checks.max_kcal` 等字段解释热量是否符合要求。

- `data_origin`：`official_mcp` 或 `synthetic`，禁止把模拟来源改成真实来源。
- `menu_context`：真实菜单对应的门店、就餐方式与采集时间。`menu_observed_at` 必须与原始菜单的 `observed_at` 指向同一时刻；适配器保存独立副本。调用计算或生成价格参数时不能换上下文而继续使用旧菜单。
- `products`：非空数组，每项有唯一 `id`、完整 `name`、明确 `spec`、布尔 `available` 和 `mapping_verified`。
- `standalone=false`：仅套餐内可选的子项，不能单独放进 `meal`。
- `nutrition`：`energy` 为非负有限数值（建议十进制字符串），`unit` 为 `kcal` 或 `kJ`，`basis` 为 `serving` 或 `per_100g`，`spec` 必须对应餐品；`source` 必须记录工具名、记录标识与带时区的 `observed_at`。
- 每 100g 数据还必须提供餐品的 `weight_g`，否则没有可核验的每份热量。
- `components`：固定子项数组，包含 `product_id`、正整数 `quantity`。
- `slots`：套餐替换槽位数组，包含唯一 `id`、正整数 `quantity`、`options` 商品 ID 数组与有效 `default`。

有组成或槽位时按组成汇总，不叠加套餐整体营养。固定组成和槽位不应重复描述同一份餐品。禁止环形组成，第一版不支持嵌套的可选槽位，复杂套餐需先明确组成再输入。

## Meal 与 Groups

`meal` 为 1–20 行，每行仅有 `product_id`、1–20 的整数 `quantity`、可选 `choices`。`choices` 将槽位 ID 映射到选中的商品 ID。缺失槽位使用官方确定的默认项；相同商品及选择自动合并数量。

`groups` 为 1–6 个组，每组是 1–30 个候选餐品行。每个组挑一行组成一餐，例如主食组、小食组、饮料组。想比较套餐与单点时可以分别构造候选，不能把套餐及其子项再次单点误算成同一套餐。脚本只搜索给定组，不代表全门店菜单。

## Requirements

所有条件可省略，不默认提供热量上限。

- `min_kcal` / `max_kcal`：用户明确的热量下限、上限，有限非负数。
- `budget_cents`：整数分；同时必须提供 `price_scope`，取 `items` 或 `total`。
- `required`：数组，项为 `{"product_id":"...", "quantity":1}`；套餐所包含的子项也计入保留数量。
- `excluded_ids`：排除商品 ID，也检查套餐子项。
- `preferred_ids`：软性偏好；不是必须项。

排序优先满足偏好、减少相对当前方案的改动，再比较核验价格，最后用稳定指纹打破并列。不按最低热量排序，不添加“健康分”。

## 候选构造

`candidates` 命令接受两种 `selection`；商品编码和槽位必须来自实际目录，以下编码仅示意。

套餐替换：

```json
{"mode":"bundle", "meal":[{"product_id":"套餐编码","quantity":1}], "index":0, "slot_ids":["饮料槽位编码"]}
```

`index` 指规范化后的餐品行。仅枚举 `slot_ids` 中列出的允许选项，保留其他槽位、餐品和数量。没有明确允许更换的范围时先澄清，不默认遍历所有槽位。

单点搭配：

```json
{"mode":"roles", "pools":[
  {"role":"main","basis":"official_category","options":[{"product_id":"主餐编码","quantity":1}]},
  {"role":"side","basis":"user_confirmed","options":[{"product_id":"小食编码","quantity":1}]},
  {"role":"drink","basis":"official_category","options":[{"product_id":"饮料编码","quantity":1}]}
]}
```

- 1–3 个唯一角色，必须有 `main`；用户未要求的小食、饮料可不设组。
- `basis` 记录官方分类或用户确认，调用者需保留对应证据。脚本校验声明与编码，不凭空证明该声明真实；不要从菜名猜测角色。
- 每组 1–30 个可单点叶子商品选项，数量固定，不把套餐或仅套餐子项放入单点组。
- 必选项与偏好优先，再按稳定编码顺序枚举；最多检查 256 个组合。数量、主餐或必选条件不会为达标而改变。
- 不可售、排除项与必选冲突会被过滤并计数；未知营养、上下文或热量超限保留给推荐器解释。

返回 `candidates`、`scope`、`total_combinations`、`examined_count`、`search_complete`、`rejected_checks` 与来源标记。`ready` 仅代表有候选，不代表已通过营养和预算条件。

将整个输出放入推荐请求的 `candidate_batch`，生成阶段的截断会传到最终 `search_complete`。不要在更新目录、上下文或用户条件后复用旧候选批次；重新生成。`recommend` 的 `generation` 保留生成范围和过滤原因。空批次可以解释为已检查范围内没有候选；数据异常不能包装成空批次。

`candidates` 字段也可直接传入最多 256 套完整餐食，但只适用于调用者明确列举的有限比较，不携带此前候选生成的覆盖证据。不得与 `groups` 或 `candidate_batch` 混用。

推荐默认一套加最多一套有不同餐品的备选；有热量差异时优先选择能说明差异的备选，主推荐仍按原偏好与价格排序。`comparison.delta_kcal` 为备选减主推荐，用未舍入值计算；`delta_cents` 仅在两套均有有效报价时提供，不代表仅商品价预算范围的差值。

## 覆盖报告

`coverage` 统计当前菜单根商品总数、已查详情根商品数、可适配根商品数、默认组成营养完整数与尚未查询数，逐项记录缺口。分母是去重后的菜单根商品，不是营养表行数或套餐子项数。

仅检查已查根商品的默认组成，不代表所有替换方案或全菜单营养覆盖。一个不支持的套餐单独标记，不使其他商品的统计消失；调用失败仍需先处理，不能伪装成未查或无营养。

## Context、Quote 和价格绑定

`context` 仅包含：

- `store_id`、`dining_mode`（首发真实适配为 `pickup`）、`menu_observed_at`。
- `coupon_ids`：明确选用的官方券 ID 数组，默认空。
- `coupon_bindings`：可选，以餐品 ID 为键，值为真实 `couponId` 与 `couponCode`。必须与 `coupon_ids` 一致；每份券商品单独试算，不推断券可叠加。

`quote` 包含脚本返回的 `fingerprint`、`source=calculate-price`、相同的 `data_origin`、整数 `total_cents` 与 `items_cents`、布尔 `includes_delivery`、带时区的 `quoted_at`。真实报价必须通过 `normalize-price` 转换，不能手工把标价包装成报价。

指纹绑定规范化餐品、数量、全部套餐选择、门店、就餐方式、菜单采集时间及优惠绑定。匹配指纹不是证明报价来自服务器的密码学签名，来源仍需依赖真实工具调用。报价最多使用五分钟；这是本项目的保守刷新规则，不是官方价格保证。CLI 使用当前时间；历史快照测试只在测试代码中注入采集时刻。

## Revise

`edits` 为非空数组，顺序执行：

```json
[
  {"op":"replace", "index":0, "line":{"product_id":"some-product", "quantity":1}},
  {"op":"add", "line":{"product_id":"another-product", "quantity":1}},
  {"op":"remove", "index":1}
]
```

索引针对当前规范化后的 `meal`，不是用户最初输入顺序。替换整行时保留不变的其他套餐槽位。返回 `before`、`after`、未舍入数值计算出的展示变化量、`price_invalidated` 和原条件；不会修改输入对象。

## 结果解释

- `nutrition_complete=false`：`total_kcal=null`，仅有已知小计。
- 条件取值为 `true`、`false`、`unknown` 或 `not_requested`。
- `recommended`：已找到满足条件的已核验方案；检查 `price_complete` 与搜索范围。
- `needs_prices`：已有符合其他条件的候选，需要逐一官方试算。
- `insufficient_data`：数据缺失或上下文不匹配，不能证明无解。
- `no_match_in_checked_candidates`：搜索上限内未找到，仍有未检查组合。
- `no_match_in_provided_groups`：给定组内全部已检查，未找到符合条件的组合；仍不是全菜单无解。
- `no_match_in_provided_candidates`：给定候选及生成范围内没有符合条件的方案；查看 `generation`，不扩大为全菜单结论。
- `price_requests`：本轮最多 8 个待报价方案。达到上限后可在用户需要时继续下一轮，不能隐藏剩余未核验项。
- `test_only=true`：假数据开发输出。

输入非法时 CLI 返回 JSON 错误并退出 `2`；正常业务结果（包括无解或待报价）退出 `0`。错误响应不回显输入内容。
