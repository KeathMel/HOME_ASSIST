#!/usr/bin/env bash
# Reads the approved command from approved.json and runs it in a terminal.
# Called by term-chat.py after you press "confirm".

APPROVED="$HOME/.config/term-chat/approved.json"

[ -f "$APPROVED" ] || { echo "no approved.json"; exit 0; }

# pull the "command" field out of the json (python is the safest parser here)
CMD="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("command",""))' "$APPROVED")"

[ -z "$CMD" ] && { echo "empty command"; exit 0; }

# run it in alacritty; keep the window open afterwards so you can read output.
# the command output is also appended to a log next to the json.
LOG="$HOME/.config/term-chat/last-run.log"

alacritty -e bash -c "echo '> $CMD'; echo '----'; { $CMD; } 2>&1 | tee '$LOG'; echo; echo '[ done — press enter to close ]'; read" &
