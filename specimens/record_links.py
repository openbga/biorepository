"""Rows other apps add to a specimen record, without specimens importing them.

DISTRIBUTION_SOURCES holds callables (user, specimen) -> [{"label", "url", "status_label",
"status_tone"}]. The distribution app registers its source in its AppConfig.ready, so specimens
never imports distribution (presentation only). Each source checks the viewing user's own read
permission and returns [] for a user who may not see its records.
"""

DISTRIBUTION_SOURCES = []


def distribution_rows(user, specimen):
    rows = []
    for source in DISTRIBUTION_SOURCES:
        rows.extend(source(user, specimen))
    return rows
