"""JSON configuration with standalone // comment lines."""
import json


def comment_lines(text):
    return [line for line in text.splitlines(keepends=True)
            if line.lstrip().startswith('//')]


def loads(text):
    # Only whole-line comments are supported; URLs and string contents stay intact.
    return json.loads('\n'.join('' if line.lstrip().startswith('//') else line
                                for line in text.splitlines()))
