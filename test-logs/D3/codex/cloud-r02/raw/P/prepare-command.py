"""Source-only command preparation; no command execution and no authorization issuance."""
import argparse
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKENS = ("__LAUNCH_NOT_AFTER_ISO__", "__DISPATCH_PEER_ID__",
          "__ISSUED_UTC__", "__LAUNCH_NOT_AFTER_UTC__")
WINDOW_SECONDS = 900


def prepare(template, values):
    if not isinstance(values, dict) or set(values) != set(TOKENS):
        raise ValueError("exact four issued token keys required")
    if any(type(value) is not str or not value or any(c in value for c in "\r\n\x00'\"")
           or any(token in value for token in TOKENS) for value in values.values()):
        raise ValueError("invalid issued literal value")
    if any(template.count(token) != 1 for token in TOKENS):
        raise ValueError("each issued token must occur exactly once")
    identifier = uuid.UUID(values["__DISPATCH_PEER_ID__"])
    if str(identifier) != values["__DISPATCH_PEER_ID__"]:
        raise ValueError("canonical system-issued peer ID required")
    issued = datetime.fromisoformat(values["__ISSUED_UTC__"])
    deadline = datetime.fromisoformat(values["__LAUNCH_NOT_AFTER_UTC__"])
    iso_deadline = datetime.fromisoformat(values["__LAUNCH_NOT_AFTER_ISO__"])
    if any(t.utcoffset() != timedelta(0) for t in (issued, deadline, iso_deadline)):
        raise ValueError("issued times must be timezone-aware UTC")
    if deadline != iso_deadline or deadline - issued != timedelta(seconds=WINDOW_SECONDS):
        raise ValueError("issuance window must be exactly900 seconds")
    rendered = template
    for token in TOKENS:
        rendered = rendered.replace(token, values[token], 1)
    # Check only the designated four tokens. Normal .__name__ text remains valid.
    if any(token in rendered for token in TOKENS):
        raise ValueError("issued token substitution incomplete")
    return rendered


def main():
    parser = argparse.ArgumentParser()
    for key in ("template", "template-sha256", "tokens-json", "output"):
        parser.add_argument("--" + key, required=True)
    args = parser.parse_args()
    path = Path(args.template)
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != args.template_sha256:
        raise ValueError("externally frozen PRE template pin mismatch")
    values = json.loads(Path(args.tokens_json).read_text(encoding="utf-8"))
    rendered = prepare(data.decode("utf-8"), values).encode("utf-8")
    output = Path(args.output)
    if not output.is_absolute() or output.resolve() != output or output.is_symlink():
        raise ValueError("canonical absolute new owned output required")
    with output.open("xb") as stream:
        stream.write(rendered)
    print(json.dumps({"scope": "command_preparation_only", "issued_by_this_script": False,
                      "template_sha256": args.template_sha256, "output": str(output),
                      "bytes": len(rendered), "sha256": hashlib.sha256(rendered).hexdigest(),
                      "window_seconds": WINDOW_SECONDS, "executed_command_count": 0}))


if __name__ == "__main__":
    main()

