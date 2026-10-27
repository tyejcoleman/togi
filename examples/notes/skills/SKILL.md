---
name: notes
summary: Add, find and tip notes.
---
# notes

Keep short notes and tip their authors.

- `togi notes add --text "..." [--tag t]` adds a note and returns its id (`n_1`, `n_2`, ...).
- `togi notes list [--tag t] [--sort oldest]` lists notes, newest first.
- `togi notes find --query words` searches note text.
- `togi notes show --id n_1` shows one note in full.
- `togi notes delete --id n_1` deletes (try `--dry-run` first).
- Tips move money, so they need a person: see `togi skill notes money`.

Errors always carry a `next` command. Run it.
