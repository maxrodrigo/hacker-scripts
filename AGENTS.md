# Hacker Scripts

Personal daily drivers for my deterministic tasks.

## What a script is

- **Narrow**: they try to do one thing, the same way every run.
- **Standalone**: each one is a single executable file with a shebang, living in a folder named after its domain. Its name stands on its own (`vidgrab.sh`, not `media/grab.sh`), so it still makes sense when copied elsewhere.
- **Data**: output goes to `data/<domain>/` at the repo root by default, never next to the code. It is tracked in git unless it is too big (media), in which case it goes in `.gitignore`.
- **Minimal**: use the stdlib or a CLI tool that is already installed before you reach for a library.
- **Idempotent**: long jobs should be able to be interrupted and run again.
- **Loud**: errors stop the run with a clear exit.

## Header

The comment block at the top of the script is the single source of truth for how to use it. It should cover:

1. What the script does, in one sentence.
2. Usage, with its arguments.
3. Outputs: where they go and what they look like.
4. Flags and env vars, with their defaults.
5. Requirements: external tools and the runtime version.
6. Gotchas: rate limits, resume behaviour, cached models.

## README

Every script gets one entry in `README.md`, in the same change that adds it. Entries are sorted by path:

```
- [`dir/script`](dir/script): What it does in 1–2 sentences.
```

Usage details stay in the header. When you rename or delete a script, update its entry too.

## Done

A change is done when all three hold:

- The script ran end to end on a real input.
- The header matches what the script does.
- The README entry matches the header.
