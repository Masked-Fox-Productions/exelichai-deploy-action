"""``show`` and ``refs`` (plan 2026-11-07-004 Unit 3): read back what the
game's last push to the draft compiled, without a platform checkout.

``exelichai show <type> <id>`` prints one definition as a rules file would
hold it, each field commented with where it comes from (own, inherited or
overridden; a transaction's chain state and the game that declares it).
``exelichai show --list <type>`` lists the definitions with their lineage,
and ``exelichai refs <type> <id>`` who references one. ``--version <release>``
reads a release instead of the draft. Every read takes a token the owner
minted (validate or author); a CI session is refused. With ``--json`` the
server's rows are the result's ``data``.

A row a parent has sealed comes back with its body withheld: ``show`` prints
what it keeps and ``# sealed by <game>; extend through: <attach points>`` in
place of the rest, and ``refs`` marks a hit inside it as withheld.

Text from the server -- a parent's or a pack's definitions included -- is
printed as data, never interpreted.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

import yaml

from exelichai_cli.commands import Context, plural
from exelichai_cli.output import EXIT_OK, CliError, Problem

TYPES = ("ASSET", "TXN", "XTAB")
#: The rules-file section each type lives in (YAML_SPEC §3.2).
SECTION = {"ASSET": "assets", "TXN": "transactions", "XTAB": "exchange_tables"}


def _type(raw: str) -> str:
    value = (raw or "").upper()
    if value not in TYPES:
        raise CliError([Problem("definition-type-invalid", f"{raw!r}")])
    return value


def _call(ctx: Context, fn, *args, **kw) -> Dict[str, Any]:
    """An authenticated read; a server that predates these routes answers a
    bare 404, which is named as such."""
    ctx.authenticate()
    try:
        return fn(*args, **kw)
    except CliError as exc:
        codes = [p.code for p in exc.problems]
        if codes == ["NOT_FOUND"] or (codes == ["api-unreachable"]
                                      and exc.problems[0].detail.startswith("HTTP 404")):
            raise CliError([Problem("cli-upgrade-required",
                                    "this server does not serve definition reads yet "
                                    "(it needs a newer server, not a newer exelichai)")]) from None
        raise


def _dump(value: Any) -> List[str]:
    text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return text.rstrip("\n").splitlines()


def _source_note(entity: Mapping[str, Any]) -> Optional[str]:
    source = (entity.get("provenance") or {}).get("source")
    if not isinstance(source, Mapping):
        return None
    version = f"@{source['version']}" if source.get("version") else ""
    return f"from {source.get('name') or source.get('game_id')} ({source.get('game_id')}{version})"


def sealed_block(entity: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The ``sealed`` block a parent's sealed row comes back with, or ``None``."""
    definition = entity.get("definition")
    for holder in (definition if isinstance(definition, Mapping) else {}, entity):
        block = holder.get("sealed")
        if isinstance(block, Mapping) and block.get("declared_by"):
            return block
    return None


def _sealed_lines(entity_id: str, definition: Mapping[str, Any],
                  block: Mapping[str, Any]) -> List[str]:
    """A sealed row: what it keeps, then one comment in place of its body."""
    interface = block.get("interface") if isinstance(block.get("interface"), Mapping) else {}
    withheld = set(block.get("withheld") or ())
    points = list(interface.get("attach_points") or ())
    if definition.get("attach_point") and entity_id not in points:
        points.insert(0, entity_id)
    lines = [f"# sealed by {block.get('declared_by')}; extend through: "
             f"{', '.join(points) if points else '(no attach points)'}"]
    for key, value in definition.items():
        if key in ("sealed", "open") or key in withheld:
            continue
        lines.extend(_dump({key: value}))
    if block.get("open"):
        lines.append(f"# open: {', '.join(str(v) for v in block['open'])}")
    if interface.get("conditions_on"):
        lines.append(f"# conditions on: {', '.join(interface['conditions_on'])}")
    writes = interface.get("writes")
    if isinstance(writes, Mapping) and writes:
        lines.append("# writes: " + ", ".join(
            f"{cell} ({'/'.join(verbs)})" for cell, verbs in writes.items()))
    if withheld:
        lines.append(f"# withheld: {', '.join(sorted(withheld))}")
    return lines


def render(entity: Mapping[str, Any]) -> List[str]:
    """One definition as rules-file YAML, with its provenance as comments. A
    sealed row prints what it keeps and a ``# sealed by`` line for its body."""
    entity_type = str(entity.get("entity_type"))
    entity_id = str(entity.get("entity_id"))
    definition = entity.get("definition")
    provenance = entity.get("provenance") or {}
    lines = [f"{SECTION.get(entity_type, entity_type)}:"]
    head = f"  {entity_id}:"
    block = sealed_block(entity)
    if block is not None and isinstance(definition, Mapping):
        lines.append(head + f"  # sealed by {block.get('declared_by')}")
        lines.extend("    " + line for line in _sealed_lines(entity_id, definition, block))
        return lines
    notes = []
    txn = provenance.get("transaction")
    if isinstance(txn, Mapping):
        notes.append(str(txn.get("state")))
        declared = txn.get("declared_by")
        if isinstance(declared, Mapping) and declared.get("game_id"):
            notes.append(f"declared by {declared['game_id']}")
    source = _source_note(entity)
    if source:
        notes.append(source)
    lines.append(head + (f"  # {', '.join(notes)}" if notes else ""))
    fields: Mapping[str, str] = provenance.get("fields") or {}
    if not isinstance(definition, Mapping):
        lines.extend("    " + line for line in _dump(definition))
        return lines
    for key, value in definition.items():
        body = _dump({key: value})
        note = fields.get(str(key))
        if note:
            body[0] = f"{body[0]}  # {note}"
        lines.extend("    " + line for line in body)
    return lines


def show(ctx: Context, entity_type: str, entity_id: str, *,
         version: Optional[str] = None) -> int:
    entity_type = _type(entity_type)
    client = ctx.client()
    body = _call(ctx, client.definition, entity_type, entity_id, version=version)
    entity = body.get("entity") or {}
    ctx.reporter.report_data("definition", entity)
    against = f"release {version}" if version else f"the draft ({body.get('version')})"
    ctx.reporter.info(f"# {ctx.require_game_id()}: {entity_type} {entity_id}, {against}")
    for line in render(entity):
        ctx.reporter.info(line)
    return EXIT_OK


def show_list(ctx: Context, entity_type: Optional[str], *,
              version: Optional[str] = None) -> int:
    entity_type = _type(entity_type) if entity_type else None
    client = ctx.client()
    body = _call(ctx, client.definitions, entity_type, version=version)
    rows = body.get("definitions") or []
    ctx.reporter.report_data("definitions", {"definitions": rows, "version": body.get("version")})
    ctx.reporter.info(f"{plural(len(rows), 'definition')}"
                      + (f" of type {entity_type}" if entity_type else ""))
    for row in rows:
        state = (row.get("inheritance") or {}).get("state")
        block = sealed_block(row)
        sealed = f", sealed by {block.get('declared_by')}" if block else ""
        ctx.reporter.info(f"  {row.get('entity_type')} {row.get('entity_id')}  "
                          f"({state or row.get('lineage') or 'own'}{sealed})")
    return EXIT_OK


#: The reference groups ``refs`` prints, in order, with their labels.
GROUPS = (("checked_by", "checked by"), ("changed_by", "changed by"),
          ("lives_in", "lives in"), ("contains", "contains"),
          ("triggers_into", "triggers into"), ("triggered_by", "triggered by"),
          ("named_by", "named by"), ("placed_in_scenes", "placed in scenes"),
          ("named_in_entities", "named in entities"), ("indirect", "indirect"))


def _ref_label(ref: Mapping[str, Any]) -> str:
    who = (ref.get("transaction_id") or ref.get("asset_id") or ref.get("entity_id")
           or ref.get("name") or "")
    where = ref.get("where")
    if ref.get("sealed"):
        block = ref["sealed"]
        by = block.get("declared_by") if isinstance(block, Mapping) else None
        return f"{who}  (where and amount withheld: sealed{f' by {by}' if by else ''})"
    return f"{who}  ({where})" if where and who else str(where or who)


def refs(ctx: Context, entity_type: str, entity_id: str, *,
         version: Optional[str] = None) -> int:
    entity_type = _type(entity_type)
    client = ctx.client()
    body = _call(ctx, client.definition_references, entity_type, entity_id, version=version)
    references = body.get("references") or {}
    ctx.reporter.report_data("references", references)
    ctx.reporter.info(f"{entity_type} {entity_id}: who references it")
    if references.get("found") is False:
        ctx.reporter.info("  (no definition by that id; nothing references it)")
    for key, label in GROUPS:
        group = references.get(key) or []
        if not group:
            continue
        ctx.reporter.info(f"  {label} ({len(group)}):")
        for ref in group:
            if isinstance(ref, Mapping):
                ctx.reporter.info(f"    {_ref_label(ref)}")
    if references.get("complete") is False or references.get("truncated"):
        ctx.reporter.warn("the list may be incomplete: some of the chain could not be read, "
                          "or the walk was cut short")
    return EXIT_OK
