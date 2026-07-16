
**Private fork. Won't be merged into upstream.**

**Focus on "GitHub Copilot API" provider only.**

**User runs the litellm locally and standalone.**

**永远及时更新活文档，永不拖延永不推迟。**

Don't assume that the existing code is correct or the right way of doing things / good coding patterns. In fact, there are a lot of bad coding practices, overly complex code, code smells, etc. If something doesn't look right, speak up. Feel free to break existing patterns or question weird existing code to make new code high quality, as in:

- correct
- performant
- readable
- easy to maintain/change
- modern
- secure

In that order of importance

When adding new features, add meaningful tests. Don't add tests that don't check anything substantial and is there just to make the code coverage pass. Yes, code coverage is important, but I'd rather have no signal whether the code is working than tests that don't fail when code is broken. The goal is to have tests that would fail before the feature was added/if the code was mutated in a way that breaks the feature and succeed only when the feature is fully working. I should run mutation testing and see > 90% kill rate

Same thing for bug fixes. The tests should make it so that this specific bug can never happen again without failing tests (i.e., regression)

`tests/test_litellm/` mirrors `litellm/` in a parallel path (see `tests/test_litellm/readme.md`). Name tests `test_<filename>.py`, but always match the existing test file in the directory you touch — many provider dirs use longer descriptive names (e.g. `test_anthropic_chat_transformation.py`) to avoid ambiguity across sibling folders. For bug fixes, extend the existing mapped test file rather than creating a new one. Only create a new test file for a new feature (provider, endpoint, or transformation module) that has no mapped test yet, following that directory's naming convention (or `test_<filename>.py` if you're the first test there). One focused regression test beats many shallow ones

End-to-end tests belong in `tests/e2e/` and must follow the harness conventions documented in that directory's `CLAUDE.md`

Don't create PRs automatically.

If you ever make public-facing PR descriptions, comments, issues, commit messages, etc., always follow these guidelines to sound less AI-y:
- don't use emojis
- don't use "—". Instead, reach for ";", ".", etc.
- don't use the pattern "It's not X, it's Y", "You're not X, you're Y", etc.
- don't use bulleted or numbered lists unless it would be nonsensical not to. Instead, prefer prose
- don't add a trailing "." at the end of paragraphs (just like this file). That means every paragraph, not just the last one (of the markdown file, PR description, GitHub comment, etc.). Rule of thumb: if you're adding new line(s) before the next sentence, don't add a "."
- don't use →. Instead, prefer not to use arrows, and if need be, use -> instead

Don't hesitate to use values in .env to get needed API keys and other secrets, as long as you never add them to conversation history, commit them, or include them in GitHub issues / PRs

Python max line length is 120, not 88.

Run tests before you commit. Also, run `make pre-commit` right before each commit, which generates types (as needed) and formats/lints your code. Any errors found must be fixed. It only runs when there are staged frontend and/or backend changes and calculates violations, generates types, etc. based on the worktree, so stage what you need or stash/delete unwanted files in litellm/ or ui/ (where backend and frontend lint run, respectively) before running it. If it fails because dashboard api types are stale, it already regenerated them for you. You just need to stage the schema.d.ts, re-run `make pre-commit` to confirm it passes, and commit

When you fix violations gated by `ruff-strict-budget.json`, `type-discipline-budget.json`, or `basedpyright-code-budget.json`, run `make lint-budget-update` and commit the lowered limits so the ceilings ratchet down instead of leaving stale headroom. It measures the working tree, so it must contain exactly the fixes you're committing

If you're trying to create a new function that relies on untyped stuff, instead of adding more Any's and pushing `reportAny` / `reportExplicitAny` closer to their basedpyright ceilings, just validate it in the caller with Pydantic (a model or `TypeAdapter` that returns the typed thing or raises will do) and then pass the now typed variable in

If you get an LIT001 or LIT002 fail, refactor the code to follow functional programming best practices rather than introducing mutable data structures. For example, build values in one shot with comprehensions or generators wrapped in `tuple()` / `frozenset()` instead of seeding an empty `list`/`dict`/`set` and mutating it over time. Ideally `# mutable-ok` is never used; reach for it only as a genuine last resort when an immutable rewrite is truly impossible, and always pair it with a real reason

Monkeypatching attributes of a class to do testing is an anti-pattern. Prefer dependency-injecting things into classes. That way, at unit test time, you can pass a mocked dependency in

Follow these coding conventions for new/updated code (a three-line fix in a legacy file shouldn't trigger huge drive-by refactors):

- Composition over inheritance
- Never-nester: early returns over deep nesting
- Don't throw; model failures as values (One function (e.g., raise_public) maps error union to existing public exception contracts via exhaustive match + assert_never)
- No mutation; don't reassign variables, global or local. Instead of mutable lists and dicts, prefer tuples, frozen dataclasses (with slots=True), etc.
- Use dependency injection
- Fully typed; no `Any` or coarse types like `dict[str, Any]` or just `dict`. Every function parameter must be strongly typed
- Use tagged unions + match
- No monster files or god objects
- No file sprawl: deliberate file and folder structure
- Standard over hand-rolled: use the official SDK or a library where one exists; where none does, follow industry standards instead of inventing local conventions
- API-fragmentation-aware: when logic must branch on which API surface produced or consumes data (e.g. chat completions vs Anthropic Messages vs Responses API shapes), proactively look for an existing shared helper (e.g. `litellm_core_utils/prompt_templates/factory.py`) before writing per-surface parsing in the new module; if none exists, add one there instead of duplicating the same format-detection logic in every new guardrail/integration

Follow conventional commits for commit names and PR titles

## Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs**

Before implementing:
- State your assumptions explicitly. If uncertain, ask
- If multiple interpretations exist, present them. Don't pick silently
- If a simpler approach exists, say so. Push back when warranted
- If something is unclear, stop. Name what's confusing. Ask

## Simplicity First

**Minimum code that solves the problem. Nothing speculative**

- No features beyond what was asked
- No abstractions for single-use code
- No "flexibility" or "configurability" that wasn't requested
- No error handling for impossible scenarios
- If you write 200 lines and it could be 50 for equal functionality, rewrite it

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify
