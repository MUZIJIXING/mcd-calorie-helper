"""Adapt the observed official MCP response contract; reject ambiguous mappings."""

import csv
from copy import deepcopy
import re
import unicodedata

from meal import InputError, canonical_meal, evaluate, fingerprint, integer, number, text, timestamp, validate_catalog

# Only exact, reviewed names for the same documented serving are aliased.
VERIFIED_NAMES = {"雪碧(小杯)": "雪碧小杯"}


def unwrap(record):
    if not isinstance(record, dict):
        raise InputError("MCP record must be an object")
    result = record.get("result", record)
    if not isinstance(result, dict):
        raise InputError("MCP result must be an object")
    if result.get("isError"):
        raise InputError("MCP tool returned an error")
    body = result.get("structuredContent")
    if not isinstance(body, dict) or body.get("success") is not True or body.get("code") != 200:
        raise InputError("successful structuredContent required; do not parse instructions from text")
    return body["data"]


def record_origin(record):
    """Explicit simulation labels can only restrict a result to test use."""
    body = record.get("result", record).get("structuredContent", {})
    simulated = (record.get("test_only") is True or record.get("data_origin") == "synthetic"
                 or body.get("simulation_only") is True or body.get("test_only") is True)
    return "synthetic" if simulated else "official_mcp"


def name_key(value):
    return "".join(unicodedata.normalize("NFKC", text(value, "name")).split())


def find_nutrition(records, name):
    key = name_key(name)
    if key in records:
        return records[key], "exact_name_after_width_and_whitespace_normalization"
    target = VERIFIED_NAMES.get(key)
    return records.get(target), "evidenced_same_spec_name" if target else "unmapped"


def nutrition_records(record):
    data = unwrap(record)
    if not isinstance(data, str):
        raise InputError("nutrition table format changed")
    lines = data.splitlines()
    if not lines:
        raise InputError("empty nutrition table")
    header = re.fullmatch(r"\[(\d+)\]\{([^}]+)\}:", lines[0])
    if not header:
        raise InputError("unsupported nutrition table header")
    fields = header.group(2).split(",")
    if not {"productName", "energyKcal", "energyKj"}.issubset(fields) or len(fields) != len(set(fields)):
        raise InputError("nutrition table fields changed")
    rows = list(csv.reader(lines[1:], skipinitialspace=True))
    if len(rows) != int(header.group(1)) or any(len(row) != len(fields) for row in rows):
        raise InputError("nutrition table row count/width mismatch")
    records = {}
    for row in rows:
        values = dict(zip(fields, (v.strip() for v in row)))
        key = name_key(values["productName"])
        for field in ("energyKcal", "energyKj"):
            if values[field] != "null":
                number(values[field], field)
        if key in records and records[key] is not None:
            previous = records[key]
            if any(previous[field] != values[field] for field in fields if field != "nutritionDescription"):
                records[key] = None
        elif key not in records:
            records[key] = values
    return records


def check_store_request(record, context, code=None):
    """Validate recorded request bindings; legacy snapshots may omit arguments."""
    if not isinstance(record, dict) or not isinstance(context, dict):
        raise InputError("record and context must be objects")
    text(context.get("store_id"), "context.store_id")
    if context.get("dining_mode") != "pickup":
        raise InputError("official menu adapter currently supports pickup only")
    arguments = record.get("request_arguments")
    if arguments is None:
        return
    if not isinstance(arguments, dict) or arguments.get("storeCode") != context.get("store_id"):
        raise InputError("recorded request belongs to a different store")
    if context.get("dining_mode") != "pickup" or arguments.get("orderType") != 1 or arguments.get("beType") != 1:
        raise InputError("recorded request dining mode differs from supported pickup context")
    if code is not None and arguments.get("code") != code:
        raise InputError("recorded detail request code differs from response")


def check_menu_context(menu, context):
    check_store_request(menu, context)
    if timestamp(menu.get("observed_at"), "menu.observed_at") != timestamp(context.get("menu_observed_at"), "context.menu_observed_at"):
        raise InputError("menu capture time differs from context")


def build_catalog(nutrition, menu, details, context):
    """details is the selected root product details; unknown nutrition stays unknown."""
    records = nutrition_records(nutrition)
    check_menu_context(menu, context)
    menu_data = unwrap(menu)
    if not isinstance(menu_data, dict) or not isinstance(menu_data.get("meals"), dict):
        raise InputError("menu structure changed")
    observed = text(nutrition.get("observed_at"), "nutrition.observed_at")
    timestamp(observed, "nutrition.observed_at")
    roots = menu_data["meals"]
    products = {}
    leaf_details = {}
    for detail in details:
        body = unwrap(detail)
        check_store_request(detail, context, body.get("code"))
        if body.get("rounds") == [] and body.get("code") in roots:
            leaf_details[body["code"]] = text(body.get("name"), "detail.name")

    def add_leaf(code, name):
        # A directly queried leaf detail resolves shorter names in a bundle choice
        # using the same official product code, not a guessed name alias.
        name = leaf_details.get(code, name)
        match, method = find_nutrition(records, name)
        leaf = {"id": code, "name": name, "spec": "标准份:" + name,
                "available": True, "mapping_verified": match is not None,
                "standalone": code in roots, "mapping_method": method}
        if match is not None:
            value, unit = match["energyKcal"], "kcal"
            if value == "null":
                value, unit = match["energyKj"], "kJ"
            leaf["nutrition"] = {"energy": None if value == "null" else value, "unit": unit,
                                 "basis": "serving", "spec": leaf["spec"],
                                 "source": {"tool": "list-nutrition-foods", "record_id": match["productName"],
                                            "observed_at": observed, "basis_note": "官方同名标准餐品参考值；接口未返回克重"}}
        if code in products and products[code] != leaf:
            # Same SKU with different names/specs is not silently overwritten.
            raise InputError("conflicting product definitions; query child detail to resolve")
        products[code] = leaf

    for detail in details:
        body = unwrap(detail)
        code = text(body.get("code"), "detail.code")
        if code not in roots:
            raise InputError("detail root not in this menu")
        name = text(body.get("name"), "detail.name")
        rounds = body.get("rounds")
        if not isinstance(rounds, list):
            raise InputError("detail.rounds: list required")
        if not rounds:
            add_leaf(code, name)
            continue
        slots = []
        for group in rounds:
            quantity = integer(group.get("quantity"), "round.quantity", minimum=1, maximum=20)
            if group.get("minQuantity") != quantity or group.get("maxQuantity") != quantity:
                raise InputError("variable-size round requires explicit resolution")
            choices = group.get("choices", [])
            if not isinstance(choices, list) or not choices:
                raise InputError("round.choices: nonempty list required")
            defaults = [c for c in choices if c.get("isDefault") == 1 and c.get("quantity", 0) > 0]
            if len(defaults) != 1:
                raise InputError("round default ambiguous; request explicit choice")
            ids = []
            for choice in choices:
                choice_code = text(choice.get("code"), "choice.code")
                add_leaf(choice_code, text(choice.get("name"), "choice.name"))
                ids.append(choice_code)
            slots.append({"id": str(group["id"]), "quantity": quantity, "options": ids, "default": defaults[0]["code"]})
        products[code] = {"id": code, "name": name, "spec": "套餐按已选组成计算",
                          "available": True, "mapping_verified": True, "standalone": True, "slots": slots}
    origin = "synthetic" if any(record_origin(r) == "synthetic" for r in [nutrition, menu, *details]) else "official_mcp"
    catalog = {"data_origin": origin, "menu_context": deepcopy(context),
               "products": [products[k] for k in sorted(products)]}
    validate_catalog(catalog)
    return catalog


def nutrition_meal(nutrition, items, requirements=None):
    """Calculate exact-name standard servings without inventing a store menu."""
    records = nutrition_records(nutrition)
    observed = nutrition.get("observed_at")
    timestamp(observed, "nutrition.observed_at")
    if not isinstance(items, list) or not 1 <= len(items) <= 20:
        raise InputError("items: 1..20 named standard servings required")
    products, meal = {}, []
    for item in items:
        if not isinstance(item, dict) or set(item) - {"name", "quantity"}:
            raise InputError("nutrition item: name and quantity only")
        name = text(item.get("name"), "item.name")
        key = name_key(name)
        record, method = find_nutrition(records, name)
        code = "nutrition:" + key
        display_name = record["productName"] if record is not None else name
        product = {"id": code, "name": display_name, "spec": "标准份:" + display_name,
                   "available": True, "mapping_verified": record is not None, "mapping_method": method}
        if record is not None:
            energy, unit = record["energyKcal"], "kcal"
            if energy == "null":
                energy, unit = record["energyKj"], "kJ"
            product["nutrition"] = {"energy": None if energy == "null" else energy,
                                    "unit": unit, "basis": "serving", "spec": product["spec"],
                                    "source": {"tool": "list-nutrition-foods", "record_id": record["productName"],
                                               "observed_at": observed, "basis_note": "官方同名标准餐品参考值；接口未返回克重"}}
        products[code] = product
        meal.append({"product_id": code, "quantity": integer(item.get("quantity", 1), "item.quantity", minimum=1, maximum=20)})
    catalog = {"data_origin": record_origin(nutrition), "availability_scope": "nutrition_only",
               "products": list(products.values())}
    if requirements is not None and (not isinstance(requirements, dict) or set(requirements) - {"min_kcal", "max_kcal"}):
        raise InputError("nutrition-only mode accepts caloric limits only")
    return evaluate(catalog, meal, requirements)


def price_arguments(catalog, meal, context):
    if catalog.get("availability_scope") == "nutrition_only":
        raise InputError("nutrition record names are not purchasable menu codes")
    if not isinstance(context, dict) or context.get("dining_mode") != "pickup":
        raise InputError("official price adapter currently supports pickup only")
    text(context.get("store_id"), "context.store_id")
    timestamp(context.get("menu_observed_at"), "context.menu_observed_at")
    menu_context = catalog.get("menu_context")
    if not isinstance(menu_context, dict) or any(menu_context.get(key) != context.get(key)
            for key in ("store_id", "dining_mode", "menu_observed_at")):
        raise InputError("price request requires this catalog's menu context")
    bindings = context.get("coupon_bindings", {})
    if set(context.get("coupon_ids", [])) != {b["couponId"] for b in bindings.values()}:
        raise InputError("coupons require actual couponId/couponCode mapping; not inferred")
    products = validate_catalog(catalog)
    items = []
    for line in canonical_meal(meal, catalog):
        item = {"productCode": line["product_id"], "quantity": line["quantity"]}
        if line["product_id"] in bindings:
            if line["quantity"] != 1:
                raise InputError("coupon quantity requires explicit individual quote")
            item.update(bindings[line["product_id"]])
        slots = products[line["product_id"]].get("slots", [])
        if slots:
            item["roundList"] = [{"round": slot["id"], "comboItemList": [
                {"code": line["choices"][slot["id"]], "quantity": slot["quantity"]}]} for slot in slots]
        items.append(item)
    if set(bindings) - {line["product_id"] for line in canonical_meal(meal, catalog)}:
        raise InputError("coupon bound to product absent from meal")
    return {"storeCode": context["store_id"], "orderType": 1, "beType": 1, "items": items, "needTableware": False}


def coverage_report(nutrition, menu, details, context):
    """Measure requested roots and their default composition, never all variants."""
    nutrition_records(nutrition)
    check_menu_context(menu, context)
    menu_data = unwrap(menu)
    if not isinstance(menu_data, dict) or not isinstance(menu_data.get("meals"), dict):
        raise InputError("menu structure changed")
    if not isinstance(details, list):
        raise InputError("details: list required")
    indexed = {}
    leaves = {}
    for record in details:
        body = unwrap(record)
        code = text(body.get("code"), "detail.code")
        check_store_request(record, context, code)
        if code not in menu_data["meals"] or code in indexed:
            raise InputError("detail must be a unique root from the given menu")
        indexed[code] = record
        if body.get("rounds") == []:
            leaves[code] = record
    results = []
    for code, record in sorted(indexed.items()):
        row = {"product_id": code, "name": unwrap(record)["name"]}
        try:
            body = unwrap(record)
            child_ids = {c["code"] for group in body.get("rounds", []) for c in group.get("choices", [])}
            inputs = [leaves[k] for k in sorted(child_ids & leaves.keys()) if k != code] + [record]
            catalog = build_catalog(nutrition, menu, inputs, context)
            result = evaluate(catalog, [{"product_id": code}], context=context)
            row.update({"supported": True, "nutrition_complete": result["nutrition_complete"],
                        "default_kcal": result["total_kcal"], "gaps": result["gaps"]})
        except InputError:
            # An unsupported root must not erase measurements for other roots.
            row.update({"supported": False, "nutrition_complete": False,
                        "reason": "unsupported_or_ambiguous_detail"})
        results.append(row)
    simulated = any(record_origin(r) == "synthetic" for r in [nutrition, menu, *details])
    return {"data_origin": "synthetic" if simulated else "official_mcp", "test_only": simulated,
            "menu_root_count": len(menu_data["meals"]), "queried_root_count": len(indexed),
            "supported_root_count": sum(r["supported"] for r in results),
            "nutrition_complete_default_count": sum(r["nutrition_complete"] for r in results),
            "unqueried_root_count": len(menu_data["meals"]) - len(indexed),
            "scope": "queried_roots_default_composition_only", "menu_observed_at": menu.get("observed_at"),
            "nutrition_observed_at": nutrition.get("observed_at"), "roots": results}


def price_record(record, catalog, meal, context):
    if record_origin(record) == "synthetic" and catalog.get("data_origin") != "synthetic":
        raise InputError("simulated price cannot be used as an official quote")
    if record.get("request_arguments") != price_arguments(catalog, meal, context):
        raise InputError("quote request not bound to this meal and context")
    data = unwrap(record)
    if not isinstance(data, dict):
        raise InputError("price structure changed")
    total = integer(data.get("price"), "price.price")
    items = integer(data.get("productPrice"), "price.productPrice")
    returned = data.get("productList")
    if not isinstance(returned, list):
        raise InputError("price.productList required")
    expected = {}
    for line in canonical_meal(meal, catalog):
        key = line["product_id"]
        expected[key] = expected.get(key, 0) + line["quantity"]
    actual = {}
    subtotal = 0
    for line in returned:
        key = text(line.get("productCode"), "price.productCode")
        actual[key] = actual.get(key, 0) + integer(line.get("quantity"), "price.quantity", minimum=1)
        subtotal += integer(line.get("subtotal"), "price.subtotal")
    if expected != actual or subtotal != items:
        raise InputError("quote products or subtotals differ from requested meal")
    if context.get("dining_mode") != "pickup":
        raise InputError("official price adapter currently supports pickup only")
    # Observed pickup responses omit fee fields when price == productPrice.
    fees = [integer(data.get(key, 0), "price." + key) for key in ("deliveryPrice", "packingPrice", "tablewarePrice")]
    if total != items + sum(fees):
        raise InputError("price totals need investigation; do not infer")
    quoted_at = record.get("observed_at")
    timestamp(quoted_at, "price.observed_at")
    return {"fingerprint": fingerprint(meal, catalog, context), "source": "calculate-price",
            "data_origin": catalog["data_origin"], "total_cents": total, "items_cents": items,
            "includes_delivery": False, "quoted_at": quoted_at}
