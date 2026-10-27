---
name: money
summary: How tips are approved by a person.
---
# Tips need approval

`togi notes tip send --id n_1 --amount 5.00` does not send anything on its own. It returns exit code 3 and an
approval id (`ap_...`). Tell the person to run `togi approve ap_...` in their own terminal. When they say it
is done, re-run the exact same command with `--approval ap_...`. An approval works once, for exactly those
arguments, and expires in 15 minutes. Never try to approve it yourself.
