"""Timeline / round_summary helpers.

Both consultations-service (/consultations/sessions/{id}/timeline) and matter-service
(/matters/{id}/timeline) proxy ai-engine's round_summary traces into the same shape:

{
  thread_id, session_id, matter_id,
  rounds: [{content: {produced_output_keys, retrieval_traces, ...}, ...}],
  total
}
"""

from __future__ import annotations

from typing import Any, Iterable

from .utils import unwrap_api_response


def unwrap_timeline(resp: Any) -> dict[str, Any]:
    data = unwrap_api_response(resp)
    if isinstance(data, dict):
        return data
    raise AssertionError(f"unexpected timeline payload: {resp}")


def rounds(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    xs = timeline.get("rounds") if isinstance(timeline, dict) else None
    if isinstance(xs, list):
        return [it for it in xs if isinstance(it, dict)]
    return []


def round_contents(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rounds(timeline):
        c = r.get("content")
        if isinstance(c, dict) and c:
            out.append(c)
    return out


def produced_output_keys(timeline: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for c in round_contents(timeline):
        ks = c.get("produced_output_keys")
        if isinstance(ks, list):
            for k in ks:
                s = str(k or "").strip()
                if s:
                    out.add(s)
    return out


def retrieval_snippets(timeline: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for c in round_contents(timeline):
        traces = c.get("retrieval_traces")
        if not isinstance(traces, list):
            continue
        for t in traces:
            if not isinstance(t, dict):
                continue
            hits = t.get("hits")
            if not isinstance(hits, list):
                continue
            for h in hits:
                if not isinstance(h, dict):
                    continue
                snip = str(h.get("snippet") or "").strip()
                if snip:
                    out.append(snip)
    return out


def assert_timeline_has_output_keys(timeline: dict[str, Any], *, must_include: Iterable[str]) -> None:
    have = produced_output_keys(timeline)
    want = {str(x).strip() for x in must_include if str(x).strip()}
    missing = sorted(want - have)
    if missing:
        raise AssertionError(f"timeline missing produced_output_keys={missing}. Have={sorted(have)}")


def assert_timeline_retrieval_includes(timeline: dict[str, Any], *, snippet_contains: str) -> None:
    needle = str(snippet_contains or "").strip()
    if not needle:
        raise ValueError("snippet_contains is required")
    for snip in retrieval_snippets(timeline):
        if needle in snip:
            return
    raise AssertionError(f"timeline retrieval traces missing snippet fragment={needle!r}. Snippets sample={retrieval_snippets(timeline)[:5]}")

