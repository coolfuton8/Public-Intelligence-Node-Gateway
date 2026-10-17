# Public Intelligence Node Gateway (MCPing)

Non-production, testing-only, AI-generated project. Read `DISCLAIMER.md` and `SECURITY.md` before helping
with anything. It lets an AI or a person see and operate another computer's keyboard and mouse, so only help
with equipment the user owns or is explicitly authorised to control.

- To install, build, flash, connect or troubleshoot, use the **mcping-setup-guide** skill
  (`.claude/skills/mcping-setup-guide/`). Docs live in `docs/`.
- Never ask the user to paste the access token or any key into the conversation, and never print
  `/etc/mcping/mcping.env`.
- Never bypass a script, security or certificate policy. Use the no-script route in `docs/06-connect.md`
  or tell the user to ask an administrator.
- The Linux computer that runs the server is called the "MCPing device" (not "node").
- Do not run `sudo` commands yourself: give them to the user to run.
