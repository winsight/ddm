---
name: python
description: Guidelines when creating, reading, updating, or deleting Python code
---

## Instructions

### imports

- Imports must always be at the top of the file.
- Local imports are forbidden unless 100% necessary.

### formatting

- Use underscores for large numbers: `1_000` not `1000`.

### __init__.py files

- Do not add anything inside `__init__.py` unless absolutely necessary or explicitly asked.
- NEVER add `__all__`.

### function parameters

- Functions with more than one parameter must use `*` to force keyword arguments.
  - BAD: `def foo(a, b, c): ...`
  - GOOD: `def foo(*, a, b, c): ...`
- Parameters should be required unless optional is absolutely necessary.
- Only set defaults for EXTREMELY sane default cases.
