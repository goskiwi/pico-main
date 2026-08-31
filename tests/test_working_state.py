from pico.working_state import WorkingState, normalize_working_update


def test_working_state_tracks_constraints_decisions_and_next_steps():
    state = WorkingState()
    state.apply_update(
        {
            "add_constraints": ["Keep Python 3.10 compatibility"],
            "add_decisions": ["The race is in token refresh"],
            "add_next_steps": ["Add a concurrent refresh test"],
        }
    )

    assert state.to_dict() == {
        "schema_version": "run-working-state-v2",
        "constraints": ["Keep Python 3.10 compatibility"],
        "decisions": ["The race is in token refresh"],
        "next_steps": ["Add a concurrent refresh test"],
    }
    assert "The race is in token refresh" in state.render_panel()


def test_working_state_updates_are_incremental_and_idempotent():
    state = WorkingState(
        constraints=("Do not change the schema",),
        next_steps=("Inspect token refresh",),
    )
    update = normalize_working_update(
        {
            "add_constraints": ["Do not change the schema"],
            "remove_next_steps": ["Inspect token refresh"],
            "add_next_steps": ["Add a regression test"],
        }
    )

    state.apply_update(update)

    assert state.constraints == ("Do not change the schema",)
    assert state.next_steps == ("Add a regression test",)
