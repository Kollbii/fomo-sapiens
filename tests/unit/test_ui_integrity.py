"""Mechanical UI/UX integrity, design-token, and WCAG 2.1 AA contrast guardrails."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS_DIR = ROOT / "src" / "web" / "static" / "css"
TEMPLATES_DIR = ROOT / "src" / "web" / "templates"
JS_DIR = ROOT / "src" / "web" / "static" / "js" / "dashboard"

CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
HARDCODED_COLOR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\s*\(|\bhsla?\s*\(")
THEME_BLOCK_RE = re.compile(r'html\[data-theme="(\w+)"\]\s*\{([^}]*)\}')
HEX_TOKEN_RE = re.compile(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\b")
CLASS_ATTR_RE = re.compile(r'class="([^"]+)"')
ARTICLE_BLOCK_RE = re.compile(r"<article\b[^>]*>.*?</article>", re.S)
BTN_OR_BADGE_CLASS_RE = re.compile(r"^(?:btn(?:-[a-z0-9-]+)?|badge--[a-z0-9-]+)$")
FONT_SIZE_PX_RE = re.compile(r"font-size\s*:\s*(\d+(?:\.\d+)?)px")


def _strip_css_comments(text: str) -> str:
    return CSS_COMMENT_RE.sub("", text)


def _relative_luminance(hex_color: str) -> float:
    rgb = [int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    l1 = _relative_luminance(fg_hex)
    l2 = _relative_luminance(bg_hex)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _parse_theme_hex_tokens() -> dict[str, dict[str, str]]:
    tokens_text = _strip_css_comments((CSS_DIR / "tokens.css").read_text(encoding="utf-8"))
    themes: dict[str, dict[str, str]] = {}
    for theme_name, body in THEME_BLOCK_RE.findall(tokens_text):
        themes[theme_name] = dict(HEX_TOKEN_RE.findall(body))
    return themes


def _all_css_class_selectors() -> set[str]:
    css = "\n".join(p.read_text(encoding="utf-8") for p in sorted(CSS_DIR.glob("*.css")))
    return set(re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)", css))


def test_no_hardcoded_colors_outside_tokens_css() -> None:
    """All theme colors, shadows, and overlays must live exclusively in tokens.css."""
    violations: list[str] = []
    for path in sorted(CSS_DIR.glob("*.css")):
        if path.name == "tokens.css":
            continue
        cleaned = _strip_css_comments(path.read_text(encoding="utf-8"))
        for idx, line in enumerate(cleaned.splitlines(), 1):
            if HARDCODED_COLOR_RE.search(line):
                violations.append(f"{path.name}:{idx}: {line.strip()}")
    assert violations == [], f"Hardcoded CSS colors outside tokens.css: {violations}"


def test_no_dead_panel_header_classes_in_templates() -> None:
    """Every panel card uses canonical .panel-header rather than dead .panel-head classes."""
    html = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")
    dead_tokens = {"panel-head", "panel-head-left", "panel-actions"}
    found_dead: list[str] = []
    for match in CLASS_ATTR_RE.findall(html):
        for tok in match.split():
            if tok in dead_tokens:
                found_dead.append(tok)
    assert found_dead == [], f"Dead panel header classes in index.html: {found_dead}"

    missing_header: list[str] = []
    for match in ARTICLE_BLOCK_RE.finditer(html):
        block = match.group(0)
        first_line = block.splitlines()[0]
        if "panel-card" in first_line and "panel-header" not in block:
            missing_header.append(first_line)
    assert missing_header == [], f"panel-card articles missing .panel-header: {missing_header}"


def test_button_and_badge_modifier_classes_are_defined_in_css() -> None:
    """Every btn* and badge--* class token in templates or JS must exist in stylesheets."""
    css_classes = _all_css_class_selectors()
    used_tokens: set[str] = set()

    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        html = path.read_text(encoding="utf-8")
        for attr in CLASS_ATTR_RE.findall(html):
            for tok in attr.split():
                if not tok.startswith("{") and BTN_OR_BADGE_CLASS_RE.match(tok):
                    used_tokens.add(tok)

    for path in sorted(JS_DIR.glob("*.js")):
        js = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(?:class=|className\s*=)\s*["\']([^"\']+)["\']', js):
            for tok in match.group(1).split():
                if BTN_OR_BADGE_CLASS_RE.match(tok):
                    used_tokens.add(tok)
        for match in re.finditer(r'classList\.(?:add|remove|toggle)\(\s*["\']([^"\']+)["\']', js):
            tok = match.group(1)
            if BTN_OR_BADGE_CLASS_RE.match(tok):
                used_tokens.add(tok)

    missing = sorted(used_tokens - css_classes)
    assert missing == [], f"Undefined btn/badge CSS classes referenced in HTML/JS: {missing}"


def test_wcag_aa_contrast_for_theme_tokens() -> None:
    """Normal-text semantic foregrounds and primary CTA text must pass WCAG 2.1 AA (>= 4.5:1)."""
    themes = _parse_theme_hex_tokens()
    assert set(themes) == {"print", "terminal"}

    fg_tokens = (
        "--text-primary",
        "--text-secondary",
        "--text-muted",
        "--accent-primary",
        "--accent-call",
        "--accent-put",
        "--accent-warn",
        "--accent-anomaly",
        "--accent-focus",
        "--badge-delayed-ink",
    )
    bg_tokens = ("--bg-canvas", "--bg-surface", "--bg-elevated")

    failures: list[str] = []
    for theme_name, mapping in themes.items():
        for fg in fg_tokens:
            for bg in bg_tokens:
                ratio = _contrast_ratio(mapping[fg], mapping[bg])
                if ratio < 4.5:
                    failures.append(
                        f"{theme_name}: {fg} ({mapping[fg]}) on {bg} ({mapping[bg]}) = {ratio:.2f}"
                    )
        cta_ratio = _contrast_ratio(mapping["--bg-canvas"], mapping["--accent-primary"])
        if cta_ratio < 4.5:
            failures.append(
                f"{theme_name}: CTA --bg-canvas ({mapping['--bg-canvas']}) "
                f"on --accent-primary ({mapping['--accent-primary']}) = {cta_ratio:.2f}"
            )
    assert failures == [], f"WCAG 2.1 AA contrast failures (< 4.5:1): {failures}"


def test_terminal_primary_and_warning_accents_are_distinct() -> None:
    """Obsidian Terminal reserves amber for warnings while primary actions use Cyan/Sky."""
    themes = _parse_theme_hex_tokens()
    terminal = themes["terminal"]
    assert terminal["--accent-primary"].lower() != terminal["--accent-warn"].lower()


def test_minimum_readable_font_size_floor_in_css() -> None:
    """Prevent sub-10px unreadable CSS labels outside compact avatar monogram fallbacks."""
    violations: list[str] = []
    for path in sorted(CSS_DIR.glob("*.css")):
        cleaned = _strip_css_comments(path.read_text(encoding="utf-8"))
        for idx, line in enumerate(cleaned.splitlines(), 1):
            match = FONT_SIZE_PX_RE.search(line)
            if match is None:
                continue
            size_px = float(match.group(1))
            if size_px < 10.0:
                context = "\n".join(cleaned.splitlines()[max(0, idx - 12) : idx])
                if ".wl-logo" in context:
                    continue
                violations.append(f"{path.name}:{idx}: {line.strip()}")
    assert violations == [], f"Sub-10px CSS font-size outside monogram badges: {violations}"


def test_two_voice_typography_reserves_font_sans_for_logo_monograms() -> None:
    """Two-Voice typography: chrome/data use --font-mono; --font-sans only on logo monograms."""
    violations: list[str] = []
    for path in sorted(CSS_DIR.glob("*.css")):
        cleaned = _strip_css_comments(path.read_text(encoding="utf-8"))
        lines = cleaned.splitlines()
        for idx, line in enumerate(lines, 1):
            if "var(--font-sans)" not in line:
                continue
            context = "\n".join(lines[max(0, idx - 14) : idx])
            if ".wl-logo__fallback" in context or ".wl-logo" in context:
                continue
            violations.append(f"{path.name}:{idx}: {line.strip()}")
    assert violations == [], (
        f"Unexpected var(--font-sans) usage violating Two-Voice typography: {violations}"
    )
