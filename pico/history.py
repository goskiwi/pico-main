"""Read-only history selection, rendering and compaction planning over Run facts."""

import json
from dataclasses import dataclass, field

from .compaction_summary import CompactedContext
from .contracts import AssistantTurn, ModelMessage, ToolOutcome

HISTORY_OMITTED = "- older events omitted by History budget"


class HistoryBudgetExceeded(RuntimeError):
    pass


CONTEXT_KINDS = frozenset(
    {
        "user_guidance",
        "assistant_turn",
        "tool_result",
        "compaction",
    }
)


@dataclass
class ContextState:
    """Bounded model continuation state derived from Run Events."""

    compacted: CompactedContext | None = None
    recent_events: list = field(default_factory=list)

    @classmethod
    def from_events(cls, events):
        state = cls()
        for event in events:
            state.apply_event(event)
        return state

    def apply_event(self, event):
        if event.kind not in CONTEXT_KINDS:
            return
        if event.kind != "compaction":
            self.recent_events.append(event)
            return
        context = CompactedContext.from_dict(event.payload["context"])
        if (
            self.compacted is not None
            and context.covered_through_sequence
            <= self.compacted.covered_through_sequence
        ):
            raise ValueError("compaction coverage must advance")
        retained = [
            item
            for item in self.recent_events
            if item.sequence > context.covered_through_sequence
        ]
        if len(retained) == len(self.recent_events):
            raise ValueError("compaction must cover recent Context Events")
        self.compacted = context
        self.recent_events = retained

    def to_dict(self):
        return {
            "compacted": self.compacted.to_dict() if self.compacted else None,
            "recent_events": [event.to_dict() for event in self.recent_events],
        }


@dataclass(frozen=True)
class _ProjectedFact:
    kind: str
    payload: dict
    source_event_ids: tuple[str, ...]
    artifact_id: str = ""


class RunHistory:
    def __init__(
        self,
        events=(),
        *,
        context_state=None,
    ):
        self.state = (
            context_state
            if context_state is not None
            else ContextState.from_events(events)
        )

    def recent_events(self):
        return tuple(self.state.recent_events)

    @staticmethod
    def _history_units(events, *, allow_incomplete=False):
        """Return complete Assistant turns with their ordered Tool Results."""

        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind != "assistant_turn":
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan Tool Result")
                units.append((entry,))
                index += 1
                continue
            turn = AssistantTurn.from_dict(entry.payload["turn"])
            calls = turn.action.tool_calls
            if not calls:
                units.append((entry,))
                index += 1
                continue
            end = index + 1 + len(calls)
            if end > len(events):
                if allow_incomplete:
                    return None
                raise RuntimeError("Run Log contains an incomplete Assistant turn")
            results = events[index + 1 : end]
            if any(
                result.kind != "tool_result" or result.call_id != call.call_id
                for call, result in zip(calls, results, strict=True)
            ):
                raise RuntimeError("Run Log Assistant turn results are not contiguous")
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
    def _tool_facts(cls, call, turn_entry, result_entry):
        outcome = dict(result_entry.payload["outcome"])
        return (
            _ProjectedFact(
                "tool_call",
                {
                    "name": call.name,
                    "args": dict(call.args),
                    "call_id": call.call_id,
                },
                (turn_entry.event_id,),
            ),
            _ProjectedFact(
                "tool_result",
                {"outcome": outcome},
                (result_entry.event_id,),
                str(outcome.get("artifact_id", "")),
            ),
        )

    @classmethod
    def _projection_units(
        cls,
        events,
        *,
        include_user_guidance=False,
        allow_incomplete=False,
    ):
        """Project response envelopes into independent completed Call facts."""

        units = []
        index = 0
        events = tuple(events)
        while index < len(events):
            entry = events[index]
            if entry.kind == "user_message" or (
                entry.kind == "user_guidance" and not include_user_guidance
            ):
                index += 1
                continue
            if entry.kind != "assistant_turn":
                if entry.kind == "tool_result":
                    raise RuntimeError("Run Log contains an orphan Tool Result")
                units.append((cls._event_fact(entry),))
                index += 1
                continue
            turn = AssistantTurn.from_dict(entry.payload["turn"])
            facts = []
            if turn.text:
                facts.append(
                    _ProjectedFact(
                        "assistant_text",
                        {"content": turn.text},
                        (entry.event_id,),
                    )
                )
            if turn.action.kind == "final":
                facts.append(
                    _ProjectedFact(
                        "final_answer",
                        {"content": turn.action.content},
                        (entry.event_id,),
                    )
                )
                units.append(tuple(facts))
                index += 1
                continue
            end = index + 1 + len(turn.action.tool_calls)
            if end > len(events):
                if allow_incomplete:
                    return None
                raise RuntimeError("Run Log contains an incomplete Assistant turn")
            results = events[index + 1 : end]
            for call, result in zip(turn.action.tool_calls, results, strict=True):
                if result.kind != "tool_result" or result.call_id != call.call_id:
                    raise RuntimeError("Run Log Assistant turn results are not contiguous")
                facts.extend(cls._tool_facts(call, entry, result))
            units.append(tuple(facts))
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
        content = str(fact.payload.get("content", ""))
        if fact.kind == "compaction":
            content = CompactedContext.from_dict(fact.payload["context"]).render()
        return f"[{fact.kind}] {content}"

    @staticmethod
    def _compacted_file_lists(units, previous=None):
        read_files = set(previous.read_files if previous is not None else ())
        modified_files = set(
            previous.modified_files if previous is not None else ()
        )
        for unit in units:
            first = unit[0]
            if first.kind != "assistant_turn":
                continue
            turn = AssistantTurn.from_dict(first.payload["turn"])
            for call, result_entry in zip(
                turn.action.tool_calls,
                unit[1:],
                strict=True,
            ):
                outcome = ToolOutcome.from_dict(result_entry.payload["outcome"])
                if call.name == "read_file" and outcome.status == "success":
                    path = outcome.structured.get("path") or call.args.get("path")
                    if isinstance(path, str) and path:
                        read_files.add(path)
                modified_files.update(outcome.affected_paths)
        read_files.difference_update(modified_files)
        return tuple(sorted(read_files)), tuple(sorted(modified_files))

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

    def plan_compaction(
        self,
        *,
        retain_tokens,
        token_counter,
        context_budget,
        context_size,
        summary_builder,
    ):
        recent_events = list(self.recent_events())
        units = self._history_units(recent_events, allow_incomplete=True)
        if units is None:
            return None
        previous = self.state.compacted

        def render(
            candidate_units,
            *,
            compacted_context=None,
        ):
            events = tuple(event for unit in candidate_units for event in unit)
            projected = self._projection_units(
                events,
                include_user_guidance=True,
            )
            lines = ["Current run events:"]
            if compacted_context is not None:
                lines.append(
                    self._render_fact(
                        _ProjectedFact(
                            "compaction",
                            {"context": compacted_context.to_dict()},
                            (),
                        )
                    )
                )
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
            if token_counter(render(candidate)) > limit:
                break
            retained = candidate
        cut = max(0, len(units) - len(retained))
        compacted = tuple(item for unit in units[:cut] for item in unit)
        if not compacted:
            return None
        retained_events = tuple(item for unit in units[cut:] for item in unit)
        summary_units = self._projection_units(
            compacted,
            include_user_guidance=True,
        )
        summary_facts = tuple(
            (
                _ProjectedFact(
                    "compaction",
                    {"context": previous.to_dict()},
                    (),
                ),
            )
            if previous is not None
            else ()
        ) + tuple(fact for unit in summary_units for fact in unit)
        if not summary_facts:
            return None
        read_files, modified_files = self._compacted_file_lists(
            units[:cut],
            previous,
        )
        max_history_tokens, history_token_counter = context_budget(retained_events)
        retained_tokens = max(1, int(history_token_counter(render(retained))))
        summary_budget = max_history_tokens - retained_tokens
        if summary_budget < 1:
            return None
        semantic = summary_builder(summary_facts, max_summary_tokens=summary_budget)
        if not isinstance(semantic, CompactedContext):
            raise TypeError("summary builder must return CompactedContext")
        compacted_context = semantic.with_runtime_facts(
            read_files=read_files,
            modified_files=modified_files,
            covered_through_sequence=compacted[-1].sequence,
        )
        before = render(units, compacted_context=previous)
        after = render(
            retained,
            compacted_context=compacted_context,
        )
        if history_token_counter(after) > max_history_tokens:
            return None
        if context_size(retained_events, after) >= context_size(
            tuple(recent_events), before
        ):
            return None
        return compacted_context

    def _recent_projection_units(self):
        return self._projection_units(
            self.recent_events(),
            include_user_guidance=True,
            allow_incomplete=True,
        ) or []

    def render_projection(self):
        units = self._recent_projection_units()
        facts = tuple(fact for unit in units for fact in unit)
        compacted = self.state.compacted
        if not facts and compacted is None:
            return ""
        lines = ["Current run events:"]
        if compacted is not None:
            lines.append(
                self._render_fact(
                    _ProjectedFact(
                        "compaction",
                        {"context": compacted.to_dict()},
                        (),
                    )
                )
            )
        lines.extend(self._render_fact(fact) for fact in facts)
        return "\n".join(lines)

    def user_texts(self, events=None):
        return tuple(
            entry.content
            for entry in (
                self.recent_events() if events is None else tuple(events)
            )
            if entry.kind == "user_guidance"
        )

    def model_message_units(self):
        """Return chronological messages without splitting an Assistant tool turn."""

        units = []
        for events in self._history_units(self.recent_events()):
            first = events[0]
            if first.kind == "user_guidance":
                units.append((ModelMessage.user(first.content),))
                continue
            if first.kind != "assistant_turn":
                continue
            turn = AssistantTurn.from_dict(first.payload["turn"])
            if turn.action.kind == "final":
                units.append((ModelMessage.assistant(text=turn.action.content),))
                continue
            messages = [
                ModelMessage.assistant(
                    text=turn.text,
                    tool_calls=turn.action.tool_calls,
                )
            ]
            messages.extend(
                ModelMessage.tool(
                    result.call_id,
                    ToolOutcome.from_dict(
                        result.payload["outcome"]
                    ).render_for_model(),
                )
                for result in events[1:]
            )
            units.append(tuple(messages))
        return tuple(units)

    def compacted_message(self):
        if self.state.compacted is None:
            return None
        return ModelMessage.user(
            "<conversation_summary>\n"
            + self.state.compacted.render()
            + "\n</conversation_summary>"
        )

    def render_required_projection(self):
        """Render the committed Summary that must survive optional selection."""

        compacted = self.state.compacted
        if compacted is None:
            return ""
        lines = ["Current run events:"]
        if self.state.recent_events:
            lines.append(HISTORY_OMITTED)
        lines.append(
            self._render_fact(
                _ProjectedFact(
                    "compaction",
                    {"context": compacted.to_dict()},
                    (),
                )
            )
        )
        return "\n".join(lines)

    def render_compacted_projection(self, *, retain_tokens, token_counter):
        """Render committed summaries followed by a bounded per-Call suffix."""

        recent = self._recent_projection_units()
        compacted = self.state.compacted
        if compacted is None:
            return None
        limit = max(0, int(retain_tokens))
        summary = (
            _ProjectedFact(
                "compaction",
                {"context": compacted.to_dict()},
                (),
            ),
        )

        def render(selected):
            omitted = len(
                self._source_ids(recent) - self._source_ids(selected)
            )
            lines = ["Current run events:"]
            if omitted:
                lines.append(HISTORY_OMITTED)
            for unit in (summary, *selected):
                lines.extend(self._render_fact(fact) for fact in unit)
            text = "\n".join(lines)
            return text, token_counter(text)

        minimum = render([])
        if minimum[1] > limit:
            raise HistoryBudgetExceeded(
                "committed compaction summary exceeds the available History budget"
            )
        retained = self._select_recent(
            recent,
            limit=limit,
            render=render,
        )
        text, _retained_tokens = render(retained)
        return text

    def render_recent_projection(self, *, retain_tokens, token_counter):
        """Render a suffix that is bounded and independently complete per Call."""

        units = self._recent_projection_units()
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
            return ""
        retained = self._select_recent(units, limit=limit, render=render)
        text, _retained_tokens = render(retained)
        return text
