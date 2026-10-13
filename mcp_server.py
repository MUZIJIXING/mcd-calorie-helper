"""Local, read-only stdio MCP server. Python standard library only."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from candidates import build_candidates
from meal import InputError, evaluate, recommend, revise
from mcp import Client, ProbeError, READ_TOOLS, load_token, scrub
from official import build_catalog, coverage_report, nutrition_meal, price_arguments, price_record, unwrap

VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
MAX_MESSAGE = 2_000_000
INSTRUCTIONS = (
    "为想控制热量但不想计算的普通人搭配麦当劳。先调用 mcd_guide 获取流程和契约。"
    "先确认用户条件，不默认热量目标。仅查热量用 mcd_nutrition；查门店/菜单/价格先用 "
    "mcd_official_tools 读取真实参数定义，再用 mcd_official_query。计算结果不得当作医疗建议。"
    "不猜商品编码、规格、价格或营养；未知不是零。仅支持查询和价格试算，不下单或领券。"
)


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


def field(kind, description):
    return {"type": kind, "description": description}


FIELDS = {
    "catalog": field("object", "mcd_normalize 返回的完整目录；不能修改来源或 menu_context。"),
    "meal": field("array", "1–20 行，每行 product_id、正整数 quantity，可选 choices 槽位ID→商品ID。"),
    "requirements": field("object", "用户明确条件：min_kcal/max_kcal、budget_cents+price_scope、required、excluded_ids、preferred_ids。"),
    "context": field("object", "原菜单上下文：store_id、dining_mode=pickup、menu_observed_at，及明确优惠。"),
    "quote": field("object", "mcd_normalize_price 返回的真实报价；五分钟失效，更换餐品/门店后重新报价。"),
    "nutrition": field("object", "list-nutrition-foods 的完整带 observed_at 响应包装。"),
    "menu": field("object", "query-meals 的完整带 observed_at 响应包装。"),
    "details": field("array", "从同一菜单取得的 query-meal-detail 响应包装数组。"),
}
LOCAL_TOOLS = {
    "normalize": ("将官方营养、门店菜单及详情适配为可计算目录。", ("nutrition", "menu", "details", "context"), ()),
    "coverage": ("检查当前菜单已查询部分的营养覆盖，不能宣称全菜单覆盖。", ("nutrition", "menu", "details", "context"), ()),
    "calculate": ("计算已选餐食热量、可售和约束；未知营养不算零。", ("catalog", "meal"), ("requirements", "context", "quote")),
    "candidates": ("在用户允许的替换槽位或确认的单点组内构造有限候选。", ("catalog", "selection"), ("requirements", "context", "max_candidates")),
    "recommend": ("从给定候选推荐一套和备选；有预算但缺报价时返回 needs_prices。", ("catalog",), ("groups", "candidates", "candidate_batch", "requirements", "context", "quotes", "current_meal", "max_candidates", "max_quotes")),
    "revise": ("替换/增加/删除餐品并保留条件；修改后旧报价失效。", ("catalog", "meal", "edits"), ("requirements", "context", "quote")),
    "price_arguments": ("生成需原样传给官方 calculate-price 的参数，拒绝旧菜单跨店复用。", ("catalog", "meal", "context"), ()),
    "normalize_price": ("将实际试算响应绑定餐品、门店、时间和请求参数。", ("price_response", "catalog", "meal", "context"), ()),
}
FIELDS.update({
    "selection": field("object", "bundle: meal/index/slot_ids；roles: pools，每组 role/basis/options。详见 mcd_guide。"),
    "groups": field("array", "每组候选餐品行，逐组挑一行；与 candidates/candidate_batch 三选一。"),
    "candidates": field("array", "明确列举的完整 meal 数组；与 groups/candidate_batch 三选一。"),
    "candidate_batch": field("object", "mcd_candidates 完整输出，保留搜索范围和截断证据。"),
    "quotes": field("array", "mcd_normalize_price 返回的有效报价数组。"),
    "current_meal": FIELDS["meal"],
    "edits": field("array", "按序执行：replace(index,line)、add(line)、remove(index)。"),
    "price_response": field("object", "官方 calculate-price 包装，必须含实际 request_arguments。"),
    "max_candidates": {"type": "integer", "minimum": 1, "maximum": 256},
    "max_quotes": {"type": "integer", "minimum": 1, "maximum": 8},
})


def definition(name, description, input_schema, online=False):
    return {"name": name, "description": description, "inputSchema": input_schema,
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": online}}


def tool_definitions():
    tools = [definition("mcd_" + name, description,
                        schema({key: FIELDS[key] for key in (*required, *optional)}, required))
             for name, (description, required, optional) in LOCAL_TOOLS.items()]
    tools.extend([
        definition("mcd_guide", "首次调用：读取点餐流程、计算契约和数据边界。", schema()),
        definition("mcd_official_tools", "查询官方六个只读工具的实时 inputSchema；不需要用户手填 JSON。", schema(), True),
        definition("mcd_official_query", "按真实 schema 调用官方查询/试算，返回带采集时间和实际参数的包装。",
                   schema({"tool": {"type": "string", "enum": sorted(READ_TOOLS)},
                           "arguments": field("object", "按 mcd_official_tools 返回的 inputSchema 构造。")},
                          ("tool", "arguments")), True),
        definition("mcd_nutrition", "只问标准餐品热量时直接用官方营养计算，无需先查门店；不证明可售。",
                   schema({"items": {"type": "array", "minItems": 1, "maxItems": 20,
                                     "items": schema({"name": field("string", "完整官方餐品名称，含规格。"),
                                                      "quantity": {"type": "integer", "minimum": 1, "maximum": 20}},
                                                     ("name", "quantity"))},
                           "requirements": field("object", "仅支持用户明确的 min_kcal/max_kcal。")}, ("items",)), True),
    ])
    return tools


class RpcError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


class OfficialError(Exception):
    def __init__(self, code):
        self.code = code


def validate(value, spec):
    """Validate the small schema vocabulary emitted by this server."""
    types = {"object": dict, "array": list, "string": str, "integer": int}
    kind = spec.get("type")
    if kind in types and (not isinstance(value, types[kind]) or (kind == "integer" and isinstance(value, bool))):
        raise InputError("invalid_type")
    if "enum" in spec and value not in spec["enum"]:
        raise InputError("invalid_enum")
    if kind == "object":
        if any(key not in value for key in spec.get("required", ())):
            raise InputError("missing_field")
        props = spec.get("properties", {})
        if spec.get("additionalProperties") is False and any(key not in props for key in value):
            raise InputError("unknown_field")
        for key in value.keys() & props.keys():
            validate(value[key], props[key])
    if kind == "array":
        if not spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", MAX_MESSAGE):
            raise InputError("invalid_length")
        if "items" in spec:
            for item in value:
                validate(item, spec["items"])
    if kind == "integer" and not spec.get("minimum", value) <= value <= spec.get("maximum", value):
        raise InputError("invalid_range")


class Server:
    def __init__(self, workspace=ROOT, client_factory=Client):
        self.workspace = Path(workspace)
        self.client_factory = client_factory
        self.client = None
        self.token = ""
        self.initialized = self.ready = False
        self.tools = {tool["name"]: tool for tool in tool_definitions()}

    def official_client(self):
        token, _ = load_token(self.workspace)
        if self.client is None or token != self.token:
            self.token = token
            self.client = None
            client = self.client_factory(token)
            client.initialize()
            self.client = client
        return self.client

    def query(self, tool, arguments):
        if tool not in READ_TOOLS:
            raise InputError("unknown_tool")
        result = self.official_client().call(tool, arguments)
        record = {"observed_at": datetime.now(timezone.utc).isoformat(), "tool": tool,
                  "request_arguments": arguments, "result": result}
        if isinstance(result, dict):
            body = result.get("structuredContent", {})
            if result.get("isError") or (isinstance(body, dict) and (body.get("success") is not True or body.get("code") != 200)):
                code = body.get("code") if isinstance(body, dict) and type(body.get("code")) is int else None
                raise OfficialError(code)
        unwrap(record)
        return scrub(record, self.token)

    def call_tool(self, name, arguments):
        if name == "mcd_guide":
            return {"workflow": (ROOT / "docs/mcp-workflow.md").read_text(encoding="utf-8"),
                    "contract": (ROOT / "docs/mcp-contract.md").read_text(encoding="utf-8")}
        if name == "mcd_official_tools":
            return {"tools": scrub(self.official_client().list_tools(), self.token)}
        if name == "mcd_official_query":
            return self.query(arguments["tool"], arguments["arguments"])
        if name == "mcd_nutrition":
            record = self.query("list-nutrition-foods", {})
            return nutrition_meal(record, arguments["items"], arguments.get("requirements"))
        action = name[4:]
        if action in ("calculate", "recommend", "revise"):
            shared = {"catalog": arguments["catalog"], "requirements": arguments.get("requirements"),
                      "context": arguments.get("context"), "now": datetime.now(timezone.utc)}
            if action == "calculate":
                return evaluate(meal=arguments["meal"], quote=arguments.get("quote"), **shared)
            if action == "recommend":
                return recommend(groups=arguments.get("groups"), candidates=arguments.get("candidates"),
                                 candidate_batch=arguments.get("candidate_batch"), quotes=arguments.get("quotes"),
                                 current_meal=arguments.get("current_meal"),
                                 max_candidates=arguments.get("max_candidates", 256),
                                 max_quotes=arguments.get("max_quotes", 8), **shared)
            return revise(meal=arguments["meal"], edits=arguments["edits"], quote=arguments.get("quote"), **shared)
        functions = {"normalize": build_catalog, "coverage": coverage_report, "candidates": build_candidates,
                     "price_arguments": price_arguments, "normalize_price": price_record}
        if action == "candidates":
            data = dict(arguments)
            data["limit"] = data.pop("max_candidates", 256)
            return build_candidates(**data)
        if action == "normalize_price":
            return price_record(arguments["price_response"], arguments["catalog"], arguments["meal"], arguments["context"])
        return functions[action](**arguments)

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise RpcError(-32600, "Invalid Request")
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized" and self.initialized:
                self.ready = True
            return None
        if type(request["id"]) not in (int, str):
            raise RpcError(-32600, "Invalid Request")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(-32602, "Invalid params")
        if method == "ping":
            return {}
        if method == "initialize":
            if self.initialized:
                raise RpcError(-32600, "Already initialized")
            if not isinstance(params.get("protocolVersion"), str) or not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
                raise RpcError(-32602, "Invalid initialize params")
            self.initialized = True
            version = params["protocolVersion"]
            return {"protocolVersion": version if version in VERSIONS else VERSIONS[-1],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcd-calorie-helper", "version": "0.2.0"},
                    "instructions": INSTRUCTIONS}
        if method not in ("tools/list", "tools/call"):
            raise RpcError(-32601, "Method not found")
        if not self.ready:
            raise RpcError(-32600, "Initialize first")
        if method == "tools/list":
            if params.get("cursor"):
                raise RpcError(-32602, "No pagination cursor")
            return {"tools": list(self.tools.values())}
        name = params.get("name")
        if not isinstance(name, str) or name not in self.tools:
            raise RpcError(-32602, "Unknown tool")
        arguments = params.get("arguments", {})
        try:
            validate(arguments, self.tools[name]["inputSchema"])
            result = self.call_tool(name, arguments)
            failed = False
        except OfficialError as error:
            result, failed = {"error": "official_business_error", "business_code": error.code,
                              "message": "官方查询/试算未成功；不能当作空菜单、无优惠或零价格。"}, True
        except ProbeError as error:
            # ProbeError messages are fixed codes, never raw HTTP body or token.
            result, failed = {"error": str(error), "message": "检查本机 Token、网络或官方接口状态；不要在聊天提供凭据。"}, True
        except (InputError, KeyError, TypeError, AttributeError, OSError, UnicodeError, ValueError, RecursionError, ArithmeticError):
            result, failed = {"error": "invalid_request_or_response", "message": "检查工具参数和数据契约；官方数据不可用时不要编造结果。"}, True
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result, "isError": failed}

    def response(self, request):
        request_id = request.get("id") if isinstance(request, dict) and type(request.get("id")) in (int, str) else None
        try:
            result = self.dispatch(request)
            if result is None:
                return None
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except RpcError as error:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": error.code, "message": error.message}}
        # Also protect offline outputs when a credential is configured but unused.
        try:
            token, _ = load_token(self.workspace)
        except ProbeError:
            token = self.token
        return scrub(response, token) if token else response


def serve(server, source, sink):
    while True:
        line = source.readline(MAX_MESSAGE + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE:
            while line and not line.endswith(b"\n"):
                line = source.readline(MAX_MESSAGE + 1)
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Message too large"}}
        else:
            try:
                request = json.loads(line.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                response = server.response(request)
            except (ValueError, UnicodeError, RecursionError):
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        if response is not None:
            sink.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            sink.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=ROOT, help="Token workspace; defaults to repository root, independent of cwd")
    args = parser.parse_args()
    try:
        serve(Server(args.workspace.resolve()), sys.stdin.buffer, sys.stdout.buffer)
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
