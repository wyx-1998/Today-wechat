"""Interpret senders using the explicitly configured account, without rewriting history."""


def identified(rows, source):
    source = source or {}
    account, self_id = source.get("account_id"), source.get("self_id")
    result = []
    for row in rows:
        effective = row["is_self"]
        if account and self_id and row.get("account_id") == account:
            sender = row.get("sender_id")
            # A nickname, message wording or missing sender is not identity evidence.
            effective = int(sender == self_id) if sender else None
        result.append(dict(row, effective_is_self=effective))
    return result


def relation(row):
    return row.get("effective_is_self", row["is_self"])
