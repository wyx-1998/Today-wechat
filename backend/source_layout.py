"""Supported text shard layout; auxiliary DBs are not message candidates.

This is an explicit version compatibility profile, not a claim that arbitrary
DBs can never contain messages. New shard layouts require a reader update.
"""

import re


def message_shards(directory):
    return [path for path in sorted(directory.glob("*.db"))
            if re.fullmatch(r"(?:biz_)?message_[0-9]+\.db", path.name)]
