"""Ids of the Gmail system labels the code reads or sets. No imports, so `policy` stays pure.

A system label's id is also its name (unlike user labels, whose ids are `Label_…`), so these
work both as label ids sent to Gmail and as names in a set of current label names.
"""

INBOX = "INBOX"
SPAM = "SPAM"
STARRED = "STARRED"
TRASH = "TRASH"
