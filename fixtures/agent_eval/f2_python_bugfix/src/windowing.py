"""Small deterministic interval helper used by the Agent eval."""


def collapse_windows(windows):
    """Merge overlapping or adjacent inclusive integer windows."""
    windows.sort()
    merged = []
    for start, end in windows:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [tuple(window) for window in merged]
