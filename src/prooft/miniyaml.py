"""Restricted YAML: just enough for ``ci-attestations.yml``, standard library only.

Allowed: block mappings, block sequences, flow ``{k: v}`` / ``[a, b]`` of
scalars, ``#`` comments, and int / float / bool / null / string scalars
(optionally quoted). Rejected: anchors, aliases, tags, merge keys, block
scalars and multiple documents. A bare developer host and a CI runner
therefore parse the file identically with nothing installed (ci.yml GATE-010).
"""

from __future__ import annotations

from dataclasses import dataclass
import re


class MiniYamlError(ValueError):
    """The text uses YAML outside the supported subset."""


@dataclass
class _Line:
    number: int
    indent: int
    text: str


_FORBIDDEN = re.compile(r"(^|[\s\[{,])[&*!][^\s]|^\s*<<\s*:|:\s*[|>][-+]?\s*$")


def parse(text: str) -> object:
    lines = _lines(text)
    if not lines:
        return {}
    value, rest = _block(lines, 0, lines[0].indent)
    if rest != len(lines):
        raise MiniYamlError(f"line {lines[rest].number}: unexpected indentation")
    return value


def _strip_comment(raw: str) -> str:
    out: list[str] = []
    quote = ""
    for index, char in enumerate(raw):
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
        elif char == "#" and (index == 0 or raw[index - 1].isspace()):
            break
        out.append(char)
    return "".join(out).rstrip()


def _lines(text: str) -> list[_Line]:
    out: list[_Line] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise MiniYamlError(f"line {number}: tabs are not allowed for indentation")
        body = _strip_comment(raw)
        if not body.strip():
            continue
        stripped = body.strip()
        if stripped in ("---", "...") or stripped.startswith("%"):
            raise MiniYamlError(f"line {number}: document markers and directives are not supported")
        if _FORBIDDEN.search(body):
            raise MiniYamlError(
                f"line {number}: anchors, aliases, tags, merge keys and block scalars are not supported"
            )
        out.append(_Line(number, len(body) - len(body.lstrip(" ")), stripped))
    return out


def _block(lines: list[_Line], start: int, indent: int) -> tuple[object, int]:
    if lines[start].text.startswith("- ") or lines[start].text == "-":
        return _sequence(lines, start, indent)
    return _mapping(lines, start, indent)


def _sequence(lines: list[_Line], start: int, indent: int) -> tuple[list[object], int]:
    items: list[object] = []
    index = start
    while index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("-"):
        rest = lines[index].text[1:].strip()
        index += 1
        if rest:
            items.append(_scalar_or_flow(rest, lines[index - 1].number))
        elif index < len(lines) and lines[index].indent > indent:
            value, index = _block(lines, index, lines[index].indent)
            items.append(value)
        else:
            items.append(None)
    return items, index


def _mapping(lines: list[_Line], start: int, indent: int) -> tuple[dict[str, object], int]:
    out: dict[str, object] = {}
    index = start
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        key, sep, rest = _split_key(line.text)
        if not sep:
            raise MiniYamlError(f"line {line.number}: expected 'key: value'")
        if key in out:
            raise MiniYamlError(f"line {line.number}: duplicate key {key!r}")
        index += 1
        if rest:
            out[key] = _scalar_or_flow(rest, line.number)
        elif index < len(lines) and lines[index].indent > indent:
            out[key], index = _block(lines, index, lines[index].indent)
        elif index < len(lines) and lines[index].indent == indent and lines[index].text.startswith("- "):
            out[key], index = _sequence(lines, index, indent)
        else:
            out[key] = None
    if index < len(lines) and lines[index].indent > indent:
        raise MiniYamlError(f"line {lines[index].number}: unexpected indentation")
    return out, index


def _split_key(text: str) -> tuple[str, bool, str]:
    if text[:1] in "'\"":
        end = text.find(text[0], 1)
        if end < 0 or text[end + 1 : end + 2] != ":":
            return text, False, ""
        return text[1:end], True, text[end + 2 :].strip()
    match = re.match(r"^([^{}\[\],]+?):(\s+|$)(.*)$", text)
    if not match:
        return text, False, ""
    return match.group(1).strip(), True, match.group(3).strip()


def _scalar_or_flow(text: str, number: int) -> object:
    if text.startswith("{") or text.startswith("["):
        value, end = _flow(text, 0, number)
        if text[end:].strip():
            raise MiniYamlError(f"line {number}: trailing text after flow collection")
        return value
    return _scalar(text)


def _flow(text: str, pos: int, number: int) -> tuple[object, int]:  # noqa: C901 - one small tokenizer loop
    opener = text[pos]
    closer = "}" if opener == "{" else "]"
    pos += 1
    mapping: dict[str, object] = {}
    sequence: list[object] = []
    while True:
        while pos < len(text) and text[pos] == " ":
            pos += 1
        if pos >= len(text):
            raise MiniYamlError(f"line {number}: unterminated flow collection")
        if text[pos] == closer:
            return (mapping if opener == "{" else sequence), pos + 1
        if text[pos] in "{[":
            raise MiniYamlError(f"line {number}: nested flow collections are not supported")
        end = pos
        quote = ""
        while end < len(text):
            char = text[end]
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"":
                quote = char
            elif char in ",}]":
                break
            end += 1
        item = text[pos:end].strip()
        if opener == "{":
            key, sep, rest = _split_key(item)
            if not sep:
                raise MiniYamlError(f"line {number}: expected 'key: value' in flow mapping")
            mapping[key] = _scalar(rest) if rest else None
        else:
            sequence.append(_scalar(item))
        pos = end
        if pos < len(text) and text[pos] == ",":
            pos += 1


def _scalar(text: str) -> object:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("null", "~", ""):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d*", text):
        return float(text)
    return text
