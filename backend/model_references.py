"""Request-local aliases; source identity and authorization remain local.

Only structured reference fields are transformed. Chat text, task titles and
timestamps must never be subjected to string replacement. Unknown returned
references become invalid typed values so business validation rejects the
whole result while the transport can still report actual provider usage.
"""

import json

from backend.contracts import ServiceError


class References:
    def __init__(self):
        self.messages = {}
        self.participants = {}
        self.tasks = {}

    @staticmethod
    def invalid_input():
        raise ServiceError("model_reference_input", "整理输入的来源关联不完整，未发送请求。")

    @staticmethod
    def alias(mapping, value, prefix):
        if not isinstance(value, str) or not value:
            References.invalid_input()
        if value not in mapping:
            mapping[value] = prefix + str(len(mapping) + 1)
        return mapping[value]

    def message(self, record):
        if not isinstance(record, dict):
            self.invalid_input()
        mid = record.get("source_message_id")
        if not isinstance(mid, str) or not mid or mid in self.messages:
            self.invalid_input()
        result = dict(record)
        result["source_message_id"] = self.alias(self.messages, mid, "M")
        participant = record.get("participant")
        result["participant"] = (self.alias(self.participants, participant, "P")
                                 if participant is not None else None)
        return result

    def sources(self, values):
        if (not isinstance(values, list) or not values
                or any(not isinstance(v, str) or v not in self.messages for v in values)
                or len(values) != len(set(values))):
            self.invalid_input()
        return [self.messages[v] for v in values]

    def existing(self, record):
        if not isinstance(record, dict):
            self.invalid_input()
        tid = record.get("task_id")
        if not isinstance(tid, str) or not tid or tid in self.tasks:
            self.invalid_input()
        result = dict(record)
        result["task_id"] = self.alias(self.tasks, tid, "T")
        result["source_message_ids"] = self.sources(record.get("source_message_ids"))
        return result

    def restore(self, answer):
        if not isinstance(answer, dict):
            return answer
        messages = {alias: original for original, alias in self.messages.items()}
        tasks = {alias: original for original, alias in self.tasks.items()}
        result = dict(answer)
        for collection in ("tasks", "changes", "candidate_decisions", "attention_items"):
            if not isinstance(answer.get(collection), list):
                continue
            items = []
            for record in answer[collection]:
                if not isinstance(record, dict):
                    items.append(record)
                    continue
                item = dict(record)
                refs = record.get("source_message_ids")
                if isinstance(refs, list):
                    item["source_message_ids"] = [messages.get(ref, {}) if isinstance(ref, str)
                                                  else {} for ref in refs]
                for field in ("existing_task_id", "task_id"):
                    if field in record and record[field] is not None:
                        ref = record[field]
                        item[field] = tasks.get(ref, {}) if isinstance(ref, str) else {}
                items.append(item)
            result[collection] = items
        return result


def compact_prompt(prompt, schema):
    """Transform only the two owned analysis envelopes, never generic prompts."""
    if not isinstance(schema, dict) or "tasks" not in schema.get("properties", {}):
        return prompt, None
    prefix, marker, raw = prompt.partition("输入：")
    if not marker:
        return prompt, None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        References.invalid_input()
    if not isinstance(payload, dict):
        References.invalid_input()
    fields = set(payload)
    if fields in ({"new_messages", "context_messages"}, {"new_messages", "context_messages", "candidate_tasks"}):
        message_fields = ("new_messages", "context_messages")
    elif fields in ({"messages", "existing_tasks"}, {"messages", "existing_tasks", "candidate_tasks"}):
        message_fields = ("messages",)
    else:
        References.invalid_input()
    references = References()
    compact = dict(payload)
    for field in message_fields:
        if not isinstance(payload[field], list):
            References.invalid_input()
        compact[field] = [references.message(record) for record in payload[field]]
    if "existing_tasks" in payload:
        if not isinstance(payload["existing_tasks"], list):
            References.invalid_input()
        compact["existing_tasks"] = [references.existing(record) for record in payload["existing_tasks"]]
    if "candidate_tasks" in payload:
        candidates = payload["candidate_tasks"]
        if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
            References.invalid_input()
        converted = []
        for candidate in candidates:
            refs = references.sources(candidate.get("source_message_ids"))
            item = dict(candidate, source_message_ids=refs)
            if "request_last_message_id" in candidate:
                last = candidate["request_last_message_id"]
                if (not isinstance(last, str) or last not in candidate["source_message_ids"]
                        or type(candidate.get("later_evidence_possible")) is not bool):
                    References.invalid_input()
                item["request_last_message_id"] = references.messages[last]
            converted.append(item)
        compact["candidate_tasks"] = converted
    explanation = ("本次输入使用短编号：M 为消息来源，P 仅区分参与人、不能据此推断本人，T 为已存事项。"
                   "输出来源及已有事项关联只能逐字引用本次给出的编号，不引用正文中出现的编号。")
    return prefix + explanation + "输入：" + json.dumps(compact, ensure_ascii=False), references
