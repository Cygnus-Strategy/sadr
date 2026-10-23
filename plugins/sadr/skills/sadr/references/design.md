# How Sadr makes changes

These are Sadr's default rules for deciding what to change and how much.
They apply to every change Sadr makes: upgrades, fixes, and features.

A repo's AGENTS.md and its owner's AGENTS.md win over every rule in
this file. See "Precedence" in SKILL.md.

## Know the scope before you write

1. Read the task and the code it touches.
2. List every place the change must reach: callers, tests, fixtures,
   config, exports, documentation, and CI.
3. Check what the change can break for users. Look for data it can destroy
   or expose, and for callers that stop working.

That list is the scope. A feature nobody asked for is not part of the scope.

## Take the smallest complete change

Use the first option that fully works:

1. **Not needed.** Skip features, options, and flexibility that nobody asked
   for. Name each skipped item in one line in the PR.
2. **Already in this repo.** Use the existing helper, component, service, or
   pattern. Use it the way the code around it uses it.
3. **In the standard library or the platform.** Use it, unless the repo has
   its own version. The repo's own component wins over a native one.
4. **In an installed dependency.** Use it. Do not add a dependency for a few
   lines of code.
5. **One clear line.** Write one line, if a reader understands it at once.
6. **Otherwise.** Write the minimum code that works.

Rules for every change:

- Finish every part of the scope. Update the callers, tests, and fixtures
  that the change breaks. A small change that leaves a broken caller is not
  complete.
- Do not add an abstraction, wrapper, option, config value, or code "for
  later" that the task does not need. Deleting code is better than adding
  code.
- Keep the structure that the repo already has: its layers, interfaces,
  names, and conventions.
- When two options have the same size, use the one that is correct on edge
  cases (empty input, zero, the last item, time zones, rounding).
- Fix a bug at its root cause, in the shared code. Before you edit a
  function, find every caller.
- Code that you move or merge keeps its error handling and validation.
- Write a comment only for a reason that the code cannot show. Use one line.
- Mark a shortcut with a known limit with a comment in this form:
  `shortcut: <the limit>, <when to change it>`.
- New logic that is not trivial gets one small test that fails when the
  logic breaks. This includes a branch, a loop, a parser, money, security,
  data writes, and a bug fix. Trivial changes do not need a test.

Never cut these to make a change smaller:

- validation of input that crosses a trust boundary
- error handling that prevents data loss
- security
- accessibility
- anything that the issue or the repo's AGENTS.md asks for

## Building a feature from an issue

- Build the smallest version that does the core job of the issue.
- If the issue does not give enough information to build it, do not guess.
  Comment on the issue with the questions, and do not open a PR.
- List in the PR what you did not build and why.

## Check your own work before the PR

Read the diff as a reviewer before you push:

1. Look for a wrong result, a crash, a missed edge case, or a caller that
   the change breaks.
2. Look for a security risk or a data-loss risk.
3. Look for code that should not exist: dead code, unused options, a copy of
   a helper that the repo already has.
4. Every problem you find needs a concrete case: "this input gives this
   wrong result". If you cannot give a case, it is not a problem.
5. Fix the problems you find before you push. Use the smallest fix that
   works.

End every PR with what Sadr did not do or did not check, and any risk that
the reviewer must know. See references/pr-format.md.
