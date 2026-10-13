"""Deterministic meal calculations. No network, credentials or account writes."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
from itertools import islice, product
import json


class InputError(ValueError):
    """An invalid normalized request, not a business no-solution result."""


def number(value, field, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise InputError(f"{field}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise InputError(f"{field}: expected a finite number") from None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise InputError(f"{field}: expected a {'positive' if positive else 'nonnegative'} finite number")
    return result


def integer(value, field, *, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise InputError(f"{field}: invalid integer")
    return value


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{field}: expected nonempty text")
    return value


def timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(text(value, field).replace("Z", "+00:00"))
    except ValueError:
        raise InputError(f"{field}: expected ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        raise InputError(f"{field}: timezone required")
    return parsed


def decimal_text(value):
    return format(value, "f")


def display_kcal(value):
    return decimal_text(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def validate_catalog(data):
    if not isinstance(data, dict) or data.get("data_origin") not in ("synthetic", "official_mcp"):
        raise InputError("catalog.data_origin: synthetic or official_mcp required")
    if not isinstance(data.get("products"), list) or not data["products"]:
        raise InputError("catalog.products: nonempty list required")
    indexed = {}
    for item in data["products"]:
        if not isinstance(item, dict):
            raise InputError("catalog product: expected object")
        key = text(item.get("id"), "product.id")
        if key in indexed:
            raise InputError("duplicate product id")
        text(item.get("name"), "product.name")
        text(item.get("spec"), "product.spec")
        if type(item.get("available")) is not bool:
            raise InputError("product.available: boolean required")
        if type(item.get("mapping_verified")) is not bool:
            raise InputError("product.mapping_verified: boolean required")
        indexed[key] = item
    return indexed


def canonical_meal(meal, catalog):
    if not isinstance(meal, list) or not meal or len(meal) > 20:
        raise InputError("meal: 1..20 lines required")
    products = validate_catalog(catalog)
    merged = {}
    for line in meal:
        if not isinstance(line, dict) or set(line) - {"product_id", "quantity", "choices"}:
            raise InputError("meal line: unsupported fields")
        key = line.get("product_id")
        if key not in products:
            raise InputError("meal line: unknown product")
        if products[key].get("standalone") is False:
            raise InputError("meal line: bundle-only component cannot be ordered standalone")
        quantity = integer(line.get("quantity", 1), "quantity", minimum=1, maximum=20)
        slots = products[key].get("slots", [])
        if not isinstance(slots, list):
            raise InputError("slots: expected list")
        supplied = line.get("choices", {})
        if not isinstance(supplied, dict):
            raise InputError("choices: expected object")
        if len({s.get("id") for s in slots}) != len(slots):
            raise InputError("duplicate slot id")
        if set(supplied) - {s.get("id") for s in slots}:
            raise InputError("choices: unknown slot")
        choices = {}
        for slot in slots:
            slot_id = text(slot.get("id"), "slot.id")
            options = slot.get("options")
            if not isinstance(options, list) or not options or any(p not in products for p in options):
                raise InputError("slot.options: valid product ids required")
            choice = supplied.get(slot_id, slot.get("default"))
            if choice not in options:
                raise InputError("choices: substitution not allowed")
            choices[slot_id] = choice
        ident = (key, json.dumps(choices, sort_keys=True, ensure_ascii=False))
        total_quantity = merged.get(ident, {}).get("quantity", 0) + quantity
        integer(total_quantity, "merged quantity", minimum=1, maximum=20)
        merged[ident] = {"product_id": key, "quantity": total_quantity, "choices": choices}
    return [merged[k] for k in sorted(merged)]


def fingerprint(meal, catalog, context):
    if not isinstance(context, dict) or set(context) - {"store_id", "dining_mode", "coupon_ids", "coupon_bindings", "menu_observed_at"}:
        raise InputError("context: unsupported fields")
    coupon_ids = context.get("coupon_ids", [])
    if not isinstance(coupon_ids, list) or any(not isinstance(c, str) for c in coupon_ids):
        raise InputError("context.coupon_ids: text list required")
    bindings = context.get("coupon_bindings", {})
    if not isinstance(bindings, dict):
        raise InputError("context.coupon_bindings: object required")
    for binding in bindings.values():
        if not isinstance(binding, dict) or set(binding) != {"couponId", "couponCode"}:
            raise InputError("coupon binding requires couponId and couponCode")
        text(binding["couponId"], "couponId")
        text(binding["couponCode"], "couponCode")
    if {b["couponId"] for b in bindings.values()} != set(coupon_ids) and bindings:
        raise InputError("coupon ids and bindings differ")
    bound_context = {**context, "coupon_ids": sorted(set(coupon_ids))}
    payload = {"meal": canonical_meal(meal, catalog), "context": bound_context}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validate_requirements(requirements, products):
    if not isinstance(requirements, dict):
        raise InputError("requirements: object required")
    allowed = {"min_kcal", "max_kcal", "budget_cents", "price_scope", "required", "excluded_ids", "preferred_ids"}
    if set(requirements) - allowed:
        raise InputError("requirements: unsupported fields")
    for key in ("min_kcal", "max_kcal"):
        if requirements.get(key) is not None:
            number(requirements[key], key)
    if requirements.get("min_kcal") is not None and requirements.get("max_kcal") is not None:
        if number(requirements["min_kcal"], "min_kcal") > number(requirements["max_kcal"], "max_kcal"):
            raise InputError("min_kcal exceeds max_kcal")
    if requirements.get("budget_cents") is not None:
        integer(requirements["budget_cents"], "budget_cents")
        if requirements.get("price_scope") not in ("items", "total"):
            raise InputError("budget requires price_scope items or total")
    for key in ("excluded_ids", "preferred_ids"):
        ids = requirements.get(key, [])
        if not isinstance(ids, list) or any(i not in products for i in ids):
            raise InputError(f"{key}: known product ids required")
    required = requirements.get("required", [])
    if not isinstance(required, list):
        raise InputError("required: list required")
    seen = set()
    for entry in required:
        if not isinstance(entry, dict) or entry.get("product_id") not in products:
            raise InputError("required: known product id required")
        if entry["product_id"] in seen:
            raise InputError("required: duplicate product")
        seen.add(entry["product_id"])
        integer(entry.get("quantity", 1), "required.quantity", minimum=1, maximum=20)


def nutrition_for(item):
    record = item.get("nutrition")
    if not item["mapping_verified"]:
        return None, "mapping_unverified"
    if not isinstance(record, dict) or record.get("energy") is None:
        return None, "nutrition_missing"
    if record.get("spec") != item["spec"]:
        return None, "nutrition_spec_mismatch"
    source = record.get("source")
    if not isinstance(source, dict) or not source.get("record_id") or source.get("tool") != "list-nutrition-foods":
        return None, "nutrition_source_missing"
    timestamp(source.get("observed_at"), "nutrition.source.observed_at")
    value = number(record["energy"], "nutrition.energy")
    if record.get("unit") == "kJ":
        value /= Decimal("4.184")
    elif record.get("unit") != "kcal":
        raise InputError("nutrition.unit: kcal or kJ required")
    if record.get("basis") == "per_100g":
        if item.get("weight_g") is None:
            return None, "weight_missing"
        value *= number(item["weight_g"], "weight_g", positive=True) / 100
    elif record.get("basis") != "serving":
        raise InputError("nutrition.basis: serving or per_100g required")
    return value, None


def expand(meal, catalog):
    products = validate_catalog(catalog)
    counts = Counter()
    leaf_counts = Counter()
    gaps = []

    def walk(key, quantity, choices, ancestors):
        if key not in products:
            raise InputError("component: unknown product")
        if key in ancestors or len(ancestors) >= 10:
            raise InputError("cyclic or too deep bundle")
        item = products[key]
        counts[key] += quantity
        if not item["available"]:
            gaps.append({"product_id": key, "reason": "unavailable"})
        components = item.get("components", [])
        slots = item.get("slots", [])
        if not isinstance(components, list):
            raise InputError("components: list required")
        if components or slots:
            for component in components:
                if not isinstance(component, dict):
                    raise InputError("component: object required")
                q = integer(component.get("quantity", 1), "component.quantity", minimum=1, maximum=20)
                child = component.get("product_id")
                if child not in products or products[child].get("slots"):
                    raise InputError("fixed component must be resolved (no nested choice slots)")
                walk(child, quantity * q, {}, (*ancestors, key))
            for slot in slots:
                child = choices[slot["id"]]
                if products[child].get("slots"):
                    raise InputError("nested choice slots unsupported")
                q = integer(slot.get("quantity", 1), "slot.quantity", minimum=1, maximum=20)
                walk(child, quantity * q, {}, (*ancestors, key))
        else:
            leaf_counts[key] += quantity

    canonical = canonical_meal(meal, catalog)
    for line in canonical:
        walk(line["product_id"], line["quantity"], line["choices"], ())
    return canonical, counts, leaf_counts, gaps


def validate_quote(quote, expected_fingerprint, catalog, context, now):
    if quote is None:
        return {"status": "unknown", "reason": "not_quoted"}
    if not isinstance(quote, dict):
        raise InputError("quote: object required")
    if quote.get("fingerprint") != expected_fingerprint:
        return {"status": "unknown", "reason": "quote_context_changed"}
    if quote.get("source") != "calculate-price" or quote.get("data_origin") != catalog["data_origin"]:
        return {"status": "unknown", "reason": "quote_source_unverified"}
    if not context.get("store_id") or not context.get("dining_mode") or not context.get("menu_observed_at"):
        return {"status": "unknown", "reason": "price_context_missing"}
    observed = timestamp(quote.get("quoted_at"), "quote.quoted_at")
    age = (now - observed).total_seconds()
    if age < 0 or age > 300:
        return {"status": "unknown", "reason": "quote_expired"}
    total = integer(quote.get("total_cents"), "quote.total_cents")
    items = integer(quote.get("items_cents"), "quote.items_cents")
    if items > total:
        raise InputError("quote.items_cents exceeds total_cents")
    if type(quote.get("includes_delivery")) is not bool:
        raise InputError("quote.includes_delivery: boolean required")
    if context.get("dining_mode") == "delivery" and not quote["includes_delivery"]:
        return {"status": "unknown", "reason": "delivery_fee_unverified"}
    return {"status": "verified", "total_cents": total, "items_cents": items,
            "includes_delivery": quote["includes_delivery"], "quoted_at": quote["quoted_at"],
            "data_origin": quote["data_origin"]}


def evaluate(catalog, meal, requirements=None, context=None, quote=None, *, now=None):
    requirements = {} if requirements is None else requirements
    context = {} if context is None else context
    now = now or datetime.now(timezone.utc)
    products = validate_catalog(catalog)
    validate_requirements(requirements, products)
    canonical, counts, leaf_counts, gaps = expand(meal, catalog)
    menu_context = catalog.get("menu_context")
    context_valid = catalog.get("availability_scope") != "nutrition_only" and (menu_context is None or all(
        menu_context.get(key) == context.get(key) for key in ("store_id", "dining_mode", "menu_observed_at")))
    if not context_valid:
        gaps.append({"product_id": None, "reason": "menu_context_unverified"})
    lines = []
    total = Decimal(0)
    for key in sorted(leaf_counts):
        item = products[key]
        per_serving, missing = nutrition_for(item)
        q = leaf_counts[key]
        subtotal = None if per_serving is None else per_serving * q
        if missing:
            gaps.append({"product_id": key, "reason": missing})
        if subtotal is not None:
            total += subtotal
        lines.append({"product_id": key, "name": item["name"], "spec": item["spec"],
                      "quantity": q, "kcal": None if subtotal is None else display_kcal(subtotal),
                      "nutrition_source": item.get("nutrition", {}).get("source") if isinstance(item.get("nutrition"), dict) else None})
    nutrition_gaps = [g for g in gaps if g["reason"] not in ("unavailable", "menu_context_unverified")]
    complete = not nutrition_gaps
    fp = fingerprint(canonical, catalog, context)
    price = validate_quote(quote, fp, catalog, context, now)
    checks = {"available": not any(g["reason"] == "unavailable" for g in gaps),
              "required": all(counts[r["product_id"]] >= r.get("quantity", 1) for r in requirements.get("required", [])),
              "excluded": not any(counts[i] for i in requirements.get("excluded_ids", []))}
    if checks["available"] and not context_valid:
        checks["available"] = "unknown"
    for name, operator in (("min_kcal", lambda a, b: a >= b), ("max_kcal", lambda a, b: a <= b)):
        target = requirements.get(name)
        checks[name] = "not_requested" if target is None else (operator(total, number(target, name)) if complete else "unknown")
    budget = requirements.get("budget_cents")
    checks["budget"] = "not_requested"
    if budget is not None:
        checks["budget"] = "unknown" if price["status"] != "verified" else price[requirements["price_scope"] + "_cents"] <= budget
    satisfied = not any(v is False for v in checks.values()) and not any(v == "unknown" for v in checks.values())
    return {"data_origin": catalog["data_origin"], "test_only": catalog["data_origin"] == "synthetic",
            "meal": canonical, "fingerprint": fp, "lines": lines, "counts": dict(sorted(counts.items())),
            "nutrition_complete": complete, "total_kcal": display_kcal(total) if complete else None,
            "known_subtotal_kcal": display_kcal(total), "gaps": gaps, "checks": checks,
            "satisfied": satisfied, "price": price}


def candidate_rank(result, requirements, current=None):
    counts = Counter(result["counts"])
    preferred = sum(bool(counts[key]) for key in set(requirements.get("preferred_ids", [])))
    changes = sum((counts - current).values()) + sum((current - counts).values()) if current is not None else 0
    price = result["price"]
    amount = price.get(requirements.get("price_scope", "total") + "_cents", float("inf"))
    return (-preferred, changes, amount, result["fingerprint"])


def exact_kcal(catalog, meal):
    products = validate_catalog(catalog)
    return sum((nutrition_for(products[key])[0] * quantity
                for key, quantity in expand(meal, catalog)[2].items()), Decimal(0))


def recommend(catalog, groups=None, requirements=None, context=None, quotes=None, current_meal=None,
              *, candidates=None, candidate_batch=None, max_candidates=256, max_quotes=8, now=None):
    requirements = {} if requirements is None else requirements
    context = {} if context is None else context
    quotes = [] if quotes is None else quotes
    products = validate_catalog(catalog)
    validate_requirements(requirements, products)
    integer(max_candidates, "max_candidates", minimum=1, maximum=256)
    integer(max_quotes, "max_quotes", minimum=1, maximum=8)
    generation = None
    if candidate_batch is not None:
        if (groups is not None or candidates is not None or not isinstance(candidate_batch, dict)
                or candidate_batch.get("data_origin") != catalog["data_origin"]
                or type(candidate_batch.get("search_complete")) is not bool
                or not isinstance(candidate_batch.get("scope"), dict)):
            raise InputError("candidate_batch: pass unchanged candidates output with matching origin")
        candidates = candidate_batch.get("candidates")
        if not isinstance(candidates, list):
            raise InputError("candidate_batch.candidates: list required")
        generation = {k: deepcopy(candidate_batch[k]) for k in
                      ("scope", "search_complete", "examined_count", "total_combinations", "rejected_checks")}
    if candidates is not None:
        if groups is not None or not isinstance(candidates, list) or not 0 <= len(candidates) <= 256:
            raise InputError("candidates: at most 256 meals, mutually exclusive with groups")
        for candidate in candidates:
            canonical_meal(candidate, catalog)
        combinations = candidates[:max_candidates + 1]
        scope = "provided_candidates_only"
    else:
        if not isinstance(groups, list) or not 1 <= len(groups) <= 6:
            raise InputError("groups: 1..6 groups required")
        for group in groups:
            if not isinstance(group, list) or not 1 <= len(group) <= 30:
                raise InputError("group: 1..30 alternative lines required")
            for option in group:
                canonical_meal([option], catalog)
        combinations = list(islice(product(*groups), max_candidates + 1))
        scope = "provided_groups_only"
    if not isinstance(quotes, list) or any(not isinstance(q, dict) for q in quotes):
        raise InputError("quotes: list of objects required")
    quote_map = {q.get("fingerprint"): q for q in quotes}
    if len(quote_map) != len(quotes):
        raise InputError("duplicate quote fingerprint")
    current = Counter(expand(current_meal, catalog)[1]) if current_meal else None
    evaluated, seen = [], set()
    search_complete = len(combinations) <= max_candidates
    if generation is not None:
        search_complete = search_complete and generation["search_complete"]
    for combination in combinations[:max_candidates]:
        fp = fingerprint(list(combination), catalog, context)
        if fp in seen:
            continue
        seen.add(fp)
        evaluated.append(evaluate(catalog, list(combination), requirements, context, quote_map.get(fp), now=now))
    evaluated.sort(key=lambda r: candidate_rank(r, requirements, current))
    eligible = [r for r in evaluated if r["satisfied"] and r["nutrition_complete"]]
    unknown = [r for r in evaluated if not any(v is False for v in r["checks"].values())
               and (not r["nutrition_complete"] or any(v == "unknown" for v in r["checks"].values()))]
    pending = [r for r in evaluated if r["nutrition_complete"] and r["price"]["status"] != "verified"
               and not any(v is False for v in r["checks"].values())
               and not any(v == "unknown" for k, v in r["checks"].items() if k != "budget")]
    if eligible:
        status = "recommended"
    elif pending:
        status = "needs_prices"
    elif unknown:
        status = "insufficient_data"
    elif not search_complete:
        status = "no_match_in_checked_candidates"
    else:
        status = "no_match_in_provided_candidates" if candidates is not None else "no_match_in_provided_groups"
    failure_counts = Counter(k for r in evaluated for k, v in r["checks"].items() if v is False)
    comparison = None
    selected = eligible[:1]
    if eligible:
        first = eligible[0]
        alternatives = [r for r in eligible[1:] if r["counts"] != first["counts"]]
        # Preserve primary preference/price ranking; choose a meaningful calorie
        # comparison when possible, without imposing an unrequested calorie limit.
        first_kcal = exact_kcal(catalog, first["meal"])
        different = [r for r in alternatives if exact_kcal(catalog, r["meal"]) != first_kcal]
        if alternatives:
            second = (different or alternatives)[0]
            selected.append(second)
            comparison = {"delta_kcal": display_kcal(exact_kcal(catalog, second["meal"]) - first_kcal),
                          "delta_cents": (second["price"]["total_cents"] - first["price"]["total_cents"])
                          if all(r["price"]["status"] == "verified" for r in (first, second)) else None}
    return {"status": status, "data_origin": catalog["data_origin"], "test_only": catalog["data_origin"] == "synthetic",
            "search_complete": search_complete, "search_scope": scope,
            "generation": generation, "comparison": comparison,
            "evaluated_count": len(evaluated), "unknown_count": len(unknown),
            "price_complete": not pending, "price_request_limit": max_quotes,
            "recommendations": selected, "price_requests": [{"meal": r["meal"], "fingerprint": r["fingerprint"]} for r in pending[:max_quotes]],
            "failed_checks": dict(sorted(failure_counts.items())),
            "unverified_gaps": [g for r in unknown[:2] for g in r["gaps"]]}


def revise(catalog, meal, edits, requirements=None, context=None, quote=None, *, now=None):
    if not isinstance(edits, list) or not edits:
        raise InputError("edits: nonempty list required")
    changed = deepcopy(canonical_meal(meal, catalog))
    for edit in edits:
        if not isinstance(edit, dict):
            raise InputError("edit: object required")
        operation = edit.get("op")
        if operation == "add":
            changed.append(edit.get("line"))
        elif operation in ("replace", "remove"):
            index = integer(edit.get("index"), "edit.index", maximum=len(changed) - 1)
            if operation == "replace":
                changed[index] = edit.get("line")
            else:
                changed.pop(index)
        else:
            raise InputError("edit.op: add, replace or remove required")
    before = evaluate(catalog, meal, requirements, context, quote, now=now)
    after = evaluate(catalog, changed, requirements, context, quote, now=now)
    delta = None
    if before["nutrition_complete"] and after["nutrition_complete"]:
        # Displayed totals are only for presentation. Calculate the exact difference.
        delta = display_kcal(exact_kcal(catalog, after["meal"]) - exact_kcal(catalog, before["meal"]))
    return {"before": before, "after": after, "delta_kcal": delta,
            "price_invalidated": before["fingerprint"] != after["fingerprint"],
            "requirements": deepcopy(requirements or {})}
