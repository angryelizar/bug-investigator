"""Tools the model can call inside a working copy: search, read, and (Coder only) write."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fff import FileFinder, GrepMatch
from langchain_community.agent_toolkits import FileManagementToolkit
from langchain_core.tools import BaseTool, StructuredTool, tool

# fff-search 0.11 does not mark Java/Python definitions with `is_definition`,
# so a declaration-shaped line is recognised here as well.
_DECLARATION_KEYWORD = re.compile(
    r"\b(class|interface|enum|record|def|fn|func|function|struct|trait)\s+\w"
)
_METHOD_SIGNATURE = re.compile(r"^\s*(?:[\w<>\[\],.?@]+\s+)+\w+\s*\([^;]*$")
_NOT_A_SIGNATURE = re.compile(r"^\s*(return|new|throw|else|case)\b")


# Groq's free tier allows ~8 000 tokens per minute; one tool result must stay well below.
DEFAULT_MAX_OUTPUT_CHARS = 6_000


def build_tools(
    workdir: str | Path, *, can_write: bool, max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS
) -> list[BaseTool]:
    """Tools over one working copy; `can_write` is given to Coder only."""
    root = str(workdir)

    @contextmanager
    def fresh_index() -> Iterator[FileFinder]:
        # A new index per call: Coder's writes must be visible to the next search.
        with FileFinder(root, watch=False) as finder:
            finder.wait_for_scan_blocking(timeout_ms=10_000)
            yield finder

    @tool
    def grep(pattern: str, definitions_only: bool = False) -> str:
        """Ищет строку в файлах рабочей копии. Возвращает строки вида `путь:номер: текст`.

        definitions_only=true оставляет только объявления (классы, методы, функции),
        без мест вызова.
        """
        with fresh_index() as finder:
            result = finder.grep(pattern, classify_definitions=definitions_only)
        matches = result.items
        if definitions_only:
            matches = [m for m in matches if _is_definition(m)]
        if not matches:
            return f"По `{pattern}` ничего не найдено. Попробуй другое имя или часть слова."
        return "\n".join(
            f"{m.relative_path}:{m.line_number}: {m.line_content.strip()}" for m in matches
        )

    @tool
    def find_files(query: str) -> str:
        """Ищет файлы рабочей копии по части имени или пути (нечётко) либо по glob вроде
        `**/*Test.java`. Возвращает относительные пути, лучшие совпадения первыми.
        """
        with fresh_index() as finder:
            is_glob = any(c in query for c in "*?[")
            result = finder.glob(query) if is_glob else finder.search(query)
        if not result.items:
            return f"Файлов по `{query}` не найдено."
        return "\n".join(item.relative_path for item in result.items)

    file_tool_names = ["list_directory", "read_file"] + (["write_file"] if can_write else [])
    file_tools = FileManagementToolkit(root_dir=root, selected_tools=file_tool_names).get_tools()
    return [_limited(t, max_output_chars) for t in [grep, find_files, *file_tools]]


def _limited(inner: BaseTool, max_chars: int) -> BaseTool:
    def run(**kwargs) -> str:
        return _truncate(str(inner.invoke(kwargs)), max_chars)

    return StructuredTool.from_function(
        func=run,
        name=inner.name,
        description=inner.description,
        args_schema=inner.args_schema,
    )


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    notice = f"\n… [ответ обрезан: показано {{shown}} из {len(text)} символов; сузь запрос]"
    shown = max(0, max_chars - len(notice.format(shown=max_chars)))
    cut = text.rfind("\n", 0, shown)
    if cut > 0:
        shown = cut
    return text[:shown] + notice.format(shown=shown)


def _is_definition(match: GrepMatch) -> bool:
    line = match.line_content
    if match.is_definition or _DECLARATION_KEYWORD.search(line):
        return True
    return bool(_METHOD_SIGNATURE.match(line)) and not _NOT_A_SIGNATURE.match(line)
