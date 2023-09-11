import logging
import os
import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import DefaultDict, Dict, Set, cast

from ipdb import set_trace
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory, Suggestion
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.completion.word_completer import WordCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from pygments.lexer import Lexer
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from pygments.util import ClassNotFound

from .code_context import CodeContext
from .commands import AddCommand, Command, RemoveCommand
from .config_manager import mentat_dir_path

logger = logging.getLogger()


class FilteredFileHistory(FileHistory):
    def __init__(self, filename: str):
        self.excluded_phrases = ["y", "n", "i", "q"]
        super().__init__(filename)

    def append_string(self, string):
        if (
            string.strip().lower() not in self.excluded_phrases
            # If the user mistypes a command, we don't want it to appear later
            and string.strip()
            and string.strip()[0] != "/"
        ):
            super().append_string(string)


class FilteredHistorySuggestions(AutoSuggestFromHistory):
    def __init__(self):
        super().__init__()

    def get_suggestion(self, buffer: Buffer, document: Document) -> Suggestion | None:
        # We want the auto completer to handle commands instead of the suggester
        if buffer.text[0] == "/":
            return None
        else:
            return super().get_suggestion(buffer, document)


@dataclass
class SyntaxCompletion:
    words: Set[str]
    created_at: datetime = datetime.utcnow()


class CommandCompleter(Completer):
    def __init__(self):
        self.completer = WordCompleter(
            words=Command.get_command_completions(),
            ignore_case=True,
            sentence=True,
        )

    def get_completions(self, document, complete_event):
        command_completions = self.completer.get_completions(document, complete_event)
        for completion in command_completions:
            yield completion


class SyntaxCompleter(Completer):
    def __init__(self, code_context: CodeContext):
        self.code_context = code_context

        self.syntax_completions: Dict[Path, SyntaxCompletion] = dict()
        self.file_name_completions: DefaultDict[str, Set[Path]] = defaultdict(set)

        self._all_syntax_words: Set[str]
        self._last_refresh_at: datetime

        self.refresh_completions()

    def refresh_completions_for_file_path(self, file_path: Path):
        """Add/edit/delete completions for some filepath"""
        try:
            with open(file_path, "r") as f:
                file_content = f.read()
        except (FileNotFoundError, NotADirectoryError):
            logging.debug(f"Skipping {file_path}. Reason: file not found")
            return
        try:
            lexer = guess_lexer_for_filename(file_path, file_content)
            lexer = cast(Lexer, lexer)
        except ClassNotFound:
            logging.debug(f"Skipping {file_path}. Reason: lexer not found")
            return

        self.file_name_completions[file_path.name].add(file_path)

        tokens = list(lexer.get_tokens(file_content))
        filtered_tokens = set()
        for token_type, token_value in tokens:
            if token_type not in Token.Name:
                continue
            if len(token_value) <= 1:
                continue
            filtered_tokens.add(token_value)
        self.syntax_completions[file_path] = SyntaxCompletion(words=filtered_tokens)

    def refresh_completions(self):
        # Remove syntax completions for files not in the context
        for file_path in set(self.syntax_completions.keys()):
            if file_path not in self.code_context.files:
                del self.syntax_completions[file_path]
                file_name = file_path.name
                self.file_name_completions[file_name].remove(file_path)
                if len(self.file_name_completions[file_name]) == 0:
                    del self.file_name_completions[file_name]

        # Add/update syntax completions for files in the context
        for file_path in self.code_context.files:
            if file_path not in self.syntax_completions:
                self.refresh_completions_for_file_path(file_path)
            else:
                modified_at = datetime.utcfromtimestamp(os.path.getmtime(file_path))
                if self.syntax_completions[file_path].created_at < modified_at:
                    self.refresh_completions_for_file_path(file_path)

        # Build de-duped syntax completions
        _all_syntax_words = set()
        for syntax_completion in self.syntax_completions.values():
            _all_syntax_words.update(syntax_completion.words)
        self._all_syntax_words = _all_syntax_words

        self._last_refresh_at = datetime.utcnow()

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        if (datetime.utcnow() - self._last_refresh_at).seconds > 5:
            self.refresh_completions()

        document_words = document.text_before_cursor.split()
        if not document_words:
            return

        last_word = document_words[-1]
        get_completion_insert = lambda word: f"`{word}`"
        if last_word[0] == "`" and len(last_word) > 1:
            last_word = last_word.lstrip("`")
            get_completion_insert = lambda word: f"{word}`"

        completions = self._all_syntax_words.union(set(self.file_name_completions))

        for completion in completions:
            if completion.lower().startswith(last_word.lower()):
                file_names = self.file_name_completions.get(completion)
                if file_names:
                    for file_name in file_names:
                        yield Completion(
                            get_completion_insert(file_name),
                            start_position=-len(last_word),
                            display=str(file_name),
                        )
                else:
                    yield Completion(
                        get_completion_insert(completion),
                        start_position=-len(last_word),
                        display=completion,
                    )


class MentatCompleter(Completer):
    def __init__(self, code_context: CodeContext):
        self.command_completer = CommandCompleter()
        self.syntax_completer = SyntaxCompleter(code_context)

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        if document.text_before_cursor == "" or document.text_before_cursor[-1] == " ":
            return []
        if document.text_before_cursor[0] == "/":
            return self.command_completer.get_completions(document, complete_event)
        else:
            return self.syntax_completer.get_completions(document, complete_event)


class MentatPromptSession(PromptSession):
    def __init__(self, code_context: CodeContext, *args, **kwargs):
        self.code_context = code_context

        self._setup_bindings()
        super().__init__(
            completer=MentatCompleter(self.code_context),
            history=FilteredFileHistory(mentat_dir_path / "history"),
            auto_suggest=FilteredHistorySuggestions(),
            multiline=True,
            prompt_continuation=self.prompt_continuation,
            key_bindings=self.bindings,
            *args,
            **kwargs,
        )

    def prompt(self, *args, **kwargs):
        # Automatically capture all commands
        while (user_input := super().prompt(*args, **kwargs)).startswith("/"):
            arguments = shlex.split(user_input[1:])
            command = Command.create_command(arguments[0])
            if isinstance(command, (AddCommand, RemoveCommand)):
                command.apply(*arguments[1:], code_context=self.code_context)
            else:
                command.apply(*arguments[1:])
        return user_input

    def prompt_continuation(self, width, line_number, is_soft_wrap):
        return (
            "" if is_soft_wrap else [("class:continuation", " " * (width - 2) + "> ")]
        )

    def _setup_bindings(self):
        self.bindings = KeyBindings()

        @self.bindings.add("s-down")
        @self.bindings.add("c-j")
        def _(event: KeyPressEvent):
            event.current_buffer.insert_text("\n")

        @self.bindings.add("enter")
        def _(event: KeyPressEvent):
            event.current_buffer.validate_and_handle()

        @Condition
        def complete_suggestion() -> bool:
            app = get_app()
            return (
                app.current_buffer.suggestion is not None
                and len(app.current_buffer.suggestion.text) > 0
                and app.current_buffer.document.is_cursor_at_the_end
                and app.current_buffer.text
                and app.current_buffer.text[0] != "/"
            )

        @self.bindings.add("right", filter=complete_suggestion)
        def _(event: KeyPressEvent):
            suggestion = event.current_buffer.suggestion
            if suggestion:
                event.current_buffer.insert_text(suggestion.text)

        @self.bindings.add("c-c")
        def _(event: KeyPressEvent):
            if event.current_buffer.text != "":
                event.current_buffer.reset()
            else:
                event.app.exit(result="q")
