PUSHARY_NOTIFY = {
    "name": "pushary_notify",
    "description": (
        "Send a push notification to the user's phone or desktop. Use when a task "
        "completes, an error occurs, or a long-running process finishes. The user "
        "sees this on their lock screen even if they're away from the computer. "
        "Optionally include structured context with file changes, error details, "
        "and next steps."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Notification title (max 100 chars, aim for under 60)",
            },
            "body": {
                "type": "string",
                "description": "Notification body (max 500 chars, aim for under 200)",
            },
            "agent_name": {
                "type": "string",
                "description": 'Identifies this Hermes instance (e.g., "Hermes - daily-briefing")',
            },
            "context_type": {
                "type": "string",
                "enum": ["task_complete", "error", "info"],
                "description": (
                    "Always pass this. It marks the notification a task update, and the "
                    "user's setting for where task updates land can only route one that "
                    "says so. Also drives the rich detail page."
                ),
            },
            "summary": {
                "type": "string",
                "description": "Short summary shown on the detail page",
            },
            "details": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Bullet-point details",
            },
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of files that were changed",
            },
            "error_message": {
                "type": "string",
                "description": "Error message if context_type is error",
            },
            "next_steps": {
                "type": "string",
                "description": "Suggested next steps for the user",
            },
            "session_id": {
                "type": "string",
                "description": "Per-session id, so parallel Hermes sessions are attributed separately. Filled in automatically when omitted.",
            },
        },
        "required": ["title", "body"],
    },
}

PUSHARY_ASK = {
    "name": "pushary_ask",
    "description": (
        "Ask the user a question via push notification and wait for their answer. "
        "By default it performs the initial wait and at most one server-directed poll. "
        "Returns an answer or a structured unanswered result; follow handoffAction "
        "when present, otherwise nextAction. "
        "Supports three question types: confirm (yes/no), select (multiple choice "
        "with 2-6 options), and input (free text). Set wait=false only when you want "
        "an immediate correlationId and will call pushary_wait once yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask (max 500 chars)",
            },
            "type": {
                "type": "string",
                "enum": ["confirm", "select", "input"],
                "description": "Question type: confirm (yes/no), select (pick from options), input (free text)",
                "default": "confirm",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Choices for select type (2-6 options)",
            },
            "placeholder": {
                "type": "string",
                "description": "Placeholder text for input type",
            },
            "context": {
                "type": "string",
                "description": "What you're working on, shown above the question",
            },
            "agent_name": {
                "type": "string",
                "description": 'Identifies this Hermes instance (e.g., "Hermes - server-maintenance")',
            },
            "wait": {
                "type": "boolean",
                "description": "Wait for the answer before returning (default true). Set false to get correlationId for manual polling.",
                "default": True,
            },
            "timeout_ms": {
                "type": "integer",
                "description": "How long to wait per poll attempt in ms (default 30000, max 55000)",
                "default": 30000,
            },
            "tool_name": {
                "type": "string",
                "description": "The tool this approval is for (e.g. \"terminal\"), so the user can choose to always-allow it.",
            },
            "tool_target": {
                "type": "string",
                "description": "Compact target of the tool call, e.g. the command head \"git push\" or a file extension. Used to mine policy suggestions.",
            },
            "repo_key": {
                "type": "string",
                "description": "Stable repository identity for the working directory, e.g. \"github.com/acme/api\", so a routing rule scoped to one repository does not govern another.",
            },
            "intent": {
                "type": "string",
                "description": "The user's stated task, one line. Shown as the Intent line so they can see why you stopped.",
            },
            "action": {
                "type": "string",
                "description": "The concrete operation about to happen, one line. Shown as the Action line.",
            },
            "blocker": {
                "type": "string",
                "description": "The single gating reason you stopped, one line. Shown as the Blocker line.",
            },
            "action_body": {
                "type": "string",
                "description": "The diff or full command, rendered as a collapsible detail block. Never used as the push body.",
            },
            "session_id": {
                "type": "string",
                "description": "Per-session id, so parallel Hermes sessions are attributed separately. Filled in automatically when omitted.",
            },
        },
        "required": ["question"],
    },
}

PUSHARY_WAIT = {
    "name": "pushary_wait",
    "description": (
        "Poll once for a question created by pushary_ask with wait=false. Follow "
        "handoffAction when present, otherwise nextAction. Do not loop."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "correlation_id": {
                "type": "string",
                "description": "The correlationId returned by pushary_ask",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "How long to wait in milliseconds (default 30000, max 55000)",
                "default": 30000,
            },
        },
        "required": ["correlation_id"],
    },
}

PUSHARY_CANCEL = {
    "name": "pushary_cancel",
    "description": (
        "Cancel a pending question so it can no longer be answered. Use when "
        "the question is no longer relevant."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "correlation_id": {
                "type": "string",
                "description": "The correlationId of the question to cancel",
            },
        },
        "required": ["correlation_id"],
    },
}

PUSHARY_PROPOSE_SCOPE = {
    "name": "pushary_propose_scope",
    "description": (
        "Propose what this run will touch and block until the user ratifies it on "
        "their phone. Do not add a redundant approval to already authorized work. "
        "The user approves the paths you intend to change, the areas you promise to "
        "leave alone, and your definition of done in one tap. After that, editing a "
        "file outside the agreed scope becomes a separate 'wants to widen scope' "
        "question instead of a silent approval, so the user is asked once about the "
        "boundary rather than repeatedly about each file. ONLY file paths are "
        "enforced, and only on tool calls that carry one; shell commands, reads, web "
        "requests and MCP tools carry no path, so the contract says nothing about "
        "them. A boundary that is not a path (recipients, channels, spend, systems) "
        "goes in promises, which is shown and recorded but never checked. Read the "
        "returned enforces: an empty list means nothing here is checked "
        "automatically, and you must say so rather than report that a scope is in "
        "force. Returns ratified:true only on an explicit yes; "
        "anything else means proceed as if no scope was agreed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "done_when": {
                "type": "string",
                "description": "What \"finished\" means for this run, one or two lines.",
            },
            "allowed_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Globs you intend to change, e.g. [\"src/**\", \"docs/*.md\"].",
            },
            "off_limits_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Globs you promise not to touch. These win wherever they overlap allowed_paths.",
            },
            "promises": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Boundaries that are not file paths: recipients, channels, spend limits, "
                    "systems you will not open. Up to 10 of 200 characters. Shown to the user "
                    "as \"Promised, not checked\" and recorded, but NEVER enforced, because the "
                    "gate judges a file path and these have none. Do not put these in allowed_paths."
                ),
            },
            "agent_name": {
                "type": "string",
                "description": 'Identifies this Hermes instance (e.g., "Hermes - migration").',
            },
            "session_id": {
                "type": "string",
                "description": "Per-session id. Filled in from the running Hermes session when omitted.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "How long to block waiting for ratification (max 55000).",
            },
        },
        "required": ["done_when"],
    },
}

# --- Partner tools (require the Partner plan) -----------------------------------
# Reach your OWN product's end-users, not the operator's own devices. Use these when
# Hermes runs as a product that serves many people (gateway mode), so each person
# approves their own action on their own phone.

PUSHARY_ENROLL = {
    "name": "pushary_enroll",
    "description": (
        "Connect one of your OWN end-users' phones so they can approve actions "
        "(Partner plan). Returns a single-use link to show that person; one tap "
        "connects them through the Pushary app or supported browser fallback, without an operator account. Call once per "
        "end-user and reuse the enrollment, not the link, which expires. Use before "
        "pushary_ask_end_user for a user who has not connected a phone yet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "external_id": {
                "type": "string",
                "description": "Your own stable id for the end-user (a user id, tenant key, email hash).",
            },
        },
        "required": ["external_id"],
    },
}

PUSHARY_ASK_END_USER = {
    "name": "pushary_ask_end_user",
    "description": (
        "Ask one of your OWN end-users a question and block until they answer on "
        "their phone (Partner plan). Durable and fail-closed: a decline, a timeout, "
        "or no answer comes back as approved=false, so silence never turns into "
        "consent. Connect the person first with pushary_enroll. In gateway "
        "(multi-user) mode, pass each user's external_id from your own user model, "
        "not the Hermes session id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask (max 500 chars).",
            },
            "external_id": {
                "type": "string",
                "description": "Your own id for the end-user who should answer.",
            },
            "type": {
                "type": "string",
                "enum": ["confirm", "select", "input"],
                "description": "confirm (yes/no), select (pick from options), input (free text).",
                "default": "confirm",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Choices for a select question (2-6 options).",
            },
            "context": {
                "type": "string",
                "description": "What you are working on, shown above the question.",
            },
            "agent_name": {
                "type": "string",
                "description": 'Identifies this Hermes instance (e.g., "Hermes - support").',
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "How long to block waiting for the answer (default 50, the decision stays open longer).",
                "default": 50,
            },
        },
        "required": ["question", "external_id"],
    },
}
