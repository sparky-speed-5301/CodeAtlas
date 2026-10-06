"""JavaScript repair adapter with explicit static-parser limitations."""

from codeatlas.adapters.base import BaseLanguageAdapter, LanguageParseResult
from codeatlas.repository.symbols import extract_ts_js_symbols


def _delimiter_error(source: str) -> str | None:
    """Conservative delimiter check without executing a JS/TS toolchain."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    closing = set(pairs.values())
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = 0
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            index += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            index += 2
            continue
        # Regex literals may contain delimiters that are not JS syntax. This
        # remains a partial check, never a compiler-success assertion.
        before = source[:index].rstrip() if char == "/" else ""
        if char == "/" and (not before or before[-1] in "=([{,:;!?&|" or before.endswith(("return", "case"))):
            index += 1
            in_class = False
            regex_escaped = False
            while index < len(source):
                regex_char = source[index]
                if regex_escaped:
                    regex_escaped = False
                elif regex_char == "\\":
                    regex_escaped = True
                elif regex_char == "[":
                    in_class = True
                elif regex_char == "]":
                    in_class = False
                elif regex_char == "/" and not in_class:
                    index += 1
                    break
                elif regex_char == "\n":
                    return "Unterminated JavaScript/TypeScript regex literal"
                index += 1
            else:
                return "Unterminated JavaScript/TypeScript regex literal"
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in closing:
            if not stack or stack.pop() != char:
                return f"Unmatched JavaScript/TypeScript delimiter '{char}'"
        index += 1
    if quote is not None:
        return "Unterminated JavaScript/TypeScript string"
    if block_comment:
        return "Unterminated JavaScript/TypeScript block comment"
    if stack:
        return f"Unclosed JavaScript/TypeScript delimiter '{stack[-1]}'"
    return None


class JavaScriptAdapter(BaseLanguageAdapter):
    language = "javascript"

    def parse_modified_file(self, path: str, source: str) -> LanguageParseResult:
        symbols, diagnostics = extract_ts_js_symbols(path, source)
        delimiter_error = _delimiter_error(source)
        errors = tuple(
            [f"Static symbol parse error at line {d.line}" for d in diagnostics if d.level == "error"]
            + ([delimiter_error] if delimiter_error else [])
        )
        return LanguageParseResult(
            symbols=tuple(symbols), syntax_valid=False if errors else None, errors=errors,
            limitations=("JavaScript/TypeScript static symbol parsing is partial; compiler syntax and type checks have not run.",),
        )


__all__ = ["JavaScriptAdapter"]
