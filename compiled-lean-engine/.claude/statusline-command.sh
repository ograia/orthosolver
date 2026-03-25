#!/bin/sh
# Claude Code status line script — blue color theme
input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // .workspace.current_dir // ""')
model=$(echo "$input" | jq -r '.model.display_name // ""')
remaining=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')

BLUE='\033[34m'
RESET='\033[0m'

if [ -n "$remaining" ]; then
    printf "${BLUE}%s  %s  ctx: %s%% remaining${RESET}" "$cwd" "$model" "$remaining"
else
    printf "${BLUE}%s  %s${RESET}" "$cwd" "$model"
fi
