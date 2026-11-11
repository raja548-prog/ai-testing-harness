"""Source signatures for the `code` block: declarations without bodies, so new tests call real methods.

Deterministic and dependency-free:
- `generic_signatures`: declaration lines in any language (class, interface, enum, record, struct, def,
  fun, func, function; public or protected methods) with the annotations above them.
- `java_signatures`: a small brace-aware scanner. It joins declarations written over several lines and
  handles generics, annotations, records, enums and nested types. It keeps public and protected members
  (all members of an interface), and the fields of model classes (requests, responses, DTOs, records).
  If it cannot make sense of a file, `signatures` falls back to the generic extractor and says so.
A Java parser library, if one is approved later, can replace `java_signatures` behind `signatures`.
"""
from __future__ import annotations

import re

_TYPE_KW = re.compile(r"(?:^|\s)(class|interface|enum|record|@interface)\s+([A-Za-z_$][\w$]*)")
_ANNOT = re.compile(r"@[\w.$]+(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*")
_MODEL_NAME = re.compile(r"(?:Request|Response|Dto|DTO|Model|Entity|Event|Message|Payload|Record|Data|Vo|VO)$")
_CONTROL = {"if", "for", "while", "switch", "catch", "synchronized", "try", "else", "do", "return", "new", "throw"}


class _Unparsed(ValueError):
    pass


def _strip_comments(src: str) -> str:
    """Drop comments; keep string literals, minus the braces and semicolons that would confuse the scan."""
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            if j < 0:
                raise _Unparsed("unclosed comment")
            out.append(" ")
            i = j + 2
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            if j < 0:
                raise _Unparsed("unclosed text block")
            out.append('"…"')
            i = j + 3
        elif c in "\"'":
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 1
                if j < n and src[j] == "\n":
                    raise _Unparsed("unclosed string")
                j += 1
            if j >= n:
                raise _Unparsed("unclosed string")
            out.append(re.sub(r"[{};]", " ", src[i:j + 1]))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _without_annotations(decl: str) -> str:
    return _ANNOT.sub("", decl).strip()


def _is_method(decl: str) -> bool:
    bare = _without_annotations(decl)
    if "(" not in bare or "=" in bare.split("(", 1)[0]:
        return False
    head = bare.split("(", 1)[0].split()
    return bool(head) and head[0] not in _CONTROL and re.fullmatch(r"[A-Za-z_$][\w$]*", head[-1]) is not None


def _visible(decl: str, parent: dict) -> bool:
    """Public or protected; in an interface, anything not private."""
    words = set(re.findall(r"\b\w+\b", _without_annotations(decl).split("(", 1)[0]))
    if "private" in words:
        return False
    if parent["kind"] in ("interface", "@interface"):
        return True
    return bool(words & {"public", "protected"})


def java_signatures(src: str) -> str:
    text = _strip_comments(src)
    out: list[str] = []
    stack: list[dict] = []  # contexts: {"kind": type kind | "body" | "init", "name", "model", "enum_head"}
    buf: list[str] = []

    def type_parent() -> dict | None:
        return stack[-1] if stack and stack[-1]["kind"] not in ("body", "init") else None

    def indent() -> str:
        return "  " * sum(1 for s in stack if s["kind"] not in ("body", "init"))

    def emit_member(decl: str) -> None:
        parent = type_parent()
        if parent is None or not decl or not parent["shown"]:
            return
        if parent.get("enum_head"):  # enum constants come first, up to `;` or the closing brace
            parent["enum_head"] = False
            names = [re.match(r"\s*([A-Za-z_$][\w$]*)", c) for c in _split_top(decl)]
            if consts := [m.group(1) for m in names if m]:
                out.append(f"{indent()}{', '.join(consts)};")
            return
        if _is_method(decl):
            if _visible(decl, parent):
                out.append(f"{indent()}{decl};")
        elif parent["model"] and not re.search(r"\bstatic\b", _without_annotations(decl)):
            out.append(f"{indent()}{decl.split('=', 1)[0].strip()};")

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        top = stack[-1] if stack else None
        if top and top["kind"] == "body":  # inside a method body or initializer: only count braces
            if c == "{":
                stack.append({"kind": "body"})
            elif c == "}":
                stack.pop()
            i += 1
            continue
        if c == "{":
            decl = _norm("".join(buf))
            m = _TYPE_KW.search(_without_annotations(decl)) if decl else None
            if top and top["kind"] == "init":
                stack.append({"kind": "init"})
                buf.append(c)
            elif m and not _is_method(decl.split(m.group(2), 1)[0] + m.group(2)):
                kind, name = m.group(1), m.group(2)
                parent = type_parent()
                if parent is None and stack:
                    raise _Unparsed("type inside a body")
                shown = parent is None or (parent["shown"] and _visible(decl, parent))
                if shown:
                    out.append(f"{indent()}{decl} {{")
                model = kind == "record" or bool(_MODEL_NAME.search(name))
                stack.append({"kind": kind, "name": name, "model": model, "enum_head": kind == "enum",
                              "shown": shown})
                buf = []
            elif type_parent() and ("=" in decl.split("(", 1)[0] or decl.endswith("=") or "new " in decl):
                stack.append({"kind": "init"})  # array initializer or anonymous class in a field
                buf.append(c)
            elif type_parent() and type_parent().get("enum_head") and decl:
                stack.append({"kind": "init"})  # enum constant with a body: `A { ... }`
                buf.append(c)
            else:
                if _is_method(decl):
                    emit_member(decl)
                stack.append({"kind": "body"})
                buf = []
            i += 1
            continue
        if c == "}":
            if top and top["kind"] == "init":
                stack.pop()
                buf.append(c)
                i += 1
                continue
            if not stack:
                raise _Unparsed("unbalanced braces")
            emit_member(_norm("".join(buf)))
            buf = []
            if stack.pop()["shown"]:
                out.append(f"{indent()}}}")
            i += 1
            continue
        if c == ";" and not (top and top["kind"] == "init"):
            decl = _norm("".join(buf))
            buf = []
            if stack:
                emit_member(decl)
            elif decl.startswith("package "):
                out.append(decl + ";")
            i += 1
            continue
        buf.append(c)
        i += 1
    if stack:
        raise _Unparsed("unbalanced braces")
    return "\n".join(out)


def _split_top(text: str) -> list[str]:
    """Split on commas outside parentheses and angle brackets."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch in "(<[{":
            depth += 1
        elif ch in ")>]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


_GENERIC_DECL = re.compile(
    r"^\s*(?:(?:public|protected|export|default|abstract|static|final|sealed|open|data|async|override|internal|"
    r"inline|suspend|pub)\s+)*(?:class|interface|enum|record|struct|trait|object|def|fun|func|function|fn|type)\b"
    r"|^\s*(?:public|protected)\s+[^=;]*\(")
_GENERIC_ANNOT = re.compile(r"^\s*@[\w.]+")


def generic_signatures(src: str) -> str:
    out, annots = [], []
    for line in src.splitlines():
        if _GENERIC_ANNOT.match(line) and not _GENERIC_DECL.match(line):
            annots.append(line.strip())
            continue
        if _GENERIC_DECL.match(line):
            decl = re.sub(r"\s*\{.*$", "", line.rstrip()).rstrip(":")
            out += annots + [decl]
        annots = []
    return "\n".join(out)


def signatures(path: str, src: str) -> tuple[str, str | None]:
    """(signatures, note). The note says when the Java extractor fell back to the generic one."""
    if path.endswith(".java"):
        try:
            return java_signatures(src), None
        except _Unparsed as e:
            return generic_signatures(src), f"{path}: Java extractor could not parse it ({e}); declaration lines only"
    return generic_signatures(src), None
