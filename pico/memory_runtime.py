"""Runtime helpers for durable memory promotion."""

import json
import re
import textwrap

from . import durable_memory
from .config import MEMORY_EXTRACTOR_MAX_TOKENS
from .workspace import clip


def promote_durable_memory(agent, user_message, final_answer):
    agent.last_durable_promotions = []
    agent.last_durable_rejections = []
    agent.last_durable_superseded = []
    if not agent.feature_enabled("durable_memory_promotion"):
        return [], [], []
    result = durable_memory.promote(agent.memory, user_message, final_answer)
    agent.session["memory"] = agent.memory.to_dict()
    agent.last_durable_promotions = result.promoted
    agent.last_durable_rejections = result.rejections
    agent.last_durable_superseded = result.superseded
    return result.promoted, result.rejections, result.superseded


def llm_memory_index_text(agent):
    if not getattr(agent.memory, "durable_store", None):
        return "- none"
    entries = agent.memory.durable_store.load_index()
    if not entries:
        return "- none"
    lines = []
    for entry in entries:
        memory_type = entry.get("type", "")
        summary = entry.get("summary", "")
        notes = agent.memory.durable_store.load_type_notes(memory_type)
        lines.append(f"- {memory_type}: {summary}")
        for note in notes[:8]:
            description = str(note.get("description") or note.get("text") or "").strip()
            if description:
                lines.append(f"  - {clip(description, 160)}")
    return "\n".join(lines) if lines else "- none"


def build_memory_extractor_prompt(agent, user_message, final_answer):
    return textwrap.dedent(
        f"""\
        You are pico's durable memory extractor. Decide whether the latest completed turn contains long-term memory worth saving.

        Allowed memory types:
        - user: stable facts about the user, their background, or skill profile.
        - feedback: user preferences, corrections, and behavioral guidance for future agent work.
        - project: stable project constraints, decisions, policies, or durable project dynamics.
        - reference: durable pointers to external sources of truth or where to look things up.

        Save only information that is likely to remain useful across future sessions.
        Do not save code facts, file paths, line numbers, git history, current task state, tool output, stack traces, secrets, or temporary debugging details.
        Do not infer user traits from a single question. Prefer explicit user feedback, corrections, or stated constraints.
        If a candidate is a feedback or project rule, include the rule as a concise standalone sentence in text. Include why/how only when the user supplied that rationale.

        Existing durable memory index:
        {llm_memory_index_text(agent)}

        Latest user message:
        {user_message}

        Final assistant answer:
        {final_answer}

        Return JSON only, with this shape:
        {{"memories":[{{"type":"user|feedback|project|reference","text":"concise memory text"}}]}}
        Return {{"memories":[]}} if there is nothing worth saving.
        """
    ).strip()


def parse_memory_extractor_output(raw):
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError("extractor returned no JSON object")
        text = match.group(0)
    data = json.loads(text)
    memories = data.get("memories", [])
    if not isinstance(memories, list):
        raise ValueError("extractor memories must be a list")
    candidates = []
    for item in memories:
        if not isinstance(item, dict):
            continue
        memory_type = str(item.get("type", "")).strip()
        note_text = str(item.get("text", "")).strip()
        if memory_type and note_text:
            candidates.append((memory_type, note_text))
    return candidates


def llm_promote_durable_memory(agent, user_message, final_answer):
    agent.last_llm_durable_promotions = []
    agent.last_llm_durable_rejections = []
    agent.last_llm_durable_superseded = []
    agent.last_llm_memory_extractor_error = ""
    if not agent.feature_enabled("durable_memory_promotion") or not agent.feature_enabled(
        "llm_memory_extract"
    ):
        return [], [], []
    prompt = build_memory_extractor_prompt(agent, user_message, final_answer)
    try:
        raw = agent.model_client.complete(prompt, MEMORY_EXTRACTOR_MAX_TOKENS)
        candidates = parse_memory_extractor_output(raw)
    except Exception as exc:
        agent.last_llm_memory_extractor_error = str(exc)
        agent.session["memory"] = agent.memory.to_dict()
        return [], [f"extractor_error:{type(exc).__name__}"], []
    result = durable_memory.promote_candidates(agent.memory, candidates)
    agent.session["memory"] = agent.memory.to_dict()
    agent.last_llm_durable_promotions = result.promoted
    agent.last_llm_durable_rejections = result.rejections
    agent.last_llm_durable_superseded = result.superseded
    return result.promoted, result.rejections, result.superseded
