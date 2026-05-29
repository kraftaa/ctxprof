#!/bin/bash
# Claude Code status line + session heartbeat.
#
# This is the RICH heartbeat statusline. It prints a compact one-line status
# AND writes per-session heartbeat files that the dashboard and audit tools read.
# Configure it as your Claude Code `statusLine` (see scripts/README.md).
#
# Claude Code supports exactly ONE statusLine. Do not wire this in addition to
# another statusline script — replace your existing one, or merge the pieces by
# hand. The installer (install-claude-context-tools.sh) refuses to clobber an
# existing statusLine and prints what to add instead.
#
# Reads Claude Code statusline JSON from stdin. Writes:
#   $CLAUDE_STATUS_STATE_DIR/<session>.json        (latest heartbeat snapshot)
#   $CLAUDE_STATUS_STATE_DIR/steps/<session>.jsonl  (per-turn token deltas)
# Default state dir: ~/.claude/session-status
#
# Env vars:
#   CLAUDE_STATUS_STATE_DIR     where to write heartbeats (default ~/.claude/session-status)
#   CLAUDE_STATUS_SERIES_POINTS how many recent turns to sparkline (default 24)
#
# Requires: jq, awk (both standard on macOS/Linux).

input=$(cat)

# ── Helper: compact number formatting (1.2k / 1.2M) ─────────────────────────
compact() {
  awk -v n="$1" 'BEGIN {
    if (n == "" || n+0 < 0) { print n; exit }
    v = n + 0
    if (v >= 1000000) {
      printf "%.1fM", v / 1000000
    } else if (v >= 1000) {
      printf "%.1fk", v / 1000
    } else {
      printf "%d", v
    }
  }'
}

token_series_large() {
  file="$1"
  [ -f "$file" ] || return 0
  tail -n "${CLAUDE_STATUS_SERIES_POINTS:-24}" "$file" 2>/dev/null \
    | jq -r '[.delta_input_tokens // 0, .delta_output_tokens // 0, .delta_cache_read_tokens // 0, .delta_cache_write_tokens // 0] | @tsv' 2>/dev/null \
    | awk 'BEGIN {
        cyan = "\033[0;36m"     # mostly non-cache tokens
        yellow = "\033[0;33m"   # mixed cache/non-cache
        green = "\033[0;32m"    # mostly cache tokens
        reset = "\033[0m"
      }
      {
        input = $1 + 0
        output = $2 + 0
        cache[NR] = ($3 + 0) + ($4 + 0)
        fresh = input - cache[NR]
        if (fresh < 0) fresh = 0
        noncache[NR] = fresh + output
        total[NR] = cache[NR] + noncache[NR]
        if (total[NR] > max) max = total[NR]
        sum += total[NR]
        sum_cache += cache[NR]
        sum_noncache += noncache[NR]
      }
      END {
        if (NR == 0 || max <= 0) exit
        printf "\033[0;90mrecent tokens C=cache N=fresh/out:\033[0m "
        for (i = 1; i <= NR; i++) {
          ratio = total[i] / max
          width = int(ratio * 3 + 0.999)
          if (width < 1) width = 1
          if (width > 3) width = 3
          share = (total[i] > 0 ? cache[i] / total[i] : 0)
          if (share >= 0.75) color = green
          else if (share >= 0.25) color = yellow
          else color = cyan
          printf "%s", color
          for (j = 0; j < width; j++) printf "█"
          printf "%s", reset
        }
        printf " \033[0;32mC %.0f%%\033[0m \033[0;36mN %.0f%%\033[0m", (sum_cache / sum) * 100, (sum_noncache / sum) * 100
      }'
}

# Model display name
model=$(echo "$input" | jq -r '.model.display_name // empty')

# Agent name (only when present)
agent=$(echo "$input" | jq -r '.agent.name // empty')
session_name=$(echo "$input" | jq -r '.session_name // empty')
version=$(echo "$input" | jq -r '.version // empty')
effort=$(echo "$input" | jq -r '.effort.level // empty')
thinking=$(echo "$input" | jq -r '.thinking.enabled // empty')
context_size=$(echo "$input" | jq -r '.context_window.context_window_size // empty')
remaining_pct=$(echo "$input" | jq -r '.context_window.remaining_percentage // empty')
api_duration_ms=$(echo "$input" | jq -r '.cost.total_api_duration_ms // 0')
lines_added=$(echo "$input" | jq -r '.cost.total_lines_added // 0')
lines_removed=$(echo "$input" | jq -r '.cost.total_lines_removed // 0')
pr_number=$(echo "$input" | jq -r '.pr.number // empty')
pr_review_state=$(echo "$input" | jq -r '.pr.review_state // empty')
worktree_name=$(echo "$input" | jq -r '.worktree.name // .workspace.git_worktree // empty')

# Context window usage
used_pct=$(echo "$input" | jq -r '.context_window.used_percentage // empty')

# Total input/output tokens
total_input=$(echo "$input" | jq -r '.context_window.total_input_tokens // empty')
total_output=$(echo "$input" | jq -r '.context_window.total_output_tokens // empty')

# Cache tokens
cache_write=$(echo "$input" | jq -r '.context_window.current_usage.cache_creation_input_tokens // empty')
cache_read=$(echo "$input" | jq -r '.context_window.current_usage.cache_read_input_tokens // empty')

# Session cost: use .cost.total_cost_usd or numeric .cost if present, else estimate
# (Opus pricing: $15/1M in, $75/1M out)
cost_field=$(echo "$input" | jq -r 'if (.cost | type) == "object" then (.cost.total_cost_usd // empty) elif (.cost | type) == "number" then .cost else empty end')
cost_str=""
if [ -n "$cost_field" ]; then
  cost_str=$(printf '$%.2f' "$cost_field")
elif [ -n "$total_input" ] || [ -n "$total_output" ]; then
  ti=${total_input:-0}
  to=${total_output:-0}
  cost_str=$(awk -v ti="$ti" -v to="$to" 'BEGIN {
    c = (ti * 15 / 1000000) + (to * 75 / 1000000)
    if (c > 0) printf "$%.2f", c
  }')
fi

# Five-hour rate limit usage
five_hr_pct=$(echo "$input" | jq -r '.rate_limits.five_hour.used_percentage // empty')
seven_day_pct=$(echo "$input" | jq -r '.rate_limits.seven_day.used_percentage // empty')

# Git branch + dirty marker
cwd=$(echo "$input" | jq -r '.workspace.current_dir // empty')
git_branch=""
git_dirty=""
if [ -n "$cwd" ] && [ -d "$cwd" ]; then
  git_branch=$(git -C "$cwd" --no-optional-locks branch --show-current 2>/dev/null)
  if [ -n "$git_branch" ]; then
    dirty=$(git -C "$cwd" --no-optional-locks status --porcelain 2>/dev/null)
    [ -n "$dirty" ] && git_dirty="*"
  fi
fi

# ── Build the line ───────────────────────────────────────────────────────────

parts=""

# Model
if [ -n "$model" ]; then
  parts="${parts}$(printf '\033[0;36m%s\033[0m' "$model")"
fi

# Agent (only when inside a subagent)
if [ -n "$agent" ]; then
  parts="${parts} $(printf '\033[0;35m[%s]\033[0m' "$agent")"
fi

# Git branch — yellow; dirty asterisk in red
if [ -n "$git_branch" ]; then
  if [ -n "$git_dirty" ]; then
    parts="${parts} $(printf '\033[0;33m %s\033[0;31m%s\033[0m' "$git_branch" "$git_dirty")"
  else
    parts="${parts} $(printf '\033[0;33m %s\033[0m' "$git_branch")"
  fi
fi

# Context bar + percentage — green <=50%, yellow 51-80%, red >80%
if [ -n "$used_pct" ]; then
  bar_filled=$(echo "$used_pct" | awk '{printf "%d", ($1 / 100) * 10}')
  bar_empty=$((10 - bar_filled))
  bar=""
  for i in $(seq 1 "$bar_filled"); do bar="${bar}█"; done
  for i in $(seq 1 "$bar_empty"); do bar="${bar}░"; done
  pct_int=$(printf '%.0f' "$used_pct")
  ctx_color=$(echo "$used_pct" | awk '{
    v = $1 + 0
    if (v > 80)      printf "\033[0;31m"
    else if (v > 50) printf "\033[0;33m"
    else             printf "\033[0;32m"
  }')
  parts="${parts} $(printf "${ctx_color}[%s] %s%%\033[0m" "$bar" "$pct_int")"
fi

# Total input / output tokens (compact)
if [ -n "$total_input" ] || [ -n "$total_output" ]; then
  ti_fmt=$(compact "${total_input:-0}")
  to_fmt=$(compact "${total_output:-0}")
  parts="${parts} $(printf '\033[0;90mtok i:%s o:%s\033[0m' "$ti_fmt" "$to_fmt")"
fi

# Cache read / write (compact)
if [ -n "$cache_write" ] || [ -n "$cache_read" ]; then
  cw=$(compact "${cache_write:-0}")
  cr=$(compact "${cache_read:-0}")
  parts="${parts} $(printf '\033[0;90mcache r:%s w:%s\033[0m' "$cr" "$cw")"
fi

# Session cost
if [ -n "$cost_str" ]; then
  parts="${parts} $(printf '\033[0;90m%s\033[0m' "$cost_str")"
fi

# Five-hour limit
if [ -n "$five_hr_pct" ]; then
  five_int=$(printf '%.0f' "$five_hr_pct")
  parts="${parts} $(printf '\033[0;90m5h:%s%%\033[0m' "$five_int")"
fi

# Write a lightweight heartbeat so a separate dashboard can aggregate all open
# Claude Code sessions. The statusline itself must stay one stdout line.
status_dir="${CLAUDE_STATUS_STATE_DIR:-$HOME/.claude/session-status}"
session_id=$(echo "$input" | jq -r '.session_id // empty')
project_dir=$(echo "$input" | jq -r '.workspace.project_dir // .workspace.current_dir // empty')
transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')
duration_ms=$(echo "$input" | jq -r '.cost.total_duration_ms // 0')
cost_total=$(echo "$input" | jq -r 'if (.cost | type) == "object" then (.cost.total_cost_usd // 0) elif (.cost | type) == "number" then .cost else 0 end')
# total_input already includes cache read/write per the Claude Code statusline docs.
total_tokens=$(awk -v ti="${total_input:-0}" -v to="${total_output:-0}" 'BEGIN { printf "%.0f", ti + to }')

if [ -n "$session_id" ]; then
  mkdir -p "$status_dir" 2>/dev/null
  session_file=$(printf '%s' "$session_id" | tr -c '[:alnum:]_-' '_')
  step_dir="$status_dir/steps"
  step_file="$step_dir/$session_file.jsonl"
  record_file="$status_dir/$session_file.json"
  mkdir -p "$step_dir" 2>/dev/null

  if [ -f "$record_file" ]; then
    prev_total_tokens=$(jq -r '.total_tokens // 0' "$record_file" 2>/dev/null)
    prev_input_tokens=$(jq -r '.input_tokens // 0' "$record_file" 2>/dev/null)
    prev_output_tokens=$(jq -r '.output_tokens // 0' "$record_file" 2>/dev/null)
    prev_cache_read_tokens=$(jq -r '.cache_read_tokens // 0' "$record_file" 2>/dev/null)
    prev_cache_write_tokens=$(jq -r '.cache_write_tokens // 0' "$record_file" 2>/dev/null)
    prev_cost_usd=$(jq -r '.cost_usd // 0' "$record_file" 2>/dev/null)
    prev_duration_ms=$(jq -r '.duration_ms // 0' "$record_file" 2>/dev/null)
    prev_api_duration_ms=$(jq -r '.api_duration_ms // 0' "$record_file" 2>/dev/null)
    prev_lines_added=$(jq -r '.lines_added // 0' "$record_file" 2>/dev/null)
    prev_lines_removed=$(jq -r '.lines_removed // 0' "$record_file" 2>/dev/null)
    prev_updated_at=$(jq -r '.updated_at // 0' "$record_file" 2>/dev/null)
    now_ts=$(date +%s)

    delta_tokens=$(awk -v cur="$total_tokens" -v prev="${prev_total_tokens:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_input=$(awk -v cur="${total_input:-0}" -v prev="${prev_input_tokens:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_output=$(awk -v cur="${total_output:-0}" -v prev="${prev_output_tokens:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_cache_read=$(awk -v cur="${cache_read:-0}" -v prev="${prev_cache_read_tokens:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_cache_write=$(awk -v cur="${cache_write:-0}" -v prev="${prev_cache_write_tokens:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_cost=$(awk -v cur="$cost_total" -v prev="${prev_cost_usd:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.6f", d }')
    delta_duration=$(awk -v cur="$duration_ms" -v prev="${prev_duration_ms:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_api_duration=$(awk -v cur="$api_duration_ms" -v prev="${prev_api_duration_ms:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_lines_added=$(awk -v cur="$lines_added" -v prev="${prev_lines_added:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_lines_removed=$(awk -v cur="$lines_removed" -v prev="${prev_lines_removed:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')
    delta_seen=$(awk -v cur="$now_ts" -v prev="${prev_updated_at:-0}" 'BEGIN { d = cur - prev; if (d < 0) d = 0; printf "%.0f", d }')

    should_log=$(awk -v tok="$delta_tokens" -v cost="$delta_cost" 'BEGIN { print (tok > 0 || cost > 0) ? "yes" : "no" }')
    if [ "$should_log" = "yes" ]; then
      step_no=$(awk 'END { print NR + 1 }' "$step_file" 2>/dev/null)
      jq -c -n \
        --arg session_id "$session_id" \
        --arg step_no "${step_no:-1}" \
        --arg timestamp "$now_ts" \
        --arg model "$model" \
        --arg agent "$agent" \
        --arg session_name "$session_name" \
        --arg effort "$effort" \
        --arg thinking "$thinking" \
        --arg cwd "$cwd" \
        --arg project_dir "$project_dir" \
        --arg repo "$(basename "${project_dir:-$cwd}")" \
        --arg transcript_path "$transcript_path" \
        --argjson context_pct "${used_pct:-0}" \
        --argjson delta_tokens "$delta_tokens" \
        --argjson delta_input_tokens "$delta_input" \
        --argjson delta_output_tokens "$delta_output" \
        --argjson delta_cache_read_tokens "$delta_cache_read" \
        --argjson delta_cache_write_tokens "$delta_cache_write" \
        --argjson delta_cost_usd "$delta_cost" \
        --argjson delta_duration_ms "$delta_duration" \
        --argjson delta_api_duration_ms "$delta_api_duration" \
        --argjson delta_seen_seconds "$delta_seen" \
        --argjson delta_lines_added "$delta_lines_added" \
        --argjson delta_lines_removed "$delta_lines_removed" \
        --argjson total_tokens "$total_tokens" \
        --argjson total_cost_usd "$cost_total" \
        '{session_id:$session_id,step_no:($step_no|tonumber),timestamp:($timestamp|tonumber),model:$model,agent:$agent,session_name:$session_name,effort:$effort,thinking:$thinking,cwd:$cwd,project_dir:$project_dir,repo:$repo,context_pct:$context_pct,delta_tokens:$delta_tokens,delta_input_tokens:$delta_input_tokens,delta_output_tokens:$delta_output_tokens,delta_cache_read_tokens:$delta_cache_read_tokens,delta_cache_write_tokens:$delta_cache_write_tokens,delta_cost_usd:$delta_cost_usd,delta_duration_ms:$delta_duration_ms,delta_api_duration_ms:$delta_api_duration_ms,delta_seen_seconds:$delta_seen_seconds,delta_lines_added:$delta_lines_added,delta_lines_removed:$delta_lines_removed,total_tokens:$total_tokens,total_cost_usd:$total_cost_usd,transcript_path:$transcript_path}' \
        >> "$step_file" 2>/dev/null
    fi
  fi

  jq -n \
    --arg session_id "$session_id" \
    --arg updated_at "$(date +%s)" \
    --arg model "$model" \
    --arg agent "$agent" \
    --arg session_name "$session_name" \
    --arg version "$version" \
    --arg effort "$effort" \
    --arg thinking "$thinking" \
    --arg cwd "$cwd" \
    --arg project_dir "$project_dir" \
    --arg repo "$(basename "${project_dir:-$cwd}")" \
    --arg transcript_path "$transcript_path" \
    --arg pr_number "$pr_number" \
    --arg pr_review_state "$pr_review_state" \
    --arg worktree_name "$worktree_name" \
    --argjson context_pct "${used_pct:-0}" \
    --argjson remaining_pct "${remaining_pct:-0}" \
    --argjson context_size "${context_size:-0}" \
    --argjson rate_pct "${five_hr_pct:-0}" \
    --argjson seven_day_pct "${seven_day_pct:-0}" \
    --argjson input_tokens "${total_input:-0}" \
    --argjson output_tokens "${total_output:-0}" \
    --argjson cache_read_tokens "${cache_read:-0}" \
    --argjson cache_write_tokens "${cache_write:-0}" \
    --argjson total_tokens "$total_tokens" \
    --argjson cost_usd "$cost_total" \
    --argjson duration_ms "$duration_ms" \
    --argjson api_duration_ms "$api_duration_ms" \
    --argjson lines_added "$lines_added" \
    --argjson lines_removed "$lines_removed" \
    '{session_id:$session_id,updated_at:($updated_at|tonumber),model:$model,agent:$agent,session_name:$session_name,version:$version,effort:$effort,thinking:$thinking,cwd:$cwd,project_dir:$project_dir,repo:$repo,pr_number:$pr_number,pr_review_state:$pr_review_state,worktree_name:$worktree_name,context_pct:$context_pct,remaining_pct:$remaining_pct,context_size:$context_size,rate_pct:$rate_pct,seven_day_pct:$seven_day_pct,input_tokens:$input_tokens,output_tokens:$output_tokens,cache_read_tokens:$cache_read_tokens,cache_write_tokens:$cache_write_tokens,total_tokens:$total_tokens,cost_usd:$cost_usd,duration_ms:$duration_ms,api_duration_ms:$api_duration_ms,lines_added:$lines_added,lines_removed:$lines_removed,transcript_path:$transcript_path}' \
    > "$status_dir/$session_file.tmp" 2>/dev/null && mv "$status_dir/$session_file.tmp" "$record_file" 2>/dev/null
fi

printf "%s\n" "$parts"

if [ -n "$step_file" ] && [ -f "$step_file" ]; then
  series=$(token_series_large "$step_file")
  if [ -n "$series" ]; then
    printf "%s\n" "$series"
  fi
fi

exit 0
