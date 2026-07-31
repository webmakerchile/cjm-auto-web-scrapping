---
name: Deployment port config drift
description: Why production showed Internal Server Error twice and how to check port mappings after merges
---

- Production serves the `[[ports]]` mapping with `externalPort = 80`. The panel listens on 5000, so `.replit` must map 5000→80 and nothing else to 80.
- **Why:** Task-agent merges can restore an older `.replit` (this happened with the task-5 merge: 8080→80 came back), silently breaking the published app while dev keeps working.
- **How to apply:** After any task merge or before telling the user to republish, check `.replit` `[deployment]` run command (must be gunicorn serving `panel.app:app`, never `correr.sh`) and the port mappings. Deployment logs line "forwarding local port NNNN to external port 80" reveals what production actually uses.
