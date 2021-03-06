"""
Data structures for the Buffer.
It holds the text, cursor position, history, etc...
"""
from __future__ import unicode_literals

from .completion import Completion, CompleteEvent
from .document import Document
from .enums import IncrementalSearchDirection
from .history import History
from .selection import SelectionType, SelectionState
from .utils import EventHook
from .validation import ValidationError
from .clipboard import ClipboardData
from .filters import Filter, Never
from .search_state import SearchState

import os
import six
import subprocess
import tempfile

__all__ = (
    'AcceptAction',
    'Buffer',
    'indent',
    'unindent',
)


class AcceptAction(object):
    """
    What to do when the input is accepted by the user.
    (When Enter was pressed in the command line.)

    :param handler: (optional) A callable which accepts a `Document' that is
                    called when the user accepts input.
    :param return_document: Set this return value. The eventloop will
                                   deliver it to the application that called
                                   the event loop.
    """
    def __init__(self, handler=None, return_document=False):
        assert not (handler and return_document)  # We cannot have both.
        assert handler is None or callable(handler)

        self.handler = handler
        self.return_document = return_document

    @property
    def is_returnable(self):
        """
        True when there is something handling accept.
        """
        return bool(self.handler or self.return_document)

    def validate_and_handle(self, cli, buffer):
        """
        Validate buffer and handle the accept action.
        """
        if buffer.validate():
            if self.return_document:
                cli.set_return_value(buffer.document)
            elif self.handler:
                self.handler(cli, buffer)

            buffer.add_to_history()

AcceptAction.RETURN_DOCUMENT = AcceptAction(return_document=True)
AcceptAction.IGNORE = AcceptAction(handler=None, return_document=False)


class CompletionState(object):
    """
    Immutable class that contains a completion state.
    """
    def __init__(self, original_document, current_completions=None, complete_index=None):
        #: Document as it was when the completion started.
        self.original_document = original_document

        #: List of all the current Completion instances which are possible at
        #: this point.
        self.current_completions = current_completions or []

        #: Position in the `current_completions` array.
        #: This can be `None` to indicate "no completion", the original text.
        self.complete_index = complete_index  # Position in the `_completions` array.

    def __repr__(self):
        return '%s(%r, <%r> completions, index=%r)' % (
            self.__class__.__name__,
            self.original_document, len(self.current_completions), self.complete_index)

    def go_to_index(self, index):
        """
        Create a new CompletionState object with the new index.
        """
        return CompletionState(self.original_document, self.current_completions, complete_index=index)

    def new_text_and_position(self):
        """
        Return (new_text, new_cursor_position) for this completion.
        """
        if self.complete_index is None:
            return self.original_document.text, self.original_document.cursor_position
        else:
            original_text_before_cursor = self.original_document.text_before_cursor
            original_text_after_cursor = self.original_document.text_after_cursor

            c = self.current_completions[self.complete_index]
            if c.start_position == 0:
                before = original_text_before_cursor
            else:
                before = original_text_before_cursor[:c.start_position]

            new_text = before + c.text + original_text_after_cursor
            new_cursor_position = len(before) + len(c.text)
            return new_text, new_cursor_position


class Buffer(object):
    """
    The core data structure that holds the text and cursor position of the
    current input line and implements all text manupulations on top of it. It
    also implements the history, undo stack and the completion state.

    :attr completer : :class:`~prompt_toolkit.completion.Completer` instance.
    :attr history: :class:`~prompt_toolkit.history.History` instance.
    :attr callbacks: :class:`~.Callbacks` instance.

    :attr tempfile_suffix: Suffix to be appended to the tempfile for the 'open
                           in editor' function.
    :attr is_multiline: Filter to indicate whether we should consider this
                        buffer a multiline input. If so, key bindings can
                        decide to insert newlines when pressing [Enter].
                        (Instead of accepting the input.)
    """
    def __init__(self, completer=None, history=None, validator=None, tempfile_suffix='',
                 is_multiline=Never(), initial_document=None,
                 accept_action=AcceptAction.RETURN_DOCUMENT):
        assert isinstance(is_multiline, Filter)

        self.completer = completer
        self.validator = validator
        self.tempfile_suffix = tempfile_suffix
        self.accept_action = accept_action
        self.is_multiline = is_multiline

        #: The command buffer history.
        # Note that we shouldn't use a lazy 'or' here. bool(history) could be
        # False when empty.
        self._history = History() if history is None else history

        self.__cursor_position = 0

        # Events
        self.onTextChanged = EventHook()
        self.onTextInsert = EventHook()
        self.onCursorPositionChanged = EventHook()

        self.reset(initial_document=initial_document)

    def reset(self, initial_document=None, append_to_history=False):
        """
        :param append_to_history: Append current input to history first.
        """
        assert initial_document is None or isinstance(initial_document, Document)

        if append_to_history:
            self.add_to_history()

        initial_document = initial_document or Document()

        self.cursor_position = initial_document.cursor_position

        # `ValidationError` instance. (Will be set when the input is wrong.)
        self.validation_error = None

        # State of the selection.
        self.selection_state = None

        # State of complete browser
        self.complete_state = None  # For interactive completion through Ctrl-N/Ctrl-P.

        # Undo/redo stacks
        self._undo_stack = []  # Stack of (text, cursor_position)
        self._redo_stack = []

        #: The working lines. Similar to history, except that this can be
        #: modified. The user can press arrow_up and edit previous entries.
        #: Ctrl-C should reset this, and copy the whole history back in here.
        #: Enter should process the current command and append to the real
        #: history.
        self._working_lines = self._history.strings[:]
        self._working_lines.append(initial_document.text)
        self.__working_index = len(self._working_lines) - 1

    # <getters/setters>

    @property
    def text(self):
        return self._working_lines[self.working_index]

    @text.setter
    def text(self, value):
        assert isinstance(value, six.text_type), 'Got %r' % value
        original_value = self._working_lines[self.working_index]
        self._working_lines[self.working_index] = value

        if value != original_value:
            self._text_changed()

    @property
    def cursor_position(self):
        return self.__cursor_position

    @cursor_position.setter
    def cursor_position(self, value):
        original_position = self.__cursor_position
        self.__cursor_position = max(0, value)

        if value != original_position:
            # Remove any validation errors and complete state.
            self.validation_error = None
            self.complete_state = None

            # Note that the cursor position can change if we have a selection the
            # new position of the cursor determines the end of the selection.

            # fire 'onCursorPositionChanged' event.
            self.onCursorPositionChanged.fire()

    @property
    def working_index(self):
        return self.__working_index

    @working_index.setter
    def working_index(self, value):
        if self.__working_index != value:
            self.__working_index = value
            self._text_changed()

    def _text_changed(self):
        # Remove any validation errors and complete state.
        self.validation_error = None
        self.complete_state = None
        self.selection_state = None

        # fire 'onTextChanged' event.
        self.onTextChanged.fire()

    # End of <getters/setters>

    @property
    def document(self):
        """
        Return :class:`Document` instance from the current text and cursor
        position.
        """
        return Document(self.text, self.cursor_position, selection=self.selection_state)

    @document.setter
    def document(self, value):
        """
        Set :class:`Document` instance.
        """
        assert isinstance(value, Document)
        self.cursor_position = value.cursor_position
        self.text = value.text

    def save_to_undo_stack(self, clear_redo_stack=True):
        """
        Safe current state (input text and cursor position), so that we can
        restore it by calling undo.
        """
        # Safe if the text is different from the text at the top of the stack
        # is different. If the text is the same, just update the cursor position.
        if self._undo_stack and self._undo_stack[-1][0] == self.text:
            self._undo_stack[-1] = (self._undo_stack[-1][0], self.cursor_position)
        else:
            self._undo_stack.append((self.text, self.cursor_position))

        # Saving anything to the undo stack, clears the redo stack.
        if clear_redo_stack:
            self._redo_stack = []

    def transform_lines(self, line_index_iterator, transform_callback):
        """
        Transforms the text on a range of lines.
        When the iterator yield an index not in the range of lines that the
        document contains, it skips them silently.

        To uppercase some lines::

            transform_lines(range(5,10), lambda text: text.upper())

        :param line_index_iterator: Iterator of line numbers (int)
        :param transform_callback: callable that takes the original text of a
                                   line, and return the new text for this line.
        """
        # Split lines
        lines = self.text.split('\n')

        # Apply transformation
        for index in line_index_iterator:
            try:
                lines[index] = transform_callback(lines[index])
            except IndexError:
                pass

        self.text = '\n'.join(lines)

    def transform_region(self, from_, to, transform_callback):
        """
        Transform a part of the input string.

        :param :from_: (int) start position.
        :param :to: (int) end position.
        :param :transform_callback: Callable which accepts a string and returns
                                    the transformed string.
        """
        assert from_ < to

        self.text = ''.join([
            self.text[:from_] +
            transform_callback(self.text[from_:to]) +
            self.text[to:]
        ])

    def cursor_left(self, count=1):
        self.cursor_position += self.document.get_cursor_left_position(count=count)

    def cursor_right(self, count=1):
        self.cursor_position += self.document.get_cursor_right_position(count=count)

    def cursor_up(self, count=1):
        """ (for multiline edit). Move cursor to the previous line.  """
        self.cursor_position += self.document.get_cursor_up_position(count=count)

    def cursor_down(self, count=1):
        """ (for multiline edit). Move cursor to the next line.  """
        self.cursor_position += self.document.get_cursor_down_position(count=count)

    def auto_up(self, count=1):
        """
        If we're not on the first line (of a multiline input) go a line up,
        otherwise go back in history. (If nothing is selected.)
        """
        if self.complete_state:
            self.complete_previous()
        elif self.document.cursor_position_row > 0:
            self.cursor_position += self.document.get_cursor_up_position(count=count)
        elif not self.selection_state:
            self.history_backward(count=count)

    def auto_down(self, count=1):
        """
        If we're not on the last line (of a multiline input) go a line down,
        otherwise go forward in history. (If nothing is selected.)
        """
        if self.complete_state:
            self.complete_next()
        elif self.document.cursor_position_row < self.document.line_count - 1:
            self.cursor_position += self.document.get_cursor_down_position(count=count)
        elif not self.selection_state:
            old_index = self.working_index
            self.history_forward(count=count)

            # If we moved to the next line, place the cursor at the beginning.
            if old_index != self.working_index:
                self.cursor_position = 0

    def delete_before_cursor(self, count=1):  # TODO: unittest return type
        """
        Delete character before cursor, return deleted character.
        """
        assert count >= 0
        deleted = ''

        if self.cursor_position > 0:
            deleted = self.text[self.cursor_position - count:self.cursor_position]
            self.text = self.text[:self.cursor_position - count] + self.text[self.cursor_position:]
            self.cursor_position -= len(deleted)

        return deleted

    def delete(self, count=1):  # TODO: unittest `count`
        """
        Delete one character. Return deleted character.
        """
        if self.cursor_position < len(self.text):
            deleted = self.document.text_after_cursor[:count]
            self.text = self.text[:self.cursor_position] + \
                self.text[self.cursor_position + len(deleted):]
            return deleted
        else:
            return ''

    def join_next_line(self):
        """
        Join the next line to the current one by deleting the line ending after
        the current line.
        """
        self.cursor_position += self.document.get_end_of_line_position()
        self.delete()

    def swap_characters_before_cursor(self):
        """
        Swap the last two characters before the cursor.
        """
        pos = self.cursor_position

        if pos >= 2:
            a = self.text[pos - 2]
            b = self.text[pos - 1]

            self.text = self.text[:pos-2] + b + a + self.text[pos:]

    def go_to_history(self, index):
        """
        Go to this item in the history.
        """
        if index < len(self._working_lines):
            self.working_index = index
            self.cursor_position = len(self.text)

    def complete_next(self, count=1):
        """
        Browse to the next completions.
        (Does nothing if there are no completion.)
        """
        if self.complete_state:
            completions_count = len(self.complete_state.current_completions)

            if self.complete_state.complete_index is None:
                index = 0
            elif self.complete_state.complete_index == completions_count - 1:
                index = None
            else:
                index = min(completions_count-1, self.complete_state.complete_index + count)
            self._go_to_completion(index)

    def complete_previous(self, count=1):
        """
        Browse to the previous completions.
        (Does nothing if there are no completion.)
        """
        if self.complete_state:
            if self.complete_state.complete_index == 0:
                index = None
            elif self.complete_state.complete_index is None:
                index = len(self.complete_state.current_completions) - 1
            else:
                index = max(0, self.complete_state.complete_index - count)

            self._go_to_completion(index)

    def cancel_completion(self):
        """
        Cancel completion, go back to the original text.
        """
        if self.complete_state:
            self._go_to_completion(None)
            self.complete_state = None

    def set_completions(self, completions, go_to_first=True, go_to_last=False):
        """
        Start completions. (Generate list of completions and initialize.)
        """
        assert not (go_to_first and go_to_last)

        # Generate list of all completions.
        if completions is None:
            if self.completer:
                completions = list(self.completer.get_completions(
                    self.document,
                    CompleteEvent(completion_requested=True)
                ))
            else:
                completions = []

        # Set `complete_state`.
        if completions:
            self.complete_state = CompletionState(
                original_document=self.document,
                current_completions=completions)
            if go_to_first:
                self._go_to_completion(0)
            elif go_to_last:
                self._go_to_completion(len(completions) - 1)
            else:
                self._go_to_completion(None)

        else:
            self.complete_state = None

    def start_history_lines_completion(self):
        """
        Start a completion based on all the other lines in the document and the
        history.
        """
        found_completions = set()
        completions = []

        # For every line of the whole history, find matches with the current line.
        current_line = self.document.current_line_before_cursor.lstrip()

        for i, string in enumerate(self._working_lines):
            for j, l in enumerate(string.split('\n')):
                l = l.strip()
                if l and l.startswith(current_line):
                    # When a new line has been found.
                    if l not in found_completions:
                        found_completions.add(l)

                        # Create completion.
                        if i == self.working_index:
                            display_meta = "Current, line %s" % (j+1)
                        else:
                            display_meta = "History %s, line %s" % (i+1, j+1)

                        completions.append(Completion(
                            l,
                            start_position=-len(current_line),
                            display_meta=display_meta))

        self.set_completions(completions=completions[::-1])

    def _go_to_completion(self, index):
        """
        Select a completion from the list of current completions.
        """
        assert self.complete_state

        # Set new completion
        state = self.complete_state.go_to_index(index)

        # Set text/cursor position
        self.text, self.cursor_position = state.new_text_and_position()

        # (changing text/cursor position will unset complete_state.)
        self.complete_state = state

    def history_forward(self, count=1):
        if self.working_index < len(self._working_lines) - count:
            # Go forward in history, and update cursor_position.
            self.working_index += count
            self.cursor_position = len(self.text)

    def history_backward(self, count=1):
        if self.working_index - count >= 0:
            # Go back in history, and update cursor_position.
            self.working_index -= count
            self.cursor_position = len(self.text)

    def start_selection(self, selection_type=SelectionType.CHARACTERS):
        """
        Take the current cursor position as the start of this selection.
        """
        self.selection_state = SelectionState(self.cursor_position, selection_type)

    def copy_selection(self, _cut=False):
        """
        Copy selected text and return :class:`ClipboardData` instance.
        """
        if self.selection_state:
            type = self.selection_state.type

            # Take start and end of selection
            from_, to = self.document.selection_range()

            copied_text = self.text[from_:to]

            # If cutting, remove the text and set the new cursor position.
            if _cut:
                self.text = self.text[:from_] + self.text[to + 1:]
                self.cursor_position = min(from_, to)

            self.selection_state = None
            return ClipboardData(copied_text, type)
        else:
            return ClipboardData('')

    def cut_selection(self):
        """
        Delete selected text and return :class:`ClipboardData` instance.
        """
        return self.copy_selection(_cut=True)

    def newline(self, copy_margin=True):
        """
        Insert a line ending at the current position.
        """
        if copy_margin:
            self.insert_text('\n' + self.document.leading_whitespace_in_current_line)
        else:
            self.insert_text('\n')

    def insert_line_above(self, copy_margin=True):
        """
        Insert a new line above the current one.
        """
        if copy_margin:
            insert = self.document.leading_whitespace_in_current_line + '\n'
        else:
            insert = '\n'

        self.cursor_position += self.document.get_start_of_line_position()
        self.insert_text(insert)
        self.cursor_position -= 1

    def insert_line_below(self, copy_margin=True):
        """
        Insert a new line below the current one.
        """
        if copy_margin:
            insert = '\n' + self.document.leading_whitespace_in_current_line
        else:
            insert = '\n'

        self.cursor_position += self.document.get_end_of_line_position()
        self.insert_text(insert)

    def insert_text(self, data, overwrite=False, move_cursor=True):
        """
        Insert characters at cursor position.
        """
        # In insert/text mode.
        if overwrite:
            # Don't overwrite the newline itself. Just before the line ending, it should act like insert mode.
            overwritten_text = self.text[self.cursor_position:self.cursor_position+len(data)]
            if '\n' in overwritten_text:
                overwritten_text = overwritten_text[:overwritten_text.find('\n')]

            self.text = self.text[:self.cursor_position] + data + self.text[self.cursor_position+len(overwritten_text):]
        else:
            self.text = self.text[:self.cursor_position] + data + self.text[self.cursor_position:]

        if move_cursor:
            self.cursor_position += len(data)

        # Fire 'onTextInsert' event.
        self.onTextInsert.fire()

    def paste_clipboard_data(self, data, before=False, count=1):
        """
        Insert the data from the clipboard.
        """
        assert isinstance(data, ClipboardData)

        if data.type == SelectionType.CHARACTERS:
            if before:
                self.insert_text(data.text * count)
            else:
                self.cursor_right()
                self.insert_text(data.text * count)
                self.cursor_left()

        elif data.type == SelectionType.LINES:
            if before:
                self.cursor_position += self.document.get_start_of_line_position(after_whitespace=False)
                self.insert_text((data.text + '\n') * count, move_cursor=False)
            else:
                self.cursor_position += self.document.get_end_of_line_position()
                self.insert_text(('\n' + data.text) * count, move_cursor=False)
                self.cursor_down()

            self.cursor_position += self.document.get_start_of_line_position(after_whitespace=True)

    def undo(self):
        # Pop from the undo-stack until we find a text that if different from
        # the current text. (The current logic of `save_to_undo_stack` will
        # cause that the top of the undo stack is usually the same as the
        # current text, so in that case we have to pop twice.)
        while self._undo_stack:
            text, pos = self._undo_stack.pop()

            if text != self.text:
                # Push current text to redo stack.
                self._redo_stack.append((self.text, self.cursor_position))

                # Set new text.
                self.text = text
                self.cursor_position = pos
                break

    def redo(self):
        if self._redo_stack:
            # Copy current state on undo stack.
            self.save_to_undo_stack(clear_redo_stack=False)

            # Pop state from redo stack.
            text, pos = self._redo_stack.pop()
            self.text = text
            self.cursor_position = pos

    def validate(self):
        """
        Returns `True` if valid.
        """
        self.validation_error = None

        # Validate first. If not valid, set validation exception.
        if self.validator:
            try:
                self.validator.validate(self.document)
            except ValidationError as e:
                # Set cursor position (don't allow invalid values.)
                cursor_position = e.index
                self.cursor_position = min(max(0, cursor_position), len(self.text))

                self.validation_error = e
                return False

        return True

    def add_to_history(self):  # TODO: Rename to `append_to_history`
        """
        Append the current input to the history.
        (Only if valid input.)
        """
        # Validate first. If not valid, set validation exception.
        if not self.validate():
            return

        # Save at the tail of the history. (But don't if the last entry the
        # history is already the same.)
        if self.text and (not len(self._history) or self._history[-1] != self.text):
            self._history.append(self.text)

    def _search(self, search_state):
        """
        Execute search. Return (working_index, cursor_position) tuple when this
        search is applied. Returns `None` when this text cannot be found.
        """
        assert isinstance(search_state, SearchState)
        text = search_state.text
        direction = search_state.direction

        if direction == IncrementalSearchDirection.FORWARD:
            # Try find at the current input.
            new_index = self.document.find(text, include_current_position=False)

            if new_index is not None:
                return (self.working_index, self.cursor_position + new_index)
            else:
                # No match, go forward in the history.
                for i in range(self.working_index + 1, len(self._working_lines)):
                    document = Document(self._working_lines[i], 0)
                    new_index = document.find(text, include_current_position=True)
                    if new_index is not None:
                        return (i, new_index)
        else:
            # Try find at the current input.
            new_index = self.document.find_backwards(text)

            if new_index is not None:
                return (self.working_index, self.cursor_position + new_index)
            else:
                # No match, go back in the history.
                for i in range(self.working_index - 1, -1, -1):
                    document = Document(self._working_lines[i], len(self._working_lines[i]))
                    new_index = document.find_backwards(text)
                    if new_index is not None:
                        return (i, len(document.text) + new_index)

    def document_for_search(self, search_state):
        """
        Return a `Document` instance that has the text/cursor position for this
        search, if we would apply it.
        """
        search_result = self._search(search_state)

        if search_result is None:
            return self.document
        else:
            working_index, cursor_position = search_result
            return Document(self._working_lines[working_index], cursor_position)

    def apply_search(self, search_state):
        """
        Return a `Document` instance that has the text/cursor position for this
        search, if we would apply it.
        """
        search_result = self._search(search_state)

        if search_result is not None:
            working_index, cursor_position = search_result
            self.working_index = working_index
            self.cursor_position = cursor_position

    def exit_selection(self):
        self.selection_state = None

    def open_in_editor(self):
        """
        Open code in editor.
        """
        # Write to temporary file
        descriptor, filename = tempfile.mkstemp(self.tempfile_suffix)
        os.write(descriptor, self.text.encode('utf-8'))
        os.close(descriptor)

        # Open in editor
        self._open_file_in_editor(filename)

        # Read content again.
        with open(filename, 'rb') as f:
            self.text = f.read().decode('utf-8')
        self.cursor_position = len(self.text)

        # Clean up temp file.
        os.remove(filename)

    def _open_file_in_editor(self, filename):
        """ Call editor executable. """
        # If the 'EDITOR' environment variable has been set, use that one.
        # Otherwise, fall back to the first available editor that we can find.
        editor = os.environ.get('EDITOR')

        editors = [
            editor,

            # Order of preference.
            '/usr/bin/editor',
            '/usr/bin/nano',
            '/usr/bin/pico',
            '/usr/bin/vi',
            '/usr/bin/emacs',
        ]

        for e in editors:
            if e:
                try:
                    subprocess.call([e, filename])
                    return

                except OSError:
                    # Executable does not exist, try the next one.
                    pass


def indent(buffer, from_row, to_row, count=1):
    """
    Indent text of the `Buffer` object.
    """
    current_row = buffer.document.cursor_position_row
    line_range = range(from_row, to_row)

    buffer.transform_lines(line_range, lambda l: '    ' * count + l)

    buffer.cursor_position = buffer.document.translate_row_col_to_index(current_row, 0)
    buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)


def unindent(buffer, from_row, to_row, count=1):
    """
    Unindent text of the `Buffer` object.
    """
    current_row = buffer.document.cursor_position_row
    line_range = range(from_row, to_row)

    def transform(text):
        remove = '    ' * count
        if text.startswith(remove):
            return text[len(remove):]
        else:
            return text.lstrip()

    buffer.transform_lines(line_range, transform)

    buffer.cursor_position = buffer.document.translate_row_col_to_index(current_row, 0)
    buffer.cursor_position += buffer.document.get_start_of_line_position(after_whitespace=True)
