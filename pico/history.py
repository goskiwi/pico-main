"""Read-only history selection, rendering and compaction planning over Run facts."""

import json

from .contracts import ToolOutcome

COMPACTED_HISTORY_OMITTED = "- recent events omitted by History budget"
CONTEXT_KINDS = frozenset(
    {
        "user_message",
        "user_guidance",
        "assistant_tool_call",
        "assistant_tool_batch",
        "tool_result",
        "model_instruction",
        "assistant_final",
        "compaction",
    }
)


class RunHistory:
    def __init__(self, events):
        self._events = tuple(events)

    def latest_user_guidance(self):
        entry = next(
            (
                candidate
                for candidate in reversed(self.active_events())
                if candidate.kind == "user_guidance"
            ),
            None,
        )
        return entry.content if entry is not None else ""

    def context_events(self):
        calls = {
            call_id
            for entry in self._events
            for call_id in (
                (entry.call_id,)
                if entry.kind == "assistant_tool_call"
                else tuple(call.call_id for call in entry.batch_calls)
            )
            if entry.kind in {"assistant_tool_call", "assistant_tool_batch"} and call_id
        }
        return tuple(
            entry
            for entry in self._events
            if entry.kind in CONTEXT_KINDS
            and (entry.kind != "tool_result" or entry.call_id in calls)
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
        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind == "assistant_tool_call":
                expected_ids = (entry.call_id,)
            elif entry.kind == "assistant_tool_batch":
                expected_ids = tuple(call.call_id for call in entry.batch_calls)
            else:
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan tool result")
                units.append((entry,))
                index += 1
                continue
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

    def plan_compaction(self, *, retain_tokens, history_token_counter, summary_builder):
        active = list(self.active_events())
        latest_guidance_id = self._latest_user_guidance_id(active)
        units = self._history_units(active, allow_incomplete=True)
        if units is None:
            return None

        def render(candidate_units, *, summary=""):
            events = tuple(event for unit in candidate_units for event in unit)
            visible = self._without_projected_state(
                events,
                projected_guidance_id=latest_guidance_id,
            )
            lines = ["Current run events:"]
            if summary:
                lines.append(f"[compaction] {summary}")
            lines.extend(self._render_event(event) for event in visible)
            if len(lines) == 1:
                lines.append("- empty")
            return "\n".join(lines)

        retained = []
        limit = max(1, int(retain_tokens))
        for unit in reversed(units):
            candidate = [unit, *retained]
            candidate_tokens = max(
                1,
                int(history_token_counter(render(candidate))),
            )
            if retained and candidate_tokens > limit:
                break
            retained = candidate
        cut = max(0, len(units) - len(retained))
        guidance_unit_index = next(
            (
                index
                for index, unit in enumerate(units)
                if any(event.event_id == latest_guidance_id for event in unit)
            ),
            None,
        )
        if guidance_unit_index is not None and cut > guidance_unit_index:
            cut = guidance_unit_index
            retained = units[cut:]
        retained_tokens = max(
            1,
            int(history_token_counter(render(retained))),
        )
        compacted = tuple(item for unit in units[:cut] for item in unit)
        if not compacted:
            return None
        summary_events = tuple(
            self._without_projected_state(
                compacted,
                projected_guidance_id=latest_guidance_id,
            )
        )
        if not summary_events:
            return None
        summary = summary_builder(summary_events)
        before = render(units)
        after = render(retained, summary=summary)
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
    def _render_event(entry):
        if entry.kind == "assistant_tool_call":
            return f"[assistant/tool] {entry.name} " + json.dumps(
                entry.args or {}, ensure_ascii=False, sort_keys=True
            )
        if entry.kind == "assistant_tool_batch":
            calls = [
                {"call_id": call.call_id, "name": call.name, "args": call.args}
                for call in entry.batch_calls
            ]
            return f"[assistant/tool_batch/{entry.batch_id}] " + json.dumps(
                calls, ensure_ascii=False, sort_keys=True
            )
        if entry.kind == "tool_result":
            artifact = f" artifact={entry.artifact_id}" if entry.artifact_id else ""
            outcome = ToolOutcome.from_dict(entry.payload["outcome"])
            return (
                f"[tool/{entry.name}/{entry.outcome_status}/"
                f"{entry.side_effect_state}{artifact}] {outcome.render_for_model()}"
            )
        return f"[{entry.kind}] {entry.content}"

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

    @staticmethod
    def _without_projected_state(events, *, projected_guidance_id=""):
        selected = []
        index = 0
        while index < len(events):
            entry = events[index]
            if entry.kind == "user_message":
                index += 1
                continue
            if (
                entry.kind == "user_guidance"
                and entry.event_id == projected_guidance_id
            ):
                index += 1
                continue
            if (
                entry.kind == "assistant_tool_call"
                and entry.name == "update_working_state"
                and index + 1 < len(events)
            ):
                result = events[index + 1]
                if (
                    result.kind == "tool_result"
                    and result.call_id == entry.call_id
                    and result.outcome_status == "success"
                ):
                    index += 2
                    continue
            selected.append(entry)
            index += 1
        return selected

    def render_projection(self):
        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        lines = ["Current run events:"]
        artifact_references = 0
        for entry in selected:
            artifact_references += bool(entry.artifact_id)
            lines.append(self._render_event(entry))
        if len(lines) == 1:
            lines.append("- empty")
        return "\n".join(lines), {
            "active_count": len(active),
            "selected_count": len(selected),
            "omitted_count": max(0, len(active) - len(selected)),
            "artifact_references": artifact_references,
        }

    def render_compacted_projection(self, *, retain_tokens, token_counter):
        """Render complete summaries followed by a complete recent-event suffix."""

        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        summaries = tuple(entry for entry in selected if entry.kind == "compaction")
        if not summaries:
            return None
        recent = tuple(entry for entry in selected if entry.kind != "compaction")
        units = self._history_units(recent)

        limit = max(0, int(retain_tokens))

        def render(candidate):
            retained_count = sum(len(unit) for unit in candidate)
            lines = ["Current run events:"]
            lines.extend(self._render_event(entry) for entry in summaries)
            if retained_count < len(recent):
                lines.append(COMPACTED_HISTORY_OMITTED)
            for unit in candidate:
                lines.extend(self._render_event(entry) for entry in unit)
            return "\n".join(lines)

        retained = []
        minimum = render(retained)
        if token_counter(minimum) > limit:
            raise ValueError("committed compaction summary exceeds the History budget")
        for unit in reversed(units):
            candidate = [unit, *retained]
            if retained and token_counter(render(candidate)) > limit:
                break
            retained = candidate
        text = render(retained)
        flattened = tuple(entry for unit in retained for entry in unit)
        return text, {
            "active_count": len(active),
            "selected_count": len(summaries) + len(flattened),
            "omitted_count": max(
                0,
                len(active) - len(summaries) - len(flattened),
            ),
            "artifact_references": sum(
                bool(entry.artifact_id) for entry in (*summaries, *flattened)
            ),
            "projection_mode": "compacted_complete_transactions",
            "retained_tokens": token_counter(text),
        }

    def render_recent_projection(self, *, retain_tokens, token_counter):
        """Render a bounded suffix without splitting a Tool transaction."""

        active = self.active_events()
        selected = self._without_projected_state(
            active,
            projected_guidance_id=self._latest_user_guidance_id(active),
        )
        units = self._history_units(selected, allow_incomplete=True) or []

        limit = max(0, int(retain_tokens))
        retained = []

        def render(candidate):
            retained_count = sum(len(unit) for unit in candidate)
            omitted = max(0, len(selected) - retained_count)
            lines = ["Current run events (bounded fallback):"]
            lines.append(f"- {omitted} older events omitted")
            for unit in candidate:
                lines.extend(self._render_event(entry) for entry in unit)
            return "\n".join(lines)

        for unit in reversed(units):
            candidate = [unit, *retained]
            if retained and token_counter(render(candidate)) > limit:
                break
            retained = candidate
        text = render(retained)
        flattened = tuple(entry for unit in retained for entry in unit)
        return text, {
            "active_count": len(active),
            "selected_count": len(flattened),
            "omitted_count": max(0, len(active) - len(flattened)),
            "artifact_references": sum(bool(entry.artifact_id) for entry in flattened),
            "retained_tokens": token_counter(text),
        }
