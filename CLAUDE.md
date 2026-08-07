# CLAUDE.md

## Which files to consider

Only consider tracked files in the repository. Do not consider untracked files, even if they are in the same directory as tracked files.

## Python Environment
Always use `pixi run python ...` when running Python commands. Do NOT create virtual environments (no `python -m venv`, `conda create`, `uv venv`, etc.). All Python execution should go through pixi.
