"""Read-only history selection, rendering and compaction planning over Run facts."""

import json
from dataclasses import dataclass

from .contracts import ToolOutcome

HISTORY_OMITTED = "- older events omitted by History budget"
CONTEXT_KINDS = frozenset(
    {
        "user_message",
        "user_guidance",
        "assistant_tool_calls",
        "tool_result",
        "model_instruction",
        "assistant_final",
        "compaction",
    }
)


@dataclass(frozen=True)
class _ProjectedFact:
    kind: str
    payload: dict
    source_event_ids: tuple[str, ...]
    artifact_id: str = ""


class RunHistory:
    def __init__(self, events, *, projected_instruction_id):
        self._events = tuple(events)
        self._projected_instruction_id = projected_instruction_id

    def latest_user_guidance(self):
        entry = next(
            (
                candidate
                for candidate in reversed(self._events)
                if candidate.kind == "user_guidance"
            ),
            None,
        )
        return entry.content if entry is not None else ""

    def context_events(self):
        call_ids = {
            call.call_id
            for entry in self._events
            if entry.kind == "assistant_tool_calls"
            for call in entry.tool_calls
        }
        return tuple(
            entry
            for entry in self._events
            if entry.kind in CONTEXT_KINDS
            and (entry.kind != "tool_result" or entry.call_id in call_ids)
        )

    def active_events(self):
        active = []
        for entry in self.context_events():
            if entry.kind != "compaction":
                active.append(entry)
                continue
            covered = entry.covered_event_ids
            prefix = tuple(item.event_id for item in active[: len(covered)])
            if not covered or prefix != covered:
                raise ValueError(
                    "compaction coverage must match the active logical prefix"
                )
            active = [entry, *active[len(covered) :]]
        return tuple(active)

    @staticmethod
    def _history_units(events, *, allow_incomplete=False):
        """Return durable response units used only for compaction coverage."""

        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind != "assistant_tool_calls":
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan tool result")
                units.append((entry,))
                index += 1
                continue
            expected_ids = tuple(call.call_id for call in entry.tool_calls)
            end = index + 1 + len(expected_ids)
            if end > len(events):
                if allow_incomplete:
                    return None
                raise RuntimeError("Run Log contains an incomplete tool transaction")
            results = events[index + 1 : end]
            if tuple(result.call_id for result in results) != expected_ids or any(
                result.kind != "tool_result" for result in results
            ):
                raise RuntimeError("Run Log tool transaction is not contiguous")
            units.append((entry, *results))
            index = end
        return units

    @staticmethod
    def _event_fact(entry):
        return _ProjectedFact(
            entry.kind,
            dict(entry.payload),
            (entry.event_id,),
            entry.artifact_id,
        )

    @classmethod
    def _projection_units(
        cls,
        events,
        *,
        projected_guidance_id="",
        projected_instruction_id="",
        allow_incomplete=False,
    ):
        """Project response envelopes into independent completed Call facts."""

        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind == "user_message" or (
                entry.kind == "user_guidance"
                and entry.event_id == projected_guidance_id
            ) or (
                entry.kind == "model_instruction"
                and entry.event_id == projected_instruction_id
            ):
                index += 1
                continue
            if entry.kind != "assistant_tool_calls":
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan tool result")
                units.append((cls._event_fact(entry),))
                index += 1
                continue
            calls = entry.tool_calls
            end = index + 1 + len(calls)
            if end > len(events):
                if allow_incomplete:
                    return None
                raise RuntimeError("Run Log contains an incomplete tool transaction")
            results = events[index + 1 : end]
            if tuple(result.call_id for result in results) != tuple(
                call.call_id for call in calls
            ) or any(result.kind != "tool_result" for result in results):
                raise RuntimeError("Run Log tool transaction is not contiguous")
            for call, result in zip(calls, results):
                if (
                    call.name == "update_working_state"
                    and result.outcome_status == "success"
                ):
                    continue
                call_fact = _ProjectedFact(
                    "tool_call",
                    {
                        "name": call.name,
                        "args": dict(call.args),
                        "call_id": call.call_id,
                    },
                    (entry.event_id,),
                )
                units.append((call_fact, cls._event_fact(result)))
            index = end
        return units

    @staticmethod
    def _render_fact(fact):
        if fact.kind == "tool_call":
            return f"[assistant/tool] {fact.payload['name']} " + json.dumps(
                fact.payload["args"], ensure_ascii=False, sort_keys=True
            )
        if fact.kind == "tool_result":
            artifact = f" artifact={fact.artifact_id}" if fact.artifact_id else ""
            outcome = ToolOutcome.from_dict(fact.payload["outcome"])
            return (
                f"[tool/{outcome.tool_name}/{outcome.status}/"
                f"{outcome.side_effect_state}{artifact}] {outcome.render_for_model()}"
            )
        if fact.kind == "model_instruction":
            return "[model_instruction] " + json.dumps(
                fact.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        content = str(fact.payload.get("content", ""))
        return f"[{fact.kind}] {content}"

    @staticmethod
    def _source_ids(units):
        return {
            event_id
            for unit in units
            for fact in unit
            for event_id in fact.source_event_ids
        }

    def _select_recent(self, units, *, limit, render):
        selected = []
        for unit in reversed(units):
            full = [unit, *selected]
            full_render = render(full)
            if full_render[1] <= limit:
                selected = full
                continue
            break
        return selected

    def plan_compaction(self, *, retain_tokens, max_history_tokens,
                        history_token_counter, summary_builder):
        active = list(self.active_events())
        latest_guidance_id = self._latest_user_guidance_id(self._events)
        pending_instruction_id = self._projected_instruction_id
        units = self._history_units(active, allow_incomplete=True)
        if units is None:
            return None

        def render(candidate_units, *, summary=""):
            events = tuple(event for unit in candidate_units for event in unit)
            projected = self._projection_units(
                events,
                projected_guidance_id=latest_guidance_id,
                projected_instruction_id=pending_instruction_id,
            )
            lines = ["Current run events:"]
            if summary:
                lines.append(f"[compaction] {summary}")
            lines.extend(
                self._render_fact(fact) for unit in projected for fact in unit
            )
            if len(lines) == 1:
                lines.append("- empty")
            return "\n".join(lines)

        retained = []
        limit = max(1, int(retain_tokens))
        for unit in reversed(units):
            candidate = [unit, *retained]
            if history_token_counter(render(candidate)) > limit:
                break
            retained = candidate
        cut = max(0, len(units) - len(retained))
        retained_tokens = max(1, int(history_token_counter(render(retained))))
        compacted = tuple(item for unit in units[:cut] for item in unit)
        if not compacted:
            return None
        summary_units = self._projection_units(
            compacted,
            projected_guidance_id=latest_guidance_id,
            projected_instruction_id=pending_instruction_id,
        )
        summary_facts = tuple(fact for unit in summary_units for fact in unit)
        if not summary_facts:
            return None
        summary_budget = max_history_tokens - retained_tokens
        if summary_budget < 1:
            return None
        summary = summary_builder(summary_facts, max_summary_tokens=summary_budget)
        before = render(units)
        after = render(retained, summary=summary)
        if history_token_counter(after) > max_history_tokens:
            return None
        if history_token_counter(after) >= history_token_counter(before):
            return None
        return (
            summary,
            [entry.event_id for entry in compacted],
            {
                "mode": "semantic_history",
                "covered_events": len(compacted),
                "retained_events": sum(len(unit) for unit in units[cut:]),
                "retained_tokens": retained_tokens,
                "summary_tokens": history_token_counter(render((), summary=summary)),
            },
        )

    @staticmethod
    def _latest_user_guidance_id(events):
        return next(
            (
                entry.event_id
                for entry in reversed(tuple(events))
                if entry.kind == "user_guidance"
            ),
            "",
        )

    def _active_projection_units(self):
        active = self.active_events()
        units = self._projection_units(
            active,
            projected_guidance_id=self._latest_user_guidance_id(self._events),
            projected_instruction_id=self._projected_instruction_id,
            allow_incomplete=True,
        )
        return active, units or []

    def render_projection(self):
        active, units = self._active_projection_units()
        facts = tuple(fact for unit in units for fact in unit)
        lines = ["Current run events:"]
        lines.extend(self._render_fact(fact) for fact in facts)
        if len(lines) == 1:
            lines.append("- empty")
        source_ids = self._source_ids(units)
        return "\n".join(lines), {
            "active_count": len(active),
            "selected_count": len(source_ids),
            "omitted_count": max(0, len(active) - len(source_ids)),
            "artifact_references": sum(bool(fact.artifact_id) for fact in facts),
        }

    def render_compacted_projection(self, *, retain_tokens, token_counter):
        """Render committed summaries followed by a bounded per-Call suffix."""

        active, units = self._active_projection_units()
        summaries = tuple(
            unit for unit in units if len(unit) == 1 and unit[0].kind == "compaction"
        )
        if not summaries:
            return None
        recent = tuple(unit for unit in units if unit not in summaries)
        limit = max(0, int(retain_tokens))

        def render(selected, *, include_summaries):
            included = (*summaries, *selected) if include_summaries else tuple(selected)
            omitted = len(self._source_ids(units) - self._source_ids(included))
            lines = ["Current run events:"]
            if omitted:
                lines.append(HISTORY_OMITTED)
            for unit in included:
                lines.extend(self._render_fact(fact) for fact in unit)
            text = "\n".join(lines)
            return text, token_counter(text)

        include_summaries = render([], include_summaries=True)[1] <= limit

        def render_selected(selected):
            return render(selected, include_summaries=include_summaries)

        minimum = render_selected([])
        if minimum[1] > limit:
            return "", {
                "active_count": len(active),
                "selected_count": 0,
                "omitted_count": len(active),
                "artifact_references": 0,
                "projection_mode": "compacted_call_transactions",
                "retained_tokens": 0,
            }
        retained = self._select_recent(
            recent,
            limit=limit,
            render=render_selected,
        )
        text, retained_tokens = render_selected(retained)
        selected = (*summaries, *retained) if include_summaries else tuple(retained)
        retained_facts = tuple(fact for unit in selected for fact in unit)
        selected_ids = self._source_ids(selected)
        return text, {
            "active_count": len(active),
            "selected_count": len(selected_ids),
            "omitted_count": max(0, len(active) - len(selected_ids)),
            "artifact_references": sum(
                bool(fact.artifact_id) for fact in retained_facts
            ),
            "projection_mode": "compacted_call_transactions",
            "retained_tokens": retained_tokens,
        }

    def render_recent_projection(self, *, retain_tokens, token_counter):
        """Render a suffix that is bounded and independently complete per Call."""

        active, units = self._active_projection_units()
        limit = max(0, int(retain_tokens))

        def render(selected):
            omitted = len(self._source_ids(units) - self._source_ids(selected))
            lines = ["Current run events (bounded fallback):"]
            lines.append(f"- {omitted} older events omitted")
            for unit in selected:
                lines.extend(self._render_fact(fact) for fact in unit)
            text = "\n".join(lines)
            return text, token_counter(text)

        minimum = render([])
        if minimum[1] > limit:
            return "", {
                "active_count": len(active),
                "selected_count": 0,
                "omitted_count": len(active),
                "artifact_references": 0,
                "retained_tokens": 0,
            }
        retained = self._select_recent(units, limit=limit, render=render)
        text, retained_tokens = render(retained)
        retained_facts = tuple(fact for unit in retained for fact in unit)
        selected_ids = self._source_ids(retained)
        return text, {
            "active_count": len(active),
            "selected_count": len(selected_ids),
            "omitted_count": max(0, len(active) - len(selected_ids)),
            "artifact_references": sum(
                bool(fact.artifact_id) for fact in retained_facts
            ),
            "retained_tokens": retained_tokens,
        }
