"""Tree-sitter based code intelligence for transcript analysis.

Extracts structured symbols (functions, classes, imports) from Python code blocks
found in transcripts, producing summaries for LLM prompt injection and structured
metadata for Memory storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Graceful degradation if tree-sitter-python not installed
TREE_SITTER_AVAILABLE = False
_PARSER = None

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser

    TREE_SITTER_AVAILABLE = True
except ImportError:
    pass


@dataclass
class CodeBlock:
    source: str
    offset: int  # char offset in original text


@dataclass
class FunctionInfo:
    name: str
    params: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)
    line_range: tuple[int, int] = (0, 0)


@dataclass
class ClassInfo:
    name: str
    bases: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=list)


@dataclass
class ImportInfo:
    module: str
    names: list[str] = field(default_factory=list)


@dataclass
class ParsedCode:
    functions: list[FunctionInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)


# Regex matching all fenced code blocks, capturing optional language tag and content
_CODE_BLOCK_RE = re.compile(
    r"^```(\w*)\s*\n(.*?)^```\s*$",
    re.DOTALL | re.MULTILINE,
)

# Heuristics for detecting Python in untagged blocks
_PYTHON_HINTS = re.compile(
    r"^\s*(?:def |class |import |from\s+\S+\s+import )",
    re.MULTILINE,
)

_PYTHON_TAGS = {"python", "py"}


def extract_python_blocks(text: str) -> list[CodeBlock]:
    """Extract Python code blocks from transcript text."""
    blocks: list[CodeBlock] = []

    for m in _CODE_BLOCK_RE.finditer(text):
        lang = m.group(1).lower()
        src = m.group(2).strip()
        if not src:
            continue

        if lang in _PYTHON_TAGS:
            blocks.append(CodeBlock(source=src, offset=m.start()))
        elif not lang and _PYTHON_HINTS.search(src):
            # Untagged block with Python heuristics
            blocks.append(CodeBlock(source=src, offset=m.start()))

    return blocks


def _get_parser() -> Any:
    """Lazy-init tree-sitter parser singleton."""
    global _PARSER
    if _PARSER is None and TREE_SITTER_AVAILABLE:
        _PARSER = Parser(Language(tspython.language()))
    return _PARSER


def _node_text(node: Any, source_bytes: bytes) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def parse_python(source: str) -> ParsedCode | None:
    """Parse Python source with tree-sitter, extracting structured symbols."""
    if not TREE_SITTER_AVAILABLE:
        return None

    parser = _get_parser()
    if parser is None:
        return None

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    root = tree.root_node

    result = ParsedCode()

    for child in root.children:
        if child.type == "function_definition":
            result.functions.append(_extract_function(child, source_bytes))
        elif child.type == "class_definition":
            result.classes.append(_extract_class(child, source_bytes))
        elif child.type in ("import_statement", "import_from_statement"):
            imp = _extract_import(child, source_bytes)
            if imp:
                result.imports.append(imp)
        elif child.type == "decorated_definition":
            # Handle decorated functions/classes
            for sub in child.children:
                if sub.type == "function_definition":
                    func = _extract_function(sub, source_bytes)
                    func.decorators = _extract_decorators(child, source_bytes)
                    result.functions.append(func)
                elif sub.type == "class_definition":
                    cls = _extract_class(sub, source_bytes)
                    result.classes.append(cls)

    if not result.functions and not result.classes and not result.imports:
        return None

    return result


def _extract_function(node: Any, source_bytes: bytes) -> FunctionInfo:
    name = ""
    params: list[str] = []
    for child in node.children:
        if child.type == "identifier":
            name = _node_text(child, source_bytes)
        elif child.type == "parameters":
            for param in child.children:
                if param.type in ("identifier", "typed_parameter", "default_parameter"):
                    p_text = _node_text(param, source_bytes)
                    if p_text not in ("(", ")", ",", "self", "cls"):
                        params.append(p_text.split(":")[0].split("=")[0].strip())
    return FunctionInfo(
        name=name,
        params=params,
        line_range=(node.start_point[0], node.end_point[0]),
    )


def _extract_class(node: Any, source_bytes: bytes) -> ClassInfo:
    name = ""
    bases: list[str] = []
    methods: list[str] = []

    for child in node.children:
        if child.type == "identifier":
            name = _node_text(child, source_bytes)
        elif child.type == "argument_list":
            for arg in child.children:
                if arg.type in ("identifier", "attribute"):
                    bases.append(_node_text(arg, source_bytes))
        elif child.type == "block":
            for stmt in child.children:
                fn_node = stmt
                if stmt.type == "decorated_definition":
                    for sub in stmt.children:
                        if sub.type == "function_definition":
                            fn_node = sub
                            break
                if fn_node.type == "function_definition":
                    for sub in fn_node.children:
                        if sub.type == "identifier":
                            methods.append(_node_text(sub, source_bytes))
                            break

    return ClassInfo(name=name, bases=bases, methods=methods)


def _extract_import(node: Any, source_bytes: bytes) -> ImportInfo | None:
    text = _node_text(node, source_bytes)
    if node.type == "import_from_statement":
        # from X import Y, Z
        module = ""
        names: list[str] = []
        found_from = False
        found_import = False
        for child in node.children:
            if child.type == "from":
                found_from = True
            elif found_from and not found_import and child.type in (
                "dotted_name",
                "relative_import",
            ):
                module = _node_text(child, source_bytes)
            elif child.type == "import":
                found_import = True
            elif found_import and child.type in ("dotted_name", "aliased_import"):
                names.append(_node_text(child, source_bytes).split(" as ")[0])
        return ImportInfo(module=module, names=names) if module else None
    elif node.type == "import_statement":
        # import X, Y
        names = []
        found_import = False
        for child in node.children:
            if child.type == "import":
                found_import = True
            elif found_import and child.type in ("dotted_name", "aliased_import"):
                names.append(_node_text(child, source_bytes).split(" as ")[0])
        if names:
            return ImportInfo(module=names[0], names=names)
    return None


def _extract_decorators(node: Any, source_bytes: bytes) -> list[str]:
    decorators = []
    for child in node.children:
        if child.type == "decorator":
            # Skip the '@' character
            dec_text = _node_text(child, source_bytes).lstrip("@").strip()
            decorators.append(dec_text)
    return decorators


_MAX_SUMMARY_CHARS = 2000
_MAX_SYMBOLS = 50


def summarize_code_blocks(blocks: list[tuple[CodeBlock, ParsedCode]]) -> str:
    """Produce a concise one-line-per-symbol summary for prompt injection."""
    if not blocks:
        return ""

    lines: list[str] = []
    symbol_count = 0

    for _block, parsed in blocks:
        for func in parsed.functions:
            if symbol_count >= _MAX_SYMBOLS:
                break
            params_str = ", ".join(func.params) if func.params else ""
            dec_str = f" [{', '.join(func.decorators)}]" if func.decorators else ""
            lines.append(f"- Function: {func.name}({params_str}){dec_str}")
            symbol_count += 1

        for cls in parsed.classes:
            if symbol_count >= _MAX_SYMBOLS:
                break
            bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
            methods_str = f" (methods: {', '.join(cls.methods)})" if cls.methods else ""
            lines.append(f"- Class: {cls.name}{bases_str}{methods_str}")
            symbol_count += 1

        # Collect all imports into one line per block
        all_imports: list[str] = []
        for imp in parsed.imports:
            if imp.names and imp.names != [imp.module]:
                all_imports.extend(imp.names)
            else:
                all_imports.append(imp.module)
        if all_imports and symbol_count < _MAX_SYMBOLS:
            lines.append(f"- Imports: {', '.join(all_imports)}")
            symbol_count += len(all_imports)

    if not lines:
        return ""

    summary = "Code structure found in transcript:\n" + "\n".join(lines)
    if len(summary) > _MAX_SUMMARY_CHARS:
        summary = summary[:_MAX_SUMMARY_CHARS] + "\n... (truncated)"
    return summary


def build_code_metadata(blocks: list[tuple[CodeBlock, ParsedCode]]) -> dict:
    """Build structured metadata dict for Memory.metadata."""
    if not blocks:
        return {}

    functions: list[str] = []
    classes: list[str] = []
    imports: list[str] = []

    for _block, parsed in blocks:
        for func in parsed.functions:
            functions.append(func.name)
        for cls in parsed.classes:
            classes.append(cls.name)
        for imp in parsed.imports:
            if imp.names and imp.names != [imp.module]:
                imports.extend(imp.names)
            else:
                imports.append(imp.module)

    return {
        "functions": functions,
        "classes": classes,
        "imports": imports,
        "language": "python",
    }
