"""Render TeX fragments as native MathML for HTML email.

Apple Mail displays HTML with WebKit, which has native MathML support. MathML
keeps formulas at the surrounding text size, responds naturally to zoom and
dark mode, remains selectable, and needs no CID image attachments.

The plain-text MIME alternative is built elsewhere and retains the original
TeX. If a fragment cannot be converted safely, this module emits a readable
Unicode/HTML approximation instead of an image.
"""

from __future__ import annotations

import html
import re

from latex2mathml.converter import convert as latex_to_mathml


_MATH_DELIMITER_RE = re.compile(
    r"(?P<display>\\\[(?P<display_bracket>.*?)\\\]|\$\$(?P<display_dollar>.*?)\$\$)"
    r"|(?P<inline>\\\((?P<inline_paren>.*?)\\\)|(?<!\\)\$(?!\$)(?P<inline_dollar>[^$\n]+?)(?<!\\)\$)",
    re.DOTALL,
)

_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "varepsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "vartheta": "ϑ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "phi": "φ",
    "varphi": "ϕ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
    "Gamma": "Γ",
    "Delta": "Δ",
    "Theta": "Θ",
    "Lambda": "Λ",
    "Xi": "Ξ",
    "Pi": "Π",
    "Sigma": "Σ",
    "Phi": "Φ",
    "Psi": "Ψ",
    "Omega": "Ω",
    "le": "≤",
    "leq": "≤",
    "ge": "≥",
    "geq": "≥",
    "neq": "≠",
    "ne": "≠",
    "approx": "≈",
    "sim": "∼",
    "simeq": "≃",
    "equiv": "≡",
    "propto": "∝",
    "pm": "±",
    "mp": "∓",
    "times": "×",
    "cdot": "·",
    "odot": "⊙",
    "oplus": "⊕",
    "infty": "∞",
    "degree": "°",
    "circ": "°",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "Leftarrow": "⇐",
    "%": "%",
    "&": "&",
    "_": "_",
    "{": "{",
    "}": "}",
}

_TEXT_COMMANDS = {
    "text",
    "mbox",
    "mathrm",
    "textrm",
    "operatorname",
}
_ITALIC_COMMANDS = {"textit", "mathit"}
_BOLD_COMMANDS = {"textbf", "mathbf"}
_SPACING_COMMANDS = {
    ",": " ",
    ":": " ",
    ";": " ",
    "quad": " ",
    "qquad": "  ",
    "!": "",
    " ": " ",
}


def _normalise_tex(tex: str) -> str:
    """Convert recurring arXiv TeX idioms before MathML conversion."""
    value = str(tex).strip().replace("\n", " ")
    value = re.sub(r"\s+", " ", value)
    replacements = (
        (r"\mbox{", r"\text{"),
        (r"\textrm{", r"\mathrm{"),
        (r"\dfrac", r"\frac"),
        (r"\tfrac", r"\frac"),
        (r"\boldsymbol", r"\mathbf"),
        (r"\bm", r"\mathbf"),
        (r"\LaTeX", r"\mathrm{LaTeX}"),
        (r"\b{eta}", r"\beta"),
    )
    for old, new in replacements:
        value = value.replace(old, new)
    value = re.sub(r"\\La(?=\s|$)", r"\\Lambda", value)
    value = re.sub(r"\\rm\s+([A-Za-z]+)", lambda m: r"\mathrm{" + m.group(1) + "}", value)
    value = re.sub(r"\\it\s+([A-Za-z]+)", lambda m: r"\mathit{" + m.group(1) + "}", value)
    value = re.sub(r"\\bf\s+([A-Za-z]+)", lambda m: r"\mathbf{" + m.group(1) + "}", value)
    value = re.sub(r"\\rm\b", lambda _m: r"\mathrm", value)
    value = re.sub(r"\\it\b", lambda _m: r"\mathit", value)
    value = re.sub(r"\\bf\b", lambda _m: r"\mathbf", value)
    return value.strip()


def _read_group(value: str, position: int) -> tuple[str, int]:
    """Read one braced group or one TeX token from *position*."""
    if position >= len(value):
        return "", position
    if value[position] == "{":
        depth = 1
        cursor = position + 1
        start = cursor
        while cursor < len(value):
            if value[cursor] == "{":
                depth += 1
            elif value[cursor] == "}":
                depth -= 1
                if depth == 0:
                    return value[start:cursor], cursor + 1
            cursor += 1
        return value[start:], len(value)
    if value[position] == "\\":
        match = re.match(r"\\([A-Za-z]+|.)", value[position:])
        if match:
            return match.group(0), position + len(match.group(0))
    return value[position], position + 1


def _linear_formula_html(value: str) -> str:
    """Return a compact HTML/Unicode approximation for conversion failures."""
    output: list[str] = []
    cursor = 0
    while cursor < len(value):
        char = value[cursor]
        if char in "^_":
            group, cursor = _read_group(value, cursor + 1)
            tag = "sup" if char == "^" else "sub"
            output.append(f"<{tag}>{_linear_formula_html(group)}</{tag}>")
            continue
        if char == "{":
            group, cursor = _read_group(value, cursor)
            output.append(_linear_formula_html(group))
            continue
        if char == "}":
            cursor += 1
            continue
        if char == "~":
            output.append(" ")
            cursor += 1
            continue
        if char == "-":
            output.append("−")
            cursor += 1
            continue
        if char != "\\":
            output.append(html.escape(char))
            cursor += 1
            continue

        match = re.match(r"\\([A-Za-z]+|.)", value[cursor:])
        if not match:
            output.append("\\")
            cursor += 1
            continue
        command = match.group(1)
        cursor += len(match.group(0))
        if command in _SYMBOLS:
            output.append(html.escape(_SYMBOLS[command]))
            continue
        if command in _SPACING_COMMANDS:
            output.append(_SPACING_COMMANDS[command])
            continue
        if command in {"left", "right", "displaystyle", "textstyle"}:
            continue
        if command in {"frac", "dfrac", "tfrac"}:
            numerator, cursor = _read_group(value, cursor)
            denominator, cursor = _read_group(value, cursor)
            output.append(
                f"({_linear_formula_html(numerator)})/"
                f"({_linear_formula_html(denominator)})"
            )
            continue
        if command == "sqrt":
            radicand, cursor = _read_group(value, cursor)
            output.append(f"√({_linear_formula_html(radicand)})")
            continue
        if command in _TEXT_COMMANDS | _ITALIC_COMMANDS | _BOLD_COMMANDS:
            group, cursor = _read_group(value, cursor)
            converted = _linear_formula_html(group)
            if command in _ITALIC_COMMANDS:
                converted = f"<i>{converted}</i>"
            elif command in _BOLD_COMMANDS:
                converted = f"<b>{converted}</b>"
            output.append(converted)
            continue
        # Keep unknown commands visible and diagnosable instead of silently
        # deleting scientific content.
        output.append(html.escape(f"\\{command}"))
    return "".join(output)


def _fallback_html(tex: str, display: bool) -> str:
    content = _linear_formula_html(_normalise_tex(tex))
    display_style = (
        "display:block;text-align:center;margin:0.65em 0;"
        if display
        else "display:inline;white-space:nowrap;"
    )
    return (
        f'<span role="math" style="{display_style}font-family:Georgia,'
        "Times New Roman,serif;font-size:1em;color:inherit;line-height:normal;"
        f'vertical-align:baseline;">{content}</span>'
    )


def _mathml_html(tex: str, display: bool) -> str:
    value = _normalise_tex(tex)
    mathml = latex_to_mathml(value, display="block" if display else "inline")
    # latex2mathml preserves unknown commands as backslash text. Treat those
    # cases as conversion failures so the explicit HTML fallback is used.
    if "\\" in mathml:
        raise ValueError("Unsupported TeX command in generated MathML")

    fallback = _linear_formula_html(value)
    alt_text = html.unescape(re.sub(r"<[^>]+>", "", fallback))
    style = (
        "display:block;text-align:center;margin:0.65em 0;font-size:1em;"
        if display
        else "display:inline;font-size:1em;vertical-align:-0.08em;"
    )
    attributes = (
        f'alttext="{html.escape(alt_text, quote=True)}" '
        f'aria-label="{html.escape(alt_text, quote=True)}" '
        f'style="{style}color:inherit;line-height:normal;" '
    )
    return mathml.replace("<math ", f"<math {attributes}", 1)


def render_math_html(text: str | None) -> str:
    """Escape text and replace TeX groups with native MathML."""
    value = "" if text is None else str(text)
    result: list[str] = []
    cursor = 0
    for match in _MATH_DELIMITER_RE.finditer(value):
        result.append(html.escape(value[cursor:match.start()]))
        is_display = bool(match.group("display"))
        formula = (
            match.group("display_bracket")
            or match.group("display_dollar")
            or match.group("inline_paren")
            or match.group("inline_dollar")
            or ""
        )
        try:
            result.append(_mathml_html(formula, is_display))
        except Exception:
            result.append(_fallback_html(formula, is_display))
        cursor = match.end()
    result.append(html.escape(value[cursor:]))
    return "".join(result)
