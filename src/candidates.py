"""Bounded candidate construction from confirmed meals or explicit role pools."""

from copy import deepcopy
from itertools import islice, product
import json
from math import prod

from meal import (InputError, canonical_meal, evaluate, expand, integer,
                  validate_catalog, validate_requirements)


def build_candidates(catalog, selection, requirements=None, context=None, *, limit=256):
    products = validate_catalog(catalog)
    requirements = {} if requirements is None else requirements
    validate_requirements(requirements, products)
    integer(limit, "limit", minimum=1, maximum=256)
    if not isinstance(selection, dict):
        raise InputError("selection: object required")
    mode = selection.get("mode")
    required = {r["product_id"]: r.get("quantity", 1) for r in requirements.get("required", [])}

    def order(line):
        counts = expand([line], catalog)[1]
        # Required items are considered before preferences and stable identifiers.
        return (-sum(min(counts[k], q) for k, q in required.items()),
                -sum(bool(counts[k]) for k in requirements.get("preferred_ids", [])),
                json.dumps(line, sort_keys=True, ensure_ascii=False))

    if mode == "bundle":
        if set(selection) - {"mode", "meal", "index", "slot_ids"}:
            raise InputError("bundle selection: unsupported fields")
        base = canonical_meal(selection.get("meal"), catalog)
        index = integer(selection.get("index"), "selection.index", maximum=len(base) - 1)
        target = base[index]
        slots = products[target["product_id"]].get("slots", [])
        ids = selection.get("slot_ids")
        if (not isinstance(ids, list) or not ids or any(not isinstance(s, str) for s in ids)
                or len(ids) != len(set(ids)) or set(ids) - {s["id"] for s in slots}):
            raise InputError("slot_ids: explicit unique allowed slots required")
        active = sorted((s for s in slots if s["id"] in ids), key=lambda s: s["id"])
        domains = []
        for slot in active:
            def choice_order(key):
                line = {**target, "choices": {**target["choices"], slot["id"]: key}}
                rank = order(line)
                return (rank[0], rank[1], key != target["choices"][slot["id"]], key)
            domains.append(sorted(set(slot["options"]), key=choice_order))

        def make(values):
            meal = deepcopy(base)
            meal[index]["choices"].update({s["id"]: v for s, v in zip(active, values)})
            return meal
        scope = {"mode": mode, "product_id": target["product_id"], "slot_ids": [s["id"] for s in active]}
    elif mode == "roles":
        if set(selection) - {"mode", "pools"}:
            raise InputError("role selection: unsupported fields")
        pools = selection.get("pools")
        if not isinstance(pools, list) or not 1 <= len(pools) <= 3:
            raise InputError("pools: 1..3 confirmed role pools required")
        roles, domains, used = [], [], set()
        for pool in sorted(pools, key=lambda p: p.get("role", "") if isinstance(p, dict) else ""):
            if not isinstance(pool, dict) or set(pool) != {"role", "basis", "options"}:
                raise InputError("pool: role, basis and options required")
            role = pool["role"]
            if role not in ("main", "side", "drink") or role in roles:
                raise InputError("pool.role: unique main, side or drink required")
            if pool["basis"] not in ("official_category", "user_confirmed"):
                raise InputError("pool.basis: confirmed source required; do not infer roles from names")
            options = pool["options"]
            if not isinstance(options, list) or not 1 <= len(options) <= 30:
                raise InputError("pool.options: 1..30 lines required")
            lines, quantities = {}, set()
            for option in options:
                line = canonical_meal([option], catalog)[0]
                item = products[line["product_id"]]
                if item.get("slots") or item.get("components"):
                    raise InputError("role pools require standalone leaves; use bundle mode for meals")
                if line["product_id"] in used:
                    raise InputError("product assigned to multiple roles")
                quantities.add(line["quantity"])
                lines[line["product_id"]] = line
            if len(quantities) != 1:
                raise InputError("role quantities must be fixed, never shrink portions to fit")
            used.update(lines)
            roles.append(role)
            domains.append(sorted(lines.values(), key=order))
        if "main" not in roles:
            raise InputError("a confirmed main course is required")
        make = lambda values: list(values)
        scope = {"mode": mode, "roles": roles, "pool_sizes": [len(d) for d in domains]}
    else:
        raise InputError("selection.mode: bundle or roles required")

    total = prod(len(d) for d in domains)
    meals, rejected, seen = [], {}, set()
    examined = min(total, limit)
    for values in islice(product(*domains), limit):
        meal = canonical_meal(make(values), catalog)
        signature = json.dumps(meal, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        result = evaluate(catalog, meal, requirements, context)
        failed = [k for k in ("available", "required", "excluded") if result["checks"][k] is False]
        if failed:
            for key in failed:
                rejected[key] = rejected.get(key, 0) + 1
        else:
            # Preserve unknown nutrition/context and caloric conflicts for the evaluator.
            meals.append(meal)
    return {"candidates": meals, "data_origin": catalog["data_origin"],
            "test_only": catalog["data_origin"] == "synthetic",
            "scope": scope, "total_combinations": total, "examined_count": examined,
            "search_complete": total <= limit, "rejected_checks": rejected,
            "status": "ready" if meals else "no_candidates_in_checked_scope"}
