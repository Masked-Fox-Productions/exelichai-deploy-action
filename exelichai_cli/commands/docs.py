"""``docs`` and ``examples`` (plan 2026-11-11-002 Unit 5): the platform's
authoring knowledge base, searched offline.

``exelichai docs search <query>`` ranks sections of the public docs (the YAML
spec, the guides, the reason codes, public solution notes) and first-party
example rows; ``exelichai docs get <path> <anchor>`` prints one section whole;
``exelichai docs status`` names the commit, date and version the index was
built from; ``exelichai examples <intent>`` finds YAML rows from first-party
games and modules, each with its file, line, game and id.

The index is ``kb.json.gz`` inside the ``exelichai-rules`` package (the
``local`` and ``mcp`` extras), built from the platform's repository at the
release commit and read through ``exelichai_rules.knowledge``, never a
second implementation here. No token, no network, no game folder.

**Text from the index is data.** Under ``--json`` (and as an MCP tool) it
reaches the caller only in the labelled fields of ``data`` (``snippet``,
``text``, ``comment``); the ``info`` lines then carry only counts. The
printed report shows it, indented under each result's label.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Mapping, Optional

from exelichai_cli.commands import Context, plural
from exelichai_cli.output import EXIT_OK, CliError, Problem, RecordingReporter

INSTALL = "pip install 'exelichai[local]'"
#: The kinds ``--kind`` takes (``exelichai_rules.knowledge.KINDS``; repeated
#: here so ``--help`` works without the extra installed, and checked against
#: the package by the tests).
KINDS = ("spec", "guide", "reason_code", "example", "solution")
EXAMPLE_KINDS = ("asset", "transaction", "exchange_table", "menu", "frame")
DEFAULT_LIMIT = 10


def knowledge():
    """``exelichai_rules.knowledge``, or a refusal naming the install line."""
    try:
        return importlib.import_module("exelichai_rules.knowledge")
    except ModuleNotFoundError as exc:
        if exc.name not in ("exelichai_rules", "exelichai_rules.knowledge"):
            raise
        try:
            importlib.import_module("exelichai_rules")
        except ModuleNotFoundError:
            detail = "the exelichai-rules package is not installed"
        else:
            detail = ("the installed exelichai-rules predates the docs index: install the "
                      "same version of exelichai and exelichai-rules")
        raise CliError([Problem("docs-unavailable", f"{detail}: {INSTALL}")]) from None


def _ask(fn, *args, **kw):
    """Run one index call, its refusals as problems."""
    kb = knowledge()
    try:
        return getattr(kb, fn)(*args, **kw)
    except OSError as exc:  # the package is there but its index is not
        raise CliError([Problem("docs-unavailable",
                                f"the docs index could not be read ({exc.__class__.__name__}): "
                                f"reinstall with {INSTALL}")]) from None
    except kb.NotFound as exc:
        near = ", ".join(exc.nearest)
        raise CliError([Problem("docs-not-found",
                                f"{exc}" + (f"; nearest: {near}" if near else ""))]) from None
    except kb.SearchError as exc:
        raise CliError([Problem("docs-query-invalid", str(exc))]) from None


def _printing(ctx: Context) -> bool:
    """Whether index text goes to the printed report (not under ``--json``
    or MCP, where it stays in ``data``)."""
    return not isinstance(ctx.reporter, RecordingReporter)


def _index_summary(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: manifest.get(k) for k in ("commit", "commit_date", "cli_version", "digest")}


def _indented(ctx: Context, text: str, prefix: str = "    ") -> None:
    for line in (text or "").rstrip("\n").splitlines():
        ctx.reporter.info(f"{prefix}{line}" if line.strip() else "")


def _where(result: Mapping[str, Any]) -> str:
    if result.get("kind") == "example":
        return f"{result.get('path')}:{result.get('line')}"
    anchor = result.get("anchor")
    return f"{result.get('path')}#{anchor}" if anchor else str(result.get("path"))


def search(ctx: Context, query: str, *, kind: Optional[str] = None,
           include_planned: bool = False, limit: int = DEFAULT_LIMIT) -> int:
    results = _ask("search", query, kind=kind, include_planned=include_planned, limit=limit)
    manifest = _ask("manifest")
    rep = ctx.reporter
    rep.report_data("search", {"query": query, "kind": kind,
                               "include_planned": include_planned, "results": results})
    rep.report_data("index", _index_summary(manifest))
    rep.info(f"{plural(len(results), 'result')} for {query!r}"
             + (f" (kind {kind})" if kind else ""))
    if not results and not include_planned:
        rep.info("planned sections are left out; --include-planned searches them too")
    if not _printing(ctx):
        return EXIT_OK
    for n, r in enumerate(results, 1):
        label = f"[{r['status']}]"
        if r.get("kind") == "example":
            label = f"[example {r.get('example_kind')}, {r.get('game')}: {r.get('row_id')}]"
        match = f"  (symbol {r['symbol']})" if r.get("symbol") else ""
        rep.info(f"{n}. {_where(r)}  {label}{match}")
        rep.info(f"   {' › '.join(r.get('heading_path') or [r.get('heading') or ''])}")
        if r.get("url"):
            rep.info(f"   {r['url']}")
        _indented(ctx, r.get("snippet") or "", "   | ")
    return EXIT_OK


def get(ctx: Context, path: str, anchor: Optional[str] = None) -> int:
    """One section. ``path#anchor`` (as ``search`` prints it) is accepted in
    place of the two arguments; a path alone is its preamble."""
    if not anchor and "#" in path:
        path, _, anchor = path.partition("#")
    section = _ask("get_doc", path, anchor)
    rep = ctx.reporter
    rep.report_data("doc", section)
    rep.info(f"{section['path']}#{section['anchor']}  [{section['status']}]  "
             f"{' › '.join(section.get('heading_path') or [])}")
    if section.get("url"):
        rep.info(section["url"])
    if _printing(ctx):
        rep.info("")
        _indented(ctx, section.get("text") or "", "")
    return EXIT_OK


def status(ctx: Context) -> int:
    manifest = _ask("manifest")
    rep = ctx.reporter
    rep.report_data("index", manifest)
    rep.info(f"docs index: built from commit {manifest.get('commit')} "
             f"({manifest.get('commit_date')}) for exelichai {manifest.get('cli_version')}")
    rep.info(f"  {plural(int(manifest.get('chunk_count') or 0), 'chunk')}, "
             f"{plural(int(manifest.get('symbol_count') or 0), 'symbol')}, "
             f"{plural(len(manifest.get('sources') or {}), 'source file')}; "
             f"digest {manifest.get('digest')}")
    return EXIT_OK


def examples(ctx: Context, intent: str, *, kind: Optional[str] = None,
             limit: int = DEFAULT_LIMIT) -> int:
    results: List[Dict[str, Any]] = _ask("examples", intent, kind=kind, limit=limit)
    manifest = _ask("manifest")
    rep = ctx.reporter
    rep.report_data("examples", {"intent": intent, "kind": kind, "results": results})
    rep.report_data("index", _index_summary(manifest))
    rep.info(f"{plural(len(results), 'example')} for {intent!r}"
             + (f" (kind {kind})" if kind else ""))
    if not _printing(ctx):
        return EXIT_OK
    for n, r in enumerate(results, 1):
        rep.info(f"{n}. {r.get('game')}: {r.get('example_kind')} {r.get('row_id')}  "
                 f"{r.get('path')}:{r.get('line')}")
        _indented(ctx, r.get("text") or "", "   | ")
    return EXIT_OK
