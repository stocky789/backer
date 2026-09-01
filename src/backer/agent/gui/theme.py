"""Small, contrast-safe ttk theme helpers."""

from __future__ import annotations

from dataclasses import dataclass


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    if len(value) != 6:
        return (245, 245, 245)
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def _luminance(value: str) -> float:
    parts = []
    for channel in _rgb(value):
        channel /= 255
        parts.append(channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


@dataclass(frozen=True)
class Tokens:
    mode: str
    surface: str
    raised: str
    text: str
    muted: str
    success: str
    danger: str
    accent: str


LIGHT = Tokens("light", "#f4f3ef", "#ffffff", "#1c2428", "#4f5c61", "#17633a", "#9f2525", "#195d7a")
DARK = Tokens("dark", "#182024", "#232d32", "#f3f5f2", "#c2cac7", "#8ee0ad", "#ffb4aa", "#8ecae6")


def resolve_tokens(background: str, override: str | None = None) -> Tokens:
    """Pick legible tokens from ttk's background, with an explicit override."""
    if override in {"light", "dark"}:
        return LIGHT if override == "light" else DARK
    return DARK if _luminance(background) < 0.25 else LIGHT


def apply(style, override: str | None = None) -> Tokens:
    tokens = resolve_tokens(style.lookup("TFrame", "background") or "#f4f3ef", override)
    for name, foreground in (
        ("Body", tokens.text),
        ("Muted", tokens.muted),
        ("Success", tokens.success),
        ("Danger", tokens.danger),
        ("Mono", tokens.text),
    ):
        style.configure(f"{name}.TLabel", **{"foreground": foreground, "background": tokens.surface})
    style.configure("TFrame", **{"background": tokens.surface})
    style.configure("TLabel", **{"background": tokens.surface})
    style.configure("Treeview", rowheight=28)
    return tokens
