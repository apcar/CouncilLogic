---
name: councillogic
description: Ask a council of independent AI models to deliberate on a big, contested, hard-to-reverse decision, with a mandatory cost plan, human spend approval, and a human final decision.
---

# CouncilLogic council

Use this only for decisions that are **big, contested and hard to reverse**,
where a wrong call is costly and reasonable models might disagree. Don't use
it for facts you can look up, or for easily reversible choices.

## Steps
1. **Plan (always first, free):**
   `echo '{"action":"plan","question":"<decision question with context>"}' | council agent`
2. If `approval_required` is true, **stop and ask your human**. Show them the
   question, `estimate.max_calls`, the token bounds, the providers and the
   `plan_hash`. Ask them to run `council approve --question "<same question>"
   --plan-hash <plan_hash> --approved-by <their name>` in their own terminal
   and give you the token. Never run `council approve` yourself.
3. **Run** with the identical question:
   `{"action":"run","question":"...","approved_by":"<name>","approval_token":"<token>"}`
4. **Report**, don't decide. Give your human the `verdict` lines, any
   `synthesis_check` mismatch or self-votes that change the outcome, and the
   `next_step` text verbatim. It starts "Awaiting human decision".
5. Only the human records the decision with `council decide`. You can't, and
   you must not try.

For a free trial without keys, put `--mock` before `agent`. See
docs/AGENT-INTERFACE.md for the full contract.
