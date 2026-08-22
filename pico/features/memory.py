"""Run-scoped structured working state projected from durable tool events."""

from dataclasses import dataclass, field

WORKING_STATE_SCHEMA_VERSION = "run-working-state-v1"
WORKING_STATE_UPDATE_FIELDS = (
    "add_constraints",
    "remove_constraints",
    "add_decisions",
    "remove_decisions",
    "add_next_steps",
    "remove_next_steps",
)
WORKING_STATE_MAX_ITEMS = 24
WORKING_STATE_ITEM_MAX_CHARS = 500


def _normalize_items(values, *, field_name):
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        raise TypeError(f"working state {field_name} must be a list")
    normalized = tuple(str(item).strip() for item in values)
    if any(not item for item in normalized):
        raise ValueError(f"working state {field_name} cannot contain empty items")
    if any(len(item) > WORKING_STATE_ITEM_MAX_CHARS for item in normalized):
        raise ValueError(
            f"working state {field_name} items cannot exceed "
            f"{WORKING_STATE_ITEM_MAX_CHARS} characters"
        )
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"working state {field_name} must contain unique items")
    return normalized


def normalize_working_update(value):
    if not isinstance(value, dict):
        raise TypeError("working state update must be an object")
    unknown = sorted(set(value) - set(WORKING_STATE_UPDATE_FIELDS))
    if unknown:
        raise ValueError("unknown working state update fields: " + ", ".join(unknown))
    normalized = {
        field_name: _normalize_items(value.get(field_name, ()), field_name=field_name)
        for field_name in WORKING_STATE_UPDATE_FIELDS
    }
    if not any(normalized.values()):
        raise ValueError("working state update must contain at least one change")
    for noun in ("constraints", "decisions", "next_steps"):
        overlap = set(normalized[f"add_{noun}"]) & set(
            normalized[f"remove_{noun}"]
        )
        if overlap:
            raise ValueError(
                f"working state cannot add and remove the same {noun}: "
                + ", ".join(sorted(overlap))
            )
    return normalized


def _updated_items(current, *, additions, removals, field_name):
    removed = set(removals)
    result = [item for item in current if item not in removed]
    for item in additions:
        if item not in result:
            result.append(item)
    if len(result) > WORKING_STATE_MAX_ITEMS:
        raise ValueError(
            f"working state {field_name} cannot exceed {WORKING_STATE_MAX_ITEMS} items"
        )
    return tuple(result)


@dataclass
class WorkingState:
    """Current task goal, constraints, decisions, and next steps."""

    goal: str = ""
    constraints: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()
    _pending_updates: dict[str, dict] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        self.goal = str(self.goal)
        self.constraints = _normalize_items(
            self.constraints, field_name="constraints"
        )
        self.decisions = _normalize_items(self.decisions, field_name="decisions")
        self.next_steps = _normalize_items(self.next_steps, field_name="next_steps")
        for field_name in ("constraints", "decisions", "next_steps"):
            if len(getattr(self, field_name)) > WORKING_STATE_MAX_ITEMS:
                raise ValueError(
                    f"working state {field_name} cannot exceed "
                    f"{WORKING_STATE_MAX_ITEMS} items"
                )

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise TypeError("working state must be an object")
        expected = {
            "schema_version",
            "goal",
            "constraints",
            "decisions",
            "next_steps",
        }
        if set(value) != expected:
            raise ValueError("invalid working state fields")
        if value.get("schema_version") != WORKING_STATE_SCHEMA_VERSION:
            raise ValueError("unsupported working state schema")
        if not isinstance(value["goal"], str):
            raise TypeError("working state goal must be text")
        for field_name in ("constraints", "decisions", "next_steps"):
            if not isinstance(value[field_name], list):
                raise TypeError(f"working state {field_name} must be a list")
        return cls(
            goal=value["goal"],
            constraints=tuple(value["constraints"]),
            decisions=tuple(value["decisions"]),
            next_steps=tuple(value["next_steps"]),
        )

    def to_dict(self):
        return {
            "schema_version": WORKING_STATE_SCHEMA_VERSION,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "decisions": list(self.decisions),
            "next_steps": list(self.next_steps),
        }

    def updated(self, update):
        normalized = normalize_working_update(update)
        return WorkingState(
            goal=self.goal,
            constraints=_updated_items(
                self.constraints,
                additions=normalized["add_constraints"],
                removals=normalized["remove_constraints"],
                field_name="constraints",
            ),
            decisions=_updated_items(
                self.decisions,
                additions=normalized["add_decisions"],
                removals=normalized["remove_decisions"],
                field_name="decisions",
            ),
            next_steps=_updated_items(
                self.next_steps,
                additions=normalized["add_next_steps"],
                removals=normalized["remove_next_steps"],
                field_name="next_steps",
            ),
        )

    def apply_update(self, update):
        updated = self.updated(update)
        self.constraints = updated.constraints
        self.decisions = updated.decisions
        self.next_steps = updated.next_steps
        return self

    def apply_event(self, event):
        payload = dict(event.payload)
        if event.kind == "user_message" and not self.goal:
            self.goal = str(payload.get("content", ""))
        elif event.kind == "assistant_tool_call" and event.name == "update_working_state":
            self._pending_updates[event.call_id] = dict(event.args)
        elif event.kind == "tool_result":
            update = self._pending_updates.pop(event.call_id, None)
            outcome = dict(payload.get("outcome", {}) or {})
            if update is not None and outcome.get("status") == "success":
                self.apply_update(update)
        return self

    @staticmethod
    def _section(title, items):
        lines = [f"{title}:"]
        lines.extend(f"- {item}" for item in items)
        if len(lines) == 1:
            lines.append("- none")
        return lines

    def render_panel(self, *, include_goal=True):
        lines = ["Run working state:"]
        if include_goal:
            lines.extend(["Goal:", f"- {self.goal or '-'}"])
        lines.extend(self._section("Constraints", self.constraints))
        lines.extend(self._section("Decisions", self.decisions))
        lines.extend(self._section("Next steps", self.next_steps))
        return "\n".join(lines)
